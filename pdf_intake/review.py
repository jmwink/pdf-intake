"""Review and pruning of stale library entries (Stage 8)."""
import json
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path

from pdf_intake.index import Index


def _entry_path(library_root: Path, entry: dict) -> Path:
    base = library_root / entry["category"]
    if entry.get("subfolder"):
        base = base / entry["subfolder"]
    return base / entry["slug"]


def _archive_path(library_root: Path, entry: dict) -> Path:
    # Plan spec: _archive/<category>/<slug>/ — flat, no subfolder preserved.
    return library_root / "_archive" / entry["category"] / entry["slug"]


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


_TITLE_RE = re.compile(r'title:\s*"(.*)"')


def _read_title(md_path: Path) -> str | None:
    if not md_path.is_file():
        return None
    with md_path.open() as f:
        in_fm = False
        for line in f:
            line = line.rstrip("\n")
            if line == "---":
                if not in_fm:
                    in_fm = True
                    continue
                return None  # end of frontmatter without title
            if in_fm:
                m = _TITLE_RE.match(line)
                if m:
                    return m.group(1).replace('\\"', '"')
    return None


def _read_summary_preview(summary_path: Path, max_chars: int = 400) -> str:
    if not summary_path.is_file():
        return "(no summary file)"
    lines = summary_path.read_text().splitlines()
    out = []
    seen_first_heading = False
    for line in lines:
        if line.startswith("## "):
            if seen_first_heading:
                break
            seen_first_heading = True
        out.append(line)
    section = "\n".join(out).strip()
    if len(section) > max_chars:
        section = section[:max_chars].rstrip() + "…"
    return section


def _read_log(log_path: Path) -> dict[str, tuple[date, str]]:
    """{citekey: (latest_date, latest_action)} from the JSONL log. Empty if missing."""
    if not log_path.is_file():
        return {}
    out: dict[str, tuple[date, str]] = {}
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # one bad line shouldn't poison the scan
            citekey = rec.get("citekey")
            d = _parse_iso_date(rec.get("at"))
            if not citekey or not d:
                continue
            action = rec.get("action", "")
            cur = out.get(citekey)
            if cur is None or d > cur[0]:
                out[citekey] = (d, action)
    return out


def last_touched(citekey: str, log_path: Path) -> date | None:
    """Most recent review-log entry date for `citekey`. None if never reviewed."""
    rec = _read_log(log_path).get(citekey)
    return rec[0] if rec else None


def find_stale(
    library_root: Path,
    days: int,
    category: str | None = None,
) -> list[dict]:
    """Walk `.index.json`; return entries last-touched ≥ `days` ago."""
    data = Index(library_root).load()
    log_index = _read_log(library_root / ".review-log.jsonl")
    today = date.today()

    out: list[dict] = []
    for content_hash, entry in data.items():
        if "slug" not in entry:
            continue  # convert-only sentinel, no full ingest
        if category and entry.get("category") != category:
            continue

        ingest = _parse_iso_date(entry.get("hash_seen_at"))
        log_rec = log_index.get(entry["citekey"])
        review_date = log_rec[0] if log_rec else None
        last_action = log_rec[1] if log_rec else None

        candidates = [d for d in (ingest, review_date) if d is not None]
        effective = max(candidates) if candidates else None

        if effective is None:
            age_days: int | None = None
            is_stale = True  # no date info → treat as stale
        else:
            age_days = (today - effective).days
            is_stale = age_days >= days

        if not is_stale:
            continue

        path = _entry_path(library_root, entry)
        md_path = path / f"{entry['slug']}.md"
        summary_path = path / f"{entry['slug']}.summary.md"
        out.append({
            "content_hash": content_hash,
            "citekey": entry["citekey"],
            "slug": entry["slug"],
            "category": entry["category"],
            "subfolder": entry.get("subfolder"),
            "title": _read_title(md_path) or entry["slug"],
            "summary_preview": _read_summary_preview(summary_path),
            "last_touched": effective,
            "last_action": last_action,
            "age_days": age_days,
            "entry_path": path,
        })

    return out


def append_log(log_path: Path, citekey: str, action: str) -> None:
    """Append one JSON line to `.review-log.jsonl`. Single short writes are atomic on POSIX."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"citekey": citekey, "action": action, "at": date.today().isoformat()}
    with log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def archive_entry(library_root: Path, entry: dict) -> Path:
    src = _entry_path(library_root, entry)
    dst = _archive_path(library_root, entry)
    if not src.is_dir():
        raise FileNotFoundError(f"entry folder missing: {src}")
    if dst.exists():
        raise FileExistsError(f"archive destination exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst


def soft_delete_entry(library_root: Path, entry: dict) -> Path:
    """Move folder to _archive/, prefix .bib lines with `% `. Reversible."""
    dst = archive_entry(library_root, entry)
    bib_path = library_root / ".bib" / f"{entry['citekey']}.bib"
    if bib_path.is_file():
        text = bib_path.read_text()
        commented = "\n".join(f"% {line}" for line in text.splitlines()) + "\n"
        tmp = bib_path.with_suffix(".bib.tmp")
        tmp.write_text(commented)
        os.replace(tmp, bib_path)
    return dst


def purge_entry(library_root: Path, entry: dict) -> Path:
    """Snapshot library.bib + .bib/<citekey>.bib to .backups/<ts>/, then delete everything."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_dir = library_root / ".backups" / ts
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    library_bib = library_root / "library.bib"
    if library_bib.is_file():
        shutil.copy2(library_bib, snapshot_dir / "library.bib")

    bib_path = library_root / ".bib" / f"{entry['citekey']}.bib"
    if bib_path.is_file():
        shutil.copy2(bib_path, snapshot_dir / f"{entry['citekey']}.bib")
        bib_path.unlink()

    src = _entry_path(library_root, entry)
    if src.is_dir():
        shutil.rmtree(src)

    index = Index(library_root)
    data = index.load()
    if entry["content_hash"] in data:
        del data[entry["content_hash"]]
        index.save(data)

    return snapshot_dir
