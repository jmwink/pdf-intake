import json
import os
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic

from pdf_intake.config import Config

PROMPTS_DIR = Path(__file__).parent / "prompts"
# The research template's five-section output exceeds 2048 for a substantive
# paper (Glock 2023 verify: truncated mid-bullet in Main interlocutors). 4096
# leaves headroom for long interlocutor lists and evidentiary discussions.
MAX_TOKENS = 4096
CATEGORIES = ("teaching", "research", "curiosity", "professional")

# Pricing in USD per million tokens. Refresh when Anthropic pricing changes.
PRICING = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write_5m": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4-6": {
        "input": 15.00,
        "output": 75.00,
        "cache_write_5m": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_write_5m": 18.75,
        "cache_read": 1.50,
    },
}


def _estimate_cost(model, input_tok, output_tok, cache_creation, cache_read):
    p = PRICING.get(model)
    if not p:
        return None
    return (
        input_tok / 1_000_000 * p["input"]
        + output_tok / 1_000_000 * p["output"]
        + cache_creation / 1_000_000 * p["cache_write_5m"]
        + cache_read / 1_000_000 * p["cache_read"]
    )


def _read_frontmatter(md: str) -> tuple[dict, str]:
    if not md.startswith("---\n"):
        return {}, md
    end = md.find("\n---\n", 4)
    if end == -1:
        return {}, md
    block = md[4:end]
    body = md[end + 5 :]
    fm: dict[str, object] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"')
        elif value == "null":
            value = None
        fm[key] = value
    return fm, body


def _resolve_slug_dir(slug_or_path: str, library_root: Path) -> Path:
    p = Path(slug_or_path).expanduser()
    if p.is_dir():
        return p.resolve()
    if p.is_file() and p.suffix == ".md":
        return p.parent.resolve()
    # Treat as slug: walk the library for a directory with this name that
    # contains <slug>.md.
    candidate = None
    for md in library_root.rglob(f"{slug_or_path}/{slug_or_path}.md"):
        candidate = md.parent
        break
    if candidate is None:
        raise FileNotFoundError(f"no slug directory named {slug_or_path!r} under {library_root}")
    return candidate.resolve()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _append_cost_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def summarize(slug_or_path: str | Path, *, cfg: Config, model: str | None = None) -> dict:
    """Generate <slug>.summary.md and append an entry to .cost-log.jsonl.

    Returns the log entry for caller display.
    """
    model = model or cfg.default_model
    slug_dir = _resolve_slug_dir(str(slug_or_path), cfg.library_root)
    slug = slug_dir.name
    md_path = slug_dir / f"{slug}.md"
    if not md_path.is_file():
        raise FileNotFoundError(f"no .md at {md_path}")

    fm, body = _read_frontmatter(md_path.read_text())
    category = fm.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"unknown or missing category in {md_path}: {category!r}")

    template_path = PROMPTS_DIR / f"{category}.md"
    system_prompt = template_path.read_text()

    api_key = os.environ.get(cfg.anthropic_api_key_env)
    if not api_key:
        raise RuntimeError(
            f"API key not found in env var {cfg.anthropic_api_key_env}. "
            f"Set it or change anthropic_api_key_env in your config."
        )

    client = Anthropic(api_key=api_key)

    # cache_control on the user body caches (system + body) together. On a
    # second call within the ephemeral TTL (~5 min) with identical content,
    # cache_read_input_tokens > 0. Cross-PDF caching is not expected.
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": body,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )

    summary_text = response.content[0].text
    summary_path = slug_dir / f"{slug}.summary.md"
    _atomic_write(summary_path, summary_text)

    usage = response.usage
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    est_cost = _estimate_cost(
        model,
        usage.input_tokens,
        usage.output_tokens,
        cache_creation,
        cache_read,
    )

    entry = {
        "slug": slug,
        "category": category,
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "est_cost_usd": est_cost,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _append_cost_log(cfg.library_root / ".cost-log.jsonl", entry)
    return entry


def cost_rollup(log_path: Path, since: str | None = None) -> dict:
    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

    totals = {
        "entries": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "est_cost_usd": 0.0,
        "by_model": {},
    }
    if not log_path.is_file():
        return totals

    for line in log_path.open():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        at_str = entry.get("at") or ""
        if since_dt and at_str:
            try:
                at_dt = datetime.fromisoformat(at_str)
                if at_dt.tzinfo is None:
                    at_dt = at_dt.replace(tzinfo=timezone.utc)
                if at_dt < since_dt:
                    continue
            except ValueError:
                pass
        totals["entries"] += 1
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            totals[k] += int(entry.get(k, 0) or 0)
        cost = entry.get("est_cost_usd") or 0.0
        totals["est_cost_usd"] += float(cost)
        model = entry.get("model", "unknown")
        bm = totals["by_model"].setdefault(model, {"entries": 0, "est_cost_usd": 0.0})
        bm["entries"] += 1
        bm["est_cost_usd"] += float(cost)
    return totals
