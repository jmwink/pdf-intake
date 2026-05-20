---
name: pdf-inbox
description: Sweep ~/Downloads for recent PDFs and ingest them into the local PDF library via the `pdf-intake` CLI. Use when the user wants to process pending PDF downloads, file new papers, or asks to "run pdf-inbox" / "process my downloads" / "ingest my new PDFs". For one-off ingest of a specific PDF, prefer `pdf-intake ingest <path>` directly.
---

# pdf-inbox

Drive `pdf-intake` against recent PDFs in `~/Downloads`. The CLI does the heavy lifting (conversion, metadata extraction, BibTeX, summarization). Your job is the human-in-the-loop glue: confirming metadata, asking for category/subfolder, and reporting results.

## Sweep window

Default: PDFs modified within the last 14 days (`find ~/Downloads -name '*.pdf' -mtime -14`).

If the user supplies a window (e.g., "in the last week"), translate to `-mtime -<N>`. If they name a specific file, skip the sweep and process just that file.

## Per-PDF flow

For each candidate PDF:

### 1. Inspect

```
pdf-intake inspect <path>
```

Returns a JSON object on stdout: `content_hash`, `page_count`, `preview_chars`, `already_ingested`, `metadata`, `validation`. Parse the JSON.

### 2. Branch on `already_ingested`

If non-null, the PDF is already in the library at the reported slug. Report it as a duplicate skip and move on. Do not delete the PDF in `~/Downloads` — that's the user's call.

### 3. Branch on `validation`

`validation.universal` is a list of universal required fields that came back invalid/null. `validation.per_type` is the same for the per-entry-type required fields (which depend on `metadata.entry_type`).

- If both lists are empty, the metadata is clean — proceed to step 4.
- If either has entries, surface the gap to the user with `AskUserQuestion`. Show the title/author/year/entry_type you have, list the missing fields, and ask for each. Update the metadata dict in place. Re-validate mentally — if the user filled all missing fields, proceed. If they explicitly skip, drop this PDF from the run.

Pay extra attention when `entry_type` is one of `inbook` / `incollection` and the chapter title looks like the book title — the model sometimes latches onto the book heading instead of the chapter. Cross-reference with `full_title` and `booktitle` in the metadata; if they don't make sense for the chosen `entry_type`, flag it to the user.

### 4. Ask for category and subfolder

Use `AskUserQuestion` with the inspected metadata as context (author, year, full_title, entry_type). Options for category: `teaching`, `research`, `curiosity`, `professional`. Subfolder is freeform (blank = category root).

If the run is processing multiple PDFs and the user has already established a category/subfolder pattern, you can offer it as the recommended choice — but always still ask, since one PDF in a batch may belong somewhere different.

### 5. Ingest non-interactively

Write the (possibly user-corrected) metadata dict to `/tmp/pdf-inbox-<short-hash>.json`, then:

```bash
pdf-intake ingest <path> \
  --non-interactive \
  --category <category> \
  --subfolder <subfolder> \
  --metadata-json "$(cat /tmp/pdf-inbox-<short-hash>.json)"
```

Omit `--subfolder` if the user chose category root.

The `"$(cat ...)"` pattern is robust against apostrophes and other shell-unfriendly characters in the metadata (e.g., titles with possessives).

### 6. Handle the result

- Exit 0: ingest succeeded. Record success in your running tally.
- Exit 1, stderr is a JSON object with `error`: parse it.
  - `error: "per_type_validation"` or `"universal_validation"`: validation regressed (e.g., the user supplied an empty string). Show the user, offer to retry or skip.
  - `error: "target_exists"`: rare — usually means a parallel ingest. Skip and report.
  - `error: "scanned_pdf"`: surface and skip; Stage 10 will add `--ocr`.
  - `error: "already_ingested"`: race against another ingest; report as duplicate.
  - Anything else: dump the error and the stderr to the user.
- Exit 2: argparse misuse — almost always a bug in this skill. Show the user the failing command verbatim.

## End-of-run report

After the last PDF, print a summary:

```
Processed N PDFs:
  - X ingested
  - Y skipped (already in library)
  - Z failed validation
  - W skipped at user request
```

List the ingest paths so the user can verify. If anything failed, name the file and the error.

## Notes

- `pdf-intake` is on the user's path (installed editable into `~/tools/pdf-intake/.venv`); the `pdf-intake` command should resolve via the activated venv. If you get "command not found", source `~/tools/pdf-intake/.venv/bin/activate` first.
- The library root is `~/Library/CloudStorage/Dropbox/Claude/Projects/PDFLibrary/` (per the project's `config.py`).
- Never call `pdf-intake ingest` without `--non-interactive` from inside this skill — the interactive path blocks on stdin, which a tool call can't satisfy.
- Don't move or delete files in `~/Downloads`. `pdf-intake ingest` moves the PDF into the library on success; on failure, the file stays where it is.
