#!/usr/bin/env python3
"""Extract Confluence pages to Markdown files with attachments.

For a single page (or an entire page tree with --tree) the script:
  1. Fetches the page body in Confluence storage format (XHTML).
  2. Converts Confluence-specific elements (layouts, macros, tasks, mentions,
     dates, links, images) into Markdown or into reversible markers.
  3. Converts the cleaned XHTML to Markdown via markdownify.
  4. Downloads all page attachments into an img/ subdirectory.
  5. Writes <title>.md (UTF-8, LF) with a front-matter header holding the
     Confluence metadata (page id, space, version, ...).

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

Use --flat with --tree to put all pages in a single directory instead.

Markdown conventions (understood by publish-to-confluence.py):
  front matter          ---\\nconfluence_page_id: "123"\\n...\\n---
  task                  - [ ] text   /   - [x] text
  status macro          {{status:Green:DONE}}
  date                  {{date:2026-02-27}}
  merged table cells    {{span:up}} / {{span:left}}  (cell covered by rowspan/colspan)
  user mention          [@Display Name](confluence-user:USERKEY)
  info/note/tip/warning > [!NOTE] optional title
  other macros          <!-- confluence: <ac:structured-macro ...>...</ac:structured-macro> -->
  macros with body      <!-- confluence-begin: <ac:structured-macro ...><ac:rich-text-body> -->
                        ...body as Markdown...
                        <!-- confluence-end: name -->

Usage:
  set CONFLUENCE_PAT=your-personal-access-token

  python scripts/extract-from-confluence.py \\
      --page "https://confluence.jtfg.com/x/u5cBCQ" \\
      --output-dir docs/confluence --tree --skip-tls-check

  Accepted --page forms: /x/TINY, /spaces/S/pages/N/..., viewpage.action?pageId=N,
  /display/S/Title, /pages/N/..., or a numeric ID with --confluence-url.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bs4 import BeautifulSoup, NavigableString, Tag  # noqa: E402
from markdownify import MarkdownConverter  # noqa: E402

from confluence_lib import (  # noqa: E402
    ApiError, ConfluenceApi, cyan, green, parse_page_arg, red,
    render_front_matter, safe_filename, section, setup_console, write_text,
    yellow,
)

ATTACH_DIR = "img"

EMOTICONS = {
    "tick": "\u2705", "cross": "\u274c", "warning": "\u26a0\ufe0f",
    "information": "\u2139\ufe0f", "question": "\u2753", "question mark": "\u2753",
    "plus": "\u2795", "slightly smiling face": "\U0001f642",
    "minus": "\u2796", "light-on": "\U0001f4a1", "light-off": "\U0001f4a1",
    "yellow-star": "\u2b50", "red-star": "\u2b50", "green-star": "\u2b50",
    "blue-star": "\u2b50", "smile": "\U0001f642", "sad": "\U0001f641",
    "cheeky": "\U0001f61b", "laugh": "\U0001f600", "wink": "\U0001f609",
    "thumbs-up": "\U0001f44d", "thumbs-down": "\U0001f44e",
}

ALERT_MACROS = {"info": "INFO", "note": "NOTE", "warning": "WARNING", "tip": "TIP"}


# ---------------------------------------------------------------------------
# Page tree
# ---------------------------------------------------------------------------

@dataclass
class PageNode:
    id: str
    title: str
    parent: PageNode | None = None
    children: list[PageNode] = field(default_factory=list)
    rel_dir: PurePosixPath = PurePosixPath(".")
    rel_md: PurePosixPath = PurePosixPath(".")


def collect_page_tree(api: ConfluenceApi, root_id: str, *, flat: bool) -> PageNode:
    root_title = api.get_page(root_id, "version")["title"]
    root = PageNode(id=root_id, title=root_title,
                    rel_md=PurePosixPath(f"{safe_filename(root_title)}.md"))

    def _build(node: PageNode) -> None:
        for child_data in api.get_child_pages(node.id):
            title = child_data["title"]
            safe = safe_filename(title)
            if flat:
                rel_dir = PurePosixPath(".")
            elif node.rel_dir == PurePosixPath("."):
                rel_dir = PurePosixPath(safe)
            else:
                rel_dir = node.rel_dir / safe
            child = PageNode(id=child_data["id"], title=title, parent=node,
                             rel_dir=rel_dir, rel_md=rel_dir / f"{safe}.md")
            node.children.append(child)
            _build(child)

    _build(root)
    return root


def flatten_tree(root: PageNode) -> list[PageNode]:
    result = [root]
    for child in root.children:
        result.extend(flatten_tree(child))
    return result


# ---------------------------------------------------------------------------
# Confluence storage XHTML -> Markdown
# ---------------------------------------------------------------------------

def _md_url(url: str) -> str:
    """Wrap a link destination in <...> when it contains spaces/parentheses."""
    if re.search(r"[\s()<>]", url):
        return "<" + url.replace("<", "%3C").replace(">", "%3E") + ">"
    return url


def _rel(target: PurePosixPath, source_dir: PurePosixPath) -> str:
    return PurePosixPath(os.path.relpath(str(target), str(source_dir)).replace("\\", "/")).as_posix()


def _comment_escape(xml: str) -> str:
    """Make storage XML safe to embed in a single-line HTML comment.

    "--" is not allowed in comments, newlines and "|" would break Markdown
    tables. Reversed exactly by publish-to-confluence.py (_comment_unescape).
    """
    return (xml.replace("%", "%25").replace("--", "-%2D")
            .replace("\r", "%0D").replace("\n", "%0A").replace("|", "%7C"))


class StorageConverter:
    """Convert one page's storage XHTML into Markdown."""

    def __init__(
        self,
        api: ConfluenceApi | None,
        space_key: str,
        title_to_path: dict[str, PurePosixPath] | None,
        source_dir: PurePosixPath,
        attachment_names: dict[str, str] | None = None,
    ):
        self.api = api
        self.space_key = space_key
        self.title_to_path = title_to_path or {}
        self.source_dir = source_dir
        self.attachment_names = attachment_names or {}
        self.tokens: dict[str, str] = {}
        self.base_url = api.base_url if api else ""

    # -- helpers ------------------------------------------------------------

    def _token(self, soup: BeautifulSoup, markdown: str) -> NavigableString:
        key = f"CFLTOKEN{len(self.tokens):05d}X"
        self.tokens[key] = markdown
        return NavigableString(key)

    def _block_token(self, soup: BeautifulSoup, markdown: str) -> Tag:
        p = soup.new_tag("p")
        p.append(self._token(soup, markdown))
        return p

    def _page_href(self, title: str, space: str | None) -> str:
        if space and space != self.space_key:
            return self.api.display_url(space, title) if self.api else title
        if title in self.title_to_path:
            return _rel(self.title_to_path[title], self.source_dir)
        if self.api:
            return self.api.display_url(self.space_key, title)
        return f"{safe_filename(title)}.md"

    def _attachment_href(self, ri: Tag) -> str:
        filename = ri.get("ri:filename", "")
        ri_page = ri.find("ri:page")
        if ri_page and ri_page.get("ri:content-title"):
            title = ri_page["ri:content-title"]
            space = ri_page.get("ri:space-key")
            if (not space or space == self.space_key) and title in self.title_to_path:
                target_dir = self.title_to_path[title].parent
                return _rel(target_dir / ATTACH_DIR / safe_filename(filename), self.source_dir)
            return self._page_href(title, space)
        local = self.attachment_names.get(filename, safe_filename(filename))
        return f"{ATTACH_DIR}/{local}"

    def _user_name(self, userkey: str) -> str:
        if self.api and userkey:
            return self.api.user_display_name(userkey)
        return userkey or "user"

    # -- main ---------------------------------------------------------------

    def preprocess(self, html: str) -> BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")

        # Instructional template placeholders are not content.
        for el in soup.find_all("ac:placeholder"):
            el.decompose()

        # Macros: innermost first so nested macros are handled before parents.
        for macro in reversed(soup.find_all("ac:structured-macro")):
            self._convert_macro(soup, macro)

        for el in soup.find_all("ac:task-list"):
            ul = soup.new_tag("ul")
            for task in el.find_all("ac:task", recursive=False):
                status = task.find("ac:task-status")
                done = status is not None and status.get_text(strip=True) == "complete"
                li = soup.new_tag("li")
                li.append(self._token(soup, "[x] " if done else "[ ] "))
                body = task.find("ac:task-body")
                if body:
                    for child in list(body.children):
                        li.append(child.extract())
                ul.append(li)
            el.replace_with(ul)

        for img_macro in soup.find_all("ac:image"):
            src = ""
            ri = img_macro.find("ri:attachment")
            if ri:
                src = self._attachment_href(ri)
                default_alt = ri.get("ri:filename", "")
            else:
                url = img_macro.find("ri:url")
                if url:
                    src = url.get("ri:value", "")
                default_alt = src.rsplit("/", 1)[-1]
            if not src:
                img_macro.decompose()
                continue
            alt = img_macro.get("ac:alt") or default_alt
            img_macro.replace_with(soup.new_tag("img", src=src, alt=alt))

        for link in soup.find_all("ac:link"):
            self._convert_link(soup, link)

        for el in soup.find_all("time"):
            dt = el.get("datetime", "")
            el.replace_with(self._token(soup, f"{{{{date:{dt}}}}}") if dt else el.get_text())

        for el in soup.find_all("ac:emoticon"):
            name = el.get("ac:name", "")
            el.replace_with(EMOTICONS.get(name, f":{name}:"))

        # Pure wrappers (layouts, inline comment markers, ...): keep content.
        for el in soup.find_all(re.compile(r"^(ac|ri):")):
            if isinstance(el, Tag) and el.parent is not None:
                el.unwrap()

        # A table with a single cell that only wraps another table ("wrapped"
        # layout trick): Markdown cannot nest tables, so drop the wrapper.
        for table in soup.find_all("table"):
            cells = [c for c in table.find_all(["td", "th"]) if c.find_parent("table") is table]
            if len(cells) == 1 and cells[0].find("table"):
                inner = [c for c in cells[0].contents if not (isinstance(c, NavigableString) and not c.strip())]
                if all(isinstance(c, Tag) for c in inner):
                    table.replace_with(*[c.extract() for c in inner])

        for table in soup.find_all("table"):
            self._normalize_spans(soup, table)

        # Whitespace-only formatting (<strong> </strong>) -> plain space, so
        # neighbouring bold runs do not glue together ("**a******b**").
        for el in soup.find_all(["strong", "b", "em", "i", "u", "s"]):
            if el.parent is not None and not el.get_text().strip() and not el.find(["img", "br"]):
                el.replace_with(" " if el.get_text() else "")

        # Nested identical formatting (<strong>a <strong>b</strong></strong>)
        # would produce "**a **b****": drop the inner tag.
        for el in soup.find_all(["strong", "b", "em", "i", "u", "s"]):
            if el.parent is not None and el.find_parent(el.name) is not None:
                el.unwrap()

        # Lists directly inside lists (<ul><li>a</li><ol>..</ol></ul>) are
        # invalid HTML; move them into the preceding <li>.
        for lst in soup.find_all(["ul", "ol"]):
            if lst.parent is not None and lst.parent.name in ("ul", "ol"):
                prev = lst.find_previous_sibling("li")
                if prev is not None:
                    prev.append(lst.extract())
                else:
                    li = soup.new_tag("li")
                    lst.wrap(li)

        # Merge adjacent identical inline formatting (<strong>A</strong><strong>B</strong>)
        # which would otherwise produce "**A****B**".
        for el in soup.find_all(["strong", "b", "em", "i", "u", "s", "code"]):
            if el.parent is None:
                continue
            nxt = el.next_sibling
            while isinstance(nxt, Tag) and nxt.name == el.name:
                for child in list(nxt.children):
                    el.append(child.extract())
                nxt.decompose()
                nxt = el.next_sibling

        return soup

    def _normalize_spans(self, soup: BeautifulSoup, table: Tag) -> None:
        """Expand rowspan/colspan into a regular grid.

        Markdown tables cannot merge cells; covered positions get the
        placeholder {{span:up}} / {{span:left}} so columns stay aligned
        (and publish-to-confluence.py can merge them again).
        """
        rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        has_span = any(
            str(c.get(a, "1")) not in ("", "1")
            for tr in rows for c in tr.find_all(["td", "th"], recursive=False)
            for a in ("rowspan", "colspan")
        )
        if not has_span:
            return
        pending: dict[int, list] = {}   # column -> [remaining_rows, colspan]
        for tr in rows:
            cells = [c for c in tr.find_all(["td", "th"], recursive=False)]
            new_cells: list[Tag] = []
            col = 0
            it = iter(cells)
            cell = next(it, None)
            while cell is not None or any(c >= col for c in pending):
                if col in pending:
                    remaining, span = pending[col]
                    for k in range(span):
                        t = soup.new_tag("td")
                        t.append(self._token(soup, "{{span:up}}" if k == 0 else "{{span:left}}"))
                        new_cells.append(t)
                    if remaining <= 1:
                        del pending[col]
                    else:
                        pending[col] = [remaining - 1, span]
                    col += span
                    continue
                if cell is None:
                    break
                rs = int(cell.get("rowspan", "1")) if str(cell.get("rowspan", "1")).isdigit() else 1
                cs = int(cell.get("colspan", "1")) if str(cell.get("colspan", "1")).isdigit() else 1
                rs, cs = max(1, min(rs, 1000)), max(1, min(cs, 1000))
                for a in ("rowspan", "colspan"):
                    if a in cell.attrs:
                        del cell[a]
                new_cells.append(cell)
                for _ in range(cs - 1):
                    t = soup.new_tag(cell.name)
                    t.append(self._token(soup, "{{span:left}}"))
                    new_cells.append(t)
                if rs > 1:
                    pending[col] = [rs - 1, cs]
                col += cs
                cell = next(it, None)
            for c in cells:
                c.extract()
            tr.clear()
            for c in new_cells:
                tr.append(c)

    def _convert_macro(self, soup: BeautifulSoup, macro: Tag) -> None:
        name = macro.get("ac:name", "")

        if name == "code":
            lang_tag = macro.find("ac:parameter", attrs={"ac:name": "language"}, recursive=False)
            lang = lang_tag.get_text(strip=True) if lang_tag else ""
            body = macro.find("ac:plain-text-body", recursive=False)
            pre = soup.new_tag("pre")
            code = soup.new_tag("code")
            if lang and lang != "none":
                code["class"] = f"language-{lang}"
            code.string = body.get_text() if body else ""
            pre.append(code)
            macro.replace_with(pre)
            return

        if name == "status":
            params = {p.get("ac:name"): p.get_text() for p in macro.find_all("ac:parameter", recursive=False)}
            colour = params.get("colour") or "Grey"
            title = params.get("title", "")
            macro.replace_with(self._token(soup, f"{{{{status:{colour}:{title}}}}}"))
            return

        body = macro.find("ac:rich-text-body", recursive=False)

        if name in ALERT_MACROS and body is not None:
            title_tag = macro.find("ac:parameter", attrs={"ac:name": "title"}, recursive=False)
            title = title_tag.get_text(strip=True) if title_tag else ""
            bq = soup.new_tag("blockquote")
            bq.append(self._block_token(soup, f"[!{ALERT_MACROS[name]}]" + (f" {title}" if title else "")))
            for child in list(body.children):
                bq.append(child.extract())
            macro.replace_with(bq)
            return

        if body is not None:
            # Generic macro with rich body: keep body as Markdown between markers.
            body_children = [c.extract() for c in list(body.children)]
            body.decompose()
            head = str(macro)
            head = head[: head.rfind("</ac:structured-macro>")] + "<ac:rich-text-body>"
            frag = soup.new_tag("div")
            frag.append(self._block_token(soup, f"<!-- confluence-begin: {_comment_escape(head)} -->"))
            for c in body_children:
                frag.append(c)
            frag.append(self._block_token(soup, f"<!-- confluence-end: {name} -->"))
            macro.replace_with(frag)
            return

        # Anything else: keep the original storage XML verbatim in a comment.
        macro.replace_with(self._token(soup, f"<!-- confluence: {_comment_escape(str(macro))} -->"))

    def _convert_link(self, soup: BeautifulSoup, link: Tag) -> None:
        anchor = link.get("ac:anchor", "")
        plain_body = link.find("ac:plain-text-link-body")
        rich_body = link.find("ac:link-body")
        text = plain_body.get_text() if plain_body else ""
        has_rich = rich_body is not None and rich_body.get_text().strip() != ""

        user = link.find("ri:user")
        if user is not None:
            name = text or self._user_name(user.get("ri:userkey", ""))
            key = user.get("ri:userkey") or user.get("ri:username", "")
            link.replace_with(self._token(soup, f"[@{name}](confluence-user:{key})"))
            return

        ri_att = link.find("ri:attachment")
        ri_page = link.find("ri:page")
        ri_space = link.find("ri:space")
        if ri_att is not None:
            href = self._attachment_href(ri_att)
            text = text or ri_att.get("ri:filename", "")
        elif ri_page is not None:
            title = ri_page.get("ri:content-title", "")
            href = self._page_href(title, ri_page.get("ri:space-key"))
            text = text or title
        elif ri_space is not None:
            key = ri_space.get("ri:space-key", "")
            href = f"{self.base_url}/display/{key}"
            text = text or key
        elif link.find("ri:url") is not None:
            href = link.find("ri:url").get("ri:value", "")
            text = text or href
        else:
            href = ""
        if anchor:
            href = f"{href}#{anchor}"
        a = soup.new_tag("a", href=href or "#")
        if has_rich and not plain_body:
            for child in list(rich_body.children):
                a.append(child.extract())
        else:
            a.string = text or href or anchor
        link.replace_with(a)


