"""Shared helpers for extract-from-confluence.py and publish-to-confluence.py.

Contents:
  * console setup (never crash on characters the console cannot display),
  * ANSI colours,
  * Confluence page URL parsing (incl. tiny links /x/XXXX),
  * a small Confluence REST API client,
  * safe file names and Windows long-path handling,
  * YAML-like front matter read/write (no PyYAML dependency).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------


def setup_console() -> None:
    """Make stdout/stderr robust against unencodable characters.

    On Windows a redirected/piped stdout uses the ANSI code page (e.g. cp1250),
    which cannot represent many characters found in Confluence content (emoji,
    box drawing, arrows ...). Keep the console encoding (so Czech characters
    display correctly) but replace what cannot be encoded instead of raising
    UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


_NO_COLOR = os.environ.get("NO_COLOR") is not None


def _c(code: str, text: str) -> str:
    if _NO_COLOR or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def cyan(t: str) -> str:
    return _c("0;36", t)


def green(t: str) -> str:
    return _c("0;32", t)


def yellow(t: str) -> str:
    return _c("0;33", t)


def red(t: str) -> str:
    return _c("0;31", t)


def white(t: str) -> str:
    return _c("1;37", t)


def section(title: str) -> str:
    bar = "-" * max(0, 60 - len(title))
    return white(f"-- {title} {bar}")


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def decode_tiny_link(code: str) -> str:
    """Decode a Confluence tiny link code (/x/<code>) into a page ID.

    Confluence encodes the page ID as little-endian bytes in base64 with
    '+' -> '_' and '/' -> '-'; trailing zero bytes (encoded as 'A') and the
    '=' padding are stripped, so the code is padded back with 'A'.
    """
    padded = code + "A" * (-len(code) % 4)
    raw = base64.b64decode(padded, altchars=b"_-")
    return str(int.from_bytes(raw, "little"))


def parse_page_arg(
    page: str,
    confluence_url: str | None,
    arg_name: str = "--page",
) -> tuple[str, str | None, str | None, str | None]:
    """Return (confluence_url, page_id, space_key, title_from_url).

    Any of page_id / space_key / title may be None.
    """
    if page.isdigit():
        if not confluence_url:
            sys.exit(
                f"ERROR: When {arg_name} is a bare numeric ID "
                "you must also supply --confluence-url."
            )
        return confluence_url.rstrip("/"), page, None, None

    if not page.startswith(("http://", "https://")):
        sys.exit(
            f"ERROR: {arg_name} must be a numeric page ID "
            "or a Confluence page URL (http/https)."
        )

    parsed = urlparse(page)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    qs = parse_qs(parsed.query)
    if "pageId" in qs:
        m = re.match(r"^(.*)/pages/", path)
        ctx = m.group(1) if m else ""
        return f"{base}{ctx}".rstrip("/"), qs["pageId"][0], None, None

    m = re.match(r"^(.*)/x/([A-Za-z0-9_-]+)$", path)
    if m:
        return f"{base}{m.group(1)}".rstrip("/"), decode_tiny_link(m.group(2)), None, None

    m = re.match(r"^(.*)/spaces/([^/]+)/pages/(\d+)", path)
    if m:
        return f"{base}{m.group(1)}".rstrip("/"), m.group(3), m.group(2), None

    m = re.match(r"^(.*)/display/([^/]+)/(.+)$", path)
    if m:
        title = unquote(m.group(3).replace("+", " "))
        return f"{base}{m.group(1)}".rstrip("/"), None, m.group(2), title

    m = re.match(r"^(.*)/pages/(\d+)", path)
    if m:
        return f"{base}{m.group(1)}".rstrip("/"), m.group(2), None, None

    sys.exit(
        f"ERROR: Unable to parse {arg_name} URL. Expected one of:\n"
        "  /x/TINYCODE\n"
        "  /spaces/SPACE/pages/NNN/Title\n"
        "  /pages/viewpage.action?pageId=NNN\n"
        "  /display/SPACE/Title\n"
        "  /pages/NNN/Title"
    )


# ---------------------------------------------------------------------------
# Confluence REST API
# ---------------------------------------------------------------------------


