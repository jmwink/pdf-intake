# pdf-intake — install

Local CLI for ingesting PDFs into a Dropbox-synced library with bibliographic metadata, BibTeX, and category-aware summaries.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (or pip + venv; uv is faster and what this guide uses)
- `ocrmypdf` for scanned-PDF support. On macOS:
  ```
  brew install ocrmypdf
  ```
  Pulls in Tesseract + Ghostscript + a few hundred MB of dependencies. On Debian/Ubuntu: `apt install ocrmypdf`. On Arch: `pacman -S ocrmypdf`. Skip only if you're certain you'll never ingest a scanned PDF.
- Anthropic API key in an environment variable. Default key name is `ANTHROPIC_API_KEY`; override in config via `anthropic_api_key_env`.

## Install

```
cd ~/tools
# clone or copy pdf-intake/ here
cd pdf-intake
uv venv
source .venv/bin/activate
uv pip install -e .
```

Verify:

```
pdf-intake --version    # should print a version
which pdf-intake        # should point into .venv/bin
```

## Config

The library root and a few thresholds live in `~/.config/pdf-intake/config.toml`. A working template:

```toml
library_root = "~/Documents/PDFLibrary"
default_model = "claude-sonnet-4-6"
extraction_min_chars = 500
extraction_min_words_per_page = 50
anthropic_api_key_env = "ANTHROPIC_API_KEY"
```

`example.config.toml` in the repo is the canonical reference. If `~/.config/pdf-intake/config.toml` is absent, the CLI falls back to the bundled defaults (`~/Documents/PDFLibrary` for the library root).

For multi-machine sync, point `library_root` at a synced folder — Dropbox, iCloud Drive, Syncthing — and use the same path on every machine.

## API key

Set the env var your config names:

```
export ANTHROPIC_API_KEY=sk-ant-…
```

Add it to your shell rc so it persists across sessions.

## Smoke test

```
pdf-intake convert <path-to-pdf>    # writes <path>.md
pdf-intake ingest <path-to-pdf>     # full pipeline; interactive prompts
```

## Multi-machine notes

The library root is Dropbox-synced, which means `.index.json`, `.bib/`, and every `<slug>/` folder propagate between machines for free. Implications:

- **Idempotency works across machines.** Ingesting a PDF on machine A then trying to ingest the same content on machine B reports "already ingested" via the synced hash index — no duplicate folder.
- **One temp file for index writes.** `Index.save` writes to a fixed `.index.json.tmp` path before atomic rename. Two machines writing simultaneously could collide. Single-user / single-active-machine is fine; if you ever have two machines writing at the same time (e.g., Dropbox replaying a queued write on wake), see the Stage 3 stretch task and the Stage 10 "Known issue" note in the plan for the fix (`tempfile.NamedTemporaryFile`).
- **`ocrmypdf` runs locally per machine.** Each machine needs `ocrmypdf` installed. The OCR'd PDF that lands in the library is portable — once on Dropbox, the other machine reads it as a normal searchable PDF.
- **Per-entry `.bib/<citekey>.bib` layout is sync-conflict-friendly.** Editing one entry on machine A while another is being written on machine B produces at worst a Dropbox conflict file on the single affected entry, not a corrupted `library.bib`. Rebuild with `pdf-intake bib build`.

## Uninstall

```
deactivate
rm -rf ~/tools/pdf-intake/.venv
brew uninstall ocrmypdf   # optional; safe to leave installed
```

Library files under `<library_root>` are independent of the install and will outlive it.
