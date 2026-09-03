---
name: qsirch
description: >
  QNAP Qsirch 7 REST API client for searching emails (.eml), documents (.pdf, .doc, .xlsx),
  and files on a QNAP NAS. Supports full-text search, server-side category filtering,
  autocomplete suggestions, two-phase async search, email HTML preview extraction, OCR text
  detection with bounding boxes, file download, pagination, and semantic similar-item search.
---

# Qsirch — QNAP NAS Email & File Search

Search indexed emails and documents on a QNAP NAS via the Qsirch 7 REST API.

## Setup

1. Set environment variables:
   ```bash
   export QSIRCH_HOST="10.0.0.3"
   export QSIRCH_PORT="8080"
   export QSIRCH_USER="your_username"
   export QSIRCH_PASS="your_password"
   ```

2. Ensure `requests` is installed:
   ```bash
   pip install requests
   ```

Exit codes: `0` success, `2` authentication failure, `3` API/transport failure.

## Commands

### Search
```bash
python qsirch.py search -q "<KEYWORDS>" [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `-q` / `--query` | *(required)* | Search query with syntax: `"exact phrase"`, `OR`, `AND`, `NOT`, `-exclude`, `(group)`. Use `.` for wildcard (`*` returns 0). |
| `--category` | | Server-side filter via POST: `Email` (only strictly reliable). Others return mixed results. |
| `--ext` | | Client-side extension filter: `eml`, `pdf`, `doc`, `xlsx`, `csv` |
| `--path` | | Client-side path substring filter |
| `--from-date` / `--to-date` | | Client-side `YYYY-MM-DD` bounds on modified date (inclusive) |
| `--limit` | `50` | Max results; server ceiling is **1000** (clamped with a warning) |
| `--offset` | `0` | Pagination offset |
| `--sort` | | `relevance`, `modified`, `created`, `size`, `name` (NOT `title` — broken) |
| `--order` | `desc` | `asc` / `desc` (server default is ascending) |
| `--mode` | `0` | `0`=text, `1`=image OCR, `2`=combined |
| `--highlight` | off | Wrap matches in `<qusion>` tags (500-char snippets; longer returns empty) |
| `--json` | off | JSON output; items enriched with `full_path`, `modified_iso`, `capabilities` |

### Suggest (autocomplete)
```bash
python qsirch.py suggest -q "inv" [--limit 10] [--json]
```
Returns suggestion groups (`name`, `kind`, `modified`, `category`, `history`) for discovering exact values before a full search.

### Async search (two-phase)
```bash
python qsirch.py async-search -q "<KEYWORDS>" [--limit 100] [--json]
```
Fast total count + result fetch. Window size is fixed at submit; `limit`/`offset` on the fetch are ignored server-side.

### Preview
```bash
python qsirch.py preview --path "<dir>" --name "<file>" [--output out.html] [--json]
```
`.eml` → `container_type: "html-eml"` with the full HTML body in `html`. Preview by item ID alone returns HTTP 500; always use path+name.

### Detect (OCR text blocks)
```bash
python qsirch.py detect --path "<dir>" --name "<file>" [--lang ENG] [--text] [--json]
```
Server-side OCR for PDFs/images (not `.eml`). Blocks carry `text`, `vertices` (bounding box), and `score` (confidence).

### Download
```bash
python qsirch.py download --path "<dir>" --name "<file>" [--ext pdf] [--output ./dir]
```

### Status
```bash
python qsirch.py status [--json]
```
Index state, indexed file count, health, and app version. If `indexing`, results may be temporarily incomplete.

### Similar (more-like-this)
```bash
python qsirch.py similar --id "<item-id>" [--limit 10] [--category Email] [--json]
```

## Agent Workflow Notes

- Use `--json` everywhere for programmatic consumption; enriched items expose `capabilities` so you know which chained actions (`download`, `preview`, `text_detect`, `mlt`) exist per item.
- Chain: `search` → pick item by `full_path` → `preview` (emails) or `detect` (scanned PDFs/images) or `download`.
- Extension/date filtering is client-side (server ignores GET filter params) — the CLI applies it; note `count` vs `total` in JSON output (`filtered_out` reports the delta).
- Snippets are 500 chars; use `detect --text` for full OCR text of image-like documents.
- Result pages are capped at 1000 items; paginate with `--offset`.
