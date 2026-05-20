import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from pdf_intake import __version__
from pdf_intake.bibtex import build_entry, build_library, write_entry
from pdf_intake.config import load_config
from pdf_intake.convert import convert
from pdf_intake.errors import BibtexBuildError, OCRError, ScannedPDFError
from pdf_intake.index import Index, content_hash
from pdf_intake.metadata import (
    MAX_INPUT_CHARS,
    OPTIONAL_FIELDS,
    confirm_entry_type,
    extract as extract_metadata,
    validate as validate_metadata,
)
from pdf_intake.review import (
    append_log,
    archive_entry,
    find_stale,
    purge_entry,
    soft_delete_entry,
)
from pdf_intake.slug import build_slug
from pdf_intake.summarize import cost_rollup, summarize


CATEGORY_MAP = {"t": "teaching", "r": "research", "c": "curiosity", "p": "professional"}

MODEL_ALIAS = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _resolve_model(name: str | None) -> str | None:
    if name is None:
        return None
    return MODEL_ALIAS.get(name, name)


def _prompt_category() -> str:
    while True:
        choice = input("Category? [t]eaching/[r]esearch/[c]uriosity/[p]rofessional: ").strip().lower()
        if choice in CATEGORY_MAP:
            return CATEGORY_MAP[choice]
        if choice in CATEGORY_MAP.values():
            return choice
        print(f"  must be one of: {', '.join(CATEGORY_MAP)}", file=sys.stderr)


def _prompt_subfolder() -> str:
    return input("Subfolder? (blank = category root): ").strip()


def _yaml_escape(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    s = str(value).replace('"', '\\"')
    return f'"{s}"'


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {_yaml_escape(v)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _cmd_convert(args: argparse.Namespace) -> int:
    pdf_path = Path(args.path).resolve()
    if not pdf_path.is_file():
        print(f"not a file: {pdf_path}", file=sys.stderr)
        return 1

    cfg = load_config()

    hash_ = content_hash(pdf_path)
    index = Index(cfg.library_root)
    existing = index.lookup(hash_)
    if existing and not args.force:
        slug = existing.get("slug", "(no slug yet)")
        print(f"already ingested as {slug} (hash {hash_})")
        return 0

    try:
        result = convert(
            pdf_path,
            min_chars=cfg.extraction_min_chars,
            min_words_per_page=cfg.extraction_min_words_per_page,
            no_ocr=args.no_ocr,
            keep_pagination=args.keep_pagination,
        )
    except ScannedPDFError as e:
        print(f"unreadable PDF ({e})", file=sys.stderr)
        return 1
    except OCRError as e:
        print(f"ocrmypdf failed: {e}", file=sys.stderr)
        if e.stderr:
            print(f"--- ocrmypdf stderr ---\n{e.stderr}", file=sys.stderr)
        return 1

    out_path = pdf_path.with_suffix(".md")
    if args.dry_run:
        print(f"would write {len(result.markdown)} chars to {out_path}")
        return 0

    out_path.write_text(result.markdown)
    index.record(hash_, {"hash_seen_at": datetime.now(timezone.utc).isoformat()})
    print(f"wrote {out_path}")
    return 0


def _cmd_metadata(args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1

    cfg = load_config()

    if path.suffix.lower() == ".pdf":
        try:
            result = convert(
                path,
                min_chars=cfg.extraction_min_chars,
                min_words_per_page=cfg.extraction_min_words_per_page,
            )
        except ScannedPDFError as e:
            print(f"unreadable PDF ({e})", file=sys.stderr)
            return 1
        except OCRError as e:
            print(f"ocrmypdf failed: {e}", file=sys.stderr)
            if e.stderr:
                print(f"--- ocrmypdf stderr ---\n{e.stderr}", file=sys.stderr)
            return 1
        md = result.markdown
    else:
        md = path.read_text()

    data = extract_metadata(
        md,
        model=cfg.default_model,
        api_key_env=cfg.anthropic_api_key_env,
    )
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _emit_error(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def _cmd_inspect(args: argparse.Namespace) -> int:
    pdf_path = Path(args.path).resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        _emit_error({"error": "not_a_pdf", "path": str(pdf_path)})
        return 1

    cfg = load_config()
    hash_ = content_hash(pdf_path)
    index = Index(cfg.library_root)
    existing = index.lookup(hash_)
    already = None
    if existing and "slug" in existing:
        already = {
            "slug": existing.get("slug"),
            "category": existing.get("category"),
            "subfolder": existing.get("subfolder"),
            "citekey": existing.get("citekey"),
        }

    try:
        result = convert(
            pdf_path,
            min_chars=cfg.extraction_min_chars,
            min_words_per_page=cfg.extraction_min_words_per_page,
        )
    except ScannedPDFError as e:
        _emit_error({"error": "scanned_pdf", "message": str(e), "path": str(pdf_path)})
        return 1
    except OCRError as e:
        _emit_error({"error": "ocr_failed", "message": str(e), "stderr": e.stderr})
        return 1

    meta = extract_metadata(
        result.markdown,
        model=cfg.default_model,
        api_key_env=cfg.anthropic_api_key_env,
        interactive=False,
    )
    errors = validate_metadata(meta)

    payload = {
        "content_hash": hash_,
        "page_count": result.page_count,
        "preview_chars": min(len(result.markdown), MAX_INPUT_CHARS),
        "ocr_source": "ocrmypdf" if result.used_ocr else "born_digital",
        "already_ingested": already,
        "metadata": meta,
        "validation": errors,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _parse_metadata_json(raw: str) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("--metadata-json must decode to a JSON object")
    for field in OPTIONAL_FIELDS:
        data.setdefault(field, None)
    return data


def _cmd_ingest(args: argparse.Namespace) -> int:
    pdf_path = Path(args.path).resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        msg = {"error": "not_a_pdf", "path": str(pdf_path)}
        if args.non_interactive:
            _emit_error(msg)
        else:
            print(f"not a PDF: {pdf_path}", file=sys.stderr)
        return 1

    cfg = load_config()
    hash_ = content_hash(pdf_path)
    index = Index(cfg.library_root)

    existing = index.lookup(hash_)
    if existing and not args.force and "slug" in existing:
        if args.non_interactive:
            _emit_error({
                "error": "already_ingested",
                "slug": existing["slug"],
                "content_hash": hash_,
            })
            return 1
        print(f"already ingested as {existing['slug']} (hash {hash_})")
        return 0

    try:
        result = convert(
            pdf_path,
            min_chars=cfg.extraction_min_chars,
            min_words_per_page=cfg.extraction_min_words_per_page,
            no_ocr=args.no_ocr,
            keep_pagination=args.keep_pagination,
        )
    except ScannedPDFError as e:
        if args.non_interactive:
            _emit_error({"error": "scanned_pdf", "message": str(e)})
        else:
            print(f"unreadable PDF ({e})", file=sys.stderr)
        return 1
    except OCRError as e:
        if args.non_interactive:
            _emit_error({"error": "ocr_failed", "message": str(e), "stderr": e.stderr})
        else:
            print(f"ocrmypdf failed: {e}", file=sys.stderr)
            if e.stderr:
                print(f"--- ocrmypdf stderr ---\n{e.stderr}", file=sys.stderr)
        return 1

    md = result.markdown
    page_count = result.page_count
    ocr_source = "ocrmypdf" if result.used_ocr else "born_digital"

    if args.metadata_json:
        try:
            meta = _parse_metadata_json(args.metadata_json)
        except (json.JSONDecodeError, ValueError) as e:
            _emit_error({"error": "metadata_json_parse", "message": str(e)})
            return 1
    else:
        print("extracting metadata…", file=sys.stderr)
        meta = extract_metadata(
            md,
            model=cfg.default_model,
            api_key_env=cfg.anthropic_api_key_env,
            interactive=not args.non_interactive,
        )

    if args.non_interactive:
        errors = validate_metadata(meta)
        if errors["universal"]:
            _emit_error({
                "error": "universal_validation",
                "entry_type": meta.get("entry_type"),
                "missing": errors["universal"],
            })
            return 1
        if errors["per_type"]:
            _emit_error({
                "error": "per_type_validation",
                "entry_type": meta.get("entry_type"),
                "missing": errors["per_type"],
            })
            return 1

    meta = confirm_entry_type(meta, interactive=not args.non_interactive)

    slug = build_slug(meta["author"], meta["year"], meta["short_title"])
    citekey = slug
    print(f"slug: {slug}", file=sys.stderr)

    if args.non_interactive:
        category = args.category
        subfolder = args.subfolder or ""
    else:
        category = args.category or _prompt_category()
        subfolder = args.subfolder if args.subfolder is not None else _prompt_subfolder()

    target_dir = cfg.library_root / category
    if subfolder:
        target_dir = target_dir / subfolder
    target_dir = target_dir / slug

    if target_dir.exists() and not args.force:
        if args.non_interactive:
            _emit_error({"error": "target_exists", "path": str(target_dir)})
        else:
            print(f"target exists: {target_dir}", file=sys.stderr)
        return 1

    target_dir.mkdir(parents=True, exist_ok=True)

    pdf_dest = target_dir / f"{slug}.pdf"
    md_dest = target_dir / f"{slug}.md"

    if result.used_ocr and result.ocr_pdf_path is not None:
        # Library stores the searchable PDF; user's original scan is unlinked.
        shutil.move(str(result.ocr_pdf_path), str(pdf_dest))
        if pdf_path.exists() and pdf_path.resolve() != pdf_dest.resolve():
            pdf_path.unlink()
    elif pdf_path.resolve() != pdf_dest.resolve():
        shutil.move(str(pdf_path), str(pdf_dest))

    frontmatter = _frontmatter({
        "title": meta["short_title"],
        "source_pdf": pdf_path.name,
        "category": category,
        "subfolder": subfolder or None,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "page_count": page_count,
        "content_hash": hash_,
        "ocr_source": ocr_source,
        "author": meta["author"],
        "year": meta["year"],
        "citekey": citekey,
    })
    md_dest.write_text(frontmatter + md)

    index.record(hash_, {
        "slug": slug,
        "category": category,
        "subfolder": subfolder or None,
        "citekey": citekey,
        "ocr_source": ocr_source,
        "hash_seen_at": datetime.now(timezone.utc).isoformat(),
    })

    try:
        bib_str = build_entry(meta, citekey)
    except BibtexBuildError as e:
        print(f"bib build failed for {citekey}: {e}", file=sys.stderr)
        return 1
    bib_path = write_entry(citekey, bib_str, cfg.library_root / ".bib")
    print(f"wrote {bib_path}", file=sys.stderr)

    print("summarizing…", file=sys.stderr)
    try:
        entry = summarize(target_dir, cfg=cfg, model=_resolve_model(args.model) or cfg.default_model)
        _print_summary_stats(entry)
    except Exception as e:
        # Non-fatal: the rest of the ingest is durable and `resummarize` can retry.
        print(f"summarize failed (non-fatal): {e}", file=sys.stderr)

    print(f"ingested → {target_dir}")
    return 0


def _print_summary_stats(entry: dict) -> None:
    cost = entry.get("est_cost_usd")
    cost_str = f"${cost:.4f}" if cost is not None else "n/a"
    print(
        f"  summary: {entry['slug']}.summary.md  "
        f"input={entry['input_tokens']} output={entry['output_tokens']} "
        f"cache_write={entry['cache_creation_input_tokens']} "
        f"cache_read={entry['cache_read_input_tokens']} "
        f"cost={cost_str}",
        file=sys.stderr,
    )


def _cmd_resummarize(args: argparse.Namespace) -> int:
    cfg = load_config()
    model = _resolve_model(args.model) or cfg.default_model
    try:
        entry = summarize(args.slug_or_path, cfg=cfg, model=model)
    except Exception as e:
        print(f"summarize failed: {e}", file=sys.stderr)
        return 1
    _print_summary_stats(entry)
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    cfg = load_config()
    log_path = cfg.library_root / ".cost-log.jsonl"
    totals = cost_rollup(log_path, since=args.since)
    if totals["entries"] == 0:
        print("no entries")
        return 0
    print(f"entries:     {totals['entries']}")
    print(f"input:       {totals['input_tokens']:,} tok")
    print(f"output:      {totals['output_tokens']:,} tok")
    print(f"cache write: {totals['cache_creation_input_tokens']:,} tok")
    print(f"cache read:  {totals['cache_read_input_tokens']:,} tok")
    print(f"est cost:    ${totals['est_cost_usd']:.4f}")
    if len(totals["by_model"]) > 1:
        print("by model:")
        for model, stats in sorted(totals["by_model"].items()):
            print(f"  {model}: {stats['entries']} entries, ${stats['est_cost_usd']:.4f}")
    return 0


def _cmd_bib_build(args: argparse.Namespace) -> int:
    cfg = load_config()
    bib_dir = cfg.library_root / ".bib"
    out = cfg.library_root / "library.bib"
    if not bib_dir.is_dir():
        print(f"no .bib/ directory at {bib_dir}", file=sys.stderr)
        return 1
    n = build_library(bib_dir, out)
    print(f"wrote {out} ({n} entries)")
    return 0


REVIEW_ACTION_MAP = {
    "k": "keep", "keep": "keep",
    "a": "archive", "archive": "archive",
    "d": "delete", "delete": "delete",
    "s": "skip", "skip": "skip",
}

REVIEW_ACTION_LABEL = {"archive": "archived", "delete": "deleted"}


def _prompt_review_action() -> str:
    while True:
        choice = input("[k]eep / [a]rchive / [d]elete / [s]kip: ").strip().lower()
        if choice in REVIEW_ACTION_MAP:
            return REVIEW_ACTION_MAP[choice]
        print("  must be one of: k, a, d, s", file=sys.stderr)


def _confirm_purge_slug(expected_slug: str) -> bool:
    # Slug confirmation, not y/n: forces visual engagement, defeats muscle memory.
    typed = input(f"  type the slug to confirm purge ({expected_slug}): ").strip()
    return typed == expected_slug


def _cmd_review(args: argparse.Namespace) -> int:
    cfg = load_config()
    log_path = cfg.library_root / ".review-log.jsonl"

    stale = find_stale(cfg.library_root, days=args.days, category=args.category)

    if not stale:
        print("nothing stale")
        return 0

    if args.dry_run:
        print("DRY RUN — no files moved, no log entries written\n")
    print(f"{len(stale)} stale entries\n")

    for entry in stale:
        age = entry.get("age_days")
        age_str = f"{age}d" if age is not None else "no date"
        last = entry.get("last_touched") or "(never)"
        la = entry.get("last_action")
        if not entry["entry_path"].exists():
            label = (
                f" [{REVIEW_ACTION_LABEL[la]} {entry['last_touched']}]"
                if la in REVIEW_ACTION_LABEL
                else " [missing]"
            )
        else:
            label = ""
        print(f"--- {entry['citekey']}{label} ({entry['category']}) ---")
        print(f"  title: {entry['title']}")
        print(f"  last touched: {last}  ({age_str})")
        print(f"  summary:")
        for line in entry["summary_preview"].splitlines():
            print(f"    {line}")
        print()

        action = _prompt_review_action()

        if action == "skip":
            print("  skipped\n")
            continue

        if action == "keep":
            if args.dry_run:
                print(f"  [dry-run] would log keep for {entry['citekey']}\n")
            else:
                append_log(log_path, entry["citekey"], "keep")
                print(f"  logged keep for {entry['citekey']}\n")
            continue

        if action == "archive":
            if args.dry_run:
                print(f"  [dry-run] would archive {entry['slug']} → _archive/{entry['category']}/{entry['slug']}/\n")
                continue
            try:
                dst = archive_entry(cfg.library_root, entry)
            except (FileNotFoundError, FileExistsError) as e:
                print(f"  archive failed: {e}\n", file=sys.stderr)
                continue
            append_log(log_path, entry["citekey"], "archive")
            print(f"  archived → {dst}\n")
            continue

        if action == "delete":
            if args.purge:
                if not _confirm_purge_slug(entry["slug"]):
                    print("  slug mismatch, aborted\n")
                    continue
                if args.dry_run:
                    print(f"  [dry-run] would purge {entry['slug']} (snapshot to .backups/<ts>/ first)\n")
                    continue
                try:
                    snap = purge_entry(cfg.library_root, entry)
                except Exception as e:
                    print(f"  purge failed: {e}\n", file=sys.stderr)
                    continue
                append_log(log_path, entry["citekey"], "purge")
                print(f"  purged. snapshot: {snap}\n")
            else:
                if args.dry_run:
                    print(f"  [dry-run] would soft-delete {entry['slug']} (move to _archive/, % -prefix .bib)\n")
                    continue
                try:
                    dst = soft_delete_entry(cfg.library_root, entry)
                except (FileNotFoundError, FileExistsError) as e:
                    print(f"  soft-delete failed: {e}\n", file=sys.stderr)
                    continue
                append_log(log_path, entry["citekey"], "delete")
                print(f"  soft-deleted → {dst} (.bib commented)\n")
            continue

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="pdf-intake")
    parser.add_argument("--version", action="version", version=f"pdf-intake {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_convert = sub.add_parser("convert", help="Convert a PDF to markdown")
    p_convert.add_argument("path", help="Path to the PDF")
    p_convert.add_argument("--dry-run", action="store_true", help="Print what would be written without writing")
    p_convert.add_argument("--force", action="store_true", help="Re-convert even if the content hash is already in the index")
    p_convert.add_argument("--no-ocr", action="store_true", help="Skip the ocrmypdf path even on scanned PDFs (falls back to pymupdf4llm's built-in OCR; output may be column-jumbled)")
    p_convert.add_argument("--keep-pagination", action="store_true", help="Skip the repeating-header/footer strip step")
    p_convert.set_defaults(func=_cmd_convert)

    p_meta = sub.add_parser("metadata", help="Extract bibliographic metadata from a PDF or markdown file")
    p_meta.add_argument("path", help="Path to a .pdf or .md file")
    p_meta.set_defaults(func=_cmd_metadata)

    p_ing = sub.add_parser("ingest", help="Full intake: convert + metadata + slug + file into the library")
    p_ing.add_argument("path", help="Path to the PDF")
    p_ing.add_argument("--category", choices=list(CATEGORY_MAP.values()), help="Skip category prompt")
    p_ing.add_argument("--subfolder", help="Skip subfolder prompt (use empty string for category root)")
    p_ing.add_argument("--force", action="store_true", help="Re-ingest even if hash already recorded")
    p_ing.add_argument("--model", help="Override default model for the summary step (e.g., claude-opus-4-7)")
    p_ing.add_argument("--non-interactive", action="store_true", help="No stdin prompts; validation failures exit 1 with structured JSON on stderr. Requires --category.")
    p_ing.add_argument("--metadata-json", help="JSON blob of the atomic metadata dict (skips LLM extraction). entry_type field is authoritative.")
    p_ing.add_argument("--no-ocr", action="store_true", help="Skip the ocrmypdf path even on scanned PDFs (output may be column-jumbled)")
    p_ing.add_argument("--keep-pagination", action="store_true", help="Skip the repeating-header/footer strip step")
    p_ing.set_defaults(func=_cmd_ingest)

    p_inspect = sub.add_parser("inspect", help="Run conversion + metadata extraction; emit JSON to stdout; no files moved")
    p_inspect.add_argument("path", help="Path to the PDF")
    p_inspect.add_argument("--json", action="store_true", help="Emit JSON (default; flag accepted for explicitness)")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_bib = sub.add_parser("bib", help="BibTeX operations")
    bib_sub = p_bib.add_subparsers(dest="bib_command")
    p_bib_build = bib_sub.add_parser("build", help="Concatenate .bib/*.bib into library.bib")
    p_bib_build.set_defaults(func=_cmd_bib_build)

    p_resum = sub.add_parser("resummarize", help="Regenerate <slug>.summary.md for an existing entry")
    p_resum.add_argument("slug_or_path", help="Slug, path to slug directory, or path to <slug>.md")
    p_resum.add_argument("--model", help="Override default model. Aliases: opus, sonnet, haiku. Or full ID (e.g., claude-opus-4-7).")
    p_resum.set_defaults(func=_cmd_resummarize)

    p_cost = sub.add_parser("cost", help="Roll up .cost-log.jsonl totals")
    p_cost.add_argument("--since", help="ISO date or timestamp (UTC); entries before this are excluded")
    p_cost.set_defaults(func=_cmd_cost)

    p_review = sub.add_parser("review", help="Review and prune stale library entries")
    p_review.add_argument("--days", type=int, default=180, help="Entries last-touched > N days ago are stale (default: 180; use 0 for all)")
    p_review.add_argument("--category", choices=list(CATEGORY_MAP.values()), help="Limit review to one category")
    p_review.add_argument("--purge", action="store_true", help="Make 'delete' a true deletion (slug confirmation required)")
    p_review.add_argument("--dry-run", action="store_true", help="Print actions without executing or logging")
    p_review.set_defaults(func=_cmd_review)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(0)
    if getattr(args, "non_interactive", False) and not getattr(args, "category", None):
        parser.error("--category is required when --non-interactive")
    sys.exit(args.func(args))
