---
name: qsirch
description: >
  QNAP Qsirch 7 REST API client for searching emails (.eml), documents (.pdf, .doc, .xlsx),
  and files on a QNAP NAS. Supports full-text search, server-side category filtering,
  email HTML preview extraction, file download, pagination, and semantic similar-item search.
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

## Commands

### Search
```bash
python qsirch.py search -q "<KEYWORDS>" [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `-q` / `--query` | *(required)* | Search query with syntax: `"exact phrase"`, `OR`, `AND`, `NOT`, `-exclude`, `(group)`. Use `.` for wildcard. |
| `--category` | | Server-side filter via POST: `Email` (only strictly reliable). Others return mixed results. |
| `--ext` | | Client-side extension filter: `eml`, `pdf`, `doc`, `xlsx`, `csv` |
| `--limit` | `50` | Max results |
| `--offset` | `0` | Pagination offset |
| `--sort` | | `relevance`, `modified`, `created`, `size`, `name` |
| `--order` | `desc` | `asc` / `desc` (default is ascending server-side; ignored for relevance) |
| `--mode` | `0` | `0`=text, `1`=image OCR, `2`=combined |
| `--path` | | Client-side path substring filter |
| `--from-date` | | Date from (`YYYY-MM-DD`) |
| `--to-date` | | Date to (`YYYY-MM-DD`) |
| `--json` | | Raw JSON output |

### Preview (Email HTML Extraction)
```bash
python qsirch.py preview --path "<path>" --name "<filename>" [--output file.html] [--json]
```

### Download
```bash
python qsirch.py download --path "<path>" --name "<filename>" [--ext pdf] [-o ./dir/]
```

### Status
```bash
python qsirch.py status [--json]
```

### Similar (More-Like-This)
```bash
python qsirch.py similar --id <item_id> [--limit 10] [--category Email] [--json]
```

## Key API Notes

- **Advanced query syntax works in `q=`**: `"exact phrase"`, `OR`, `AND`, `NOT`, `-exclude`, `(grouping)` are all processed server-side.
- `q.*` params (`q.category`, `q.modified`, `q.path`, `q.name`, `q.string`) in the web UI URL are **client-side UI state** — the API ignores them.
- Extension/type GET params are **silently ignored** — filtering is done client-side.
- POST `tools=Email` is the only strictly reliable category filter. Other values return mixed types — combine with `--ext`.
- Sort param is `sort_by` (not `sort`). Direction is `sort_dir` (not `order`). Default direction is ascending. Ignored for `relevance`.
- `sort_by=title` is broken (returns 0 results) — use `name`.
- Wildcard is `.` or space — `*` returns 0 results.
- `advanced_mode`: `0`=text, `1`=image OCR, `2`=combined.
- `highlight=content` wraps matches in `<qusion>` tags; `highlight_limit` controls snippet length.
- `item["path"]` is parent dir only — full path is in `item["preview"]["info"][key=="path"]`.
- Session auto-recovers on HTTP 401 / error code 101.
- API aliases: `/v1/`, `/v2/`, `/stable/`, `/latest/` all resolve identically.
