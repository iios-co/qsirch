#!/usr/bin/env python3
"""
Qsirch CLI — QNAP Qsirch 7 REST API Client (agent-oriented)

A command-line tool for searching emails, documents, and files indexed by
QNAP Qsirch 7. Designed for AI agents: stable exit codes, compact JSON,
one-shot content retrieval, session reuse across invocations, and automatic
recovery from the API's loading windows.

Query syntax (in q= parameter):
    "exact phrase"     — Match exact phrase
    term1 OR term2     — Boolean OR
    term1 AND term2    — Boolean AND (stricter than default)
    term1 NOT term2    — Exclude term2
    term1 -term2       — Exclude term2 (short form)
    (term1, OR term2)  — Grouping with parentheses
    .                  — Wildcard, match all indexed files ('*' returns 0)

Search modes (advanced_mode):
    0 — Standard full-text search (default)
    1 — Image OCR search (searches text within images only)
    2 — Combined text + image search

Environment variables:
    QSIRCH_HOST  — NAS IP/hostname (default: 10.0.0.3)
    QSIRCH_PORT  — HTTP port (default: 8080)
    QSIRCH_USER  — Username (required if not passed via --user)
    QSIRCH_PASS  — Password (required if not passed via --pass)
    QSIRCH_SSL   — Set to "1" for HTTPS (default: 0)

Session reuse: successful logins cache the NAS session id (never the
password) in ~/.cache/qsirch/session.json (mode 0600), so consecutive
invocations skip the authLogin.cgi round-trip. Set QSIRCH_NO_CACHE=1 to
disable.

Exit codes: 0 success, 2 authentication failure, 3 API/transport failure.
"""

