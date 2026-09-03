# Qsirch CLI

A Python command-line client for the **QNAP Qsirch 7 REST API**. Search emails, documents, and files indexed on your QNAP NAS directly from the terminal or integrate into automated workflows.

Built from comprehensive reverse-engineering of the undocumented Qsirch 7 API (v7.1.0.0 verified against a production 915k-file index).

## Features

- **Full-text search** with advanced query syntax (exact phrases, boolean OR/AND/NOT, exclusion, grouping)
- **Server-side category filtering** via POST (Email is strictly reliable)
- **Client-side filtering** by extension, path substring, and date range
- **Autocomplete suggestions** — discover exact filenames, file kinds, and categories for partial queries
- **Two-phase async search** — get the total match count fast, then fetch the result window
- **Email preview** — extract full rendered HTML email bodies without downloading raw `.eml` files
- **OCR text detection** — server-side text blocks with bounding boxes and confidence (PDFs and images)
- **File download** — save any indexed file to local disk
- **More-like-this** — find semantically similar documents by item ID
- **Status check** — monitor indexing health, file count, and app version
- **Auto re-authentication** — seamless session recovery on token expiry
- **Agent-friendly JSON** — `--json` items are enriched with `full_path`, `modified_iso`, and `capabilities`
- **Machine-readable exit codes** — `0` success, `2` auth failure, `3` API/transport failure
- **Backward-compatible CLI** — works with or without explicit subcommands

## Requirements

- Python 3.8+
- `requests` library

```bash
pip install requests
```

## Configuration

Authentication is configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `QSIRCH_HOST` | NAS IP/hostname | `10.0.0.3` |
| `QSIRCH_PORT` | HTTP port | `8080` |
| `QSIRCH_USER` | NAS username | *(required)* |
| `QSIRCH_PASS` | NAS password | *(required)* |
| `QSIRCH_SSL` | Set to `1` for HTTPS | `0` |

```bash
export QSIRCH_HOST="10.0.0.3"
export QSIRCH_PORT="8080"
export QSIRCH_USER="your_username"
export QSIRCH_PASS="your_password"
```

Or pass credentials via CLI flags: `--host`, `--port`, `--user`, `--pass`, `--ssl`, `--timeout`.

## Usage

### Search

```bash
# Basic search
python qsirch.py search -q "invoice"

# Exact phrase search
python qsirch.py search -q '"tax invoice"'

# Boolean operators
python qsirch.py search -q "invoice OR receipt"
python qsirch.py search -q "invoice AND amazon"
python qsirch.py search -q "invoice NOT ebay"

# Exclusion (short form) and grouping
python qsirch.py search -q "invoice -ebay"
python qsirch.py search -q "(invoice, OR receipt) -ebay"

# Search emails only (server-side category filter via POST)
python qsirch.py search -q "invoice" --category Email

# Filter by extension, path, and date range (client-side)
python qsirch.py search -q "statement" --ext pdf --path "QmailAgent" --from-date 2025-04-01 --to-date 2025-06-30

# Sort by most recently modified, output JSON
python qsirch.py search -q "receipt" --sort modified --limit 20 --json

# Highlight matches with <qusion> tags (500-char snippets)
python qsirch.py search -q "invoice" --highlight --json

# Image OCR search (find text within images)
python qsirch.py search -q "receipt" --mode 1

# Wildcard (match all indexed files)
python qsirch.py search -q "." --ext pdf --limit 100
```

#### Query Syntax

The `q=` parameter supports advanced query syntax:

| Syntax | Example | Effect |
|--------|---------|--------|
| `"phrase"` | `"tax invoice"` | Exact phrase match |
| `OR` | `invoice OR receipt` | Match either term |
| `AND` | `invoice AND amazon` | Match both terms (stricter than default) |
| `NOT` | `invoice NOT ebay` | Exclude results containing term |
| `-term` | `invoice -ebay` | Exclude (short form) |
| `(group)` | `(invoice, OR receipt)` | Group terms |
| `.` | `.` | Wildcard — match all indexed files |

> **Note:** `*` as wildcard returns 0 results. Use `.` or a space instead.

**Available categories** (POST `tools` filter): `Email` is the only strictly reliable filter. Other values (`PDF`, `Documents`, `Images`, `Videos`, `Music`, `Excel`, `Word`) return mixed results — use `--ext` for precise filtering.

**Sort fields**: `relevance`, `modified`, `created`, `size`, `name`

**Search modes** (`--mode`): `0` = text search (default), `1` = image OCR search, `2` = combined

**Limit**: max results per page is **1000** (server ceiling). Requests above it return an empty body instead of an error; the CLI clamps and warns.

