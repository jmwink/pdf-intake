import os
from pathlib import Path

import bibtexparser
from bibtexparser.model import Entry, Field
from bibtexparser import Library

from pdf_intake.errors import BibtexBuildError, BibtexValidationError


# Field order in the emitted entry. short_title is mapped to `title`.
FIELD_ORDER = (
    "author",
    "title",
    "year",
    "journal",
    "booktitle",
    "editor",
    "chapter",
    "volume",
    "number",
    "pages",
    "publisher",
    "address",
    "school",
    "doi",
)

# Scalar fields the model may emit as int; BibTeX wants strings.
_COERCE_TO_STR = {"volume", "number", "year", "pages", "chapter"}

# LaTeX special characters that must be escaped inside a braced field value.
# Order matters: backslash first so we don't double-escape our own escapes.
_ESCAPES = (
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("$", r"\$"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
)


def _escape(value: str) -> str:
    # No typographic normalization (en/em-dashes, curly quotes, ellipsis): the
    # target workflow is Pandoc-citeproc, which is Unicode-native. Revisit if
    # these .bib files ever feed legacy bibtex + pdflatex.
    for src, dst in _ESCAPES:
        value = value.replace(src, dst)
    return value


def _normalize(key: str, value) -> str | None:
    if value is None:
        return None
    if key in _COERCE_TO_STR:
        value = str(value)
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return None
    return _escape(value)


def build_entry(fields: dict, citekey: str) -> str:
    entry_type = fields.get("entry_type")
    if not entry_type:
        raise BibtexBuildError("fields dict missing entry_type")

    # BibTeX `title` prefers full_title (includes subtitle); fall back to
    # short_title if only that was provided (e.g., entries built before
    # the full_title prompt change).
    source = dict(fields)
    if "title" not in source:
        source["title"] = source.get("full_title") or source.get("short_title")

    built: list[Field] = []
    for key in FIELD_ORDER:
        norm = _normalize(key, source.get(key))
        if norm is not None:
            built.append(Field(key, norm))

    lib = Library()
    lib.add(Entry(entry_type=entry_type, key=citekey, fields=built))
    bib_str = bibtexparser.write_string(lib)

    # Round-trip: parse what we just wrote, fail loudly if it doesn't come back clean.
    check = bibtexparser.parse_string(bib_str)
    if check.failed_blocks:
        raise BibtexBuildError(
            f"built entry for {citekey} failed to round-trip:\n{bib_str}"
        )
    return bib_str


def write_entry(citekey: str, bib_str: str, bib_dir: Path) -> Path:
    bib_dir.mkdir(parents=True, exist_ok=True)
    out = bib_dir / f"{citekey}.bib"
    tmp = out.with_suffix(".bib.tmp")
    with tmp.open("w") as f:
        f.write(bib_str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out)
    return out


def validate_user_edited(path: Path) -> Library:
    text = path.read_text()
    lib = bibtexparser.parse_string(text)
    if lib.failed_blocks:
        msgs = [str(b) for b in lib.failed_blocks]
        raise BibtexValidationError(
            f"{path} failed to parse: {'; '.join(msgs)}"
        )
    return lib


def build_library(bib_dir: Path, out: Path) -> int:
    files = sorted(bib_dir.glob("*.bib"))
    parts: list[str] = []
    for p in files:
        parts.append(p.read_text().rstrip() + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write("\n".join(parts))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out)
    return len(files)
