"""Turn a stored raw page into a title + clean Markdown.

One extractor per page shape:

* BBE bundle (GitHub `examples/<name>/`): the `.md` explanation with each
  `::: code x.bal :::` / `::: out x.out :::` directive replaced by the file,
  verbatim, so code and explanation stay together and in order.
* ballerina.io learn pages: Next.js embeds the page's Markdown source in
  `__NEXT_DATA__` (exact text and code fences); enterprise-integration-pattern
  pages embed their fields and code. Other HTML falls back to trafilatura.
* the language spec: a structure-preserving HTML -> Markdown walk.
* Central packages: README + API reference rendered from the docs API.
* GitHub module files: Markdown as is; `.bal` files as one fenced block.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sftgen import markdown as md

DIRECTIVE_RE = re.compile(r"^:::\s*(code|out)\s+([\w.\-/]+)\s*:::\s*$", re.MULTILINE)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class Extracted:
    title: str
    markdown: str
    extractor: str


def _first_heading(markdown: str) -> str | None:
    m = re.search(r"^#{1,2}\s+(.+?)\s*#*\s*$", markdown, re.MULTILINE)
    return m.group(1).strip() if m else None


def _clean(markdown: str) -> str:
    markdown = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
    markdown = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)  # images carry no text signal
    markdown = md.relabel_fences(markdown)
    return re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"


def _with_title(title: str, body: str, description: str | None = None) -> str:
    body = body.strip()
    if description and description.strip() not in body:
        body = f"{description.strip()}\n\n{body}"
    if not body.startswith("# "):
        body = f"# {title}\n\n{body}"
    return body


def next_data(html: str) -> dict[str, Any] | None:
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        return None
    return props if isinstance(props, dict) else None


def _html_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    title = html_mod.unescape(TAG_RE.sub("", m.group(1)))
    return re.split(r"\s+[-|]\s+(?:The Ballerina|Ballerina)\b", title)[0].strip() or None


def _strip_tags(code_html: str) -> str:
    return html_mod.unescape(TAG_RE.sub("", code_html)).strip("\n")


# --------------------------------------------------------------------------- #
# Ballerina by Example
# --------------------------------------------------------------------------- #
def extract_bbe_bundle(bundle_json: str) -> Extracted | None:
    files: dict[str, str] = json.loads(bundle_json)
    md_files = sorted(f for f in files if f.endswith(".md"))
    if not md_files:
        return None

    def inline(m: re.Match[str]) -> str:
        kind, fname = m.group(1), PurePosixPath(m.group(2)).name
        if fname not in files:
            return ""
        return f"```{'ballerina' if kind == 'code' else 'output'}\n{files[fname].rstrip()}\n```"

    body = _clean(DIRECTIVE_RE.sub(inline, files[md_files[0]]))
    meta = {k.strip().lower(): v.strip() for line in next((t for f, t in files.items() if f.endswith(".metatags")),
                                                          "").splitlines() if ":" in line
            for k, v in [line.split(":", 1)]}
    title = _first_heading(body) or PurePosixPath(md_files[0]).stem.replace("-", " ").capitalize()
    return Extracted(title, _with_title(title, body, meta.get("description")) + "\n", "bbe_bundle")


def extract_bbe_page(html: str) -> Extracted | None:
    """Fallback for an example with no GitHub folder: the page carries only its code."""
    props = next_data(html) or {}
    codes = props.get("codes")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        return None
    blocks = [c for c in (_strip_tags(c) for c in codes) if len(c.strip()) > 1]
    if not blocks:
        return None
    fm = props.get("frontmatter") or {}
    title = _strip_tags(fm.get("title") or _html_title(html) or "Ballerina by Example")
    parts = [f"# {title}", (fm.get("description") or "").strip()] + [f"```ballerina\n{c}\n```" for c in blocks]
    return Extracted(title, "\n\n".join(p for p in parts if p) + "\n", "bbe_page_codes")


# --------------------------------------------------------------------------- #
# ballerina.io learn pages
# --------------------------------------------------------------------------- #
def extract_learn(html: str, url: str) -> Extracted | None:
    props = next_data(html)
    if props and isinstance(props.get("content"), str) and props["content"].strip():
        fm = props.get("frontmatter") or {}
        title = str(fm.get("title") or _first_heading(props["content"]) or _html_title(html) or url)
        intro = fm.get("intro") or fm.get("description")
        body = _clean(md.split_front_matter(props["content"])[1])
        return Extracted(title, _with_title(title, body, str(intro) if intro else None) + "\n", "nextjs_markdown")
    if props and isinstance(props.get("content"), list) and props.get("name"):  # integration pattern page
        title = str(props["name"])
        parts = [f"# {title}"]
        parts += [str(props[k]).strip() for k in ("tagline", "desc", "helps") if props.get(k)]
        for item in props["content"]:
            code = item.get("raw") if isinstance(item, dict) else None
            if isinstance(code, str) and code.strip():
                if item.get("name"):
                    parts.append(f"`{item['name']}`:")
                parts.append(f"```ballerina\n{code.strip()}\n```")
        return Extracted(title, _clean("\n\n".join(parts)), "nextjs_pattern")
    return extract_html(html, url)


def extract_html(html: str, url: str) -> Extracted | None:
    import trafilatura

    text = trafilatura.extract(html, output_format="markdown", include_formatting=True, include_tables=True,
                               include_comments=False, favor_recall=True, url=url)
    if not text:
        return None
    title = _html_title(html) or _first_heading(text) or url
    return Extracted(title, _with_title(title, _clean(text)) + "\n", "trafilatura")


# --------------------------------------------------------------------------- #
# Language specification
# --------------------------------------------------------------------------- #
_INLINE_SKIP = {"p", "pre", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dl", "div",
                "li", "script", "style"}
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _inline(el) -> str:  # type: ignore[no-untyped-def]
    """Inline text of an element, excluding nested block elements (they are emitted on their own)."""
    parts = [el.text or ""]
    for child in el:
        tag = child.tag if isinstance(child.tag, str) else ""
        if tag in _INLINE_SKIP:
            parts.append(child.tail or "")
            continue
        inner = _inline(child)
        if tag == "code":
            parts.append(f"`{inner.strip()}`" if inner.strip() else "")
        elif tag in ("em", "i", "var"):
            parts.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif tag in ("strong", "b"):
            parts.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif tag == "br":
            parts.append("\n")
        else:
            parts.append(inner)
        parts.append(child.tail or "")
    return "".join(parts)


def html_to_markdown_structured(html: str, *, skip_sections: tuple[str, ...] = ("table of contents",)) -> str:
    """Faithful HTML -> Markdown for clean, semantic documents (the language spec).

    Walks elements in document order (robust to unclosed tags that nest the whole
    page inside one element). Unlike boilerplate removers it keeps every <pre>:
    `grammar` -> ```ebnf, `langlib` -> ```ballerina, others language-detected.
    """
    from lxml import html as lxml_html

    root = lxml_html.fromstring(html)
    body = root.find("body") if root.find("body") is not None else root
    out: list[str] = []
    skipping = False
    for el in body.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag or any(a.tag in ("pre", "table", "script", "style", *_HEADINGS) for a in el.iterancestors()):
            continue
        if tag in _HEADINGS:
            title = re.sub(r"\s+", " ", el.text_content()).strip()
            skipping = title.lower() in skip_sections
            if not skipping and title:
                out.append(f"{'#' * int(tag[1])} {title}")
        elif skipping:
            continue
        elif tag == "pre":
            code = el.text_content().strip("\n")
            cls = el.get("class", "")
            lang = "ebnf" if "grammar" in cls else "ballerina" if "langlib" in cls else md.detect_language(code)
            out.append(f"```{lang}\n{code}\n```")
        elif tag in ("p", "li", "dt", "dd", "blockquote"):
            text = re.sub(r"[ \t]*\n[ \t]*", " ", _inline(el)).strip()
            if text:
                out.append(("- " if tag == "li" else "") + text)
        elif tag == "table":
            rows = [[re.sub(r"\s+", " ", c.text_content()).strip() for c in tr if getattr(c, "tag", "") in ("td", "th")]
                    for tr in el.iter("tr")]
            rows = [r for r in rows if r]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                out.append("\n".join(lines))
    text = "\n\n".join(out)
    text = re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", text)  # tighten consecutive list items
    return EMAIL_RE.sub("[email removed]", text) + "\n"


def extract_spec(html: str) -> Extracted | None:
    markdown = html_to_markdown_structured(html)
    if not markdown.strip():
        return None
    return Extracted(_first_heading(markdown) or "Ballerina Language Specification", markdown, "spec_html")


# --------------------------------------------------------------------------- #
# Ballerina Central
# --------------------------------------------------------------------------- #
def _signature(f: dict[str, Any]) -> str:
    params = ", ".join(f"{p['type']} {p['name']}" + (f" = {p['default']}" if p.get("default") else "")
                       for p in f.get("parameters") or [])
    rets = "|".join(r["type"] for r in f.get("returns") or [] if r.get("type"))
    quals = " ".join(f.get("qualifiers") or [])
    name = f.get("name") or ""
    if f.get("accessor"):
        name = f"{f['accessor']} {f.get('resourcePath') or name}"
    return f"{quals + ' ' if quals else ''}function {name}({params}){' returns ' + rets if rets else ''}"


def _render_function(f: dict[str, Any], level: str) -> list[str]:
    out = [f"{level} {f.get('name')}", f"```ballerina\n{_signature(f)}\n```"]
    if f.get("description"):
        out.append(f["description"])
    params = [f"- `{p['name']}` (`{p['type']}`): {p['description']}".rstrip(": ") for p in f.get("parameters") or []]
    if params:
        out.append("Parameters:\n" + "\n".join(params))
    rets = [f"- `{r['type']}`: {r['description']}".rstrip(": ") for r in f.get("returns") or [] if r.get("type")]
    if rets:
        out.append("Returns:\n" + "\n".join(rets))
    return out


KIND_TITLES = {
    "clients": "Clients", "listeners": "Listeners", "functions": "Functions", "classes": "Classes",
    "objectTypes": "Object types", "records": "Records", "enums": "Enums", "errors": "Errors", "types": "Types",
    "unionTypes": "Union types", "constants": "Constants", "annotations": "Annotations",
    "serviceTypes": "Service types",
}


def render_api_docs(modules: list[dict[str, Any]], readme: str = "") -> str:
    out: list[str] = []
    readme_words = " ".join(re.findall(r"\w+", readme.lower()))
    for mod in modules:
        out.append(f"## API reference: module `{mod.get('id')}`")
        desc = str(mod.get("description") or "").strip()
        # the default module's description is usually the package README again: keep it once
        if desc and " ".join(re.findall(r"\w+", desc.lower()))[:2000] not in readme_words:
            out.append(md.demote_headings(desc, min_level=3))
        for kind, label in KIND_TITLES.items():
            for it in mod.get(kind) or []:
                if kind == "functions":
                    out.extend(_render_function(it, "###"))
                    continue
                out.append(f"### {label[:-1] if label.endswith('s') else label}: {it.get('name')}")
                if it.get("type"):
                    out.append(f"Type: `{it['type']}`")
                if it.get("description"):
                    out.append(it["description"])
                if it.get("fields"):
                    out.append("Fields:\n" + "\n".join(
                        f"- `{f['name']}` (`{f['type']}`): {f['description']}".rstrip(": ") for f in it["fields"]))
                for m in it.get("methods") or []:
                    out.extend(_render_function(m, "####"))
    return "\n\n".join(out)


def extract_central(body_json: str) -> Extracted | None:
    data = json.loads(body_json)
    pkg = data.get("package") or {}
    title = f"{pkg.get('organization')}/{pkg.get('name')}"
    parts = [f"# {title}"]
    if pkg.get("summary"):
        parts.append(str(pkg["summary"]).strip())
    readme = md.demote_headings(str(pkg.get("readme") or "").strip(), min_level=2)
    if readme:
        parts.append(readme)
    api = render_api_docs(data.get("api_docs") or [], readme)
    if api:
        parts.append(api)
    if len(parts) <= 2 and not readme and not api:
        return None
    return Extracted(title, _clean("\n\n".join(parts)), "central_api")


# --------------------------------------------------------------------------- #
# GitHub module repo files
# --------------------------------------------------------------------------- #
def module_name(repo: str) -> str:
    """'ballerina-platform/module-ballerinax-kafka' -> 'ballerinax/kafka'."""
    name = repo.split("/")[-1].removeprefix("module-")
    for org in ("ballerinax", "ballerina"):
        if name.startswith(org + "-"):
            return f"{org}/{name[len(org) + 1:]}"
    return name


def extract_github_file(text: str, meta: dict[str, Any]) -> Extracted | None:
    path, repo = str(meta.get("path", "")), str(meta.get("repo", ""))
    module = module_name(repo)
    fname = PurePosixPath(path).name
    if fname.endswith(".bal"):
        if not text.strip():
            return None
        title = f"{module}: {fname}"
        return Extracted(title, f"# {title}\n\nSource of `{fname}` in the `{module}` module.\n\n"
                                f"```ballerina\n{text.rstrip()}\n```\n", "bal_source")
    front, body = md.split_front_matter(text)
    title = str(front.get("title") or _first_heading(body) or f"{module}: {fname}")
    if not title.startswith(module):
        title = f"{module}: {title}"
    return Extracted(title, _with_title(title, _clean(body)) + "\n", "github_markdown")


# --------------------------------------------------------------------------- #
def extract(source: str, url: str, raw_body: str, meta: dict[str, Any]) -> Extracted | None:
    if source == "bbe":
        return extract_bbe_bundle(raw_body) if meta.get("origin") == "github" else extract_bbe_page(raw_body)
    if source == "spec":
        return extract_spec(raw_body)
    if source == "learn":
        return extract_learn(raw_body, url)
    if source == "central":
        return extract_central(raw_body)
    if source == "github":
        return extract_github_file(raw_body, meta)
    raise ValueError(f"unknown source {source!r}")