class ConfluenceApi:
    def __init__(self, base_url: str, pat: str, *, verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self._api = f"{self.base_url}/rest/api"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        })
        self._session.verify = verify_tls
        self._user_cache: dict[str, str] = {}

    # -- low level ----------------------------------------------------------

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        url = f"{self._api}{endpoint}"
        resp = self._session.request(method, url, **kwargs)
        if not resp.ok:
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise ApiError(resp.status_code, f"{method} {url}: {detail}")
        if resp.status_code == 204 or not resp.content:
            return {}
        # Confluence always answers in UTF-8; do not rely on header sniffing.
        return json.loads(resp.content.decode("utf-8"))

    def _paged(self, endpoint: str, limit: int = 100) -> list[dict]:
        results: list[dict] = []
        start = 0
        sep = "&" if "?" in endpoint else "?"
        while True:
            data = self._request(
                "GET", f"{endpoint}{sep}start={start}&limit={limit}",
            )
            batch = data.get("results", [])
            results.extend(batch)
            if not batch or "next" not in data.get("_links", {}):
                break
            start += len(batch)
        return results

    # -- pages --------------------------------------------------------------

    def get_page(self, page_id: str, expand: str = "space,version") -> dict:
        return self._request("GET", f"/content/{page_id}", params={"expand": expand})

    def get_page_full(self, page_id: str) -> dict:
        return self.get_page(page_id, "body.storage,version,space,ancestors")

    def find_page(self, space_key: str, title: str, expand: str = "version,ancestors") -> dict | None:
        data = self._request(
            "GET", "/content",
            params={"spaceKey": space_key, "title": title, "type": "page", "expand": expand},
        )
        results = data.get("results", [])
        return results[0] if results else None

    def get_child_pages(self, page_id: str) -> list[dict]:
        return self._paged(f"/content/{page_id}/child/page?expand=version")

    def create_page(self, space_key: str, parent_id: str, title: str, body: str) -> dict:
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "ancestors": [{"id": str(parent_id)}],
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        return self._request("POST", "/content", json=payload)

    def update_page(
        self, page_id: str, title: str, body: str, version: int,
        *, parent_id: str | None = None, message: str = "Published from repository",
    ) -> dict:
        payload: dict = {
            "type": "page",
            "title": title,
            "version": {"number": version + 1, "message": message},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]
        return self._request("PUT", f"/content/{page_id}", json=payload)

    def resolve_page_id_by_title(self, space_key: str, title: str) -> str:
        page = self.find_page(space_key, title)
        if not page:
            sys.exit(f"ERROR: Could not find a page titled '{title}' in space '{space_key}'.")
        return page["id"]

    def resolve_space_key(self, page_id: str) -> str:
        return self.get_page(page_id, "space")["space"]["key"]

    # -- attachments --------------------------------------------------------

    def list_attachments(self, page_id: str) -> list[dict]:
        return self._paged(f"/content/{page_id}/child/attachment")

    def get_attachment(self, page_id: str, filename: str) -> dict | None:
        data = self._request(
            "GET", f"/content/{page_id}/child/attachment",
            params={"filename": filename},
        )
        results = data.get("results", [])
        return results[0] if results else None

    def download_attachment(self, download_path: str, dest: Path) -> None:
        url = f"{self.base_url}{download_path}"
        resp = self._session.get(url, stream=True)
        if not resp.ok:
            raise RuntimeError(f"Download failed HTTP {resp.status_code} for {url}")
        dest = long_path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

    def download_bytes(self, download_path: str) -> bytes:
        url = f"{self.base_url}{download_path}"
        resp = self._session.get(url)
        if not resp.ok:
            raise RuntimeError(f"Download failed HTTP {resp.status_code} for {url}")
        return resp.content

    def upload_attachment(
        self, page_id: str, filepath: Path, filename: str | None = None,
        *, existing: dict | None | bool = False, comment: str = "",
    ) -> None:
        """Create or update (new version of) an attachment.

        existing: the attachment dict if known, None if known not to exist,
        False (default) to look it up.
        """
        import mimetypes
        name = filename or filepath.name
        if existing is False:
            existing = self.get_attachment(page_id, name)
        if existing:
            url = f"{self._api}/content/{page_id}/child/attachment/{existing['id']}/data"
        else:
            url = f"{self._api}/content/{page_id}/child/attachment"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        data = {"minorEdit": "true"}
        if comment:
            data["comment"] = comment
        with open(long_path(filepath), "rb") as fh:
            resp = self._session.post(url, files={"file": (name, fh, mime)}, data=data)
        if not resp.ok:
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise RuntimeError(f"Attachment upload HTTP {resp.status_code} for '{name}': {detail}")

    # -- users --------------------------------------------------------------

    def user_display_name(self, userkey: str) -> str:
        if userkey not in self._user_cache:
            try:
                data = self._request("GET", "/user", params={"key": userkey})
                self._user_cache[userkey] = data.get("displayName") or userkey
            except ApiError:
                self._user_cache[userkey] = userkey
        return self._user_cache[userkey]

    def page_url(self, page_id: str) -> str:
        return f"{self.base_url}/pages/viewpage.action?pageId={page_id}"

    def display_url(self, space_key: str, title: str) -> str:
        return f"{self.base_url}/display/{space_key}/{quote(title.replace(' ', '+'), safe='+')}"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Confluence API HTTP {status} on {message}")
        self.status = status


# ---------------------------------------------------------------------------
# File names / paths
# ---------------------------------------------------------------------------

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str:
    """Return a file/directory name that is valid on Windows and POSIX.

    Replaces reserved characters and control characters with '_', strips
    trailing dots/spaces (Windows silently drops them) and avoids reserved
    device names. Unicode (diacritics) is preserved.
    """
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    s = s.strip().rstrip(". ")
    if not s:
        s = "_"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s


def long_path(p: Path) -> Path:
    """On Windows, return an extended-length path (\\\\?\\) to bypass MAX_PATH."""
    if os.name != "nt":
        return p
    s = str(p)
    if s.startswith("\\\\?\\"):
        return p
    s = os.path.abspath(s)
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def file_sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(long_path(p), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(p: Path) -> str:
    """Read a UTF-8 text file (BOM tolerated), normalising newlines to \\n."""
    with open(long_path(p), "r", encoding="utf-8-sig", newline=None) as fh:
        return fh.read()


def write_text(p: Path, text: str) -> None:
    """Write UTF-8 (no BOM) with LF newlines."""
    lp = long_path(p)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


def split_front_matter(text: str) -> tuple[dict, str]:
    """Return (metadata, body). Only flat `key: value` pairs are supported."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            value = raw.strip("'")
        meta[key.strip()] = value
    return meta, text[m.end():]


def render_front_matter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"
