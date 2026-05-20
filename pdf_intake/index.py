import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


def content_hash(path: Path) -> str:
    # Read in chunks so large PDFs don't load fully into memory.
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


@dataclass
class Index:
    library_root: Path

    @property
    def path(self) -> Path:
        return self.library_root / ".index.json"

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        with self.path.open("r") as f:
            return json.load(f)

    def save(self, data: dict) -> None:
        # Write to a sibling temp file in the same directory, then os.replace.
        # Same-filesystem rename is atomic on POSIX and Windows; a temp file in
        # /tmp would cross filesystems and lose atomicity.
        self.library_root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def lookup(self, hash_: str) -> dict | None:
        return self.load().get(hash_)

    def record(self, hash_: str, entry: dict) -> None:
        data = self.load()
        data[hash_] = entry
        self.save(data)
