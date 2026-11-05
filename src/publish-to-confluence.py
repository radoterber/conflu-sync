#!/usr/bin/env python3
"""Publish Markdown files to an on-premise Confluence instance.

Modes
-----
Directory mode (--source-dir + --parent-page):
  Publishes every .md file in --source-dir (with --recursive the whole tree,
  preserving the directory hierarchy as page hierarchy, i.e. the layout
  produced by extract-from-confluence.py --tree).

  All files are converted first (so links and cross-page attachments are
  known), then published parents-first. For each file the target page is
  resolved in this order:
    1. front matter `confluence_page_id` -- only if that page lives in the
       target space (so a tree extracted from space A can safely be published
       as a copy into space B without touching space A),
    2. a page with the same title in the target space (titles are unique
       within a space) -- taken over only if it already sits under the
       expected parent; otherwise CONFLICT (use --adopt to take it over),
    3. otherwise a new page is created under the computed parent.

  With front matter `version` present, the page is only updated when the
  remote version equals it (optimistic locking); otherwise CONFLICT.
  Use --force to overwrite anyway. If a parent page fails, its children are
  skipped (never created at a wrong place).

  Front matter write-back (default, disable with --no-write-back): after a
  page is created/updated, its confluence_page_id, space, version and url are
  written into the file, so later runs do not depend on titles and do not
  report false version conflicts. The file is never re-bound: nothing is
  written when the file belongs to another page/space (e.g. publishing a copy)
  or when --title-prefix is used; nothing is written when the version did not
  change. last_modified is not written (a stale one is removed).

  Attachments: files referenced from a page (img/<file> or any relative
  path) are uploaded to that page; files in img/ of another page of the run
  are uploaded to that page. Only new/changed files are uploaded (SHA-256 is
  stored in the attachment comment). Name collisions and upload failures
  are errors (exit code 1). Remote attachments are never deleted.

Single file mode (--source-file [--page-id | --parent-page]):
  Updates one existing page in place (keeps its Confluence title); the page
  ID comes from --page-id or the file's front matter. Without a page ID (or
  if that page was deleted) and with --parent-page, a new page is created.

Markdown conventions (as produced by extract-from-confluence.py):
  front matter          ---\\ntitle: "..."\\nconfluence_page_id: "123"\\nversion: 7\\n---
  task                  - [ ] text   /   - [x] text
  status macro          {{status:Green:DONE}}
  date                  {{date:2026-02-27}}
  merged table cells    {{span:up}} / {{span:left}}  (merged back to rowspan/colspan)
  user mention          [@Display Name](confluence-user:USERKEY)
  info/note/tip/warning > [!NOTE] optional title
  raw storage macro     <!-- confluence: <ac:structured-macro ...>...</ac:structured-macro> -->
  macro with body       <!-- confluence-begin: ... --> markdown <!-- confluence-end: name -->
  links to other .md    converted to Confluence page links (by title)
  images / links to img/<file>  converted to attachments (and uploaded)

Usage:
  set CONFLUENCE_PAT=your-personal-access-token

  python scripts/publish-to-confluence.py --source-dir docs/cs \\
      --parent-page "https://confluence.jtfg.com/x/75WZEQ" --skip-tls-check

  python scripts/publish-to-confluence.py --source-dir docs/confluence --recursive \\
      --parent-page "https://confluence.jtfg.com/x/75WZEQ" --dry-run --skip-tls-check

  python scripts/publish-to-confluence.py --source-file "docs/confluence/X/X.md" \\
      --skip-tls-check

Dependencies:
  pip install -r scripts/requirements-confluence.txt
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import escape as html_escape, unescape as html_unescape
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mistune  # noqa: E402

from confluence_lib import (  # noqa: E402
    ApiError, ConfluenceApi, cyan, file_sha256, green, long_path, parse_page_arg,
    read_text, red, render_front_matter, section, setup_console,
    split_front_matter, write_text, yellow,
)

ATTACH_DIR = "img"


# ---------------------------------------------------------------------------
# Markdown documents
# ---------------------------------------------------------------------------

@dataclass
class MdDoc:
    path: Path
    meta: dict
    title: str
    body: str
    parent: MdDoc | None = None
    page_id: str | None = None
    children: list[MdDoc] = field(default_factory=list)
    in_run: bool = False                  # part of the current publish run
    storage: str | None = None            # converted storage format
    ctx: "LinkContext | None" = None      # link/attachment info of the conversion
    errors: list[str] = field(default_factory=list)


_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def load_doc(path: Path) -> MdDoc:
    meta, body = split_front_matter(read_text(path))
    title = str(meta.get("title") or "").strip()
    m = _H1_RE.search(body)
    if m and not body[: m.start()].strip():
        h1 = m.group(1).strip()
        title = title or h1
        if h1 == title:
            body = body[m.end():]
    if not title:
        title = path.stem
    return MdDoc(path=path.resolve(), meta=meta, title=title, body=body.lstrip("\n"))


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p).removeprefix("\\\\?\\")))


# ---------------------------------------------------------------------------
# Markdown -> Confluence storage format (XHTML)
# ---------------------------------------------------------------------------

def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _attr(value: str) -> str:
    return html_escape(value, quote=True)


def _comment_unescape(s: str) -> str:
    table = {"%25": "%", "%2D": "-", "%0D": "\r", "%0A": "\n", "%7C": "|"}
    return re.sub(r"%(25|2D|0D|0A|7C)", lambda m: table[m.group(0)], s)


_COMMENT_RAW = re.compile(r"^\s*<!--\s*confluence:\s*(.*?)\s*-->\s*$", re.DOTALL)
_COMMENT_BEGIN = re.compile(r"^\s*<!--\s*confluence-begin:\s*(.*?)\s*-->\s*$", re.DOTALL)
_COMMENT_END = re.compile(r"^\s*<!--\s*confluence-end:\s*([\w-]*)\s*-->\s*$", re.DOTALL)
_BR = re.compile(r"^<br\s*/?>$", re.IGNORECASE)
_HTML_PARTS = re.compile(r"(<!--.*?-->|<br\s*/?>)", re.DOTALL | re.IGNORECASE)
_ALERT = re.compile(r"^\s*<p>\[!(NOTE|INFO|TIP|WARNING|CAUTION|IMPORTANT)\][ \t]*(.*?)(?:</p>|<br />\n|\n)", re.DOTALL)
_ALERT_MACRO = {"NOTE": "note", "INFO": "info", "TIP": "tip", "WARNING": "warning",
                "CAUTION": "warning", "IMPORTANT": "note"}


class ConfluenceRenderer(mistune.HTMLRenderer):
    """mistune renderer emitting Confluence storage format."""

    def __init__(self, ctx: "LinkContext"):
        super().__init__(escape=True)
        self.ctx = ctx

    # -- raw HTML ----------------------------------------------------------

    def _html(self, html: str) -> str:
        out: list[str] = []
        for part in _HTML_PARTS.split(html):
            if not part:
                continue
            if part.startswith("<!--"):
                for rx, fn in (
                    (_COMMENT_RAW, lambda m: _comment_unescape(m.group(1))),
                    (_COMMENT_BEGIN, lambda m: _comment_unescape(m.group(1))),
                    (_COMMENT_END, lambda m: "</ac:rich-text-body></ac:structured-macro>"),
                ):
                    m = rx.match(part)
                    if m:
                        out.append(fn(m))
                        break
                # ordinary comments are dropped
            elif _BR.match(part):
                out.append("<br/>")
            elif part.strip():
                out.append(html_escape(part, quote=False))
            else:
                out.append(part)
        return "".join(out)

    def inline_html(self, html: str) -> str:
        return self._html(html)

    def block_html(self, html: str) -> str:
        out = self._html(html.strip())
        if not out.lstrip().startswith("<"):
            return f"<p>{out}</p>\n"
        return out + "\n"

    # -- blocks ------------------------------------------------------------

    def block_code(self, code: str, info: str | None = None, **attrs) -> str:
        lang = (info or "").strip().split()[0] if info and info.strip() else ""
        param = f'<ac:parameter ac:name="language">{_attr(lang)}</ac:parameter>' if lang else ""
        return (
            '<ac:structured-macro ac:name="code">' + param
            + f"<ac:plain-text-body>{_cdata(code.rstrip(chr(10)))}</ac:plain-text-body>"
            + "</ac:structured-macro>\n"
        )

    def block_quote(self, text: str) -> str:
        m = _ALERT.match(text)
        if not m:
            return super().block_quote(text)
        name = _ALERT_MACRO[m.group(1)]
        title = m.group(2).strip()
        body = text[m.end():].strip() or "<p></p>"
        param = f'<ac:parameter ac:name="title">{title}</ac:parameter>' if title else ""
        return (f'<ac:structured-macro ac:name="{name}">{param}'
                f"<ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>\n")

    def list(self, text: str, ordered: bool, **attrs) -> str:
        if text.lstrip().startswith("<ac:task>"):
            return f"<ac:task-list>\n{text}</ac:task-list>\n"
        return super().list(text, ordered, **attrs)

    def task_list_item(self, text: str, checked: bool = False) -> str:
        body = text.strip()
        m = re.match(r"^<p>(.*?)</p>(.*)$", body, re.DOTALL)
        if m and "<p>" not in m.group(1):
            body = m.group(1) + m.group(2)
        status = "complete" if checked else "incomplete"
        return (f"<ac:task><ac:task-status>{status}</ac:task-status>"
                f"<ac:task-body>{body}</ac:task-body></ac:task>\n")

    def thematic_break(self) -> str:
        return "<hr/>\n"

    def linebreak(self) -> str:
        return "<br/>\n"

    # -- inline ------------------------------------------------------------

    def image(self, text: str, url: str, title: str | None = None) -> str:
        url = html_unescape(url)
        alt = f' ac:alt="{_attr(html_unescape(text))}"' if text else ""
        ttl = f' ac:title="{_attr(title)}"' if title else ""
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
            return f'<ac:image{alt}{ttl}><ri:url ri:value="{_attr(url)}"/></ac:image>'
        return f"<ac:image{alt}{ttl}>{self.ctx.attachment_ri(url)}</ac:image>"

    def link(self, text: str, url: str, title: str | None = None) -> str:
        url = html_unescape(url)
        if url.startswith("confluence-user:"):
            key = url[len("confluence-user:"):]
            return f'<ac:link><ri:user ri:userkey="{_attr(key)}"/></ac:link>'
        if url.startswith("#"):
            return (f'<ac:link ac:anchor="{_attr(unquote(url[1:]))}">'
                    f"<ac:link-body>{text}</ac:link-body></ac:link>")
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
            return super().link(text, url, title)
        ri, anchor = self.ctx.local_link(url)
        if ri is None:
            return text  # unresolvable local link: keep the text only
        anc = f' ac:anchor="{_attr(anchor)}"' if anchor else ""
        return f"<ac:link{anc}>{ri}<ac:link-body>{text}</ac:link-body></ac:link>"


_INLINE_TOKEN = re.compile(r"\{\{(status|date):([^}]*)\}\}")


def _inline_tokens(html: str) -> str:
    def repl(m: re.Match) -> str:
        kind, value = m.group(1), html_unescape(m.group(2))
        if kind == "date":
            return f'<time datetime="{_attr(value)}"/>'
        colour, _, title = value.partition(":")
        return ('<ac:structured-macro ac:name="status">'
                f'<ac:parameter ac:name="colour">{_attr(colour or "Grey")}</ac:parameter>'
                f'<ac:parameter ac:name="title">{_attr(title)}</ac:parameter>'
                "</ac:structured-macro>")
    # Do not touch CDATA sections (code blocks).
    parts = re.split(r"(<!\[CDATA\[.*?\]\]>)", html, flags=re.DOTALL)
    return "".join(p if p.startswith("<![CDATA[") else _INLINE_TOKEN.sub(repl, p) for p in parts)


_CELL = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL)
_LIST_MARK = "\ue000"   # private-use chars marking list items inside table cells
_HR_MARK = "\ue001"
_TEXT_MARK = "\ue002"   # + indentation char: non-list line inside a cell
_TEXT_LINE = re.compile(_TEXT_MARK + r"([\ue100-\ue1ff])(.*)$", re.DOTALL)
_INDENT_BASE = 0xE100   # + indentation width, follows _LIST_MARK
_CELL_ITEM = re.compile(_LIST_MARK + r"([\ue100-\ue1ff])(?:(-|\*|\+)|(\d{1,9})[.)])\s+(.*)$", re.DOTALL)
_CELL_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$", re.DOTALL)
_MD_CELL_ITEM = re.compile(r"^([ \t]*)((?:[-*+]|\d{1,9}[.)])[ \t]+)")


def _mark_cell_lists(md_text: str) -> str:
    """Mark list items inside Markdown table cells ('- a<br>- b').

    Done on the Markdown source, where escaped text ("15\\. 11.") can still be
    told apart from real list markers; the marker survives mistune and is
    turned into a list by _cell_lists().
    """
    out = []
    in_code = False
    for line in md_text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        if in_code or not line.lstrip().startswith("|"):
            out.append(line)
            continue
        cells = re.split(r"(?<!\\)(\|)", line)
        for i, cell in enumerate(cells):
            if cell == "|":
                continue
            segs = re.split(r"(<br\s*/?>)", cell, flags=re.IGNORECASE)
            for j in range(0, len(segs), 2):
                seg = segs[j]
                m = _MD_CELL_ITEM.match(seg)
                if m:
                    indent = len(m.group(1).expandtabs(4)) if j > 0 else 0
                    segs[j] = _LIST_MARK + chr(_INDENT_BASE + min(indent, 255)) + seg.lstrip(" \t")
                elif j > 0 and seg.strip() in ("---", "***", "___"):
                    segs[j] = _HR_MARK
                elif j > 0:
                    indent = len(seg[: len(seg) - len(seg.lstrip(" \t"))].expandtabs(4))
                    segs[j] = _TEXT_MARK + chr(_INDENT_BASE + min(indent, 255)) + seg.lstrip(" \t")
            cells[i] = "".join(segs)
        out.append("".join(cells))
    return "\n".join(out)


def _cell_lists(content: str) -> str:
    """Markdown tables cannot hold lists; extract writes them as '- a<br>- b'.

    Turn marked <br/>-separated lines back into <ul>/<ol>/<ac:task-list>.
    """
    if _LIST_MARK not in content and _HR_MARK not in content:
        return re.sub(_TEXT_MARK + "[\ue100-\ue1ff]", "", content)
    lines = re.split(r"<br\s*/?>\n?", content)

    out: list[str] = []
    # stack of open lists: (indent, kind)
    stack: list[tuple[int, str]] = []
    open_li = []   # per stack level: whether an <li>/<ac:task> is open

    def close_to(level: int) -> None:
        while len(stack) > level:
            _, kind = stack.pop()
            if open_li.pop():
                out.append("</ac:task-body></ac:task>" if kind == "task" else "</li>")
            out.append("</ac:task-list>" if kind == "task" else f"</{kind}>")

    plain: list[str] = []

    def flush_plain() -> None:
        if plain:
            out.append("<br/>".join(plain))
            plain.clear()

    for line in lines:
        s = line.strip()
        if s == _HR_MARK:
            close_to(0)
            flush_plain()
            out.append("<hr/>")
            continue
        m = _CELL_ITEM.match(s)
        if not m:
            tm = _TEXT_LINE.match(s)
            indent = ord(tm.group(1)) - _INDENT_BASE if tm else 0
            text = tm.group(2) if tm else s
            # Markdown rule: a continuation line belongs to the list item only
            # when indented at least past the item's marker ("- " = 2).
            if stack and text.strip() and indent >= stack[-1][0] + 2:
                out.append("<br/>" + text.strip())
            elif text.strip() or not stack:
                close_to(0)
                plain.append(text)
            continue
        flush_plain()
        indent = ord(m.group(1)) - _INDENT_BASE
        text = m.group(4)
        t = _CELL_TASK.match(text) if m.group(2) else None
        kind = "task" if t else ("ul" if m.group(2) else "ol")
        while stack and indent < stack[-1][0]:
            close_to(len(stack) - 1)
        if stack and indent == stack[-1][0] and kind != stack[-1][1]:
            close_to(len(stack) - 1)
        if not stack or indent > stack[-1][0]:
            if kind == "task":
                out.append("<ac:task-list>")
            else:
                out.append(f"<{kind}>")
            stack.append((indent, kind))
            open_li.append(False)
        elif open_li[-1]:
            out.append("</ac:task-body></ac:task>" if stack[-1][1] == "task" else "</li>")
        if t:
            status = "incomplete" if t.group(1) == " " else "complete"
            out.append(f"<ac:task><ac:task-status>{status}</ac:task-status><ac:task-body>{t.group(2)}")
        else:
            out.append(f"<li>{text}")
        open_li[-1] = True
    close_to(0)
    flush_plain()
    return "".join(out)


_SPAN_UP = "{{span:up}}"
_SPAN_LEFT = "{{span:left}}"
_TABLE = re.compile(r"<table>.*?</table>", re.DOTALL)
_ROW = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_CELL_FULL = re.compile(r"[ \t]*<(t[dh])([^>]*)>(.*?)</\1>\n?", re.DOTALL)


def _merge_spans(table: str) -> str:
    """Turn {{span:up}} / {{span:left}} placeholder cells into rowspan/colspan."""
    if _SPAN_UP not in table and _SPAN_LEFT not in table:
        return table
    grid: list[list[list]] = []   # rows of [tag, attrs, content, rowspan, colspan, keep]
    for rm in _ROW.finditer(table):
        grid.append([[m.group(1), m.group(2), m.group(3), 1, 1, True]
                     for m in _CELL_FULL.finditer(rm.group(1))])
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            content = cell[2].strip()
            if content == _SPAN_LEFT:
                k = c - 1
                while k >= 0 and not row[k][5]:
                    k -= 1
                if k >= 0:
                    row[k][4] += 1
                    cell[5] = False
            elif content == _SPAN_UP:
                k = r - 1
                while k >= 0 and (c >= len(grid[k]) or not grid[k][c][5]):
                    k -= 1
                if k >= 0:
                    grid[k][c][3] += 1
                    cell[5] = False
    rows_out = []
    for row in grid:
        cells = []
        for tag, attrs, content, rs, cs, keep in row:
            if not keep:
                continue
            extra = (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
            cells.append(f"  <{tag}{attrs}{extra}>{content}</{tag}>\n")
        rows_out.append("<tr>\n" + "".join(cells) + "</tr>\n")
    return "<table>\n<tbody>\n" + "".join(rows_out) + "</tbody>\n</table>"


def _fix_tables(html: str) -> str:
    # Confluence keeps header rows as <th> inside a single <tbody>.
    html = re.sub(r"</?(thead|tbody)>\n?", "", html)
    html = html.replace("<table>\n", "<table>\n<tbody>\n").replace("</table>", "</tbody>\n</table>")
    html = _CELL.sub(lambda m: m.group(1) + _cell_lists(m.group(2)) + m.group(3), html)
    html = _TABLE.sub(lambda m: _merge_spans(m.group(0)), html)
    return html


_TOC_LINE = re.compile(r"^[ \t]*-\s+\[.*\]\(#.*\)\s*$")
_TOC_MACRO = '<ac:structured-macro ac:name="toc"><ac:parameter ac:name="maxLevel">3</ac:parameter></ac:structured-macro>'


def _replace_toc(md_text: str) -> str:
    """Replace a hand-written Markdown TOC (list of #anchor links) by a TOC macro."""
    lines = md_text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _TOC_LINE.match(lines[i]) and (i + 1 < len(lines) and _TOC_LINE.match(lines[i + 1])):
            while i < len(lines) and _TOC_LINE.match(lines[i]):
                i += 1
            out.extend(["", f"<!-- confluence: {_TOC_MACRO} -->", ""])
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def markdown_to_confluence(md_text: str, ctx: "LinkContext | None" = None) -> str:
    ctx = ctx or LinkContext(None, {}, "")
    renderer = ConfluenceRenderer(ctx)
    md = mistune.create_markdown(
        renderer=renderer,
        plugins=["table", "strikethrough", "task_lists"],
    )
    # mistune's table plugin only registers tables for top-level blocks;
    # enable them inside list items and block quotes too.
    for rules in (md.block.list_rules, md.block.block_quote_rules):
        for name in ("nptable", "table"):
            if name not in rules:
                rules.insert(0, name)
    html = md(_mark_cell_lists(_replace_toc(md_text)))
    html = _inline_tokens(html)
    html = _fix_tables(html)
    html = re.sub("[" + _LIST_MARK + _TEXT_MARK + "][\ue100-\ue1ff]?|" + _HR_MARK, "", html)
    # (Re)number tasks so every <ac:task> has a unique id within the page.
    html = re.sub(r"<ac:task-id>\d+</ac:task-id>", "", html)
    counter = iter(range(1, 1_000_000))
    html = re.sub(r"<ac:task>", lambda m: f"<ac:task><ac:task-id>{next(counter)}</ac:task-id>", html)
    return html


_XML_WRAP = ('<!DOCTYPE r [<!ENTITY nbsp "&#160;">]>'
             '<r xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource">{}</r>')


_VOLATILE_ATTRS = {"macro-id", "schema-version", "local-id", "data-uuid", "uuid"}


def _canonical_storage(xhtml: str) -> str | None:
    """Canonical form of storage XML for change detection (None if unparsable)."""
    try:
        root = ET.fromstring(_XML_WRAP.format(xhtml))
    except ET.ParseError:
        return None
    for el in root.iter():
        # attributes Confluence adds/changes on save; attribute order is irrelevant
        attrs = {k: v for k, v in el.attrib.items()
                 if k.rsplit("}", 1)[-1] not in _VOLATILE_ATTRS}
        el.attrib.clear()
        el.attrib.update(sorted(attrs.items()))
        if el.text:
            el.text = re.sub(r"\s+", " ", el.text)
        if el.tail:
            el.tail = re.sub(r"\s+", " ", el.tail)
        if el.tag.endswith("}task-id"):
            el.text = ""          # task ids are renumbered by Confluence
    s = ET.tostring(root, encoding="unicode")
    return re.sub(r">\s+<", "><", s).strip()


def storage_equal(a: str, b: str) -> bool:
    ca, cb = _canonical_storage(a), _canonical_storage(b)
    return ca is not None and ca == cb


def validate_storage(xhtml: str) -> str | None:
    """Return None when the storage format is well-formed XML, else an error."""
    try:
        ET.fromstring(_XML_WRAP.format(xhtml))
        return None
    except ET.ParseError as exc:
        line, col = exc.position
        lines = xhtml.split("\n")
        snippet = lines[line - 1][max(0, col - 60): col + 40] if 0 < line <= len(lines) else ""
        return f"{exc} near: {snippet!r}"


# ---------------------------------------------------------------------------
# Local links / attachments
# ---------------------------------------------------------------------------

class LinkContext:
    """Resolves relative links of one Markdown file."""

    def __init__(self, doc: MdDoc | None, docs_by_path: dict[str, MdDoc], title_prefix: str):
        self.doc = doc
        self.docs_by_path = docs_by_path
        self.title_prefix = title_prefix
        self.attachments: dict[str, Path] = {}   # filename -> local file of THIS page
        self.foreign: list[tuple[MdDoc, str, Path]] = []  # attachments owned by other pages
        self.missing: list[str] = []
        self.errors: list[str] = []

    def add_attachment(self, name: str, path: Path) -> None:
        prev = self.attachments.get(name)
        if prev is not None and _norm(prev) != _norm(path):
            self.errors.append(
                f"attachment name collision '{name}': {prev} vs {path}")
            return
        self.attachments[name] = path

    def _resolve(self, url: str) -> tuple[Path | None, str]:
        path, _, anchor = url.partition("#")
        if not self.doc or not path:
            return None, anchor
        return Path(os.path.normpath(self.doc.path.parent / unquote(path))), unquote(anchor)

    def _doc_for(self, target: Path) -> MdDoc | None:
        key = _norm(target)
        if key in self.docs_by_path:
            return self.docs_by_path[key]
        if target.suffix.lower() == ".md" and long_path(target).is_file():
            doc = load_doc(target)
            self.docs_by_path[key] = doc
            return doc
        return None

    def page_ri(self, doc: MdDoc) -> str:
        return f'<ri:page ri:content-title="{_attr(self.title_prefix + doc.title)}"/>'

    def attachment_ri(self, url: str) -> str:
        target, _ = self._resolve(url)
        filename = unquote(url.split("#")[0]).replace("\\", "/").rsplit("/", 1)[-1]
        if target is None:
            return f'<ri:attachment ri:filename="{_attr(filename)}"/>'
        exists = long_path(target).is_file()
        if target.parent.name == ATTACH_DIR and _norm(target.parent.parent) == _norm(self.doc.path.parent):
            if exists:
                self.add_attachment(filename, target)
            else:
                self.missing.append(url)
            return f'<ri:attachment ri:filename="{_attr(filename)}"/>'
        if target.parent.name == ATTACH_DIR:
            # attachment of another page: <dir>/img/<file>, owner page <dir>/<dir>.md
            owner = self._owner_doc(target.parent.parent)
            if owner and owner is not self.doc:
                if exists:
                    # uploaded to the owner page (if published in this run)
                    self.foreign.append((owner, filename, target))
                else:
                    self.missing.append(url)
                return (f'<ri:attachment ri:filename="{_attr(filename)}">'
                        f"{self.page_ri(owner)}</ri:attachment>")
        if exists:
            self.add_attachment(filename, target)
        else:
            self.missing.append(url)
        return f'<ri:attachment ri:filename="{_attr(filename)}"/>'

    def _owner_doc(self, directory: Path) -> MdDoc | None:
        candidates = [d for k, d in self.docs_by_path.items() if _norm(d.path.parent) == _norm(directory)]
        for d in candidates:
            if d.path.stem == directory.name:
                return d
        if len(candidates) == 1:
            return candidates[0]
        own = directory / f"{directory.name}.md"
        return self._doc_for(own) if long_path(own).is_file() else None

    def local_link(self, url: str) -> tuple[str | None, str]:
        target, anchor = self._resolve(url)
        if target is None:
            return None, anchor
        if target.suffix.lower() == ".md":
            doc = self._doc_for(target)
            if doc:
                return self.page_ri(doc), anchor
            self.missing.append(url)
            return None, anchor
        return self.attachment_ri(url), anchor

    def raw_attachment_refs(self, body: str) -> None:
        """Register attachments referenced from raw storage comments (e.g. view-file)."""
        if not self.doc:
            return
        for m in re.finditer(r"<!--\s*confluence(?:-begin)?:(.*?)-->", body, re.DOTALL):
            xml = _comment_unescape(m.group(1))
            for a in re.finditer(r'<ri:attachment ri:filename="([^"]+)"\s*(/>|></ri:attachment>)', xml):
                name = html_unescape(a.group(1))
                local = self.doc.path.parent / ATTACH_DIR / name
                if long_path(local).is_file():
                    self.add_attachment(name, local)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

@dataclass
class Options:
    space_key: str
    parent_id: str
    title_prefix: str = ""
    dry_run: bool = False
    force: bool = False
    adopt: bool = False
    write_back: bool = True
    attachments: str = "referenced"   # referenced | all | none
    move: bool = False


def build_tree(source_dir: Path, recursive: bool) -> list[MdDoc]:
    """Load docs and compute parents. Returns docs ordered parents-first."""
    files = sorted(long_path(source_dir).rglob("*.md") if recursive else long_path(source_dir).glob("*.md"))
    docs = [load_doc(f) for f in files]
    root = _norm(source_dir)
    by_dir: dict[str, list[MdDoc]] = {}
    for d in docs:
        d.in_run = True
        by_dir.setdefault(_norm(d.path.parent), []).append(d)

    def owner(dir_key: str, dir_name: str) -> MdDoc | None:
        cands = by_dir.get(dir_key, [])
        for d in cands:
            if d.path.stem == dir_name:
                return d
        if dir_key == root and len(cands) == 1 and recursive:
            return cands[0]
        return None

    for d in docs:
        dkey = _norm(d.path.parent)
        own = owner(dkey, d.path.parent.name)
        if own is d or own is None:
            # owner page of its directory (or no owner): parent is owner of an ancestor dir
            p = d.path.parent
            parent = None
            while _norm(p) != root and _norm(p.parent) != _norm(p):
                p = p.parent
                parent = owner(_norm(p), p.name)
                if parent:
                    break
                if _norm(p) == root:
                    break
            d.parent = parent if parent is not d else None
        else:
            d.parent = own
        if d.parent:
            d.parent.children.append(d)

    ordered: list[MdDoc] = []

    def visit(d: MdDoc) -> None:
        ordered.append(d)
        for c in d.children:
            visit(c)

    for d in docs:
        if d.parent is None:
            visit(d)
    return ordered


# -- attachments -------------------------------------------------------------

_HASH_RE = re.compile(r"sha256:([0-9a-f]{64})")


@dataclass
class AttachmentPlan:
    name: str
    path: Path
    status: str            # new | changed | unchanged
    existing: dict | None
    sha: str


def plan_attachments(api: ConfluenceApi | None, page_id: str | None,
                     files: dict[str, Path]) -> list[AttachmentPlan]:
    """Compare local files with the page's attachments (by hash, fallback size+content)."""
    remote: dict[str, dict] = {}
    if api and page_id and page_id.isdigit():
        remote = {a["title"]: a for a in api.list_attachments(page_id)}
    plans = []
    for name, path in files.items():
        sha = file_sha256(path)
        att = remote.get(name)
        if att is None:
            plans.append(AttachmentPlan(name, path, "new", None, sha))
            continue
        ext = att.get("extensions") or {}
        m = _HASH_RE.search(ext.get("comment") or "")
        if m:
            same = m.group(1) == sha
        elif int(ext.get("fileSize") or -1) != long_path(path).stat().st_size:
            same = False
        else:
            # same size, no stored hash (e.g. uploaded manually): compare content
            import hashlib
            link = (att.get("_links") or {}).get("download", "")
            same = bool(link) and hashlib.sha256(api.download_bytes(link)).hexdigest() == sha
        plans.append(AttachmentPlan(name, path, "unchanged" if same else "changed", att, sha))
    return plans


def upload_planned(api: ConfluenceApi, page_id: str, plans: list[AttachmentPlan],
                   indent: str = "  ") -> tuple[int, int]:
    """Upload new/changed attachments. Returns (uploaded, failed)."""
    ok = failed = 0
    for pl in plans:
        if pl.status == "unchanged":
            print(f"{indent}Attach   : {pl.name}  unchanged (skipped)")
            continue
        try:
            api.upload_attachment(page_id, pl.path, pl.name,
                                  existing=pl.existing, comment=f"sha256:{pl.sha}")
            print(green(f"{indent}Attach   : {pl.name}  {pl.status.upper()} OK"))
            ok += 1
        except Exception as exc:
            print(red(f"{indent}ERROR    : Attach {pl.name} FAILED -- {exc}"))
            failed += 1
    return ok, failed


def describe_plans(plans: list[AttachmentPlan], indent: str = "  ") -> None:
    for pl in plans:
        print(yellow(f"{indent}WhatIf   : attachment {pl.name}: {pl.status}"))


# -- pages -------------------------------------------------------------------

def convert_doc(doc: MdDoc, docs_by_path: dict[str, MdDoc], opt: Options) -> None:
    ctx = LinkContext(doc, docs_by_path, opt.title_prefix)
    doc.storage = markdown_to_confluence(doc.body, ctx)
    ctx.raw_attachment_refs(doc.body)
    if opt.attachments == "all":
        img_dir = doc.path.parent / ATTACH_DIR
        if long_path(img_dir).is_dir():
            for f in sorted(long_path(img_dir).iterdir()):
                if f.is_file():
                    ctx.add_attachment(f.name, f)
    err = validate_storage(doc.storage)
    if err:
        ctx.errors.append(f"generated storage format is not valid XML -- {err}")
    doc.ctx = ctx


def collect_foreign(docs: list[MdDoc]) -> None:
    """Move attachments referenced from other pages to their owner page's upload list."""
    for doc in docs:
        if not doc.ctx:
            continue
        for owner, name, path in doc.ctx.foreign:
            if owner.in_run and owner.ctx is not None:
                owner.ctx.add_attachment(name, path)
            else:
                doc.ctx.missing.append(
                    f"{path} (attachment of page '{owner.title}' which is not part of this run "
                    "- not uploaded)")


def publish_doc(api: ConfluenceApi, doc: MdDoc, opt: Options) -> tuple[str, int]:
    """Publish one (already converted) document. Returns (status, attachments_uploaded)."""
    title = opt.title_prefix + doc.title
    print(section(doc.path.name))
    print(f"  Title    : {title}")

    ctx = doc.ctx
    assert ctx is not None and doc.storage is not None

    # A failed/skipped parent: never publish children to a wrong place.
    if doc.parent is not None and doc.parent.in_run and not doc.parent.page_id:
        print(red(f"  ERROR    : parent '{doc.parent.title}' was not published - skipped"))
        print()
        return "error", 0
    parent_id = doc.parent.page_id if doc.parent and doc.parent.page_id else opt.parent_id
    if parent_id.startswith("(new:"):
        parent_id_display = parent_id
    else:
        parent_id_display = f"id={parent_id}"

    for miss in dict.fromkeys(ctx.missing):
        print(yellow(f"  WARNING  : link target not found locally: {miss}"))
    if ctx.errors:
        for e in ctx.errors:
            print(red(f"  ERROR    : {e}"))
        print()
        return "error", 0

    # -- resolve target page ------------------------------------------------
    existing = None
    meta_id = str(doc.meta.get("confluence_page_id") or "")
    if meta_id and doc.meta.get("space") in (None, opt.space_key):
        try:
            page = api.get_page(meta_id, "version,space,ancestors,body.storage")
            if page["space"]["key"] == opt.space_key:
                existing = page
        except ApiError as exc:
            if exc.status != 404:
                raise
            print(yellow(f"  NOTE     : page id={meta_id} from front matter no longer exists"))
    by_title = False
    if existing is None:
        existing = api.find_page(opt.space_key, title, "version,space,ancestors,body.storage")
        by_title = existing is not None

    move_to = None
    unchanged = False
    if existing:
        remote_ver = existing["version"]["number"]
        anc = existing.get("ancestors") or []
        cur_parent = anc[-1]["id"] if anc else None
        if by_title:
            # Found only by title: it may be someone else's page with the same
            # title. Only take it over when it sits under the expected parent.
            if cur_parent != parent_id and not (opt.adopt or opt.force):
                print(red(f"  CONFLICT : a page titled '{title}' already exists elsewhere in the space "
                          f"(id={existing['id']}, parent id={cur_parent}); the local file has no "
                          "confluence_page_id. Rename the file/title, or use --adopt to take it over."))
                print()
                return "conflict", 0
        else:
            local_ver = doc.meta.get("version")
            if local_ver is not None and int(local_ver) != remote_ver and not opt.force:
                print(red(f"  CONFLICT : page id={existing['id']} is at v{remote_ver}, "
                          f"local file is based on v{local_ver} (use --force to overwrite)"))
                doc.page_id = existing["id"]
                print()
                return "conflict", 0
        if cur_parent != parent_id:
            if opt.move:
                move_to = parent_id
            else:
                print(yellow(f"  NOTE     : page is located under parent id={cur_parent}, not under "
                             f"{parent_id_display}; updating in place (use --move to re-parent)"))
        remote_body = ((existing.get("body") or {}).get("storage") or {}).get("value", "")
        unchanged = (not move_to and existing.get("title") == title
                     and storage_equal(remote_body, doc.storage))
        if unchanged:
            action = f"keep id={existing['id']} v{remote_ver} (content unchanged)"
        else:
            action = f"UPDATE id={existing['id']} v{remote_ver}->v{remote_ver + 1}"
    else:
        action = f"CREATE under parent {parent_id_display}"

    uploads = {} if opt.attachments == "none" else ctx.attachments

    if opt.dry_run:
        plans = plan_attachments(api, existing["id"] if existing else None, uploads)
        print(yellow(f"  WhatIf   : Would {action}"))
        describe_plans(plans)
        doc.page_id = existing["id"] if existing else f"(new:{title})"
        print()
        return "ok", 0

    if existing and unchanged:
        result = existing
        print(f"  Page     : UNCHANGED id={existing['id']} v{existing['version']['number']} (skipped)")
    elif existing:
        result = api.update_page(existing["id"], title, doc.storage, existing["version"]["number"],
                                 parent_id=move_to)
        new_ver = result["version"]["number"]
        if new_ver == existing["version"]["number"]:
            msg = f"UNCHANGED id={existing['id']} v{new_ver} (content identical)"
        else:
            msg = f"UPDATED id={existing['id']} v{existing['version']['number']}->v{new_ver}"
        print(green(f"  Page     : {msg}" + (f" (moved under id={move_to})" if move_to else "")))
    else:
        result = api.create_page(opt.space_key, parent_id, title, doc.storage)
        print(green(f"  Page     : CREATED id={result['id']}"))
    doc.page_id = result["id"]

    plans = plan_attachments(api, doc.page_id, uploads)
    count, failed = upload_planned(api, doc.page_id, plans)

    write_front_matter(doc, api, opt.space_key, result, parent_id,
                       enabled=opt.write_back, title_prefix=opt.title_prefix)
    print()
    return ("error" if failed else "ok"), count


def write_front_matter(doc: MdDoc, api: ConfluenceApi, space_key: str, result: dict,
                       parent_id: str | None, *, enabled: bool, title_prefix: str = "",
                       indent: str = "  ") -> None:
    """Write Confluence metadata back into the file's front matter.

    Safety rules (the file must never be re-bound to a different page):
      * disabled (--no-write-back) or --title-prefix (test copy) -> nothing,
      * file already bound to another page/space -> nothing (note only),
      * file bound to this page and version unchanged -> nothing (no git noise),
      * otherwise id/space/version/url (+ parent for new files) are written.
    `last_modified` (written by extract) is not updated; it is removed when the
    version changes, because it would be stale.
    """
    label = f"{indent}Front matter"
    if not enabled:
        return
    if title_prefix:
        print(f"{label}: not updated (--title-prefix: publishing a copy)")
        return
    meta_id = str(doc.meta.get("confluence_page_id") or "")
    meta_space = doc.meta.get("space")
    page_id = str(result["id"])
    version = result["version"]["number"]
    if meta_id and (meta_id != page_id or (meta_space and meta_space != space_key)):
        print(f"{label}: not updated (file belongs to page id={meta_id} in space "
              f"{meta_space or '?'}, published page is id={page_id} in {space_key})")
        return
    if meta_id == page_id and doc.meta.get("version") == version:
        return  # already up to date - do not touch the file
    meta = dict(doc.meta)
    meta.pop("last_modified", None)
    meta.update({
        "title": doc.title,
        "confluence_page_id": page_id,
        "space": space_key,
        "version": version,
        "url": api.page_url(page_id),
    })
    if parent_id and not meta_id:
        meta["parent_id"] = parent_id
    write_text(doc.path, render_front_matter(meta) + f"\n# {doc.title}\n\n{doc.body}")
    doc.meta = meta
    print(green(f"{label}: updated (id={page_id}, v{version})"))


def extract_leading_banner(existing_storage: str) -> str:
    """Return the storage prefix that precedes the first <h1> (e.g. a warning banner)."""
    m = re.search(r"<h1[ >]", existing_storage, re.IGNORECASE)
    return existing_storage[: m.start()].strip() if m else ""


def publish_single(args, api: ConfluenceApi, page_id: str | None, md_path: Path,
                   parent_page: tuple[str, str] | None) -> int:
    """Single-file mode. Update page_id, or create under parent_page=(id, space)
    when page_id is None. Returns the process exit code."""
    doc = load_doc(md_path)
    opt = Options(space_key="", parent_id="", attachments=args.attachments,
                  title_prefix=args.title_prefix)
    convert_doc(doc, {_norm(doc.path): doc}, opt)
    ctx = doc.ctx
    for owner, name, path in ctx.foreign:
        ctx.missing.append(f"{path} (attachment of page '{owner.title}' - not uploaded)")
    uploads = {} if args.attachments == "none" else ctx.attachments

    print()
    print(cyan(f"Confluence  : {api.base_url}"))
    print(cyan(f"Source file : {md_path}"))
    for miss in dict.fromkeys(ctx.missing):
        print(yellow(f"WARNING     : link target not found locally: {miss}"))
    if ctx.errors:
        for e in ctx.errors:
            print(red(f"ERROR       : {e}"))
        return 1

    if page_id is None:
        parent_id, space_key = parent_page
        title = args.title_prefix + doc.title
        clash = api.find_page(space_key, title, "version,ancestors")
        if clash:
            print(red(f"CONFLICT    : a page titled '{title}' already exists (id={clash['id']}). "
                      "Use --page-id to update it."))
            return 1
        print(cyan(f"Space       : {space_key}"))
        print(cyan(f"Parent page : {parent_id}"))
        if args.dry_run:
            print(yellow(f"WhatIf      : Would CREATE page '{title}' under parent id={parent_id}"))
            describe_plans(plan_attachments(None, None, uploads), indent="")
            return 0
        result = api.create_page(space_key, parent_id, title, doc.storage)
        print(green(f"Page        : CREATED id={result['id']}"))
        _, failed = upload_planned(api, result["id"], plan_attachments(api, result["id"], uploads), "")
        write_front_matter(doc, api, space_key, result, parent_id, enabled=args.write_back,
                           title_prefix=args.title_prefix, indent="")
        return 1 if failed else 0

    page = api.get_page_full(page_id)
    page_title = page["title"]
    version = page["version"]["number"]
    space_key = page["space"]["key"]
    local_ver = doc.meta.get("version")
    if (str(doc.meta.get("confluence_page_id") or "") == page_id and local_ver is not None
            and int(local_ver) != version and not args.force):
        print(red(f"CONFLICT    : page {page_id} is at v{version}, local file is based on "
                  f"v{local_ver}. Use --force to overwrite."))
        return 1

    storage = doc.storage
    banner = extract_leading_banner(page["body"]["storage"]["value"]) if args.preserve_banner else ""
    if banner:
        storage = banner + "\n" + storage
        err = validate_storage(storage)
        if err:
            print(red(f"ERROR       : storage format with banner is not valid XML -- {err}"))
            return 1

    print(cyan(f"Space       : {space_key}"))
    print(cyan(f"Page        : {page_id}  '{page_title}'  (v{version})"))
    if args.preserve_banner:
        print(f"Banner      : {'preserved' if banner else 'none found'}")
    if args.dry_run:
        print(yellow(f"WhatIf      : Would UPDATE page {page_id} -> v{version + 1}"))
        describe_plans(plan_attachments(api, page_id, uploads), indent="")
        return 0

    if storage_equal(page["body"]["storage"]["value"], storage):
        result = page
        print(f"Page        : UNCHANGED id={page_id} v{version} (skipped)")
    else:
        result = api.update_page(page_id, page_title, storage, version)
        print(green(f"Page        : UPDATED id={page_id} v{result['version']['number']}"))
    _, failed = upload_planned(api, page_id, plan_attachments(api, page_id, uploads), "")
    write_front_matter(doc, api, space_key, result, None, enabled=args.write_back,
                       title_prefix=args.title_prefix, indent="")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_console()
    p = argparse.ArgumentParser(description="Publish Markdown files to Confluence.")
    p.add_argument("--source-dir", help="Directory with .md files (directory mode).")
    p.add_argument("--parent-page",
                   help="Parent page URL (incl. /x/ tiny link) or numeric ID. Directory mode: "
                        "root parent. Single file mode: parent for creating a new page.")
    p.add_argument("--recursive", action="store_true",
                   help="Directory mode: publish sub-directories as a page hierarchy.")
    p.add_argument("--source-file", help="Single .md file (update a page, or create one with --parent-page).")
    p.add_argument("--page-id", help="Page to update (single file mode; default: front matter).")
    p.add_argument("--preserve-banner", action="store_true",
                   help="Single file mode: keep existing content before the first <h1>.")
    p.add_argument("--title-prefix", default="",
                   help="Prefix added to every page title (e.g. for test copies).")
    p.add_argument("--attachments", choices=["referenced", "all", "none"], default="referenced",
                   help="Which local files to upload as attachments (default: referenced).")
    p.add_argument("--force", action="store_true",
                   help="Overwrite despite a version conflict (implies --adopt).")
    p.add_argument("--adopt", action="store_true",
                   help="Allow updating an existing page found only by title outside the expected parent.")
    p.add_argument("--move", action="store_true",
                   help="Re-parent existing pages to match the local hierarchy.")
    wb = p.add_mutually_exclusive_group()
    wb.add_argument("--write-back", dest="write_back", action="store_true", default=True,
                    help="Update front matter of published files (page id, version). "
                         "Default; kept for compatibility.")
    wb.add_argument("--no-write-back", dest="write_back", action="store_false",
                    help="Never modify the source files.")
    p.add_argument("--confluence-url", default=None,
                   help="Base URL of Confluence (needed for numeric IDs without URL).")
    p.add_argument("--pat", default=os.environ.get("CONFLUENCE_PAT", ""),
                   help="Personal Access Token (default: $CONFLUENCE_PAT).")
    p.add_argument("--skip-tls-check", action="store_true", help="Disable TLS certificate validation.")
    p.add_argument("--dry-run", action="store_true", help="Show planned actions; change nothing.")
    args = p.parse_args()

    if not args.pat:
        sys.exit("ERROR: No PAT provided. Use --pat <token> or set the CONFLUENCE_PAT env var.")
    file_mode = bool(args.source_file or args.page_id)
    if file_mode and args.source_dir:
        sys.exit("ERROR: Use either --source-dir (directory mode) or --source-file (single file mode).")
    if not file_mode and not (args.source_dir and args.parent_page):
        sys.exit("ERROR: Use --source-dir + --parent-page, or --source-file [--page-id | --parent-page].")
    if args.skip_tls_check:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        if file_mode:
            sys.exit(run_single(args))
        run_dir(args)
    except ApiError as exc:
        sys.exit(f"ERROR: {exc}")


def run_single(args) -> int:
    if not args.source_file:
        sys.exit("ERROR: --page-id requires --source-file.")
    md_path = Path(args.source_file).resolve()
    if not long_path(md_path).is_file():
        sys.exit(f"ERROR: Source file not found: {md_path}")
    meta, _ = split_front_matter(read_text(md_path))
    page_id = args.page_id or str(meta.get("confluence_page_id") or "") or None
    if page_id is not None and not page_id.isdigit():
        sys.exit("ERROR: --page-id / confluence_page_id must be numeric.")

    if page_id is None and not args.parent_page:
        sys.exit("ERROR: The file has no confluence_page_id. Use --page-id to update an existing "
                 "page, or --parent-page to create a new one.")
    base = args.confluence_url
    parent = None
    if args.parent_page:
        base_p, parent_id, space_key, ptitle = parse_page_arg(args.parent_page, args.confluence_url,
                                                              "--parent-page")
        base = base or base_p
    if not base and meta.get("url"):
        u = urlparse(meta["url"])
        base = f"{u.scheme}://{u.netloc}"
    if not base:
        sys.exit("ERROR: --confluence-url is required.")
    api = ConfluenceApi(base, args.pat, verify_tls=not args.skip_tls_check)

    if page_id is not None:
        try:
            api.get_page(page_id, "version")
        except ApiError as exc:
            if exc.status != 404:
                raise
            if not args.parent_page:
                sys.exit(f"ERROR: page id={page_id} no longer exists. Use --parent-page to create "
                         "it again.")
            print(yellow(f"NOTE        : page id={page_id} no longer exists - creating a new page"))
            page_id = None
    if page_id is None:
        if not args.parent_page:
            sys.exit("ERROR: The file has no confluence_page_id. Use --page-id to update an existing "
                     "page, or --parent-page to create a new one.")
        if not parent_id:
            parent_id = api.resolve_page_id_by_title(space_key, ptitle)
        space_key = api.get_page(parent_id, "space")["space"]["key"]
        parent = (parent_id, space_key)
    return publish_single(args, api, page_id, md_path, parent)


def run_dir(args) -> None:
    source_dir = Path(args.source_dir).resolve()
    if not long_path(source_dir).is_dir():
        sys.exit(f"ERROR: Source directory not found: {source_dir}")

    base, parent_id, space_key, ptitle = parse_page_arg(args.parent_page, args.confluence_url, "--parent-page")
    api = ConfluenceApi(base, args.pat, verify_tls=not args.skip_tls_check)
    if not parent_id:
        parent_id = api.resolve_page_id_by_title(space_key, ptitle)
    parent = api.get_page(parent_id, "space")
    space_key = parent["space"]["key"]

    docs = build_tree(source_dir, args.recursive)
    if not docs:
        print(yellow(f"WARNING: No .md files found in: {source_dir}"))
        return
    docs_by_path = {_norm(d.path): d for d in docs}

    print()
    print(cyan(f"Confluence  : {base}"))
    print(cyan(f"Space       : {space_key}"))
    print(cyan(f"Parent page : {parent_id}  '{parent['title']}'"))
    print(cyan(f"Source dir  : {source_dir}{'  (recursive)' if args.recursive else ''}"))
    print(f"Files found : {len(docs)}")
    if args.title_prefix:
        print(f"Title prefix: {args.title_prefix!r}")
    if args.dry_run:
        print(yellow("Mode        : DRY RUN"))
    print()

    opt = Options(space_key=space_key, parent_id=parent_id, title_prefix=args.title_prefix,
                  dry_run=args.dry_run, force=args.force, adopt=args.adopt,
                  write_back=args.write_back, attachments=args.attachments, move=args.move)

    # Convert everything first: link targets and cross-page attachments are
    # known before anything is sent to Confluence.
    for doc in docs:
        convert_doc(doc, docs_by_path, opt)
    collect_foreign(docs)

    stats = {"ok": 0, "conflict": 0, "error": 0}
    uploaded = 0
    for doc in docs:
        try:
            status, n = publish_doc(api, doc, opt)
        except Exception as exc:
            print(red(f"  ERROR    : {exc}"))
            print()
            status, n = "error", 0
        stats[status] += 1
        uploaded += n

    print(cyan("-" * 37))
    print((green if stats["ok"] else yellow)(f"Pages published : {stats['ok']}"))
    print(f"Attachments     : {uploaded}")
    if stats["conflict"]:
        print(red(f"Conflicts       : {stats['conflict']}"))
    print((red if stats["error"] else green)(f"Errors          : {stats['error']}"))
    if stats["error"] or stats["conflict"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
