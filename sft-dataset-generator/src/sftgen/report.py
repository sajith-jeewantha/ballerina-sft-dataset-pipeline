"""Progress report: a short terminal summary and a single-file reports/report.html.

Everything is read from data/pipeline.db, so the report can be rebuilt at any
time (`sftgen report`) and is refreshed after every command.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.ids import utc_now_iso

SOURCE_LABELS = {"bbe": "Ballerina by Example", "spec": "Language spec 2024R1", "learn": "Learn docs",
                 "central": "Ballerina Central", "github": "GitHub modules"}


@dataclass
class ReportData:
    generated_at: str
    sources: list[str]
    pages: dict[str, dict[str, int]]                # source -> status -> n
    failed_pages: list[dict[str, Any]]
    chunks: dict[str, dict[str, int]]               # source -> status -> n
    questions_by_source: dict[str, int]
    questions_by_category: dict[str, int]
    questions_by_difficulty: dict[str, int]
    question_status: dict[str, int]
    answer_status: dict[str, int]                   # validation
    runs: list[dict[str, Any]]
    usage: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    limits: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] | None = None  # knowledge-graph coverage (graph/coverage.py)
    swan_lake: str = ""

    @property
    def total_pages(self) -> int:
        return sum(sum(v.values()) for v in self.pages.values())

    @property
    def total_questions(self) -> int:
        return sum(self.question_status.values())


def _grouped(rows: list[Any]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        out[r[0]][r[1]] = r[2]
    return dict(out)


def collect(s: Settings, db: Database) -> ReportData:
    sources = [x for x in s.sources.order]
    pages = _grouped(db.query("SELECT source, status, COUNT(*) FROM pages GROUP BY 1, 2"))
    failed = [dict(r) for r in db.query(
        "SELECT source, url, status, error, fetched_at FROM pages WHERE status IN ('failed', 'blocked') "
        "ORDER BY updated_at DESC LIMIT 200")]
    chunks = _grouped(db.query("SELECT source, status, COUNT(*) FROM chunks GROUP BY 1, 2"))
    by = lambda col: {r[0]: r[1] for r in db.query(  # noqa: E731
        f"SELECT {col}, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC")}
    runs = []
    for r in db.query("SELECT * FROM runs ORDER BY id DESC LIMIT 50"):
        d = dict(r)
        d["limits"] = json.loads(d.get("limits") or "{}")
        d["stats"] = json.loads(d.get("stats") or "{}")
        runs.append(d)
    usage = [dict(r) for r in db.query(
        "SELECT phase, GROUP_CONCAT(DISTINCT model) AS models, COUNT(*) AS calls, SUM(cached) AS cached, "
        "SUM(status = 'error') AS errors, SUM(prompt_tokens) AS prompt_tokens, "
        "SUM(completion_tokens) AS completion_tokens, SUM(cost_usd) AS cost_usd, "
        "SUM(cost_usd IS NULL AND cached = 0 AND status = 'ok') AS unpriced FROM llm_usage GROUP BY phase")]
    samples = [dict(r) for r in db.query(
        "SELECT q.question, a.answer, q.source_url, q.source, q.category, q.difficulty, a.status AS validation "
        "FROM questions q JOIN answers a ON a.question_id = q.id ORDER BY RANDOM() LIMIT ?", (s.report.samples,))]
    return ReportData(
        generated_at=utc_now_iso(), sources=sources, pages=pages, failed_pages=failed, chunks=chunks,
        questions_by_source=by("source"), questions_by_category=by("category"),
        questions_by_difficulty=by("difficulty"), question_status=by("status"),
        answer_status={r[0]: r[1] for r in db.query("SELECT status, COUNT(*) FROM answers GROUP BY 1")},
        runs=runs, usage=usage, samples=samples, limits=s.limits(), coverage=_coverage(db),
        swan_lake=s.sources.swan_lake,
    )


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #
def _coverage(db: Database) -> dict[str, Any] | None:
    from sftgen.graph.coverage import coverage_summary

    return coverage_summary(db)


def terminal_summary(d: ReportData, report_path: Path) -> str:
    def kv(m: dict[str, int]) -> str:
        return ", ".join(f"{k} {v}" for k, v in m.items()) or "none"

    usable = {src: st.get("chunked", 0) for src, st in d.pages.items()}
    failed = sum(st.get("failed", 0) + st.get("blocked", 0) for st in d.pages.values())
    active_chunks = {src: sum(n for k, n in st.items() if k != "stale") for src, st in d.chunks.items()}
    qs = d.question_status
    cost = sum(u["cost_usd"] or 0 for u in d.usage)
    tokens = sum((u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0) for u in d.usage)
    lines = [
        "",
        "── Summary " + "─" * 49,
        f"Pages      {d.total_pages} total, {failed} failed  |  usable: " + kv({k: usable.get(k, 0) for k in d.sources
                                                                                if k in d.pages}),
        f"Chunks     {sum(active_chunks.values())}  |  " + kv({k: active_chunks.get(k, 0) for k in d.sources
                                                                 if k in active_chunks}),
        f"Questions  {d.total_questions}  |  pending {qs.get('pending', 0)}, answered {qs.get('answered', 0)}, "
        f"failed {qs.get('failed', 0)}",
        f"Validation {kv(d.answer_status)}",
        *([f"Graph      grounded {d.coverage['grounded']:,}/{d.coverage['items']:,} items "
           f"(core {d.coverage['by_priority'].get('core', {}).get('grounded', 0)}/"
           f"{d.coverage['by_priority'].get('core', {}).get('items', 0)}) · graph {d.coverage['graph_version']}"]
          if d.coverage else []),
        f"LLM usage  {tokens:,} tokens, ${cost:.4f}" + ("" if d.usage else " (no calls yet)"),
        f"Report     {report_path}",
        "─" * 60,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--bar:#2f6fde;--bar2:#16a34a;
--bad:#c2410c;--code:#f1f1ee}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--text:#ececea;--muted:#a3a39e;--line:#33332f;
--bar:#5b8def;--bar2:#34c46a;--bad:#f0844a;--code:#262624}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 12px}h3{font-size:15px;margin:0 0 8px}.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi{font-size:26px;font-weight:650}.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}
.bar{display:grid;grid-template-columns:160px 1fr 48px;gap:8px;align-items:center;margin:4px 0;font-size:14px}
.track{background:var(--code);border-radius:4px;height:14px}.fill{background:var(--bar);height:14px;border-radius:4px}
.fill.alt{background:var(--bar2)}.err{color:var(--bad);word-break:break-word}.url{word-break:break-all}
pre{background:var(--code);padding:10px;border-radius:8px;overflow-x:auto;font-size:13px}
.qa{margin-bottom:12px}.tag{display:inline-block;font-size:12px;padding:1px 8px;border:1px solid var(--line);
border-radius:99px;margin-right:4px;color:var(--muted)}code{font-size:13px}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _bars(data: dict[str, int], labels: dict[str, str] | None = None, alt: bool = False) -> str:
    if not data:
        return '<p class="muted">No data yet.</p>'
    top = max(data.values()) or 1
    rows = [f'<div class="bar"><span>{_e((labels or {}).get(k, k))}</span><div class="track"><div class="fill'
            f'{" alt" if alt else ""}" style="width:{100 * v / top:.1f}%"></div></div><span class="n">{v}</span></div>'
            for k, v in data.items()]
    return "".join(rows)


def _table(headers: list[str], rows: list[list[Any]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(f'<th{" class=n" if i in numeric else ""}>{_e(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td{" class=n" if i in numeric else ""}>{c}</td>' for i, c in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _render_answer(text: str) -> str:
    """Minimal Markdown: code fences -> <pre>, other text escaped with line breaks."""
    out = []
    for i, part in enumerate(re.split(r"```[\w+-]*\n?", text)):
        if i % 2:
            out.append(f"<pre><code>{_e(part.rstrip())}</code></pre>")
        elif part.strip():
            out.append("<p>" + _e(part.strip()).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>")
    return "".join(out)


def _fmt_stats(stats: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in stats.items() if not isinstance(v, (dict, list))) or "—"


def _coverage_html(d: ReportData) -> list[str]:
    c = d.coverage
    if not c:
        return ["<h2>Knowledge graph coverage</h2>",
                '<div class="card"><p class="muted">Not synced yet: run <code>sftgen graph sync</code> '
                "(it also runs after every scrape).</p></div>"]
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "—"  # noqa: E731
    rows = []
    for p, v in c["by_priority"].items():
        n = v.get("items", 0)
        rows.append([_e(p), str(n), f"{v.get('grounded', 0)} ({pct(v.get('grounded', 0), n)})",
                     str(v.get("not_scraped", 0)), str(v.get("external_only", 0) + v.get("no_sources", 0)),
                     str(v.get("no_own_sources", 0)), str(v.get("with_questions", 0))])
    type_bars = {t: v["grounded"] for t, v in c["by_type"].items()}
    type_totals = ", ".join(f"{t} {v['items']}" for t, v in c["by_type"].items())
    missing = c["missing_core"]
    scrape_next = ", ".join(f"{k} {n}" for k, n in c["not_scraped_urls"].items()) or "none"
    return [
        "<h2>Knowledge graph coverage</h2>",
        f'<p class="muted">Graph {_e(c["graph_version"])} · Swan Lake pin {_e(d.swan_lake)}. An item is '
        "<b>grounded</b> when one of its sources (or its node's, if it has none) is a scraped page, or for the "
        "spec, a scraped section. <b>With questions</b> is approximate (questions from a grounding page or "
        "section) until prompts are tagged by item.</p>",
        '<div class="grid">' + "".join(
            f'<div class="card"><div class="muted">{_e(k)}</div><div class="kpi">{_e(v)}</div></div>' for k, v in [
                ("Items (active)", f"{c['items']:,}"), ("Grounded", f"{c['grounded']:,} ({pct(c['grounded'], c['items'])})"),
                ("Core grounded", f"{c['by_priority'].get('core', {}).get('grounded', 0)}/"
                                  f"{c['by_priority'].get('core', {}).get('items', 0)}"),
                ("With questions (approx.)", f"{c['with_questions']:,}")]) + "</div>",
        '<div class="card" style="margin-top:12px">' + _table(
            ["Priority", "Items", "Grounded", "Not scraped yet", "External / none", "No own sources",
             "With questions"], rows, numeric={1, 3, 4, 5, 6}) + "</div>",
        '<div class="two" style="margin-top:12px">',
        f'<div class="card"><h3>Grounded items by node type</h3>{_bars(type_bars)}'
        f'<p class="muted">Items per type: {_e(type_totals)}</p></div>',
        f'<div class="card"><h3>Source pages to scrape next</h3><p>{_e(scrape_next)}</p><p class="muted">Distinct '
        "item-source URLs not in the database yet, by kind. Raise <code>LIMIT_SCRAPE_PAGES</code> or widen the "
        "sources (module repos beyond the current set arrive in M3).</p></div>",
        "</div>",
        f'<h3 style="margin-top:16px">Core items not grounded ({c["missing_core_total"]}; first {len(missing)})</h3>',
        '<div class="card">' + (_table(["Item", "Node", "Status", "First scrapeable source"], [
            [f"<code>{_e(m['ref'])}</code>", _e(m["node_title"]), _e(m["status"]),
             f'<span class="url">{_e(m["url"] or "")}</span>'] for m in missing])
            if missing else '<p class="muted">Every core item is grounded.</p>') + "</div>",
    ]


def render_html(d: ReportData) -> str:
    srcs = [x for x in d.sources if x in d.pages or x in d.chunks] + sorted(
        (set(d.pages) | set(d.chunks)) - set(d.sources))
    statuses = sorted({st for v in d.pages.values() for st in v})
    page_rows = [[_e(SOURCE_LABELS.get(src, src)), *[str(d.pages.get(src, {}).get(st, 0)) for st in statuses],
                  str(sum(d.pages.get(src, {}).values()))] for src in srcs]
    chunk_by_source = {SOURCE_LABELS.get(src, src): sum(n for k, n in d.chunks.get(src, {}).items() if k != "stale")
                       for src in srcs if src in d.chunks}
    failed = sum(v.get("failed", 0) + v.get("blocked", 0) for v in d.pages.values())
    qs = d.question_status
    answered = qs.get("answered", 0)
    cost_total = sum(u["cost_usd"] or 0 for u in d.usage)

    kpis = [("Pages", d.total_pages), ("Failed pages", failed), ("Chunks", sum(chunk_by_source.values())),
            ("Questions", d.total_questions), ("Answered", answered), ("Pending", qs.get("pending", 0)),
            ("Failed answers", qs.get("failed", 0)), ("LLM cost", f"${cost_total:.4f}")]
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>Pipeline report</title><style>{CSS}</style></head><body><main>",
        "<h1>Ballerina Q&amp;A dataset: pipeline report</h1>",
        f'<p class="muted">Generated {_e(d.generated_at)} from data/pipeline.db · current limits: '
        + _e(", ".join(f"{k}={v}" for k, v in d.limits.items())) + "</p>",
        '<div class="grid">' + "".join(f'<div class="card"><div class="muted">{_e(k)}</div><div class="kpi">{_e(v)}'
                                       "</div></div>" for k, v in kpis) + "</div>",
        "<h2>Pages scraped</h2>",
        '<div class="card">' + _table(["Source", *statuses, "Total"], page_rows,
                                      numeric=set(range(1, len(statuses) + 2))) + "</div>",
        '<p class="muted">chunked = converted and chunked · empty = no usable content · duplicate = same content '
        "as another page · blocked = disallowed by robots.txt · failed = fetch or conversion error</p>",
        f"<h2>Failed pages ({len(d.failed_pages)})</h2>",
        '<div class="card">' + (_table(["Source", "URL", "Status", "Error", "When"], [
            [_e(f["source"]), f'<span class="url">{_e(f["url"])}</span>', _e(f["status"]),
             f'<span class="err">{_e(f["error"])}</span>', _e(f["fetched_at"])] for f in d.failed_pages])
            if d.failed_pages else '<p class="muted">None.</p>') + "</div>",
        *_coverage_html(d),
        "<h2>Chunks per source</h2>",
        f'<div class="card">{_bars(chunk_by_source)}</div>',
        "<h2>Questions</h2>",
        '<div class="two">',
        f'<div class="card"><h3>By source</h3>{_bars(d.questions_by_source, SOURCE_LABELS)}</div>',
        f'<div class="card"><h3>By category</h3>{_bars(d.questions_by_category)}</div>',
        f'<div class="card"><h3>By difficulty</h3>{_bars(d.questions_by_difficulty)}</div>',
        f'<div class="card"><h3>Answer status</h3>{_bars(d.question_status, alt=True)}</div>',
        "</div>",
        "<h2>Validation</h2>",
        f'<div class="card">{_bars(d.answer_status, alt=True)}'
        '<p class="muted">valid = all Ballerina code compiled · invalid = a block failed to compile · '
        "not_checked = no code, bal not installed, or validate not run yet</p></div>",
        "<h2>Token usage and cost per phase</h2>",
        '<div class="card">' + (_table(["Phase", "Models", "Calls", "Cached", "Errors", "Prompt tokens",
                                         "Completion tokens", "Cost (USD)"], [
            [_e(u["phase"]), _e(u["models"]), str(u["calls"]), str(u["cached"] or 0), str(u["errors"] or 0),
             f'{u["prompt_tokens"] or 0:,}', f'{u["completion_tokens"] or 0:,}',
             (f'{u["cost_usd"]:.4f}' if u["cost_usd"] is not None else "n/a")
             + (f' <span class="muted">({u["unpriced"]} calls without a price)</span>' if u["unpriced"] else "")]
            for u in d.usage], numeric={2, 3, 4, 5, 6, 7}) if d.usage else '<p class="muted">No model calls yet.</p>')
        + "</div>",
        "<h2>Run history</h2>",
        '<div class="card">' + _table(["#", "Command", "Status", "Started (UTC)", "Duration", "Limits", "Processed"], [
            [str(r["id"]), _e(r["command"]), _e(r["status"]) + (f'<div class="err">{_e(r["error"])}</div>'
                                                                 if r.get("error") else ""),
             _e(r["started_at"]), f'{r["duration_s"]:.1f}s' if r.get("duration_s") is not None else "—",
             _e(", ".join(f"{k}={v}" for k, v in r["limits"].items())), _e(_fmt_stats(r["stats"]))]
            for r in d.runs], numeric={0}) + "</div>",
        f"<h2>Sample Q&amp;A ({len(d.samples)} random)</h2>",
    ]
    if not d.samples:
        parts.append('<div class="card"><p class="muted">No answers yet.</p></div>')
    for smp in d.samples:
        parts.append(
            f'<div class="card qa"><h3>{_e(smp["question"])}</h3><p>'
            f'<span class="tag">{_e(SOURCE_LABELS.get(smp["source"], smp["source"]))}</span>'
            f'<span class="tag">{_e(smp["category"])}</span><span class="tag">{_e(smp["difficulty"])}</span>'
            f'<span class="tag">{_e(smp["validation"])}</span></p>{_render_answer(smp["answer"])}'
            f'<p class="muted url">Source: <a href="{_e(smp["source_url"])}">{_e(smp["source_url"])}</a></p></div>')
    parts.append("</main></body></html>")
    return "\n".join(parts)


def write_report(s: Settings, db: Database) -> tuple[Path, str]:
    data = collect(s, db)
    path = s.path(s.report.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(data), encoding="utf-8")
    return path, terminal_summary(data, path)
