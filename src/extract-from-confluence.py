#!/usr/bin/env python3
"""Extract Confluence pages to Markdown files with attachments.

For a single page (or an entire page tree with --tree) the script:
  1. Fetches the page body in Confluence storage format (XHTML).
  2. Pre-processes Confluence-specific macros (code, TOC, images, links).
  3. Converts the cleaned XHTML to Markdown via markdownify.
  4. Downloads all page attachments into an img/ subdirectory.
  5. Writes the Markdown file with the original page title as filename.

In --tree mode the output mirrors the Confluence page hierarchy:
  output-dir/
    Root Page.md
    img/
    Child A/
      Child A.md
      img/
      Grandchild/
        Grandchild.md
        img/
    Child B/
      Child B.md
      img/

Use --flat with --tree to put all pages in a single directory instead.

Usage:
  export CONFLUENCE_PAT="your-personal-access-token"

  # Extract a single page
  python scripts/extract-from-confluence.py \\
      --page "https://confluence.jtfg.com/spaces/AI/pages/218694676/My+Page" \\
      --output-dir docs/extracted \\
      --skip-tls-check

  # Extract an entire page tree (hierarchical directories)
  python scripts/extract-from-confluence.py \\
      --page "https://confluence.jtfg.com/spaces/AI/pages/218694676/My+Page" \\
      --output-dir docs/extracted \\
      --tree \\
      --skip-tls-check

  # Extract page tree into flat directory
  python scripts/extract-from-confluence.py \\
      --page "https://confluence.jtfg.com/spaces/AI/pages/218694676/My+Page" \\
      --output-dir docs/extracted \\
      --tree --flat \\
      --skip-tls-check

  # Dry run -- list pages without downloading
  python scripts/extract-from-confluence.py \\
      --page "https://confluence.jtfg.com/spaces/AI/pages/218694676/My+Page" \\
      --output-dir docs/extracted \\
      --tree --dry-run --skip-tls-check

Dependencies:
  pip install -r scripts/requirements-confluence.txt
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse, parse_qs, quote

import requests
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

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
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(title: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", title)


# ---------------------------------------------------------------------------
# URL parsing -- resolve page URL/ID into (confluence_url, page_id, space_key)
# ---------------------------------------------------------------------------

def parse_page_arg(
    page: str,
    confluence_url: str | None,
) -> tuple[str, str | None, str | None]:
    if page.isdigit():
        if not confluence_url:
            sys.exit(
                "ERROR: When --page is a bare numeric ID "
                "you must also supply --confluence-url."
            )
        return confluence_url.rstrip("/"), page, None

    if not page.startswith(("http://", "https://")):
        sys.exit(
            "ERROR: --page must be a numeric page ID "
            "or a Confluence page URL (http/https)."
        )

    parsed = urlparse(page)
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
        "ERROR: Unable to parse --page URL. Expected one of:\n"
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
        self._base_url = base_url
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

    def get_page_by_id(self, page_id: str, expand: str = "space") -> dict:
        return self._request("GET", f"/content/{page_id}?expand={expand}")

    def get_page_body(self, page_id: str) -> dict:
        return self._request(
            "GET",
            f"/content/{page_id}?expand=body.storage,version,space",
        )

    def find_page(self, space_key: str, title: str) -> dict | None:
        enc = quote(title, safe="")
        data = self._request(
            "GET",
            f"/content?spaceKey={space_key}&title={enc}&expand=version",
        )
        results = data.get("results", [])
        return results[0] if results else None

    def get_child_pages(self, page_id: str) -> list[dict]:
        results: list[dict] = []
        start = 0
        limit = 50
        while True:
            data = self._request(
                "GET",
                f"/content/{page_id}/child/page"
                f"?start={start}&limit={limit}&expand=version",
            )
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < limit:
                break
            start += limit
        return results

    def list_attachments(self, page_id: str) -> list[dict]:
        results: list[dict] = []
        start = 0
        limit = 50
        while True:
            data = self._request(
                "GET",
                f"/content/{page_id}/child/attachment"
                f"?start={start}&limit={limit}",
            )
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < limit:
                break
            start += limit
        return results

    def download_attachment(self, download_path: str, dest: Path) -> None:
        url = f"{self._base_url}{download_path}"
        resp = self._session.get(url, stream=True)
        if not resp.ok:
            raise RuntimeError(
                f"Download failed HTTP {resp.status_code} for {url}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

    def resolve_page_id_by_title(self, space_key: str, title: str) -> str:
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
# Page tree
# ---------------------------------------------------------------------------

@dataclass
class PageNode:
    id: str
    title: str
    children: list[PageNode] = field(default_factory=list)
    rel_dir: PurePosixPath = PurePosixPath(".")
    rel_md: PurePosixPath = PurePosixPath(".")


def collect_page_tree(api: ConfluenceApi, root_id: str) -> PageNode:
    root_info = api.get_page_by_id(root_id, expand="version")
    root_title = root_info["title"]
    safe = _safe_filename(root_title)

    root = PageNode(
        id=root_id,
        title=root_title,
        rel_dir=PurePosixPath("."),
        rel_md=PurePosixPath(f"{safe}.md"),
    )

    def _build(node: PageNode) -> None:
        children_data = api.get_child_pages(node.id)
        for child_data in children_data:
            child_title = child_data["title"]
            child_safe = _safe_filename(child_title)
            if node.rel_dir == PurePosixPath("."):
                child_dir = PurePosixPath(child_safe)
            else:
                child_dir = node.rel_dir / child_safe
            child_node = PageNode(
                id=child_data["id"],
                title=child_title,
                rel_dir=child_dir,
                rel_md=child_dir / f"{child_safe}.md",
            )
            node.children.append(child_node)
            _build(child_node)

    _build(root)
    return root


def collect_page_tree_flat(api: ConfluenceApi, root_id: str) -> PageNode:
    root_info = api.get_page_by_id(root_id, expand="version")
    root_title = root_info["title"]
    safe = _safe_filename(root_title)

    root = PageNode(
        id=root_id,
        title=root_title,
        rel_dir=PurePosixPath("."),
        rel_md=PurePosixPath(f"{safe}.md"),
    )

    def _build(node: PageNode) -> None:
        children_data = api.get_child_pages(node.id)
        for child_data in children_data:
            child_title = child_data["title"]
            child_safe = _safe_filename(child_title)
            child_node = PageNode(
                id=child_data["id"],
                title=child_title,
                rel_dir=PurePosixPath("."),
                rel_md=PurePosixPath(f"{child_safe}.md"),
            )
            node.children.append(child_node)
            _build(child_node)

    _build(root)
    return root


def build_title_map(root: PageNode) -> dict[str, PurePosixPath]:
    result: dict[str, PurePosixPath] = {root.title: root.rel_md}

    def _walk(node: PageNode) -> None:
        for child in node.children:
            result[child.title] = child.rel_md
            _walk(child)

    _walk(root)
    return result


def flatten_tree(root: PageNode) -> list[PageNode]:
    result: list[PageNode] = [root]

    def _walk(node: PageNode) -> None:
        for child in node.children:
            result.append(child)
            _walk(child)

    _walk(root)
    return result


def count_tree(root: PageNode) -> int:
    count = 1
    for child in root.children:
        count += count_tree(child)
    return count


# ---------------------------------------------------------------------------
# Confluence storage XHTML -> Markdown
# ---------------------------------------------------------------------------

def _preprocess_confluence_xhtml(
    html: str,
    title_to_path: dict[str, PurePosixPath] | None,
    source_dir: PurePosixPath | None,
    base_url: str,
    space_key: str,
) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for macro in soup.find_all("ac:structured-macro"):
        name = macro.get("ac:name", "")

        if name == "code":
            lang = ""
            lang_tag = macro.find("ac:parameter", attrs={"ac:name": "language"})
            if lang_tag:
                lang = lang_tag.get_text(strip=True)
            body_tag = macro.find("ac:plain-text-body")
            code_text = ""
            if body_tag:
                code_text = body_tag.get_text()
            pre = soup.new_tag("pre")
            code_tag = soup.new_tag("code")
            if lang:
                code_tag["class"] = f"language-{lang}"
            code_tag.string = code_text
            pre.append(code_tag)
            macro.replace_with(pre)

        elif name == "toc":
            macro.decompose()

        elif name in ("info", "note", "warning", "tip"):
            label = name.capitalize()
            body_tag = macro.find("ac:rich-text-body")
            if body_tag:
                bq = soup.new_tag("blockquote")
                strong = soup.new_tag("strong")
                strong.string = f"{label}: "
                p = soup.new_tag("p")
                p.append(strong)
                for child in list(body_tag.children):
                    p.append(child)
                bq.append(p)
                macro.replace_with(bq)
            else:
                macro.decompose()

        else:
            comment = f"<!-- unsupported macro: {name} -->"
            macro.replace_with(comment)

    for img_macro in soup.find_all("ac:image"):
        ri = img_macro.find("ri:attachment")
        if ri:
            filename = ri.get("ri:filename", "")
            img = soup.new_tag("img", src=f"img/{filename}", alt=filename)
            img_macro.replace_with(img)
        else:
            img_macro.decompose()

    for link_macro in soup.find_all("ac:link"):
        ri_page = link_macro.find("ri:page")
        if ri_page:
            target_title = ri_page.get("ri:content-title", "")
            target_space = ri_page.get("ri:space-key", "")
            link_body = link_macro.find("ac:plain-text-link-body")
            visible_text = link_body.get_text() if link_body else target_title

            if not visible_text:
                visible_text = target_title or "link"

            if target_space and target_space != space_key:
                enc_title = quote(target_title.replace(" ", "+"), safe="+")
                href = f"{base_url}/display/{target_space}/{enc_title}"
            elif title_to_path and target_title in title_to_path:
                target_md = title_to_path[target_title]
                src = source_dir or PurePosixPath(".")
                rel = os.path.relpath(
                    str(target_md), str(src),
                ).replace("\\", "/")
                href = rel
            else:
                safe = _safe_filename(target_title)
                href = f"{safe}.md"

            a = soup.new_tag("a", href=href)
            a.string = visible_text
            link_macro.replace_with(a)
        else:
            link_body = link_macro.find("ac:plain-text-link-body")
            if link_body:
                link_macro.replace_with(link_body.get_text())
            else:
                link_macro.decompose()

    for tag in soup.find_all(re.compile(r"^(ac|ri):")):
        if isinstance(tag, Tag):
            tag.decompose()

    return str(soup)


class _ConfluenceMarkdownConverter(MarkdownConverter):

    def convert_pre(self, el, text, **kwargs):
        code_el = el.find("code")
        if code_el:
            lang = ""
            cls = code_el.get("class", [])
            if isinstance(cls, list):
                for c in cls:
                    if c.startswith("language-"):
                        lang = c[len("language-"):]
                        break
            code_text = code_el.get_text()
            return f"\n```{lang}\n{code_text.rstrip(chr(10))}\n```\n\n"
        return f"\n```\n{text.strip()}\n```\n\n"


def confluence_to_markdown(
    storage_html: str,
    title_to_path: dict[str, PurePosixPath] | None,
    source_dir: PurePosixPath | None,
    base_url: str,
    space_key: str,
) -> str:
    cleaned = _preprocess_confluence_xhtml(
        storage_html, title_to_path, source_dir, base_url, space_key,
    )
    converter = _ConfluenceMarkdownConverter(
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
    )
    md = converter.convert(cleaned)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


# ---------------------------------------------------------------------------
# Extract a single page
# ---------------------------------------------------------------------------

def extract_page(
    api: ConfluenceApi,
    page_id: str,
    page_dir: Path,
    title_to_path: dict[str, PurePosixPath] | None,
    source_rel_dir: PurePosixPath | None,
    base_url: str,
    space_key: str,
    *,
    dry_run: bool = False,
) -> tuple[str, int]:
    page = api.get_page_body(page_id)
    title = page["title"]
    body_html = page["body"]["storage"]["value"]

    bar = "\u2500" * max(0, 60 - len(title))
    print(white(f"\u2500\u2500 {title} {bar}"))
    print(f"  Page ID  : {page_id}")

    attachments = api.list_attachments(page_id)
    print(f"  Attachments : {len(attachments)}")

    if dry_run:
        print(yellow(
            f"  DRY RUN  : Would write '{title}.md' "
            f"+ {len(attachments)} file(s)"
        ))
        print()
        return title, 0

    md_body = confluence_to_markdown(
        body_html, title_to_path, source_rel_dir, base_url, space_key,
    )
    md_content = f"# {title}\n\n{md_body}"

    safe_title = _safe_filename(title)
    page_dir.mkdir(parents=True, exist_ok=True)
    md_path = page_dir / f"{safe_title}.md"
    md_path.write_text(md_content, encoding="utf-8")
    print(green(f"  Markdown : {md_path.name}"))

    attach_count = 0
    img_dir = page_dir / "img"
    for att in attachments:
        att_title = att["title"]
        download_link = att.get("_links", {}).get("download", "")
        if not download_link:
            print(yellow(
                f"  WARNING  : No download link for '{att_title}', skipping"
            ))
            continue
        dest = img_dir / att_title
        try:
            api.download_attachment(download_link, dest)
            print(green(f"  Attach   : {att_title}  OK"))
            attach_count += 1
        except Exception as exc:
            print(yellow(f"  WARNING  : {att_title}  FAILED -- {exc}"))

    print()
    return title, attach_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Confluence pages to Markdown files with attachments.",
    )
    parser.add_argument(
        "--page", required=True,
        help=(
            "Source page: a Confluence page URL "
            "(e.g. https://confluence.jtfg.com/spaces/AI/pages/123/Title) "
            "or a bare numeric page ID (requires --confluence-url)."
        ),
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory to write .md files and img/ subdirectory.",
    )
    parser.add_argument(
        "--confluence-url", default=None,
        help=(
            "Base URL of the Confluence instance. "
            "Only needed when --page is a bare numeric ID."
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
        "--tree", action="store_true",
        help="Extract the entire page tree (page + all descendant pages).",
    )
    parser.add_argument(
        "--flat", action="store_true",
        help=(
            "With --tree, write all pages into a single flat directory "
            "instead of mirroring the page hierarchy."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List pages that would be extracted without writing files.",
    )
    args = parser.parse_args()

    if not args.pat:
        sys.exit(
            "ERROR: No PAT provided. "
            "Use --pat <token> or set the CONFLUENCE_PAT env var."
        )

    if args.flat and not args.tree:
        sys.exit("ERROR: --flat can only be used together with --tree.")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_tls_check:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    confluence_url, page_id, space_key = parse_page_arg(
        args.page, args.confluence_url,
    )

    api = ConfluenceApi(
        confluence_url, args.pat, verify_tls=not args.skip_tls_check,
    )

    if not page_id and space_key:
        parsed = urlparse(args.page)
        title_slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        title = unquote(title_slug.replace("+", " "))
        page_id = api.resolve_page_id_by_title(space_key, title)

    assert page_id is not None

    if not space_key:
        space_key = api.resolve_space_key(page_id)

    title_to_path: dict[str, PurePosixPath] | None = None

    if args.tree:
        print(cyan("Collecting page tree..."))
        if args.flat:
            tree_root = collect_page_tree_flat(api, page_id)
        else:
            tree_root = collect_page_tree(api, page_id)
        total_pages = count_tree(tree_root)
        print(cyan(f"Found {total_pages} page(s) in tree"))
        title_to_path = build_title_map(tree_root)
        all_nodes = flatten_tree(tree_root)
    else:
        root_info = api.get_page_by_id(page_id, expand="version")
        root_title = root_info["title"]
        safe = _safe_filename(root_title)
        single_node = PageNode(
            id=page_id,
            title=root_title,
            rel_dir=PurePosixPath("."),
            rel_md=PurePosixPath(f"{safe}.md"),
        )
        all_nodes = [single_node]
        total_pages = 1

    print()
    print(cyan(f"Confluence  : {confluence_url}"))
    print(cyan(f"Space       : {space_key}"))
    print(cyan(f"Root page   : {page_id}"))
    print(cyan(f"Output dir  : {output_dir}"))
    print(f"Pages       : {total_pages}")
    if args.tree:
        mode = "Tree (flat)" if args.flat else "Tree (hierarchical)"
        print(cyan(f"Mode        : {mode}"))
    if args.dry_run:
        print(yellow("Mode        : DRY RUN"))
    print()

    pages_extracted = 0
    total_attachments = 0
    error_count = 0

    for node in all_nodes:
        page_dir = output_dir / str(node.rel_dir) if str(node.rel_dir) != "." else output_dir
        try:
            _, att_count = extract_page(
                api, node.id, page_dir,
                title_to_path, node.rel_dir,
                confluence_url, space_key,
                dry_run=args.dry_run,
            )
            pages_extracted += 1
            total_attachments += att_count
        except SystemExit:
            raise
        except Exception as exc:
            print(yellow(f"WARNING:   ERROR extracting page {node.id} -- {exc}"))
            error_count += 1
            print()

    print(cyan("\u2500" * 37))
    color = green if pages_extracted else yellow
    print(color(f"Pages extracted  : {pages_extracted}"))
    print(f"Attachments      : {total_attachments}")
    if error_count:
        print(red(f"Errors           : {error_count}"))
    else:
        print(green("Errors           : 0"))

    if error_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