> **Note:** Do not use `title` as a sort field — it is broken server-side and returns 0 results. Default sort direction is ascending; use `--order desc` for newest-first. For `sort_by=relevance`, sort direction is ignored (always best-match-first).

### Suggest

Autocomplete for a partial query — useful for agents to discover exact filenames, kinds, and categories before running a full search:

```bash
python qsirch.py suggest -q "inv"
python qsirch.py suggest -q "inv" --json
```

Returns groups keyed `name`, `kind`, `modified`, `category`, `history`.

### Async Search

Two-phase search: submit returns the total match count and a result URL immediately (fast even on a huge index), then the items are fetched from that URL. The result window size is fixed at submission — `limit`/`offset` on the fetch are ignored by the server.

```bash
python qsirch.py async-search -q "invoice" --limit 100
python qsirch.py async-search -q "invoice" --json
```

### Preview

Extract email HTML body or file preview metadata without downloading the file:

```bash
# Preview an email (returns full rendered HTML body)
python qsirch.py preview --path "Library/QmailAgent/mail/2025/08/16" --name "message.eml"

# Save HTML to file
python qsirch.py preview --path "..." --name "..." --output email.html

# Output raw JSON metadata
python qsirch.py preview --path "..." --name "..." --json
```

**Response types:**
- `.eml` files → `container_type: "html-eml"` with full email body in `html` field (verified)
- PDFs/Books → `container_type: "image"` with page count and image URLs
- Other files → `container_type: "info"` with metadata

