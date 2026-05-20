import json
import os
import sys
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic

MAX_INPUT_CHARS = 6000
MIN_YEAR = 1600
MAX_TOKENS = 1024

ENTRY_TYPES = (
    "article",
    "book",
    "inbook",
    "incollection",
    "inproceedings",
    "misc",
    "phdthesis",
)
REQUIRED_FIELDS = ("author", "year", "full_title", "short_title", "entry_type")
OPTIONAL_FIELDS = (
    "journal",
    "booktitle",
    "volume",
    "number",
    "pages",
    "chapter",
    "publisher",
    "address",
    "school",
    "doi",
    "editor",
)

# Per-entry-type required-when-not-null fields. After universal validation
# passes, any field listed here that came back null gets escalated to the
# same interactive prompt as the universal-required failures.
PER_TYPE_REQUIRED = {
    "article": ("journal",),
    "book": ("publisher",),
    "inbook": ("publisher", "pages"),
    "incollection": ("booktitle", "publisher", "editor"),
    "inproceedings": ("booktitle",),
    "phdthesis": ("school",),
    "misc": (),
}

PROMPT_PATH = Path(__file__).parent / "prompts" / "metadata.md"


def _load_prompt() -> str:
    return PROMPT_PATH.read_text()


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if not s.startswith("```"):
        return s
    s = s.split("\n", 1)[1] if "\n" in s else s[3:]
    s = s.rstrip()
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s


def _validate(data: dict) -> list[str]:
    """Return a list of required fields that failed validation (including null)."""
    bad: list[str] = []

    author = data.get("author")
    if author is None or not isinstance(author, str) or not author.strip():
        bad.append("author")

    year = data.get("year")
    current_year = datetime.now().year
    if year is None:
        bad.append("year")
    else:
        try:
            year_int = int(year)
            if not (MIN_YEAR <= year_int <= current_year + 1):
                bad.append("year")
        except (TypeError, ValueError):
            bad.append("year")

    full_title = data.get("full_title")
    if (
        full_title is None
        or not isinstance(full_title, str)
        or not full_title.strip()
    ):
        bad.append("full_title")

    short_title = data.get("short_title")
    if (
        short_title is None
        or not isinstance(short_title, str)
        or len(short_title.split()) < 2
    ):
        bad.append("short_title")

    entry_type = data.get("entry_type")
    if entry_type not in ENTRY_TYPES:
        bad.append("entry_type")

    return bad


def _validate_per_type(data: dict) -> list[str]:
    """Return per-type required fields that are null/missing/blank in data.

    Callers should run _validate first so entry_type is known to be valid.
    Stage 5 will call this again after a user-driven entry_type override.
    """
    entry_type = data.get("entry_type")
    required = PER_TYPE_REQUIRED.get(entry_type, ())
    bad: list[str] = []
    for field in required:
        value = data.get(field)
        if value is None:
            bad.append(field)
            continue
        if isinstance(value, str) and not value.strip():
            bad.append(field)
    return bad


def _prompt_user(field: str) -> str:
    if field == "entry_type":
        hint = f" ({'/'.join(ENTRY_TYPES)})"
    else:
        hint = ""
    while True:
        value = input(f"  {field}{hint}: ").strip()
        if not value:
            continue
        if field == "entry_type" and value not in ENTRY_TYPES:
            print(f"    must be one of: {', '.join(ENTRY_TYPES)}", file=sys.stderr)
            continue
        return value


def validate(data: dict) -> dict[str, list[str]]:
    """Public view of universal + per-type validation results.

    Used by non-interactive callers (`inspect`, `ingest --non-interactive`)
    that surface failures via exit code rather than `input()`. Per-type
    errors are only computed when universal validation passes — entry_type
    must be valid before we know which per-type list to consult.
    """
    universal = _validate(data)
    per_type = [] if universal else _validate_per_type(data)
    return {"universal": universal, "per_type": per_type}


def extract(
    markdown: str,
    *,
    model: str,
    api_key_env: str,
    interactive: bool = True,
) -> dict:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(
            f"API key not found in env var {api_key_env}. "
            f"Set it or change anthropic_api_key_env in your config."
        )

    client = Anthropic(api_key=api_key)
    system = _load_prompt()
    user_content = markdown[:MAX_INPUT_CHARS]

    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = response.content[0].text.strip()
    cleaned = _strip_code_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"model did not return valid JSON ({e})", file=sys.stderr)
        print(f"--- raw response ---\n{raw}\n--- end ---", file=sys.stderr)
        data = {}

    if interactive:
        bad = _validate(data)
        if bad:
            print(f"validation failed on: {', '.join(bad)}", file=sys.stderr)
            print(f"--- raw response ---\n{raw}\n--- end ---", file=sys.stderr)
            print("enter values (stdin):", file=sys.stderr)
            for field in bad:
                value = _prompt_user(field)
                data[field] = int(value) if field == "year" else value

        bad_per_type = _validate_per_type(data)
        if bad_per_type:
            entry_type = data.get("entry_type")
            print(
                f"per-type validation failed for entry_type={entry_type}: "
                f"{', '.join(bad_per_type)}",
                file=sys.stderr,
            )
            print("enter values (stdin):", file=sys.stderr)
            for field in bad_per_type:
                data[field] = _prompt_user(field)

    # Ensure optional keys are present (as null) so downstream code can rely on shape.
    for field in OPTIONAL_FIELDS:
        data.setdefault(field, None)

    return data


def confirm_entry_type(data: dict, *, interactive: bool = True) -> dict:
    """Stage 5 confirmation: show detected entry_type and let the user override.

    On override, re-runs per-type validation against the new type and prompts
    for any required fields the new type needs that the original did not.

    Under interactive=False this is a no-op — non-interactive callers trust
    the entry_type embedded in the supplied metadata (r7 resolution).
    """
    if not interactive:
        return data

    detected = data.get("entry_type")
    print(
        f"  detected: entry_type={detected}  "
        f"author={data.get('author')!r}  "
        f"year={data.get('year')!r}  "
        f"title={data.get('full_title')!r}",
        file=sys.stderr,
    )
    while True:
        ans = input(f"entry_type={detected} — keep? [y]/n: ").strip().lower()
        if ans in ("", "y", "yes"):
            return data
        if ans in ("n", "no"):
            break
        print("  please answer y or n", file=sys.stderr)

    new_type = _prompt_user("entry_type")
    if new_type == detected:
        return data
    data["entry_type"] = new_type

    bad = _validate_per_type(data)
    if bad:
        print(
            f"per-type validation failed for entry_type={new_type}: "
            f"{', '.join(bad)}",
            file=sys.stderr,
        )
        print("enter values (stdin):", file=sys.stderr)
        for field in bad:
            data[field] = _prompt_user(field)
    return data
