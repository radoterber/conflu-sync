#!/usr/bin/env python3
"""Publish Markdown files from a local directory to an on-premise Confluence instance.

For every .md file found in --source-dir the script:
  1. Extracts the page title from the first H1 heading.
  2. Converts the Markdown body to Confluence storage format (XHTML).
  3. Creates the page under the parent page, or updates it if a page with
     that title already exists in the space (idempotent -- safe to re-run).
  4. Uploads every image referenced in the Markdown as a page attachment,
     replacing any existing attachment with the same filename.

Usage:
  export CONFLUENCE_PAT="your-personal-access-token"

  # Pass a Confluence page URL directly
  python scripts/publish-to-confluence.py \\
      --source-dir docs/cs \\
      --parent-page "https://confluence.jtfg.com/spaces/AI/pages/218694676/My+Page"

  # Or pass a bare numeric ID (requires --confluence-url)
  python scripts/publish-to-confluence.py \\
      --source-dir docs/cs \\
      --parent-page 218694676 \\
      --confluence-url https://confluence.jtfg.com

  # Dry run
  python scripts/publish-to-confluence.py \\
      --source-dir docs/cs \\
      --parent-page "https://confluence.jtfg.com/spaces/AI/pages/218694676/My+Page" \\
      --dry-run

Dependencies:
  pip install -r scripts/requirements-confluence.txt
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

import mistune
import requests

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# URL parsing -- resolve ParentPage into (confluence_url, page_id, space_key)
# ---------------------------------------------------------------------------

def parse_parent_page(
    parent_page: str,
    confluence_url: str | None,
) -> tuple[str, str | None, str | None]:
    """Return (confluence_url, parent_page_id_or_None, space_key_or_None)."""

    if parent_page.isdigit():
        if not confluence_url:
            sys.exit(
                "ERROR: When --parent-page is a bare numeric ID "
                "you must also supply --confluence-url."
            )
        return confluence_url.rstrip("/"), parent_page, None

    if not parent_page.startswith(("http://", "https://")):
        sys.exit(
            "ERROR: --parent-page must be a numeric page ID "
            "or a Confluence page URL (http/https)."
        )

    parsed = urlparse(parent_page)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    qs = parse_qs(parsed.query)
    if "pageId" in qs:
        return base, qs["pageId"][0], None

    m = re.match(r"^(.*)/spaces/([^/]+)/pages/(\d+)", path)
    if m:
        ctx = m.group(1) or ""
        return f"{base}{ctx}".rstrip("/"), m.group(3), m.group(2)

    m = re.match(r"^(.*)/display/([^/]+)/(.+)", path)
    if m:
        ctx = m.group(1) or ""
        return f"{base}{ctx}".rstrip("/"), None, m.group(2)

    m = re.match(r"^(.*)/pages/(\d+)", path)
    if m:
        ctx = m.group(1) or ""
        return f"{base}{ctx}".rstrip("/"), m.group(2), None

    sys.exit(
        "ERROR: Unable to parse --parent-page URL. Expected one of:\n"
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
        self._api = f"{base_url}/rest/api"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {pat}",
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        })
        self._session.verify = verify_tls

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        url = f"{self._api}{endpoint}"
        resp = self._session.request(method, url, **kwargs)
        if not resp.ok:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = resp.text[:300]
            sys.exit(
                f"ERROR: Confluence API HTTP {resp.status_code} "
                f"on {method} {url}: {detail}"
            )
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    # -- page lookup --------------------------------------------------------

    def get_page_by_id(self, page_id: str, expand: str = "space") -> dict:
        return self._request("GET", f"/content/{page_id}?expand={expand}")

    def find_page(self, space_key: str, title: str) -> dict | None:
        from urllib.parse import quote
        enc = quote(title, safe="")
        data = self._request(
            "GET",
            f"/content?spaceKey={space_key}&title={enc}&expand=version",
        )
        results = data.get("results", [])
        return results[0] if results else None

    # -- page CUD -----------------------------------------------------------

    def create_page(
        self, space_key: str, parent_id: str, title: str, body: str,
    ) -> dict:
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
    ) -> dict:
        payload = {
            "type": "page",
            "title": title,
            "version": {
                "number": version + 1,
                "message": "Published from repository",
            },
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        return self._request("PUT", f"/content/{page_id}", json=payload)

    # -- attachments --------------------------------------------------------

    def get_attachment(self, page_id: str, filename: str) -> dict | None:
        from urllib.parse import quote
        enc = quote(filename, safe="")
        data = self._request(
            "GET",
            f"/content/{page_id}/child/attachment?filename={enc}",
        )
        results = data.get("results", [])
        return results[0] if results else None

    def upload_attachment(self, page_id: str, filepath: Path) -> None:
        existing = self.get_attachment(page_id, filepath.name)
        if existing:
            url = (
                f"{self._api}/content/{page_id}"
                f"/child/attachment/{existing['id']}/data"
            )
        else:
            url = f"{self._api}/content/{page_id}/child/attachment"

        mime = mimetypes.guess_type(filepath.name)[0] or "application/octet-stream"
        with open(filepath, "rb") as fh:
            resp = self._session.post(
                url,
                files={"file": (filepath.name, fh, mime)},
            )
        if not resp.ok:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise RuntimeError(
                f"Attachment upload HTTP {resp.status_code} "
                f"for '{filepath.name}': {detail}"
            )

    # -- resolve helpers ----------------------------------------------------

    def resolve_page_id_by_title(
        self, space_key: str, title: str,
    ) -> str:
        page = self.find_page(space_key, title)
        if not page:
            sys.exit(
                f"ERROR: Could not find a page titled '{title}' "
                f"in space '{space_key}'."
            )
        return page["id"]

    def resolve_space_key(self, page_id: str) -> str:
        info = self.get_page_by_id(page_id, expand="space")
        return info["space"]["key"]


# ---------------------------------------------------------------------------
# Markdown -> Confluence storage format (XHTML)
# ---------------------------------------------------------------------------

class ConfluenceRenderer(mistune.HTMLRenderer):
    """Custom mistune renderer that emits Confluence storage-format macros."""

    def block_code(self, code: str, info: str | None = None, **attrs) -> str:
        lang = (info or "none").strip().split()[0] if info else "none"
        safe = code.rstrip("\n")
        safe = safe.replace("]]>", "]]]]><![CDATA[>")
        return (
            '<ac:structured-macro ac:name="code">'
            f'<ac:parameter ac:name="language">{lang}</ac:parameter>'
            f"<ac:plain-text-body><![CDATA[{safe}]]></ac:plain-text-body>"
            "</ac:structured-macro>\n"
        )

    def image(self, alt: str, url: str, title: str | None = None) -> str:
        filename = url.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return (
            f'<ac:image><ri:attachment ri:filename="{filename}"/></ac:image>'
        )


_TOC_LINE = re.compile(r"^[ \t]*-\s+\[.*\]\(#.*\)\s*$")
_TOC_PLACEHOLDER = "<!-- CONFLUENCE_TOC_PLACEHOLDER -->"
_TOC_MACRO = (
    '<ac:structured-macro ac:name="toc">'
    '<ac:parameter ac:name="maxLevel">3</ac:parameter>'
    "</ac:structured-macro>"
)


def _replace_toc_with_placeholder(md_text: str) -> str:
    lines = md_text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _TOC_LINE.match(lines[i]):
            while i < len(lines) and (
                _TOC_LINE.match(lines[i]) or lines[i].strip() == ""
            ):
                i += 1
            out.append(_TOC_PLACEHOLDER)
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _strip_anchor_links(html: str) -> str:
    return re.sub(r'<a\s+href="#[^"]*">([^<]*)</a>', r"\1", html)


def markdown_to_confluence(md_text: str) -> str:
    md_text = _replace_toc_with_placeholder(md_text)
    renderer = ConfluenceRenderer(escape=False)
    md = mistune.create_markdown(
        renderer=renderer,
        plugins=["table", "strikethrough"],
    )
    html = md(md_text)
    html = html.replace(_TOC_PLACEHOLDER, _TOC_MACRO)
    html = _strip_anchor_links(html)
    return html


def extract_image_refs(md_text: str) -> list[str]:
    return list(dict.fromkeys(
        m.group(1) for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", md_text)
    ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish Markdown files to Confluence.",
    )
    parser.add_argument(
        "--source-dir", required=True,
        help="Directory containing .md files to publish.",
    )
    parser.add_argument(
        "--parent-page", required=True,
        help=(
            "Parent page: a Confluence page URL "
            "(e.g. https://confluence.jtfg.com/spaces/AI/pages/123/Title) "
            "or a bare numeric page ID (requires --confluence-url)."
        ),
    )
    parser.add_argument(
        "--confluence-url", default=None,
        help=(
            "Base URL of the Confluence instance. "
            "Only needed when --parent-page is a bare numeric ID."
        ),
    )
    parser.add_argument(
        "--pat", default=os.environ.get("CONFLUENCE_PAT", ""),
        help="Personal Access Token (default: $CONFLUENCE_PAT).",
    )
    parser.add_argument(
        "--skip-tls-check", action="store_true",
        help="Disable TLS certificate validation.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print planned actions without touching Confluence.",
    )
    args = parser.parse_args()

    if not args.pat:
        sys.exit(
            "ERROR: No PAT provided. "
            "Use --pat <token> or set the CONFLUENCE_PAT env var."
        )

    source_dir = Path(args.source_dir).resolve()
    if not source_dir.is_dir():
        sys.exit(f"ERROR: Source directory not found: {source_dir}")

    if args.skip_tls_check:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    confluence_url, parent_page_id, space_key = parse_parent_page(
        args.parent_page, args.confluence_url,
    )

    api = ConfluenceApi(
        confluence_url, args.pat, verify_tls=not args.skip_tls_check,
    )

    if not parent_page_id and space_key:
        parsed = urlparse(args.parent_page)
        title_slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        title = unquote(title_slug.replace("+", " "))
        parent_page_id = api.resolve_page_id_by_title(space_key, title)

    assert parent_page_id is not None

    if not space_key:
        space_key = api.resolve_space_key(parent_page_id)

    md_files = sorted(source_dir.glob("*.md"))
    if not md_files:
        print(yellow("WARNING: No .md files found in: " + str(source_dir)))
        return

    print()
    print(cyan(f"Confluence  : {confluence_url}"))
    print(cyan(f"Space       : {space_key}"))
    print(cyan(f"Parent page : {parent_page_id}"))
    print(cyan(f"Source dir  : {source_dir}"))
    print(f"Files found : {len(md_files)}")
    if args.dry_run:
        print(yellow("Mode        : DRY RUN (--dry-run)"))
    print()

    pages_upserted = 0
    attach_uploads = 0
    error_count = 0

    for md_file in md_files:
        bar = "\u2500" * max(0, 60 - len(md_file.name))
        print(white(f"\u2500\u2500 {md_file.name} {bar}"))

        try:
            raw = md_file.read_text(encoding="utf-8")
        except Exception as exc:
            print(yellow(f"WARNING:   Could not read {md_file}: {exc}"))
            error_count += 1
            continue

        title = md_file.stem
        m = re.search(r"(?m)^#\s+(.+)$", raw)
        if m:
            title = m.group(1).strip()

        body = re.sub(r"(?m)^#\s+.+\r?\n?", "", raw, count=1).lstrip()

        image_refs = extract_image_refs(raw)

        print(f"  Title    : {title}")
        print(f"  Images   : {len(image_refs)}")

        if args.dry_run:
            try:
                existing = api.find_page(space_key, title)
            except Exception:
                existing = None
            if existing:
                action = f"UPDATE existing page (id={existing['id']})"
            else:
                action = "CREATE new page"
            print(yellow(
                f"  WhatIf   : Would {action} '{title}' "
                f"+ {len(image_refs)} attachment(s)"
            ))
            print()
            continue

        try:
            storage_body = markdown_to_confluence(body)

            existing = api.find_page(space_key, title)
            if existing:
                api.update_page(
                    existing["id"], title, storage_body,
                    existing["version"]["number"],
                )
                page_id = existing["id"]
                ver = existing["version"]["number"] + 1
                print(green(f"  Page     : UPDATED  id={page_id}  v{ver}"))
            else:
                created = api.create_page(
                    space_key, parent_page_id, title, storage_body,
                )
                page_id = created["id"]
                print(green(f"  Page     : CREATED  id={page_id}"))
            pages_upserted += 1

            for img_ref in image_refs:
                img_path = md_file.parent / img_ref
                if not img_path.exists():
                    print(yellow(
                        f"WARNING:   Image not found, skipping: {img_ref}"
                    ))
                    continue
                try:
                    api.upload_attachment(page_id, img_path)
                    print(green(f"  Attach   : {img_path.name}  OK"))
                    attach_uploads += 1
                except Exception as exc:
                    print(yellow(
                        f"WARNING:   Attach: {img_path.name}  FAILED -- {exc}"
                    ))
                    error_count += 1

        except SystemExit:
            raise
        except Exception as exc:
            print(yellow(f"WARNING:   ERROR -- {exc}"))
            error_count += 1

        print()

    print(cyan("\u2500" * 37))
    color = green if pages_upserted else yellow
    print(color(f"Pages upserted  : {pages_upserted}"))
    print(f"Attachments     : {attach_uploads}")
    if error_count:
        print(red(f"Errors          : {error_count}"))
    else:
        print(green("Errors          : 0"))

    if error_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
