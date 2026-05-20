class ScannedPDFError(Exception):
    pass


class BibtexBuildError(Exception):
    """Raised when our own build_entry output fails a round-trip parse."""


class BibtexValidationError(Exception):
    """Raised when a .bib file (typically user-edited) fails to parse."""


class OCRError(Exception):
    """Raised when ocrmypdf fails. Carries the captured stderr for surfacing."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr
