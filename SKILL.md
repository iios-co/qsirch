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
| `-q` / `--query` | *(required)* | Search query (use `.` for wildcard match-all) |
| `--category` | | Server-side filter: `Email`, `PDF`, `Documents`, `Images`, `Videos`, `Music`, `Excel`, `Word` |
| `--ext` | | Client-side extension filter: `eml`, `pdf`, `doc`, `xlsx`, `csv` |
| `--limit` | `50` | Max results |
| `--offset` | `0` | Pagination offset |
| `--sort` | | `relevance`, `modified`, `created`, `size`, `name` |
| `--order` | `desc` | `asc` / `desc` |
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

- Extension/type GET params are **broken server-side** — filtering is done client-side.
- Category filtering uses POST with `{"tools": "Email"}`.
- `sort_by=title` is broken — use `name`.
- `item["path"]` is parent dir only — full path is in `item["preview"]["info"][key=="path"]`.
- Session auto-recovers on HTTP 401 / error code 101.