> **Note:** Preview by item ID alone returns HTTP 500 server-side; always use `--path` + `--name` (or the item's own `actions.preview` URL from a search result).

### Detect (OCR Text Blocks)

Server-side OCR with bounding boxes — available for PDFs and images, not `.eml`:

```bash
# Text blocks with coordinates and confidence
python qsirch.py detect --path "Library/..." --name "scan.pdf"

# Plain text, one block per line
python qsirch.py detect --path "Library/..." --name "scan.pdf" --text

# Raw JSON (blocks with vertices and score)
python qsirch.py detect --path "Library/..." --name "scan.pdf" --json
```

Each block: `text`, `vertices` (four corner points), `score` (confidence 0–1).

### Download

```bash
python qsirch.py download --path "Library/QmailAgent/attachment" --name "invoice.pdf" --ext pdf --output ./downloads/
```

### Status

```bash
python qsirch.py status
# Qsirch Status: indexing
# Indexed files: 915,580
# Health: 0
# Version: v7.1.0.0 (b1ccd12) (2026-07-22)

python qsirch.py status --json   # adds brief + version details
```

If status is `indexing`, search results may be temporarily incomplete.

### Similar (More-Like-This)

```bash
python qsirch.py similar --id "934a6bd662abdb5dfc3654e4d8ac8c92145d00ea" --limit 5 --category Email
```

The item ID is also available in search results; each item's `actions.mlt` URL carries the same query with the item's own category/extension pre-filled.

## JSON Output for Agents

With `--json`, search items are augmented client-side:

```json
{
  "full_path": "Library/QmailAgent/attachment/2026/invoice.pdf",
  "modified_iso": "2026-08-06T06:00:05+00:00",
  "capabilities": ["download", "mlt", "preview", "text_detect", "thumbnail"]
}
```

- `full_path` — the resolved absolute NAS path (`item["path"]` alone is only the parent directory)
- `modified_iso` — normalized UTC timestamp (handles epoch, ISO-Z, and `YYYY/MM/DD HH:MM:SS` formats)
- `capabilities` — which chained actions exist for this item, so an agent knows what it can call next without parsing URLs

## API Quirks & Caveats

This client works around several undocumented Qsirch 7 API behaviors, verified via live testing against a production NAS (v7.1.0.0):

1. **GET filter parameters are silently ignored** — `ext`, `extension`, `type`, `category`, `file_type` as GET query parameters have **no effect on results** (same total, same items). All extension/type filtering must be done client-side.

2. **`q.*` params are UI state, not API filters** — the Qsirch web frontend includes `q.category`, `q.modified`, `q.path`, `q.name`, and `q.string` in its URLs, but these are **client-side state stored in the URL for the web UI**. The API backend ignores them entirely. `q.string` without `q` returns HTTP 400.

3. **Advanced query syntax works in `q=`** — the `q` parameter supports exact phrases (`"..."`), boolean operators (`OR`, `AND`, `NOT`), exclusion (`-term`), and grouping (`(...)`). These are processed server-side and affect result counts.

4. **POST `tools` filtering only works reliably for `Email`** — `POST /qsirch/latest/api/search?q=<query>` with body `{"tools": "Email"}` correctly restricts results to `.eml` files. Other tools values (`PDF`, `Documents`, `Excel`, `Word`, `Images`) return **mixed file types**. The `q` parameter must be in the URL query string, not the JSON body.

5. **Sort parameter is `sort_by`, not `sort`** — the legacy name `sort` is silently ignored. Valid values: `modified`, `created`, `size`, `name`, `relevance`.

6. **`sort_by=title` is broken** — returns `total: 0`. Use `name` instead.

7. **Sort direction is `sort_dir`, not `order`** — only `sort_dir` (`asc`/`desc`) works. Default is **ascending**. For `sort_by=relevance`, `sort_dir` is ignored (always best-match-first).

8. **`highlight=content`** — wraps search term matches in `<qusion>...</qusion>` tags within the `content` snippet field. **`highlight_limit` above ~500 returns an empty content field** (server bug); the default 500-char snippet is the usable maximum.

9. **`advanced_mode`** — `0` = standard text search (default), `1` = image OCR search (finds text within images only, returns jpg/png/webp/bmp), `2` = combined text + image results.

10. **Wildcard is `.` or space, not `*`** — `q=*` returns 0 results. Use `q=.` or `q= ` for match-all.

11. **Path resolution** — `item["path"]` is only the parent directory. The full file path is in `item["preview"]["info"]` where `key == "path"`.

12. **All file actions route through `/qusion-item`** — no separate download/preview endpoints. Action URLs are returned dynamically in each item's `actions` object. Actions include `thumbnail`, `open`, `download`, `share`, `preview`, `text_detect`, `mlt`, `icon`, and `open_to`.

13. **Session expiry** — returns HTTP 401 with `{"error": {"code": 101, ...}}` (or sometimes a bare 401). This client automatically re-authenticates once and retries on both shapes.

14. **API path aliases** — `/qsirch/v1/api/`, `/qsirch/v2/api/`, `/qsirch/stable/api/`, and `/qsirch/latest/api/` all resolve to the same endpoint.

15. **Result limit ceiling is 1000** — `limit` values above 1000 return an empty body without a `total` key instead of an error. 1000 works reliably (~3.9 MB payload, ~2.4 s on a 915k-file index).

16. **`/api/suggest`** — undocumented autocomplete endpoint. `GET /qsirch/latest/api/suggest?q=<prefix>&limit=N` returns suggestion groups (`name`, `kind`, `modified`, `category`, `history`).

17. **`/api/async-search`** — two-phase search. Submit `GET /qsirch/latest/api/async-search?q=<query>&limit=N` → returns `total` plus `context.url`; fetch that URL for the items. The window is fixed at submit time; `limit`/`offset` on the fetch URL are ignored.

18. **`/api/preview/text-detection`** — undocumented OCR endpoint returning text blocks with `vertices` and `score`. Available via `actions.text_detect` for PDFs and images. Accepts `lang` (e.g. `ENG`).

19. **Preview/download by item ID alone fails** — `qusion-item?action=preview&id=<id>` returns HTTP 500; `path` + `name` are required.

20. **Auxiliary read endpoints** — `/api/about` (version), `/api/status/brief` (license/health/index state), `/api/setting/system` (feature flags incl. `image_ocr_extract_enable`). Windows/platform endpoints (`/api/sources`, `/api/text-mining`, `/api/qmail`) 404 on NAS installs.

## Search Response Structure

Each item in search results contains:

```json
{
  "id": "sha1_hash",
  "name": "filename_without_extension",
  "extension": "eml",
  "type": "file",
  "category": ["Email"],
  "size": 30226,
  "path": "Library/QmailAgent/.../parent_directory",
  "content": "...text snippet with match...",
  "metadata": {
    "all": [
      {"key": "from", "value": "sender@example.com"},
      {"key": "subject", "value": "Invoice #12345"}
    ]
  },
  "preview": {
    "info": [{"key": "path", "value": "full/path/to/file.eml"}]
  },
  "actions": {
    "thumbnail": "/qsirch/latest/api/qusion-item?action=thumbnail&...",
    "download": "/qsirch/latest/api/qusion-item?action=download&...",
    "preview": "/qsirch/latest/api/qusion-item?action=preview&...",
    "text_detect": "/qsirch/latest/api/preview/text-detection?...",
    "mlt": "/qsirch/latest/api/more-like-this/<id>?categories=..."
  }
}
```

## Authentication Flow

This client uses the QTS CGI login method:

1. `POST /cgi-bin/authLogin.cgi` with Base64-encoded password
2. Parses XML response for `authSid`
3. Sets `NAS_SID` session cookie for all subsequent requests
4. On HTTP 401 (code 101 or bare 401), re-authenticates once and retries

## License

MIT
