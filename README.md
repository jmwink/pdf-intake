# pdf-intake

A local CLI for ingesting academic PDFs into an organized library with bibliographic metadata, BibTeX entries, and category-aware summaries written by Claude.

## What it does

Given a PDF, `pdf-intake` will:

1. **Convert** it to markdown — routing scanned PDFs through `ocrmypdf` for searchable text, born-digital PDFs through `pymupdf4llm` for markdown structure.
2. **Extract** bibliographic metadata (author, year, title, entry type, publisher, etc.) via the Claude API, with honest null-when-unknown discipline and per-entry-type required-field validation.
3. **File** it under `<library>/<category>/[<subfolder>/]<authorYYYY_short-title>/` with the PDF, a markdown rendering, and a category-tailored summary.
4. **Track** it in a content-hash-keyed index (`.index.json`) so re-ingesting the same content — under any filename — is a no-op.
5. **Emit** a BibTeX entry at `.bib/<citekey>.bib`. `pdf-intake bib build` concatenates them into a library-wide `library.bib`.

Designed for a single-user, multi-machine setup where the library lives in a synced folder (Dropbox, iCloud, Syncthing) and the CLI is installed on each machine.

## Why it exists

I wanted summaries that knew what kind of document they were summarizing — a teaching reading gets a different treatment than a research paper or a curiosity find — and a BibTeX collection that didn't drift from the files it referenced. Doing this by hand was the kind of work I'd start with good intentions and abandon within a month. The CLI is the version that does it the same way every time.

## Quick start

See [INSTALL.md](INSTALL.md) for prerequisites and per-machine setup.

```bash
pdf-intake ingest ~/Downloads/some-paper.pdf
```

The interactive flow asks for category and subfolder, runs metadata extraction, and writes the entry. A `pdf-inbox` Claude skill drives a batch-from-Downloads workflow over the same CLI under `--non-interactive`.

## Architecture in one paragraph

The model is asked for atomic bibliographic fields, never for BibTeX strings — BibTeX is constructed locally from the validated dict. Null is a first-class value in the schema: when a field isn't printed in the source, the model returns null rather than guessing. Validation tiers by entry type (a `@book` must have a publisher; an `@inbook` must have publisher and pages; `@article` needs a journal). When the model can't supply a required field, an interactive prompt asks the user — the LLM isn't looped on its own failure. Identity is content-hash-derived, not name-derived, so the same PDF under any filename collapses to one library entry.

## Status

Built as a staged learning project; the staged plan lives outside this repo. Stages 0–10 are complete. The plan's revision history (r2 through r8) captures the decisions that shaped each stage.

## License

MIT — see [LICENSE](LICENSE).