import sys
import os
import re
import json
import time
import stat
import base64
import xml.etree.ElementTree as ET
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("[Error] 'requests' library required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)


# ─── Errors ───────────────────────────────────────────────────────────────────


class QsirchError(Exception):
    """Base error. Carries a process exit code for machine-readable handling."""

    exit_code = 3


class AuthError(QsirchError):
    exit_code = 2


class APIError(QsirchError):
    pass


# The API returns an empty body (no 'total' key) instead of an error when
# 'limit' exceeds the server ceiling. Verified live: 1000 works, 1200+ yields
# {"items": []} with no total.
MAX_RESULT_LIMIT = 1000

# 'highlight=content' wraps matches in <qusion> tags but only works reliably
# at the default snippet length; highlight_limit above ~500 returns an EMPTY
# content field (server bug, verified live). Snippets stay 500 chars.
MAX_HIGHLIGHT_LIMIT = 500

# The search backend periodically reports "Qsirch service loading" (HTTP 503,
# error code 400) during index maintenance. It self-heals quickly (observed
# ~10 s), so requests are retried with backoff instead of failing.
SERVICE_LOADING_MESSAGE = "Qsirch service loading"
LOADING_RETRIES = 6
LOADING_BACKOFF = (2, 3, 5, 8, 13, 20)

# Fields on raw search items that carry large HATEOAS URLs or per-render
# noise. Compact JSON drops them; 'capabilities' summarizes what is usable.
_RAW_ITEM_DROP_FIELDS = ("actions", "icon", "alt_thumbnail", "open_to", "open_to_default")


# ─── Session cache ────────────────────────────────────────────────────────────


class SessionCache:
    """Cache NAS session ids (never passwords) for reuse across invocations.

    One JSON file at ~/.cache/qsirch/session.json, mode 0600, keyed by
    host:port:user. A stale sid simply 401s and the client re-logs in, so the
    cache is always safe to trust optimistically.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path.home() / ".cache" / "qsirch" / "session.json"

    def _key(self, host: str, port: int, user: str) -> str:
        return f"{host}:{port}:{user}"

    def get(self, host: str, port: int, user: str) -> Optional[str]:
        if os.environ.get("QSIRCH_NO_CACHE"):
            return None
        try:
            data = json.loads(self.path.read_text())
            return data.get(self._key(host, port, user), {}).get("sid")
        except (OSError, ValueError):
            return None

    def put(self, host: str, port: int, user: str, sid: str) -> None:
        if os.environ.get("QSIRCH_NO_CACHE") or not sid:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass
            data[self._key(host, port, user)] = {
                "sid": sid,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self.path.write_text(json.dumps(data))
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # caching must never break the client

    def drop(self, host: str, port: int, user: str) -> None:
        try:
            data = json.loads(self.path.read_text())
            data.pop(self._key(host, port, user), None)
            self.path.write_text(json.dumps(data))
        except (OSError, ValueError):
            pass


# ─── Client ───────────────────────────────────────────────────────────────────


class QsirchClient:
    """Qsirch 7 REST API client with session management and auto re-authentication."""

    def __init__(self, host: str, port: int = 8080, use_ssl: bool = False,
                 timeout: int = 15, cache: Optional[SessionCache] = None):
        protocol = "https" if use_ssl else "http"
        self.base_url = f"{protocol}://{host}:{port}"
        self.host = host
        self.port = port
        self.session = requests.Session()
        self.timeout = timeout
        self.cache = cache if cache is not None else SessionCache()
        self.sid: Optional[str] = None
        self._username: Optional[str] = None
        self._password: Optional[str] = None

    # -- authentication ------------------------------------------------------

    def use_cached_session(self, username: str, password: Optional[str] = None) -> bool:
        """Adopt a cached session id without logging in.

        Returns True when a cached sid was installed. Validity is not checked
        here: a stale sid 401s on first use and _request() transparently
        re-authenticates (using `password`, when available), which keeps the
        common case at zero auth overhead.
        """
        self._username = username
        self._password = password
        sid = self.cache.get(self.host, self.port, username)
        if sid:
            self.sid = sid
            self.session.cookies.set("NAS_SID", sid)
            return True
        return False

    def login(self, username: str, password: str) -> bool:
        """Authenticate via QTS CGI login. Stores credentials for re-auth."""
        self._username = username
        self._password = password
        if not self._do_login():
            raise AuthError(f"authentication failed for '{username}'")
        return True

    def _do_login(self) -> bool:
        """Perform the actual login request."""
        if not self._username or not self._password:
            return False
        login_url = f"{self.base_url}/cgi-bin/authLogin.cgi"
        b64_password = base64.b64encode(self._password.encode("utf-8")).decode("utf-8")
        payload = {"user": self._username, "pwd": b64_password}

        try:
            resp = self.session.post(login_url, data=payload, timeout=self.timeout)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)

            if root.findtext("authPassed") == "1":
                self.sid = root.findtext("authSid")
                if self.sid:
                    self.session.cookies.set("NAS_SID", self.sid)
                    self.cache.put(self.host, self.port, self._username, self.sid)
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

    # -- transport -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an authenticated request.

        Handles the two failure shapes of this API:
        - Session expiry: HTTP 401 as JSON {"error": {"code": 101}} or bare.
          Re-authenticates once and retries.
        - Service loading: HTTP 503 with "Qsirch service loading, please
          wait..". The backend self-heals in seconds; retries with backoff.
        """
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        timeout = kwargs.pop("timeout", self.timeout)

        for attempt in range(LOADING_RETRIES + 1):
            resp = self.session.request(method, url, timeout=timeout, **kwargs)

            if resp.status_code == 401:
                expired = False
                try:
                    body = resp.json()
                    expired = body.get("error", {}).get("code") == 101
                except ValueError:
                    expired = True  # non-JSON 401: treat as session expiry too
                if expired and self._do_login():
                    self.cache.drop(self.host, self.port, self._username or "")
                    continue  # retry immediately with the fresh session
                return resp

            if resp.status_code == 503 and SERVICE_LOADING_MESSAGE in resp.text:
                if attempt < LOADING_RETRIES:
                    wait = LOADING_BACKOFF[min(attempt, len(LOADING_BACKOFF) - 1)]
                    print(f"[Info] Qsirch service loading; retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
            return resp

        return resp

    def _get_json(self, path: str, params: Optional[Dict] = None, what: str = "request") -> Dict[str, Any]:
        """GET expecting JSON. Raises APIError with the server's detail."""
        resp = self._request("GET", path, params=params)
        if resp.status_code != 200:
            raise APIError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise APIError(f"{what} returned non-JSON: {resp.text[:200]}")

    # ─── Search ───────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort_by: Optional[str] = None,
        sort_dir: str = "desc",
        category: Optional[str] = None,
        advanced_mode: int = 0,
        highlight: bool = False,
    ) -> Dict[str, Any]:
        """
        Search the Qsirch index (one page).

        IMPORTANT:
        - GET filter params (ext, type, category, q.category, q.modified,
          q.path, q.name, q.string) are all silently ignored by the API.
        - Category filtering is a search expression appended to the query:
          `category:Email` etc. (verified live; works in GET q=). The POST
          body `tools` member is NOT a structured filter: the server
          concatenates it onto the query string, so {"tools": "Email"} is
          just the extra word "Email".
        - Sort param is 'sort_by' (not 'sort'); direction 'sort_dir'.
        - limit is clamped to MAX_RESULT_LIMIT (1000): above it the API
          returns an empty body without 'total' instead of an error.
        - store_history=0 keeps CLI searches out of the web client history.

        Args:
            query: Search terms with optional query syntax ('.' = match all).
            limit: Max results for this page (server ceiling 1000).
            offset: Pagination offset.
            sort_by: relevance, modified, created, size, name. NOT 'title'.
            sort_dir: 'desc' or 'asc'. Default server behavior is ascending.
            category: Qsirch category expression (Email, Documents, PDF,
                Excel, Word, Images, Music, Videos — whichever the index
                defines; discover via GET /api/search/tools?filter_syntax=true).
            advanced_mode: 0=text (default), 1=image OCR, 2=combined.
            highlight: wrap matches in <qusion> tags (500-char snippets).
        """
        if limit > MAX_RESULT_LIMIT:
            print(f"[Warn] limit clamped from {limit} to {MAX_RESULT_LIMIT} (server ceiling)", file=sys.stderr)
            limit = MAX_RESULT_LIMIT

        if category and category.lower() != "all":
            query = f"{query} category:{category.strip()}"

        params: Dict[str, Any] = {
            "q": query,
            "limit": limit,
            "offset": offset,
            "advanced_mode": str(advanced_mode),
            # API consumers should not create entries in a user's NAS history.
            "store_history": 0,
        }
        if highlight:
            params["highlight"] = "content"

        if sort_by and sort_by != "relevance":
            params["sort_by"] = sort_by
            params["sort_dir"] = sort_dir

        resp = self._request("GET", "/qsirch/latest/api/search", params=params)

        if resp.status_code != 200:
            raise APIError(f"search failed: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError:
            raise APIError(f"search returned non-JSON: {resp.text[:200]}")

        if "total" not in data:
            raise APIError("search returned no result set (empty body); query or parameters rejected")
        return data

    # ─── Async search ─────────────────────────────────────────────────────────

    def async_search(self, query: str, limit: int = 50, category: Optional[str] = None) -> Dict[str, Any]:
        """
        Two-phase search. Phase 1 returns the total match count and a result
        URL immediately; phase 2 fetches the items from that URL. The result
        window is fixed at submission: limit/offset on the result URL are
        ignored (verified live), so request the window you want up front.
        """
        if category:
            query = f"{query} category:{category.strip()}"
        params: Dict[str, Any] = {"q": query, "limit": min(limit, MAX_RESULT_LIMIT)}
        data = self._get_json("/qsirch/latest/api/async-search", params, "async-search submit")
        context = data.get("context", {})
        result_url = context.get("url")
        if not result_url:
            raise APIError(f"async-search returned no result URL: {json.dumps(data)[:200]}")
        data["items"] = self._get_json(result_url, None, "async-search fetch").get("items", [])
        return data

    # ─── Suggest ──────────────────────────────────────────────────────────────

    def suggest(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Autocomplete suggestions for a partial query (name/kind/category/...)."""
        return self._get_json(
            "/qsirch/latest/api/suggest",
            {"q": query, "limit": limit},
            "suggest",
        )

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
                f"action=preview&path={quote(path)}&name={quote(name)}&app_id=badguy"
            )

        return self._get_json(action_url, None, "preview")

    # ─── Text detection (OCR blocks) ──────────────────────────────────────────

    def text_detect(self, item: Dict[str, Any], lang: str = "ENG") -> Dict[str, Any]:
        """
        Server-side OCR text detection with bounding boxes (PDFs and images).
        Each block has 'text', 'vertices' (four corner points), 'score' (0-1).
        """
        action_url = item.get("actions", {}).get("text_detect")
        if not action_url:
            path = self._resolve_path(item)
            name = item.get("name", "")
            action_url = (
                f"/qsirch/latest/api/preview/text-detection?"
                f"path={quote(path)}&name={quote(name)}&lang={lang}&app_id=badguy"
            )
        return self._get_json(action_url, None, "text-detection")

    # ─── Read (one-shot content retrieval) ────────────────────────────────────

    def read_content(self, item: Dict[str, Any], lang: str = "ENG") -> Dict[str, Any]:
        """
        One-shot text content retrieval for an agent.

        Strategy by type:
        - .eml  -> preview -> HTML body -> text (source: 'email-preview')
        - pdf/image with text_detect -> OCR blocks joined (source: 'ocr')
        - anything else -> preview metadata summary (source: 'metadata')

        Returns {full_path, kind, source, text, blocks?}.
        """
        ext = str(item.get("extension", "")).lower()
        full_path = self._resolve_path(item)

        if ext == "eml":
            preview = self.preview(item)
            container = preview.get("container_type", "")
            if container == "html-eml":
                return {
                    "full_path": full_path,
                    "kind": ext,
                    "source": "email-preview",
                    "text": html_to_text(preview.get("html", "")),
                }
            # Non-renderable mail (e.g. backups): fall back to whatever info exists.
            return {
                "full_path": full_path,
                "kind": ext,
                "source": "metadata",
                "text": json.dumps(preview.get("container_info", {}), default=str)[:2000],
            }

        if "text_detect" in item.get("actions", {}) or ext in ("pdf", "jpg", "jpeg", "png", "bmp", "webp", "tiff"):
            try:
                data = self.text_detect(item, lang=lang)
                blocks = data.get("blocks", [])
                return {
                    "full_path": full_path,
                    "kind": ext,
                    "source": "ocr",
                    "text": "\n".join(b.get("text", "") for b in blocks),
                    "blocks": len(blocks),
                }
            except QsirchError:
                pass

        try:
            preview = self.preview(item)
            return {
                "full_path": full_path,
                "kind": ext,
                "source": "metadata",
                "text": json.dumps(preview.get("container_info", preview), default=str)[:2000],
            }
        except QsirchError:
            raise APIError(f"no content path available for {full_path}")

    # ─── Download ─────────────────────────────────────────────────────────────

    def download(self, item: Dict[str, Any], output_dir: str = ".", timeout: int = 120) -> Optional[str]:
        """Download a file from the NAS. Returns the local file path."""
        action_url = item.get("actions", {}).get("download")
        if not action_url:
            path = self._resolve_path(item)
            name = item.get("name", "")
            action_url = (
                f"/qsirch/latest/api/qusion-item?"
                f"action=download&path={quote(path)}&name={quote(name)}&app_id=badguy"
            )

        url = f"{self.base_url}{action_url}" if action_url.startswith("/") else action_url
        resp = self.session.get(url, stream=True, timeout=timeout)
        if resp.status_code == 401 and self._do_login():
            resp = self.session.get(url, stream=True, timeout=timeout)
        if resp.status_code != 200:
            raise APIError(f"download failed: HTTP {resp.status_code}: {resp.text[:200]}")

        ext = item.get("extension", "")
        filename = item.get("name", "download")
        if ext and not filename.endswith(f".{ext}"):
            filename = f"{filename}.{ext}"

        output_path = os.path.join(output_dir, filename)
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        return output_path

    # ─── Status ───────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Check Qsirch indexing status and health."""
        return self._get_json("/qsirch/latest/api/status", None, "status")

    def status_brief(self) -> Dict[str, Any]:
        """Lightweight status: license, health, index state."""
        return self._get_json("/qsirch/latest/api/status/brief", None, "status/brief")

    def about(self) -> Dict[str, Any]:
        """Qsirch application version info."""
        return self._get_json("/qsirch/latest/api/about", None, "about")

    def system_settings(self) -> Dict[str, Any]:
        """Feature flags: image OCR extraction, qumagie core, online viewer, ..."""
        return self._get_json("/qsirch/latest/api/setting/system", None, "setting/system")

    # ─── More-Like-This ───────────────────────────────────────────────────────

    def similar(self, item_id: str, limit: int = 10, category: Optional[str] = None) -> Dict[str, Any]:
        """Find items similar to the given item ID (semantic/content similarity)."""
        params: Dict[str, Any] = {"limit": limit}
        if category:
            params["categories"] = category
        return self._get_json(f"/qsirch/latest/api/more-like-this/{item_id}", params, "more-like-this")

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
        """Extract metadata key-values (from, to, subject, author, ...) from an item."""
        meta = {}
        for m in item.get("metadata", {}).get("all", []):
            key = m.get("key")
            val = m.get("value")
            if key and val:
                meta[key] = val
        return meta

    def search_all(
        self,
        query: str,
        limit: int = 50,
        category: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_dir: str = "desc",
        advanced_mode: int = 0,
        predicate=None,
    ) -> tuple:
        """Fetch up to `limit` results (post-filter) across pages.

        `predicate`, when given, is applied per page so pagination keeps
        going until enough matching items are collected or the query is
        exhausted. Returns (items, total_reported).
        """
        collected: List[Dict[str, Any]] = []
        offset = 0
        total = 0
        page = MAX_RESULT_LIMIT
        while len(collected) < limit:
            data = self.search(
                query, limit=page, offset=offset, sort_by=sort_by,
                sort_dir=sort_dir, category=category, advanced_mode=advanced_mode,
            )
            total = data.get("total", 0)
            items = data.get("items", [])
            if not items:
                break
            offset += len(items)
            if predicate is not None:
                items = [i for i in items if predicate(i)]
            collected.extend(items)
            if offset >= total:
                break
        return collected[:limit], total
    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Agent-friendly augmentation of a search item, computed client-side:

        - full_path: the resolved absolute NAS path
        - modified_iso: normalized UTC-aware timestamp when parseable
        - capabilities: which item actions are available
        """
        item = dict(item)
        item["full_path"] = QsirchClient._resolve_path(item)

        modified = item.get("modified")
        parsed = parse_modified(modified)
        if parsed:
            item["modified_iso"] = parsed.isoformat()

        actions = item.get("actions", {})
        item["capabilities"] = sorted(
            k for k in ("download", "preview", "text_detect", "mlt", "thumbnail") if k in actions
        )
        return item

    @staticmethod
    def compact(item: Dict[str, Any]) -> Dict[str, Any]:
        """Drop raw HATEOAS/URL fields for token-efficient JSON output.

        'capabilities' (from enrich) already tells a caller which actions
        exist; the full action URLs remain available via --json-full.
        """
        return {k: v for k, v in item.items() if k not in _RAW_ITEM_DROP_FIELDS}


def parse_modified(value: Any) -> Optional[datetime]:
    """
    Normalize the 'modified' field into an aware datetime.

    Observed formats (all verified against the live API):
      - epoch seconds as int/numeric string ("1754460005")
      - ISO-8601 with Z ("2026-08-06T06:00:05.739939Z")
      - "YYYY/MM/DD HH:MM:SS" (preview.info entries)
    Returns None when unparseable rather than guessing.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        if text.endswith("Z"):
            return datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(text)
    except (ValueError, OSError):
        pass
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


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
        snippet = re.sub(r"</?qusion>", "", content).replace("\n", " ")[:200]
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
    dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date_from else None
    dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc) if date_to else None

    filtered = []
    for item in items:
        if ext:
            item_ext = str(item.get("extension", "")).lower()
            if item_ext != ext.lower().lstrip("."):
                continue

        if path_filter:
            item_path = QsirchClient._resolve_path(item)
            if path_filter.lower() not in item_path.lower():
                continue

        if dt_from or dt_to:
            item_dt = parse_modified(item.get("modified"))
            if item_dt is None:
                # Unparseable timestamp: keep the item rather than silently
                # dropping data the filter cannot judge.
                filtered.append(item)
                continue
            if dt_from and item_dt < dt_from:
                continue
            if dt_to and item_dt > dt_to:
                continue

        filtered.append(item)

    return filtered


def html_to_text(html: str) -> str:
    """Strip HTML to text for terminal display (scripts/styles removed first)."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


# ─── Subcommand Handlers ──────────────────────────────────────────────────────


def _emit_items_json(items: List[Dict[str, Any]], total: int, offset: int, full: bool, extra: Optional[Dict] = None) -> None:
    """Shared JSON emitter: enrich, optionally compact, annotate counts."""
    processed = [QsirchClient.enrich(i) for i in items]
    if not full:
        processed = [QsirchClient.compact(i) for i in processed]
    output: Dict[str, Any] = {
        "total": total,
        "count": len(processed),
        "offset": offset,
        "items": processed,
    }
    if extra:
        output.update(extra)
    print(json.dumps(output, indent=2, default=str))


def cmd_search(args, client: QsirchClient):
    """Handle the 'search' subcommand."""
    if getattr(args, "all_pages", False):
        # Filter during pagination so sparse categories still reach --limit.
        def _keep(item):
            return bool(filter_items([item], ext=args.ext, path_filter=args.path,
                                     date_from=args.from_date, date_to=args.to_date))

        items, total = client.search_all(
            args.query, limit=args.limit, category=args.category,
            sort_by=args.sort, sort_dir=args.order, advanced_mode=args.mode,
            predicate=_keep,
        )
        raw_count = len(items)  # filtering already applied during pagination
        if args.json or args.json_full:
            _emit_items_json(items, total, 0, full=args.json_full)
            return
    else:
        data = client.search(
            query=args.query,
            limit=args.limit,
            offset=args.offset,
            sort_by=args.sort,
            sort_dir=args.order,
            category=args.category,
            advanced_mode=args.mode,
            highlight=args.highlight,
        )
        total = data.get("total", 0)
        items = data.get("items", [])
        raw_count = len(items)
        items = filter_items(items, ext=args.ext, path_filter=args.path,
                             date_from=args.from_date, date_to=args.to_date)
        if args.json or args.json_full:
            _emit_items_json(items, total, args.offset, full=args.json_full,
                             extra={"filtered_out": raw_count - len(items)})
            return

    shown_note = f" (filtered {raw_count - len(items)} of {raw_count} client-side)" if raw_count != len(items) else ""
    print(f"Query: '{args.query}' | Total indexed matches: {total} | Showing: {len(items)}{shown_note}")
    if args.category:
        print(f"Category filter: {args.category}")
    print("=" * 70)

    for idx, item in enumerate(items, 1):
        print(format_item(item, idx))
        print("-" * 70)


def cmd_read(args, client: QsirchClient):
    """Handle the 'read' subcommand (one-shot content retrieval)."""
    if args.path and args.name:
        item = {"name": args.name, "extension": args.name.rsplit(".", 1)[-1].lower() if "." in args.name else "",
                "path": args.path}
    elif args.id:
        # Resolve the id via a bounded scan of the wildcard index.
        item = None
        offset = 0
        while offset < 5000:
            data = client.search(".", limit=MAX_RESULT_LIMIT, offset=offset)
            for candidate in data.get("items", []):
                if candidate.get("id") == args.id:
                    item = candidate
                    break
            if item or not data.get("items"):
                break
            offset += MAX_RESULT_LIMIT
        if item is None:
            raise APIError(f"item id '{args.id}' not found (searched {offset} index entries)")
    elif args.query:
        results, _ = client.search_all(args.query, limit=1, category=args.category)
        if not results:
            raise APIError(f"no results for query '{args.query}'")
        item = results[0]
    else:
        print("[Error] Provide --query, --id, or --path+--name", file=sys.stderr)
        sys.exit(1)

    content = client.read_content(item, lang=args.lang)
    if args.json:
        print(json.dumps(content, indent=2, default=str))
    else:
        print(content.get("text", ""))


def cmd_async_search(args, client: QsirchClient):
    """Handle the 'async-search' subcommand."""
    data = client.async_search(args.query, limit=args.limit, category=args.category)
    items = data.get("items", [])

    if args.json or args.json_full:
        _emit_items_json(items, data.get("total", 0), 0, full=args.json_full)
        return

    print(f"Query: '{args.query}' | Total indexed matches: {data.get('total', 0)} | Showing: {len(items)}")
    print("=" * 70)
    for idx, item in enumerate(items, 1):
        print(format_item(item, idx))
        print("-" * 70)


def cmd_suggest(args, client: QsirchClient):
    """Handle the 'suggest' subcommand."""
    data = client.suggest(args.query, limit=args.limit)
    groups = data.get("items", [])

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return

    shown = False
    for group in groups:
        values = group.get("values", [])
        if not values:
            continue
        shown = True
        print(f"[{group.get('key', '?')}]")
        for v in values:
            print(f"  {v.get('display', v.get('key', ''))}")
    if not shown:
        print("No suggestions.")


def cmd_preview(args, client: QsirchClient):
    """Handle the 'preview' subcommand."""
    if not (args.path and args.name):
        if args.id:
            # NOTE: the server returns 500 for id-only preview (verified live);
            # this path is kept for API versions that may support it.
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
        item = {
            "name": args.name,
            "actions": {
                "preview": (
                    f"/qsirch/latest/api/qusion-item?"
                    f"action=preview&path={quote(args.path)}&name={quote(args.name)}&app_id=badguy"
                )
            },
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
                text = html_to_text(html)
                print(f"Email body ({len(html)} chars HTML):")
                print(text[:2000])
        else:
            print(json.dumps(result, indent=2, default=str))


def cmd_detect(args, client: QsirchClient):
    """Handle the 'detect' subcommand (OCR text blocks with coordinates)."""
    if not (args.path and args.name):
        print("[Error] --path and --name are required for detect", file=sys.stderr)
        sys.exit(1)

    item = {
        "name": args.name,
        "actions": {
            "text_detect": (
                f"/qsirch/latest/api/preview/text-detection?"
                f"path={quote(args.path)}&name={quote(args.name)}&lang={args.lang}&app_id=badguy"
            )
        },
    }
    data = client.text_detect(item, lang=args.lang)
    blocks = data.get("blocks", [])

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return

    if args.text:
        for b in blocks:
            print(b.get("text", ""))
        return

    print(f"Detected {len(blocks)} text block(s):")
    for i, b in enumerate(blocks, 1):
        score = b.get("score", 0)
        print(f"[{i}] ({score:.2f}) {b.get('text', '')}")


def cmd_download(args, client: QsirchClient):
    """Handle the 'download' subcommand."""
    if not (args.path and args.name):
        print("[Error] --path and --name are required for download", file=sys.stderr)
        sys.exit(1)

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
        try:
            data["brief"] = client.status_brief()
        except QsirchError:
            pass
        try:
            data["about"] = client.about()
        except QsirchError:
            pass
        print(json.dumps(data, indent=2, default=str))
        return

    overview = data.get("overview", {})
    indexed = overview.get("indexed", "?")
    status = data.get("status", "unknown")
    health = data.get("health", "?")
    print(f"Qsirch Status: {status}")
    print(f"Indexed files: {indexed:,}" if isinstance(indexed, int) else f"Indexed files: {indexed}")
    print(f"Health: {health}")
    try:
        about = client.about().get("overview", {})
        if about.get("version"):
            print(f"Version: {about['version']}")
    except QsirchError:
        pass
    if status == "indexing":
        print("[Warning] Index is currently rebuilding — results may be temporarily incomplete.")
        print("[Info] Search requests during a rebuild are retried automatically ('service loading').")


def cmd_similar(args, client: QsirchClient):
    """Handle the 'similar' subcommand."""
    if not args.id:
        print("[Error] --id is required", file=sys.stderr)
        sys.exit(1)

    data = client.similar(args.id, limit=args.limit, category=args.category)
    items = data.get("items", [])

    if args.json or args.json_full:
        _emit_items_json(items, data.get("total", len(items)), 0, full=args.json_full)
    else:
        print(f"Items similar to ID: {args.id} | Found: {len(items)}")
        print("=" * 70)
        for idx, item in enumerate(items, 1):
            print(format_item(item, idx))
            print("-" * 70)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Qsirch CLI — QNAP Qsirch 7 REST API Client (agent-oriented)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables:\n"
            "  QSIRCH_HOST      NAS IP/hostname (default: 10.0.0.3)\n"
            "  QSIRCH_PORT      HTTP port (default: 8080)\n"
            "  QSIRCH_USER      Username\n"
            "  QSIRCH_PASS      Password\n"
            "  QSIRCH_SSL       Set to '1' for HTTPS\n"
            "  QSIRCH_NO_CACHE  Set to '1' to disable session caching\n"
            "\nExit codes: 0 success, 2 authentication failure, 3 API/transport failure.\n"
        ),
    )

    parser.add_argument("--host", default=os.environ.get("QSIRCH_HOST", "10.0.0.3"), help="NAS IP/hostname")
    parser.add_argument("--port", type=int, default=int(os.environ.get("QSIRCH_PORT", "8080")), help="Port")
    parser.add_argument("--user", default=os.environ.get("QSIRCH_USER"), help="Username")
    parser.add_argument("--pass", dest="password", default=os.environ.get("QSIRCH_PASS"), help="Password")
    parser.add_argument("--ssl", action="store_true", default=os.environ.get("QSIRCH_SSL", "0") == "1", help="HTTPS")
    parser.add_argument("--timeout", type=int, default=15, help="Request timeout seconds (default: 15)")

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # ─── search ───────────────────────────────────────────────────────────────
    sp_search = subparsers.add_parser("search", help="Full-text search")
    sp_search.add_argument(
        "--query", "-q", required=True,
        help="Search query: \"exact phrase\", OR, AND, NOT, -exclude, (group), '.' wildcard",
    )
    sp_search.add_argument("--ext", help="Client-side extension filter (eml, pdf, doc, xlsx, csv)")
    sp_search.add_argument(
        "--category",
        choices=["Email", "PDF", "Excel", "Word", "Documents", "Images", "Music", "Videos"],
        help="Server-side category expression (category:<Name>); use --ext for exact file types",
    )
    sp_search.add_argument("--limit", type=int, default=50, help=f"Max results (default: 50, ceiling per page: {MAX_RESULT_LIMIT})")
    sp_search.add_argument("--offset", type=int, default=0, help="Pagination offset")
    sp_search.add_argument("--all-pages", action="store_true", help="Auto-paginate until --limit items collected")
    sp_search.add_argument(
        "--sort",
        choices=["relevance", "modified", "created", "size", "name"],
        help="Sort field (NOT 'title' — broken server-side)",
    )
    sp_search.add_argument("--order", choices=["asc", "desc"], default="desc", help="Sort direction")
    sp_search.add_argument("--mode", type=int, choices=[0, 1, 2], default=0,
                           help="Search mode: 0=text (default), 1=image OCR, 2=combined")
    sp_search.add_argument("--path", help="Client-side path substring filter")
    sp_search.add_argument("--from-date", help="Client-side date filter from (YYYY-MM-DD)")
    sp_search.add_argument("--to-date", help="Client-side date filter to (YYYY-MM-DD)")
    sp_search.add_argument("--highlight", action="store_true", help="Wrap matches in <qusion> tags (500-char snippets)")
    sp_search.add_argument("--json", action="store_true",
                           help="Compact JSON (enriched items, action URLs dropped)")
    sp_search.add_argument("--json-full", action="store_true",
                           help="Full JSON (enriched items including raw action URLs)")

    # ─── read ─────────────────────────────────────────────────────────────────
    sp_read = subparsers.add_parser("read", help="One-shot content retrieval: search -> open -> text")
    sp_read.add_argument("--query", "-q", help="Search query; reads the first match")
    sp_read.add_argument("--category", help="Category filter for the query (only 'Email' reliable)")
    sp_read.add_argument("--id", help="Item ID to read")
    sp_read.add_argument("--path", help="Parent directory (with --name)")
    sp_read.add_argument("--name", help="Filename with extension (with --path)")
    sp_read.add_argument("--lang", default="ENG", help="OCR language for scanned content (default: ENG)")
    sp_read.add_argument("--json", action="store_true", help="JSON output {full_path, kind, source, text}")

    # ─── async-search ─────────────────────────────────────────────────────────
    sp_async = subparsers.add_parser("async-search", help="Two-phase search: fast total, then fetch result window")
    sp_async.add_argument("--query", "-q", required=True, help="Search query")
    sp_async.add_argument("--limit", type=int, default=50, help="Result window size (fixed at submit, ceiling 1000)")
    sp_async.add_argument("--category", help="tools= filter (only 'Email' reliable)")
    sp_async.add_argument("--json", action="store_true", help="Compact JSON output")
    sp_async.add_argument("--json-full", action="store_true", help="Full JSON output")

    # ─── suggest ──────────────────────────────────────────────────────────────
    sp_suggest = subparsers.add_parser("suggest", help="Autocomplete suggestions (name, kind, category, history)")
    sp_suggest.add_argument("--query", "-q", required=True, help="Partial query to complete")
    sp_suggest.add_argument("--limit", type=int, default=10, help="Suggestions per group (default: 10)")
    sp_suggest.add_argument("--json", action="store_true", help="Output JSON")

    # ─── preview ──────────────────────────────────────────────────────────────
    sp_preview = subparsers.add_parser("preview", help="Get file preview / email HTML body")
    sp_preview.add_argument("--id", help="Item ID from search results (NOT supported server-side; use path+name)")
    sp_preview.add_argument("--path", help="File path on NAS")
    sp_preview.add_argument("--name", help="Filename (with extension)")
    sp_preview.add_argument("--output", "-o", help="Save output to file")
    sp_preview.add_argument("--json", action="store_true", help="Output JSON")

    # ─── detect ───────────────────────────────────────────────────────────────
    sp_detect = subparsers.add_parser("detect", help="OCR text detection with bounding boxes (PDF/images)")
    sp_detect.add_argument("--path", required=True, help="File path on NAS")
    sp_detect.add_argument("--name", required=True, help="Filename (with extension)")
    sp_detect.add_argument("--lang", default="ENG", help="OCR language (default: ENG)")
    sp_detect.add_argument("--text", action="store_true", help="Print detected text only, one block per line")
    sp_detect.add_argument("--json", action="store_true", help="Output JSON (blocks with vertices and score)")

    # ─── download ─────────────────────────────────────────────────────────────
    sp_download = subparsers.add_parser("download", help="Download a file from NAS")
    sp_download.add_argument("--path", required=True, help="File path on NAS")
    sp_download.add_argument("--name", required=True, help="Filename (with extension)")
    sp_download.add_argument("--ext", help="File extension (for naming)")
    sp_download.add_argument("--output", "-o", help="Output directory (default: current)")

    # ─── status ───────────────────────────────────────────────────────────────
    sp_status = subparsers.add_parser("status", help="Check Qsirch index status & health")
    sp_status.add_argument("--json", action="store_true", help="Output JSON (includes version + brief)")

    # ─── similar ──────────────────────────────────────────────────────────────
    sp_similar = subparsers.add_parser("similar", help="Find similar items (more-like-this)")
    sp_similar.add_argument("--id", required=True, help="Item ID to find similar items for")
    sp_similar.add_argument("--limit", type=int, default=10, help="Max results")
    sp_similar.add_argument("--category", help="Filter by category (Email, PDF, etc.)")
    sp_similar.add_argument("--json", action="store_true", help="Compact JSON output")
    sp_similar.add_argument("--json-full", action="store_true", help="Full JSON output")

    # Backward compatibility: if no subcommand but -q is present, assume "search"
    subcommands = {"search", "read", "async-search", "suggest", "preview", "detect", "download", "status", "similar"}
    if not any(arg in subcommands for arg in sys.argv[1:]):
        if "-q" in sys.argv or "--query" in sys.argv:
            sys.argv.insert(1, "search")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if not args.user:
        print(
            "[Error] Credentials required. Set QSIRCH_USER/QSIRCH_PASS environment "
            "variables or pass --user/--pass flags.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = QsirchClient(host=args.host, port=args.port, use_ssl=args.ssl, timeout=args.timeout)
    try:
        # Session cache first: a cached sid skips authLogin.cgi entirely. A
        # stale sid 401s and _request re-authenticates (needs the password).
        if client.use_cached_session(args.user, args.password):
            if not args.password:
                print(
                    "[Info] using cached session; QSIRCH_PASS unset, so an expired "
                    "session cannot auto-renew",
                    file=sys.stderr,
                )
        else:
            if not args.password:
                print(
                    "[Error] No cached session and no password. Set QSIRCH_PASS "
                    "or pass --pass.",
                    file=sys.stderr,
                )
                sys.exit(1)
            client.login(args.user, args.password)

        handlers = {
            "search": cmd_search,
            "read": cmd_read,
            "async-search": cmd_async_search,
            "suggest": cmd_suggest,
            "preview": cmd_preview,
            "detect": cmd_detect,
            "download": cmd_download,
            "status": cmd_status,
            "similar": cmd_similar,
        }
        handlers[args.command](args, client)
    except QsirchError as e:
        print(f"[Error] {e}", file=sys.stderr)
        sys.exit(e.exit_code)
    except BrokenPipeError:
        # Output consumer (e.g. `head`) closed early: exit quietly.
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
