# Qsirch CLI

A Python command-line client for the **QNAP Qsirch 7 REST API**. Search emails, documents, and files indexed on your QNAP NAS directly from the terminal or integrate into automated workflows.

Built from comprehensive reverse-engineering of the undocumented Qsirch 7 API.

## Features

- **Full-text search** with server-side category filtering and client-side extension/path/date filtering
- **Email preview** — extract full rendered HTML email bodies without downloading raw `.eml` files
- **File download** — save any indexed file to local disk
- **More-like-this** — find semantically similar documents by item ID
- **Status check** — monitor indexing health and file count
- **Auto re-authentication** — seamless session recovery on token expiry
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
| `QSIRCH_HOST` | QNAP NAS IP or hostname | `10.0.0.3` |
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

Or pass credentials via CLI flags: `--host`, `--port`, `--user`, `--pass`, `--ssl`.

## Usage

### Search

```bash
# Basic search
python qsirch.py search -q "invoice"

# Search emails only (server-side category filter via POST)
python qsirch.py search -q "invoice" --category Email

# Filter by extension, path, and date range (client-side)
python qsirch.py search -q "statement" --ext pdf --path "QmailAgent" --from-date 2025-04-01 --to-date 2025-06-30

# Sort by most recently modified, output JSON
python qsirch.py search -q "receipt" --sort modified --limit 20 --json
```

**Available categories** (POST `tools` filter): `Email` is the only strictly reliable filter. Other values (`PDF`, `Documents`, `Images`, `Videos`, `Music`, `Excel`, `Word`) return mixed results — use `--ext` for precise filtering.

**Sort fields**: `relevance`, `modified`, `created`, `size`, `name`

> **Note:** Do not use `title` as a sort field — it is broken server-side and returns 0 results. Default sort direction is ascending; use `--order desc` for newest-first.

### Preview

Extract email HTML body or file preview metadata without downloading the file:

```bash
# Preview an email (returns full rendered HTML body)
python qsirch.py preview --path "Library/QmailAgent/mail/2025/08/16/message.eml" --name "message.eml"

# Save HTML to file
python qsirch.py preview --path "..." --name "..." --output email.html

# Output raw JSON metadata
python qsirch.py preview --path "..." --name "..." --json
```

**Response types:**
- `.eml` files → `container_type: "html-eml"` with full email body in `html` field
- PDFs/Books → `container_type: "image"` with page count and image URLs

### Download

```bash
python qsirch.py download --path "Library/QmailAgent/attachment/invoice.pdf" --name "invoice.pdf" --ext pdf --output ./downloads/
```

### Status

```bash
python qsirch.py status
# Qsirch Status: indexing
# Indexed files: 898,040
# Health: 0
```

If status is `indexing`, search results may be temporarily incomplete.

### Similar (More-Like-This)

```bash
python qsirch.py similar --id "934a6bd662abdb5dfc3654e4d8ac8c92145d00ea" --limit 5 --category Email
```

## API Quirks & Caveats

This client works around several undocumented Qsirch 7 API behaviors, verified via live testing:

1. **GET filter parameters are silently ignored** — passing `ext`, `extension`, `type`, `category`, or `file_type` as GET query parameters does not cause errors but has **no effect on results** (same total, same items as without them). All extension/type filtering must be done client-side.

2. **POST `tools` filtering only works reliably for `Email`** — `POST /qsirch/latest/api/search?q=<query>` with body `{"tools": "Email"}` correctly restricts results to `.eml` files. However, other tools values (`PDF`, `Documents`, `Excel`, `Word`, `Images`) return **mixed file types** and are not reliable as strict filters. The `q` parameter must be in the URL query string, not the JSON body.

3. **Sort parameter is `sort_by`, not `sort`** — only `sort_by` is recognized. The legacy name `sort` is silently ignored. Valid values: `modified`, `created`, `size`, `name`, `relevance`.

4. **`sort_by=title` is broken** — returns `total: 0`. Use `name` instead.

5. **Sort direction parameter is `sort_dir`, not `order`** — only `sort_dir` (`asc`/`desc`) works. The default direction (when `sort_dir` is omitted) is **ascending**. For `sort_by=relevance`, `sort_dir` is ignored (always returns best match first).

6. **`highlight=content`** — wraps search term matches in `<qusion>...</qusion>` tags within the `content` snippet field. Use `highlight_limit` to control snippet length.

7. **`advanced_mode=1`** — activates image/OCR search mode. Dramatically reduces results to image files only (jpg, webp, bmp, png). Default `advanced_mode=0` is standard full-text search.

8. **Path resolution** — `item["path"]` is only the parent directory. The actual full file path is in `item["preview"]["info"]` where `key == "path"`.

9. **All file actions route through `/qusion-item`** — no separate download/preview endpoints. Action URLs are returned dynamically in each item's `actions` object.

10. **Session expiry** — returns HTTP 401 with `{"error": {"code": 101, ...}}`. This client automatically re-authenticates once and retries.

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
    "preview": "/qsirch/latest/api/qusion-item?action=preview&..."
  }
}
```

## Authentication Flow

This client uses the QTS CGI login method:

1. `POST /cgi-bin/authLogin.cgi` with Base64-encoded password
2. Parses XML response for `authSid`
3. Sets `NAS_SID` session cookie for all subsequent requests
4. On HTTP 401 (code 101), re-authenticates once and retries

## License

MIT
