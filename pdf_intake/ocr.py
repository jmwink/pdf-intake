import hashlib
import subprocess
import tempfile
from pathlib import Path

from pdf_intake.errors import OCRError

OCRMYPDF_ARGS = ["--skip-text", "--deskew", "--clean"]


def _artifact_path(src: Path) -> Path:
    # Keyed by source hash so two ingests of the same PDF in one session share the artifact.
    h = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"pdf-intake-ocr-{h}.pdf"


def run_ocrmypdf(src: Path) -> Path:
    """Run ocrmypdf on `src`, returning the searchable output PDF's path.

    Uses --skip-text so hybrid PDFs preserve their existing text layer and only
    image-only pages get OCR'd. --deskew + --clean for the small character-level
    wins observed in Spike 1.
    """
    dst = _artifact_path(src)
    if dst.exists():
        return dst

    cmd = ["ocrmypdf", *OCRMYPDF_ARGS, str(src), str(dst)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise OCRError(
            "ocrmypdf binary not found on PATH. Install via `brew install ocrmypdf` "
            "(macOS) or your distribution's package manager."
        ) from e
    except subprocess.CalledProcessError as e:
        raise OCRError(
            f"ocrmypdf exited {e.returncode} on {src.name}",
            stderr=e.stderr or "",
        ) from e

    if not dst.exists():
        raise OCRError(f"ocrmypdf reported success but {dst} is missing")
    return dst