_ROW_SPLIT = re.compile(r"(?<!\\)\|")


class _MarkdownConverter(MarkdownConverter):

    def escape(self, text, parent_tags=None):
        text = super().escape(text, parent_tags)
        if not text:
            return text
        # Text that would turn into block syntax at the start of a line
        # ("1. ", "- ", "+ ", "# ", "> ") must be escaped to stay a paragraph.
        text = re.sub(r"(^|\n)([ \t]*\d{1,9})([.)])(?=\s|$)", r"\1\2\\\3", text)
        text = re.sub(r"(^|\n)([ \t]*)([-+>#])(?=\s|$)", r"\1\2\\\3", text)
        # Lines of only -, =, *, _ would become <hr> or setext headings.
        text = re.sub(r"(^|\n)([ \t]*)([-=*_])(?=[ \t]*\3[-=*_ \t]*(?:\n|$))", r"\1\2\\\3", text)
        # Literal "<tag ...>" text must not become HTML / autolinks.
        text = re.sub(r"<(?=[A-Za-z/!?])", r"\\<", text)
        return text

    def convert_em(self, el, text, parent_tags=None, **kwargs):
        # "**a***b*" is ambiguous in Markdown; use "_" when touching bold text.
        touching = any(
            isinstance(sib, Tag) and sib.name in ("strong", "b")
            for sib in (el.previous_sibling, el.next_sibling)
        )
        if touching and text.strip():
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            return f"{lead}_{text.strip()}_{trail}"
        return super().convert_em(el, text, parent_tags, **kwargs)

    convert_i = convert_em

    def convert_table(self, el, text, parent_tags=None, **kwargs):
        md = super().convert_table(el, text, parent_tags, **kwargs)
        lines = md.strip("\n").split("\n")
        rows = [ln for ln in lines if ln.startswith("|")]
        if not rows:
            return md
        width = max(len(_ROW_SPLIT.split(r)) - 2 for r in rows)
        fixed = []
        seen_sep = False
        for ln in lines:
            if re.fullmatch(r"\|( *:?-+:? *\|)+", ln):
                if seen_sep:
                    continue  # repeated header row: keep it as a normal row
                seen_sep = True
            if ln.startswith("|"):
                n = len(_ROW_SPLIT.split(ln)) - 2
                if n < width:
                    filler = " --- |" if re.fullmatch(r"\|( *:?-+:? *\|)+", ln) else "  |"
                    ln = ln + filler * (width - n)
            fixed.append(ln)
        return "\n\n" + "\n".join(fixed) + "\n\n"

    def convert_li(self, el, text, parent_tags=None, **kwargs):
        # A table must not start on the bullet line ("- | a |"); put it on
        # its own (indented) line below an empty bullet instead.
        if text and text.lstrip().startswith("|"):
            text = "\n\n" + text.lstrip()
        md = super().convert_li(el, text, parent_tags, **kwargs)
        return re.sub(r"^((?:[-*+]|\d+\.) )[ \t]*\n", r"\1\n", md)

    def convert_pre(self, el, text, parent_tags=None, **kwargs):
        code_el = el.find("code")
        source = code_el if code_el else el
        lang = ""
        if code_el:
            for c in code_el.get("class", []) or []:
                if c.startswith("language-"):
                    lang = c[len("language-"):]
        code_text = source.get_text().rstrip("\n")
        fence = "```"
        while fence in code_text:
            fence += "`"
        return f"\n\n{fence}{lang}\n{code_text}\n{fence}\n\n"

    def convert_img(self, el, text, parent_tags=None, **kwargs):
        alt = (el.attrs.get("alt") or "").replace("]", "\\]")
        src = el.attrs.get("src") or ""
        return f"![{alt}]({_md_url(src)})"

    def convert_a(self, el, text, parent_tags=None, **kwargs):
        href = el.get("href")
        if not href or (parent_tags and "_noformat" in parent_tags):
            return text
        stripped = text.strip()
        if not stripped:
            return ""
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        if stripped.replace(r"\_", "_") == href and re.match(r"https?://\S+$", href):
            return f"{lead}<{href}>{trail}"
        return f"{lead}[{stripped}]({_md_url(href)}){trail}"

    def _cell(self, text: str) -> str:
        # Markdown table cells are single-line: newlines become <br>, the
        # indentation of nested list items is kept so nesting stays visible
        # (and is restored by publish-to-confluence.py).
        text = text.strip()
        text = re.sub(r"[ \t]*\n(?:[ \t]*\n)*([ \t]*)", lambda m: "<br>" + m.group(1), text)
        return re.sub(r"(?<!\\)\|", r"\\|", text)

    def convert_td(self, el, text, parent_tags=None, **kwargs):
        return " " + self._cell(text) + " |"

    def convert_th(self, el, text, parent_tags=None, **kwargs):
        return self.convert_td(el, text, parent_tags, **kwargs)


