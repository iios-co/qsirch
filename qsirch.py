#!/usr/bin/env python3
"""
Qsirch CLI — QNAP Qsirch 7 REST API Client

A command-line tool for searching emails, documents, and files indexed by
QNAP Qsirch 7. Supports full-text search, category filtering, email HTML
preview extraction, file download, and semantic similar-item discovery.

Authentication: QTS CGI login (POST /cgi-bin/authLogin.cgi) with Base64 password.
Session: NAS_SID cookie with automatic re-authentication on expiry.

Environment variables:
    QSIRCH_HOST  — NAS IP/hostname (default: 10.0.0.3)
    QSIRCH_PORT  — HTTP port (default: 8080)
    QSIRCH_USER  — Username (required if not passed via --user)
    QSIRCH_PASS  — Password (required if not passed via --pass)
    QSIRCH_SSL   — Set to "1" for HTTPS (default: 0)
"""

import sys
import os
import base64
import xml.etree.ElementTree as ET
import argparse
import json
from datetime import datetime
from typing import Dict, Any, Optional

try:
    import requests
except ImportError:
    print("[Error] 'requests' library required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)


class QsirchClient:
    """Qsirch 7 REST API client with session management and auto re-authentication."""

    def __init__(self, host: str, port: int = 8080, use_ssl: bool = False):
        protocol = "https" if use_ssl else "http"
        self.base_url = f"{protocol}://{host}:{port}"
        self.session = requests.Session()
        self.sid: Optional[str] = None
        self._username: Optional[str] = None
        self._password: Optional[str] = None

    def login(self, username: str, password: str) -> bool:
        """Authenticate via QTS CGI login. Stores credentials for re-auth."""
        self._username = username
        self._password = password
        return self._do_login()

    def _do_login(self) -> bool:
        """Perform the actual login request."""
        login_url = f"{self.base_url}/cgi-bin/authLogin.cgi"
        b64_password = base64.b64encode(self._password.encode("utf-8")).decode("utf-8")
        payload = {"user": self._username, "pwd": b64_password}

        try:
            resp = self.session.post(login_url, data=payload, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)

            if root.findtext("authPassed") == "1":
                self.sid = root.findtext("authSid")
                if self.sid:
                    self.session.cookies.set("NAS_SID", self.sid)
                    return True

            error_val = root.findtext("errorValue")
            print(f"[Error] Authentication failed for '{self._username}'. Code: {error_val}", file=sys.stderr)
            return False

        except requests.exceptions.ConnectionError:
            print(f"[Error] Cannot connect to {self.base_url}. Is the NAS reachable?", file=sys.stderr)
            return False
        except Exception as e:
            print(f"[Error] Login failed: {e}", file=sys.stderr)
            return False

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an authenticated request with auto re-auth on 401."""
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        timeout = kwargs.pop("timeout", 15)

        resp = self.session.request(method, url, timeout=timeout, **kwargs)

        # Handle expired session: re-auth once and retry
        if resp.status_code == 401:
            try:
                body = resp.json()
                if body.get("error", {}).get("code") == 101:
                    print("[Info] Session expired, re-authenticating...", file=sys.stderr)
                    if self._do_login():
                        resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except (ValueError, KeyError):
                pass

        return resp

    # ─── Search ───────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort_by: Optional[str] = None,
        sort_dir: str = "desc",
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search the Qsirch index.

        Uses GET for general search, POST with {"tools": category} for
        category-scoped search.

        IMPORTANT:
        - Server-side GET filter params (ext, type, category) are silently ignored.
          All extension/path/date filtering must be done client-side.
        - POST tools=Email is the only strictly reliable category filter.
          Other tools values return mixed file types.
        - Sort param is 'sort_by' (not 'sort'). Direction is 'sort_dir' (not 'order').
        - Default sort direction is ascending when sort_dir is omitted.

        Args:
            query: Search terms. Use '.' or ' ' for wildcard. '*' is unreliable.
            limit: Max results per page (practical max ~500).
            offset: Pagination offset.
            sort_by: relevance, modified, created, size, name. NOT 'title' (broken).
            sort_dir: 'desc' or 'asc'. Default server behavior is ascending.
            category: POST tools filter — only 'Email' is strictly reliable.
                      Other values (PDF, Documents, etc.) return mixed results.
        """
        params = {
            "q": query,
            "limit": limit,
            "offset": offset,
            "highlight": "content",
            "highlight_limit": "200",
        }

        if sort_by and sort_by != "relevance":
            params["sort_by"] = sort_by
            params["sort_dir"] = sort_dir

        try:
            if category and category.lower() != "all":
                resp = self._request(
                    "POST",
                    "/qsirch/latest/api/search",
                    params=params,
                    json={"tools": category, "limit": limit},
                )
            else:
                resp = self._request("GET", "/qsirch/latest/api/search", params=params)

            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            print(f"[Error] Search failed: {e}", file=sys.stderr)
            return {"total": 0, "items": []}

    # ─── Preview ──────────────────────────────────────────────────────────────

    def preview(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get preview for an item. For .eml files, returns full rendered HTML body.
        Uses the HATEOAS action URL from search results when available.
        """
        action_url = item.get("actions", {}).get("preview")
        if not action_url:
            path = self._resolve_path(item)
            name = item.get("name", "")
            action_url = (
                f"/qsirch/latest/api/qusion-item?"
                f"action=preview&path={path}&name={name}&app_id=badguy"
            )

        try:
            resp = self._request("GET", action_url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Error] Preview failed: {e}", file=sys.stderr)
            return {}

    # ─── Download ─────────────────────────────────────────────────────────────

    def download(self, item: Dict[str, Any], output_dir: str = ".") -> Optional[str]:
        """
        Download a file from the NAS. Returns the local file path on success.
        Uses the HATEOAS action URL from search results when available.
        """
        action_url = item.get("actions", {}).get("download")
        if not action_url:
            path = self._resolve_path(item)
            name = item.get("name", "")
            action_url = (
                f"/qsirch/latest/api/qusion-item?"
                f"action=download&path={path}&name={name}&app_id=badguy"
            )

        try:
            resp = self._request("GET", action_url, stream=True, timeout=60)
            resp.raise_for_status()

            ext = item.get("extension", "")
            filename = item.get("name", "download")
            if ext and not filename.endswith(f".{ext}"):
                filename = f"{filename}.{ext}"

            output_path = os.path.join(output_dir, filename)
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            return output_path

        except Exception as e:
            print(f"[Error] Download failed: {e}", file=sys.stderr)
            return None

    # ─── Status ───────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Check Qsirch indexing status and health."""
        try:
            resp = self._request("GET", "/qsirch/latest/api/status")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Error] Status check failed: {e}", file=sys.stderr)
            return {}

    # ─── More-Like-This ───────────────────────────────────────────────────────

    def similar(self, item_id: str, limit: int = 10, category: Optional[str] = None) -> Dict[str, Any]:
        """Find items similar to the given item ID (semantic/content similarity)."""
        params = {"limit": limit}
        if category:
            params["categories"] = category

        try:
            resp = self._request("GET", f"/qsirch/latest/api/more-like-this/{item_id}", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[Error] Similar search failed: {e}", file=sys.stderr)
            return {"total": 0, "items": []}

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_path(item: Dict[str, Any]) -> str:
        """
        Resolve the actual file path from an item.

        item['path'] is only the parent directory. The real full path is in
        item['preview']['info'] where key == 'path'.
        """
        preview_info = item.get("preview", {}).get("info", [])
        for entry in preview_info:
            if entry.get("key") == "path":
                return entry.get("value", item.get("path", ""))
        return item.get("path", "")

    @staticmethod
    def extract_metadata(item: Dict[str, Any]) -> Dict[str, str]:
        """Extract metadata key-values (from, to, subject, etc.) from an item."""
        meta = {}
        for m in item.get("metadata", {}).get("all", []):
            key = m.get("key")
            val = m.get("value")
            if key and val:
                meta[key] = val
        return meta


# ─── CLI Output Formatting ────────────────────────────────────────────────────


def format_item(item: Dict[str, Any], idx: int) -> str:
    """Format a single search result for human-readable display."""
    name = item.get("name", "unknown")
    ext = item.get("extension", "")
    category = ", ".join(item.get("category", []))
    size = item.get("size", 0)
    path = QsirchClient._resolve_path(item)
    meta = QsirchClient.extract_metadata(item)
    content = item.get("content", "")

    lines = [f"[{idx}] {name}.{ext}  ({category}, {size:,} bytes)"]
    lines.append(f"    Path: {path}")

    if meta.get("subject"):
        lines.append(f"    Subject: {meta['subject']}")
    if meta.get("from"):
        lines.append(f"    From: {meta['from']}")
    if meta.get("to"):
        lines.append(f"    To: {meta['to']}")

    other_meta = {k: v for k, v in meta.items() if k not in ("subject", "from", "to")}
    if other_meta:
        meta_str = ", ".join(f"{k}={v}" for k, v in other_meta.items())
        lines.append(f"    Meta: {meta_str[:150]}")

    if content:
        snippet = content.replace("\n", " ")[:200]
        lines.append(f"    Snippet: {snippet}")

    lines.append(f"    ID: {item.get('id', 'N/A')}")
    return "\n".join(lines)


def filter_items(
    items: list,
    ext: Optional[str] = None,
    path_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list:
    """
    Client-side filtering for search results.

    Required because server-side GET filter parameters (ext, type, category)
    are silently ignored by Qsirch 7 — they have no effect on results.
    """
    dt_from = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
    dt_to = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None

    filtered = []
    for item in items:
        # Extension filter
        if ext:
            item_ext = str(item.get("extension", "")).lower()
            if item_ext != ext.lower().lstrip("."):
                continue

        # Path substring filter
        if path_filter:
            item_path = QsirchClient._resolve_path(item)
            if path_filter.lower() not in item_path.lower():
                continue

        # Date range filter on modified timestamp
        if dt_from or dt_to:
            modified = item.get("modified", "")
            if modified:
                try:
                    if str(modified).isdigit():
                        item_dt = datetime.fromtimestamp(int(modified))
                    else:
                        item_dt = datetime.strptime(str(modified).split()[0], "%Y/%m/%d")

                    if dt_from and item_dt < dt_from:
                        continue
                    if dt_to and item_dt > dt_to:
                        continue
                except (ValueError, OSError):
                    pass

        filtered.append(item)

    return filtered


# ─── Subcommand Handlers ──────────────────────────────────────────────────────


def cmd_search(args, client: QsirchClient):
    """Handle the 'search' subcommand."""
    data = client.search(
        query=args.query,
        limit=args.limit,
        offset=args.offset,
        sort_by=args.sort,
        sort_dir=args.order,
        category=args.category,
    )

    total = data.get("total", 0)
    items = data.get("items", [])

    # Apply client-side filters
    items = filter_items(
        items,
        ext=args.ext,
        path_filter=args.path,
        date_from=args.from_date,
        date_to=args.to_date,
    )

    if args.json:
        output = {"total": total, "count": len(items), "offset": args.offset, "items": items}
        print(json.dumps(output, indent=2, default=str))
        return

    print(f"Query: '{args.query}' | Total indexed matches: {total} | Showing: {len(items)}")
    if args.category:
        print(f"Category filter: {args.category}")
    print("=" * 70)

    for idx, item in enumerate(items, 1):
        print(format_item(item, idx))
        print("-" * 70)


def cmd_preview(args, client: QsirchClient):
    """Handle the 'preview' subcommand."""
    if not (args.path and args.name):
        if args.id:
            item = {
                "actions": {
                    "preview": (
                        f"/qsirch/latest/api/qusion-item?"
                        f"action=preview&id={args.id}&app_id=badguy"
                    )
                }
            }
        else:
            print("[Error] Provide --path and --name, or --id", file=sys.stderr)
            sys.exit(1)
    else:
        from urllib.parse import quote

        item = {
            "actions": {
                "preview": (
                    f"/qsirch/latest/api/qusion-item?"
                    f"action=preview&path={quote(args.path)}&name={quote(args.name)}&app_id=badguy"
                )
            }
        }

    result = client.preview(item)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        container = result.get("container_type", "unknown")
        print(f"Container type: {container}")

        if container == "html-eml":
            html = result.get("html", "")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"Email HTML saved to: {args.output}")
            else:
                import re

                text = re.sub(r"<[^>]+>", "", html)
                text = re.sub(r"\s+", " ", text).strip()
                print(f"Email body ({len(html)} chars HTML):")
                print(text[:2000])
        else:
            print(json.dumps(result, indent=2, default=str))


def cmd_download(args, client: QsirchClient):
    """Handle the 'download' subcommand."""
    if not (args.path and args.name):
        print("[Error] --path and --name are required for download", file=sys.stderr)
        sys.exit(1)

    from urllib.parse import quote

    item = {
        "name": args.name,
        "extension": args.ext or "",
        "actions": {
            "download": (
                f"/qsirch/latest/api/qusion-item?"
                f"action=download&path={quote(args.path)}&name={quote(args.name)}&app_id=badguy"
            )
        },
    }

    output_dir = args.output or "."
    os.makedirs(output_dir, exist_ok=True)
    result = client.download(item, output_dir=output_dir)

    if result:
        print(f"Downloaded: {result}")
    else:
        print("[Error] Download failed", file=sys.stderr)
        sys.exit(1)


def cmd_status(args, client: QsirchClient):
    """Handle the 'status' subcommand."""
    data = client.status()

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        overview = data.get("overview", {})
        indexed = overview.get("indexed", "?")
        status = data.get("status", "unknown")
        health = data.get("health", "?")
        print(f"Qsirch Status: {status}")
        print(f"Indexed files: {indexed:,}" if isinstance(indexed, int) else f"Indexed files: {indexed}")
        print(f"Health: {health}")
        if status == "indexing":
            print("[Warning] Index is currently rebuilding — results may be incomplete.")


def cmd_similar(args, client: QsirchClient):
    """Handle the 'similar' subcommand."""
    if not args.id:
        print("[Error] --id is required", file=sys.stderr)
        sys.exit(1)

    data = client.similar(args.id, limit=args.limit, category=args.category)
    items = data.get("items", [])

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(f"Items similar to ID: {args.id} | Found: {len(items)}")
        print("=" * 70)
        for idx, item in enumerate(items, 1):
            print(format_item(item, idx))
            print("-" * 70)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Qsirch CLI — QNAP Qsirch 7 REST API Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables:\n"
            "  QSIRCH_HOST  NAS IP/hostname (default: 10.0.0.3)\n"
            "  QSIRCH_PORT  HTTP port (default: 8080)\n"
            "  QSIRCH_USER  Username\n"
            "  QSIRCH_PASS  Password\n"
            "  QSIRCH_SSL   Set to '1' for HTTPS\n"
        ),
    )

    # Global connection arguments (env var fallback)
    parser.add_argument("--host", default=os.environ.get("QSIRCH_HOST", "10.0.0.3"), help="NAS IP/hostname")
    parser.add_argument("--port", type=int, default=int(os.environ.get("QSIRCH_PORT", "8080")), help="Port")
    parser.add_argument("--user", default=os.environ.get("QSIRCH_USER"), help="Username")
    parser.add_argument("--pass", dest="password", default=os.environ.get("QSIRCH_PASS"), help="Password")
    parser.add_argument("--ssl", action="store_true", default=os.environ.get("QSIRCH_SSL", "0") == "1", help="HTTPS")

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # ─── search ───────────────────────────────────────────────────────────────
    sp_search = subparsers.add_parser("search", help="Full-text search")
    sp_search.add_argument("--query", "-q", required=True, help="Search query (use '.' for wildcard)")
    sp_search.add_argument(
        "--ext", help="Client-side extension filter (eml, pdf, doc, xlsx, csv)"
    )
    sp_search.add_argument(
        "--category",
        choices=["Email", "PDF", "Documents", "Images", "Videos", "Music", "Excel", "Word"],
        help="Server-side category filter (POST search)",
    )
    sp_search.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")
    sp_search.add_argument("--offset", type=int, default=0, help="Pagination offset")
    sp_search.add_argument(
        "--sort",
        choices=["relevance", "modified", "created", "size", "name"],
        help="Sort field (NOT 'title' — broken server-side)",
    )
    sp_search.add_argument("--order", choices=["asc", "desc"], default="desc", help="Sort direction")
    sp_search.add_argument("--path", help="Client-side path substring filter")
    sp_search.add_argument("--from-date", help="Client-side date filter from (YYYY-MM-DD)")
    sp_search.add_argument("--to-date", help="Client-side date filter to (YYYY-MM-DD)")
    sp_search.add_argument("--json", action="store_true", help="Output JSON")

    # ─── preview ──────────────────────────────────────────────────────────────
    sp_preview = subparsers.add_parser("preview", help="Get file preview / email HTML body")
    sp_preview.add_argument("--id", help="Item ID from search results")
    sp_preview.add_argument("--path", help="File path on NAS")
    sp_preview.add_argument("--name", help="Filename (with extension)")
    sp_preview.add_argument("--output", "-o", help="Save output to file")
    sp_preview.add_argument("--json", action="store_true", help="Output JSON")

    # ─── download ─────────────────────────────────────────────────────────────
    sp_download = subparsers.add_parser("download", help="Download a file from NAS")
    sp_download.add_argument("--path", required=True, help="File path on NAS")
    sp_download.add_argument("--name", required=True, help="Filename (with extension)")
    sp_download.add_argument("--ext", help="File extension (for naming)")
    sp_download.add_argument("--output", "-o", help="Output directory (default: current)")

    # ─── status ───────────────────────────────────────────────────────────────
    sp_status = subparsers.add_parser("status", help="Check Qsirch index status & health")
    sp_status.add_argument("--json", action="store_true", help="Output JSON")

    # ─── similar ──────────────────────────────────────────────────────────────
    sp_similar = subparsers.add_parser("similar", help="Find similar items (more-like-this)")
    sp_similar.add_argument("--id", required=True, help="Item ID to find similar items for")
    sp_similar.add_argument("--limit", type=int, default=10, help="Max results")
    sp_similar.add_argument("--category", help="Filter by category (Email, PDF, etc.)")
    sp_similar.add_argument("--json", action="store_true", help="Output JSON")

    # Backward compatibility: if no subcommand but -q is present, assume "search"
    subcommands = {"search", "preview", "download", "status", "similar"}
    if not any(arg in subcommands for arg in sys.argv[1:]):
        if "-q" in sys.argv or "--query" in sys.argv:
            sys.argv.insert(1, "search")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Validate credentials
    if not args.user or not args.password:
        print(
            "[Error] Credentials required. Set QSIRCH_USER/QSIRCH_PASS environment "
            "variables or pass --user/--pass flags.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Connect
    client = QsirchClient(host=args.host, port=args.port, use_ssl=args.ssl)
    if not client.login(args.user, args.password):
        sys.exit(1)

    # Dispatch
    handlers = {
        "search": cmd_search,
        "preview": cmd_preview,
        "download": cmd_download,
        "status": cmd_status,
        "similar": cmd_similar,
    }
    handlers[args.command](args, client)


if __name__ == "__main__":
    main()
