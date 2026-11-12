"""Source-specific extractors: RawRecord(s) -> clean Markdown + metadata.

Each extractor has a name and version recorded on the Document, so a fix to
one extractor only requires re-cleaning the documents it produced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from balpipe_common.schemas import BBEInfo, RawRecord

from sftgen import markdown as md

DOC_TYPES_BY_PATH = [
    (re.compile(r"(^|/)spec(/|$)"), "spec"),
    (re.compile(r"release-notes"), "release_notes"),
    (re.compile(r"development-tutorials|integration-tutorials|get-started|tutorial"), "tutorial"),
]
DIRECTIVE_RE = re.compile(r"^:::\s*(code|out)\s+([\w.\-/]+)\s*:::\s*$", re.MULTILINE)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
MODULE_SPEC_RE = re.compile(r"^public/spec/([\w.-]+)/spec\.md$")


@dataclass
class Extracted:
    """What an extractor produces; the stage turns it into a Document."""

    raw: RawRecord
    markdown: str
    title: str
    doc_type: str
    extractor: str
    version: str = "1.0.0"
    canonical_url: str | None = None
    description: str | None = None
    category: str | None = None
    ballerina_version: str | None = None
    bbe: BBEInfo | None = None
    extra_raw_ids: list[str] = field(default_factory=list)


def _first_heading(markdown: str) -> str | None:
    m = re.search(r"^#{1,2}\s+(.+?)\s*#*\s*$", markdown, re.MULTILINE)
    return m.group(1).strip() if m else None


def _strip_html_noise(markdown: str) -> str:
    markdown = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
    markdown = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)  # images carry no text signal
    return re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"


# --------------------------------------------------------------------------- #
# Ballerina-by-Example (ballerina-distribution/examples/<name>/...)
# --------------------------------------------------------------------------- #
def parse_metatags(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def parse_bbe_index(index_json: str) -> dict[str, dict]:
    """examples/index.json -> {folder_name: {category, title, flags}}"""
    out: dict[str, dict] = {}
    for group in json.loads(index_json):
        for sample in group.get("samples", []):
            out[sample["url"]] = {
                "category": group.get("category"),
                "group": group.get("title"),
                "name": sample.get("name"),
                "verify_build": sample.get("verifyBuild"),
                "verify_output": sample.get("verifyOutput"),
                "need_central": sample.get("needCentral"),
            }
    return out


def extract_bbe(name: str, files: dict[str, RawRecord], index: dict[str, dict]) -> Extracted | None:
    """Inline `::: code x.bal :::` / `::: out x.out :::` directives as labelled fences."""
    md_files = [f for f in files if f.endswith(".md")]
    if not md_files:
        return None
    main_md = files[md_files[0]]

    def inline(m: re.Match[str]) -> str:
        kind, fname = m.group(1), PurePosixPath(m.group(2)).name
        rec = files.get(fname)
        if rec is None:
            return ""
        lang = "ballerina" if kind == "code" else "output"
        return f"```{lang}\n{rec.body.rstrip()}\n```"

    body = DIRECTIVE_RE.sub(inline, main_md.body)
    body = _strip_html_noise(md.relabel_fences(body))
    meta = parse_metatags(next((r.body for f, r in files.items() if f.endswith(".metatags")), ""))
    info = index.get(name, {})
    return Extracted(
        raw=main_md,
        markdown=body,
        title=_first_heading(body) or info.get("name") or name,
        doc_type="bbe",
        extractor="bbe_markdown",
        canonical_url=f"https://ballerina.io/learn/by-example/{name}/",
        description=meta.get("description"),
        category=info.get("category"),
        bbe=BBEInfo(
            name=name,
            verify_build=info.get("verify_build"),
            verify_output=info.get("verify_output"),
            need_central=info.get("need_central"),
        ),
        extra_raw_ids=[r.raw_id for f, r in files.items() if r.raw_id != main_md.raw_id],
    )


# --------------------------------------------------------------------------- #
# Enterprise Integration Patterns (ballerina-dev-website/pages/learn/enterprise-integration-patterns/...)
# --------------------------------------------------------------------------- #
def extract_eip(name: str, files: dict[str, RawRecord]) -> Extracted | None:
    """One pattern folder: `<name>.yml` (tagline/desc/category/helps) + one or more `.bal` files."""
    bals = sorted(f for f in files if f.endswith(".bal"))
    if not bals:
        return None
    meta: dict = {}
    yml = next((r for f, r in files.items() if f.endswith((".yml", ".yaml"))), None)
    if yml:
        try:
            loaded = yaml.safe_load(yml.body)
            meta = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            meta = {}
    title = name.replace("-", " ").capitalize()
    parts = [f"# {title}"]
    intro = " ".join(str(meta[k]).strip().rstrip(".") + "." for k in ("tagline", "desc") if meta.get(k))
    if intro:
        parts.append(intro)
    if meta.get("helps"):  # same section as the code, so pairing picks it up as the explanation
        parts.append(str(meta["helps"]).strip())
    for f in bals:
        if len(bals) > 1:
            parts.append(f"`{f}`:")
        parts.append(f"```ballerina\n{files[f].body.rstrip()}\n```")
    main = files[bals[0]]
    return Extracted(
        raw=main, markdown="\n\n".join(parts) + "\n", title=f"Enterprise integration pattern: {title}",
        doc_type="guide", extractor="eip_bundle",
        canonical_url=f"https://ballerina.io/learn/enterprise-integration-patterns/{name}/",
        description=str(meta.get("desc")).strip() if meta.get("desc") else None,
        category=str(meta.get("category")) if meta.get("category") else "Enterprise integration patterns",
        extra_raw_ids=[r.raw_id for r in files.values() if r.raw_id != main.raw_id],
    )


# --------------------------------------------------------------------------- #
# Generic GitHub Markdown (dev-website docs, module READMEs, module specs)
# --------------------------------------------------------------------------- #
def extract_github_markdown(raw: RawRecord) -> Extracted:
    assert raw.git is not None
    meta, body = md.split_front_matter(raw.body)
    intro = meta.get("intro")
    if intro and str(intro).strip() not in body:
        body = f"{str(intro).strip()}\n\n{body}"
    title = str(meta.get("title") or _first_heading(body) or PurePosixPath(raw.git.path).stem)
    if not body.lstrip().startswith("# "):
        body = f"# {title}\n\n{body.lstrip()}"
    path = raw.git.path
    name = PurePosixPath(path).name
    if name in ("Module.md", "Package.md", "README.md") and raw.git.repo.split("/")[-1].startswith("module-"):
        doc_type = "api_readme"
    else:
        doc_type = next((t for rx, t in DOC_TYPES_BY_PATH if rx.search(path)), "guide")
    permalink = meta.get("permalink")
    spec_m = MODULE_SPEC_RE.match(path)
    if spec_m and "ballerina-dev-website" in raw.git.repo and not permalink:
        permalink = f"/spec/{spec_m.group(1)}/"
    return Extracted(
        raw=raw,
        markdown=_strip_html_noise(md.relabel_fences(body)),
        title=title,
        doc_type=doc_type,
        extractor="github_markdown",
        canonical_url=f"https://ballerina.io{permalink}" if permalink and "ballerina-dev-website" in raw.git.repo else None,
        description=(str(meta["description"]).strip() if meta.get("description") else None),
    )


# --------------------------------------------------------------------------- #
# Language specification (well-structured HTML in ballerina-dev-website/public/spec)
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


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


def extract_spec_html(raw: RawRecord) -> Extracted:
    assert raw.git is not None
    markdown = html_to_markdown_structured(raw.body)
    title = _first_heading(markdown) or "Ballerina Language Specification"
    rel = raw.git.path.removeprefix("public/").removesuffix("index.html")
    return Extracted(raw=raw, markdown=markdown, title=title, doc_type="spec", extractor="spec_html",
                     canonical_url=f"https://ballerina.io/{rel}")


# --------------------------------------------------------------------------- #
# Ballerina source files from module repos
# --------------------------------------------------------------------------- #
def extract_bal_source(raw: RawRecord) -> Extracted:
    assert raw.git is not None
    module = raw.git.repo.split("/")[-1].removeprefix("module-").replace("ballerinax-", "ballerinax/").replace(
        "ballerina-", "ballerina/"
    )
    fname = PurePosixPath(raw.git.path).name
    body = f"# {module}: {fname}\n\nSource of `{fname}` in the `{module}` module.\n\n```ballerina\n{raw.body.rstrip()}\n```\n"
    return Extracted(raw=raw, markdown=body, title=f"{module}: {fname}", doc_type="module_source",
                     extractor="bal_source")


# --------------------------------------------------------------------------- #
# Central package README
# --------------------------------------------------------------------------- #
def extract_central(raw: RawRecord) -> Extracted | None:
    pkg = json.loads(raw.body)
    readme = (pkg.get("readme") or "").strip()
    if not readme:
        return None
    title = f"{pkg['organization']}/{pkg['name']}"
    body = f"# {title}\n\n{readme}\n"
    return Extracted(
        raw=raw,
        markdown=_strip_html_noise(md.relabel_fences(body)),
        title=title,
        doc_type="api_readme",
        extractor="central_readme",
        canonical_url=raw.source_uri,
        description=pkg.get("summary"),
        ballerina_version=pkg.get("ballerinaVersion"),
    )


# --------------------------------------------------------------------------- #
# HTML pages
# --------------------------------------------------------------------------- #
def _html_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    import html as html_mod

    title = html_mod.unescape(TAG_RE.sub("", m.group(1)))
    return re.split(r"\s+[-|]\s+(?:The Ballerina|Ballerina)\b", title)[0].strip() or None


def extract_nextjs(raw: RawRecord) -> Extracted | None:
    """ballerina.io by-example pages keep code only in __NEXT_DATA__ (not in the HTML body)."""
    import html as html_mod

    m = NEXT_DATA_RE.search(raw.body)
    if not m:
        return None
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError):
        return None
    raw_codes = props.get("codes")
    # Only BBE pages carry a *list* of highlighted code strings; other Next.js pages
    # (e.g. the blog) use the same key for unrelated string data.
    if not isinstance(raw_codes, list) or not all(isinstance(c, str) for c in raw_codes):
        return None
    codes = [html_mod.unescape(TAG_RE.sub("", c)).strip("\n") for c in raw_codes]
    codes = [c for c in codes if c.strip() and len(c.strip()) > 1]
    if not codes:
        return None
    fm = props.get("frontmatter") or {}
    title = html_mod.unescape(TAG_RE.sub("", fm.get("title") or _html_title(raw.body) or raw.source_uri)).strip()
    desc = (fm.get("description") or "").strip()
    parts = [f"# {title}", desc] + [f"```ballerina\n{c}\n```" for c in codes]
    return Extracted(raw=raw, markdown="\n\n".join(p for p in parts if p) + "\n", title=title,
                     doc_type="bbe", extractor="nextjs_data", description=desc or None, canonical_url=raw.source_uri)


def extract_html(raw: RawRecord) -> Extracted | None:
    nxt = extract_nextjs(raw)
    if nxt:
        return nxt
    import trafilatura

    text = trafilatura.extract(
        raw.body,
        output_format="markdown",
        include_formatting=True,
        include_tables=True,
        include_comments=False,
        favor_recall=True,
        url=raw.source_uri,
    )
    if not text:
        return None
    title = _html_title(raw.body) or _first_heading(text) or raw.source_uri
    if not text.lstrip().startswith("# "):
        text = f"# {title}\n\n{text}"
    host = re.sub(r"^https?://", "", raw.source_uri).split("/")[0]
    return Extracted(
        raw=raw,
        markdown=_strip_html_noise(md.relabel_fences(text)),
        title=title,
        doc_type="blog" if host.startswith("blog.") else "other",
        extractor="trafilatura_md",
        version=f"1.0.0+trafilatura{trafilatura.__version__}",
        canonical_url=raw.source_uri,
    )
