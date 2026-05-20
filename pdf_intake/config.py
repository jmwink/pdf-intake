import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "pdf-intake" / "config.toml"

DEFAULT_LIBRARY_ROOT = Path.home() / "Documents" / "PDFLibrary"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_EXTRACTION_MIN_CHARS = 500
DEFAULT_EXTRACTION_MIN_WORDS_PER_PAGE = 50
DEFAULT_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


@dataclass
class Config:
    library_root: Path
    default_model: str
    extraction_min_chars: int
    extraction_min_words_per_page: int
    anthropic_api_key_env: str


def load_config(path: Path = CONFIG_PATH) -> Config:
    data: dict = {}
    if path.is_file():
        with path.open("rb") as f:
            data = tomllib.load(f)

    library_root = data.get("library_root")
    return Config(
        library_root=Path(library_root).expanduser() if library_root else DEFAULT_LIBRARY_ROOT,
        default_model=data.get("default_model", DEFAULT_MODEL),
        extraction_min_chars=data.get("extraction_min_chars", DEFAULT_EXTRACTION_MIN_CHARS),
        extraction_min_words_per_page=data.get(
            "extraction_min_words_per_page", DEFAULT_EXTRACTION_MIN_WORDS_PER_PAGE
        ),
        anthropic_api_key_env=data.get("anthropic_api_key_env", DEFAULT_ANTHROPIC_API_KEY_ENV),
    )