def confluence_to_markdown(
    storage_html: str,
    *,
    api: ConfluenceApi | None = None,
    space_key: str = "",
    title_to_path: dict[str, PurePosixPath] | None = None,
    source_dir: PurePosixPath = PurePosixPath("."),
    attachment_names: dict[str, str] | None = None,
) -> str:
    conv = StorageConverter(api, space_key, title_to_path, source_dir, attachment_names)
    soup = conv.preprocess(storage_html)
    md = _MarkdownConverter(
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
        table_infer_header=True,
    ).convert_soup(soup)
    for key, value in conv.tokens.items():
        md = md.replace(key, value)
    md = re.sub(r"[ \t]+\n", lambda m: "  \n" if m.group(0).startswith("  ") else "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


# ---------------------------------------------------------------------------
# Extract a single page
# ---------------------------------------------------------------------------

def extract_page(
    api: ConfluenceApi,
    node: PageNode,
    output_dir: Path,
    title_to_path: dict[str, PurePosixPath] | None,
    space_key: str,
    *,
    dry_run: bool = False,
    with_attachments: bool = True,
) -> int:
    page = api.get_page_full(node.id)
    title = page["title"]
    body_html = page["body"]["storage"]["value"]
    page_dir = output_dir / node.rel_dir

    print(section(title))
    print(f"  Page ID     : {node.id}")
    attachments = api.list_attachments(node.id)
    print(f"  Attachments : {len(attachments)}")

    if dry_run:
        print(yellow(f"  DRY RUN     : Would write '{node.rel_md}' + {len(attachments)} file(s)"))
        print()
        return 0

    attachment_names = {a["title"]: safe_filename(a["title"]) for a in attachments}
    md_body = confluence_to_markdown(
        body_html, api=api, space_key=space_key, title_to_path=title_to_path,
        source_dir=node.rel_dir, attachment_names=attachment_names,
    )

    ancestors = page.get("ancestors") or []
    meta = {
        "title": title,
        "confluence_page_id": node.id,
        "space": page["space"]["key"],
        "version": page["version"]["number"],
        "last_modified": page["version"].get("when", ""),
        "parent_id": ancestors[-1]["id"] if ancestors else None,
        "parent_title": ancestors[-1]["title"] if ancestors else None,
        "url": api.page_url(node.id),
    }
    content = render_front_matter(meta) + f"\n# {title}\n\n{md_body}"
    md_path = output_dir / node.rel_md
    write_text(md_path, content)
    print(green(f"  Markdown    : {node.rel_md}"))

    count = 0
    if not with_attachments:
        print()
        return 0
    for att in attachments:
        link = att.get("_links", {}).get("download", "")
        name = attachment_names[att["title"]]
        if not link:
            print(yellow(f"  WARNING     : No download link for '{att['title']}', skipping"))
            continue
        try:
            api.download_attachment(link, page_dir / ATTACH_DIR / name)
            print(green(f"  Attach      : {name}  OK"))
            count += 1
        except Exception as exc:
            print(yellow(f"  WARNING     : {name}  FAILED -- {exc}"))
    print()
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(
        description="Extract Confluence pages to Markdown files with attachments.",
    )
    parser.add_argument("--page", required=True,
                        help="Source page URL (incl. /x/ tiny links) or numeric page ID.")
    parser.add_argument("--output-dir", required=True,
                        help="Directory to write .md files and img/ subdirectories.")
    parser.add_argument("--confluence-url", default=None,
                        help="Base URL of Confluence (only needed for a numeric --page).")
    parser.add_argument("--pat", default=os.environ.get("CONFLUENCE_PAT", ""),
                        help="Personal Access Token (default: $CONFLUENCE_PAT).")
    parser.add_argument("--skip-tls-check", action="store_true",
                        help="Disable TLS certificate validation.")
    parser.add_argument("--tree", action="store_true",
                        help="Extract the page and all descendant pages.")
    parser.add_argument("--flat", action="store_true",
                        help="With --tree, write all pages into one directory.")
    parser.add_argument("--no-attachments", action="store_true",
                        help="Do not download attachments.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List pages that would be extracted without writing files.")
    args = parser.parse_args()

    if not args.pat:
        sys.exit("ERROR: No PAT provided. Use --pat <token> or set the CONFLUENCE_PAT env var.")
    if args.flat and not args.tree:
        sys.exit("ERROR: --flat can only be used together with --tree.")

    output_dir = Path(args.output_dir).resolve()

    if args.skip_tls_check:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    confluence_url, page_id, space_key, title = parse_page_arg(args.page, args.confluence_url)
    api = ConfluenceApi(confluence_url, args.pat, verify_tls=not args.skip_tls_check)

    try:
        if not page_id:
            page_id = api.resolve_page_id_by_title(space_key, title)
        if not space_key:
            space_key = api.resolve_space_key(page_id)

        if args.tree:
            print(cyan("Collecting page tree..."))
            root = collect_page_tree(api, page_id, flat=args.flat)
            nodes = flatten_tree(root)
            title_to_path = {n.title: n.rel_md for n in nodes}
        else:
            t = api.get_page(page_id, "version")["title"]
            nodes = [PageNode(id=page_id, title=t, rel_md=PurePosixPath(f"{safe_filename(t)}.md"))]
            title_to_path = None
    except ApiError as exc:
        sys.exit(f"ERROR: {exc}")

    print()
    print(cyan(f"Confluence  : {confluence_url}"))
    print(cyan(f"Space       : {space_key}"))
    print(cyan(f"Root page   : {page_id}"))
    print(cyan(f"Output dir  : {output_dir}"))
    print(f"Pages       : {len(nodes)}")
    if args.tree:
        print(cyan(f"Mode        : {'Tree (flat)' if args.flat else 'Tree (hierarchical)'}"))
    if args.dry_run:
        print(yellow("Mode        : DRY RUN"))
    print()

    extracted = attachments = errors = 0
    for node in nodes:
        try:
            attachments += extract_page(
                api, node, output_dir, title_to_path, space_key,
                dry_run=args.dry_run, with_attachments=not args.no_attachments,
            )
            extracted += 1
        except Exception as exc:
            print(red(f"  ERROR       : page {node.id} '{node.title}' -- {exc}"))
            errors += 1
            print()

    print(cyan("-" * 37))
    print((green if extracted else yellow)(f"Pages extracted  : {extracted}"))
    print(f"Attachments      : {attachments}")
    print((red if errors else green)(f"Errors           : {errors}"))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
