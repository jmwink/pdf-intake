"""Slug construction per r2 rules: authorYYYY_kebab-short-title, ≤60 chars, ASCII-folded.

Citekey = slug (r5 Decisions): one paper, one identity.
"""

import re
import unicodedata

MAX_SLUG_LEN = 60

# Title stopwords. Deliberately excludes interrogatives (how/why/where/who/what)
# because they carry meaning in titles ("How Institutions Think" would collapse to
# "institutions-think"). Iterate this list as real titles expose gaps.
STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "are",
    "it", "many", "of", "on", "so", "that", "the", "there", "this", "to", "with",
})


def _fold_ascii(s: str) -> str:
    # NFKD decomposes "é" into "e" + combining-accent; encode drops the accent.
    decomposed = unicodedata.normalize("NFKD", s)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _last_name(full_name: str) -> str:
    if "," in full_name:
        return full_name.split(",", 1)[0].strip()
    tokens = full_name.split()
    return tokens[-1] if tokens else ""


def _author_token(author: str) -> str:
    """Collapse a BibTeX-style ' and '-separated author string to a slug token."""
    parts = [p.strip() for p in re.split(r"\s+and\s+", author) if p.strip()]
    last_names = [re.sub(r"[^a-z0-9]", "", _fold_ascii(_last_name(p)).lower()) for p in parts]
    last_names = [n for n in last_names if n]

    if not last_names:
        return "unknown"
    if len(last_names) == 1:
        return last_names[0]
    if len(last_names) == 2:
        return f"{last_names[0]}-{last_names[1]}"
    if len(last_names) == 3:
        return f"{last_names[0]}-{last_names[1]}-{last_names[2]}"
    return f"{last_names[0]}-etal"


def _title_token(short_title: str, budget: int) -> str:
    folded = _fold_ascii(short_title).lower()
    words = re.findall(r"[a-z0-9]+", folded)
    words = [w for w in words if w not in STOPWORDS]
    if not words:
        return "untitled"
    result = "-".join(words)
    # Trim words off the end until under budget; preserve word boundaries.
    while len(result) > budget and len(words) > 1:
        words.pop()
        result = "-".join(words)
    if len(result) > budget:
        result = result[:budget]
    return result


def build_slug(author: str, year, short_title: str) -> str:
    author_tok = _author_token(author)
    year_str = str(year)
    prefix = f"{author_tok}{year_str}_"
    title_budget = MAX_SLUG_LEN - len(prefix)
    if title_budget < 3:
        # Author token alone is too long; hard-truncate the author side.
        keep = MAX_SLUG_LEN - len(year_str) - 1 - 3
        author_tok = author_tok[:max(keep, 1)]
        prefix = f"{author_tok}{year_str}_"
        title_budget = MAX_SLUG_LEN - len(prefix)
    return f"{prefix}{_title_token(short_title, title_budget)}"
