"""Report history: every run's report kept as data, viewable in report.html and comparable in compare.html.

After every `sftgen` command, `write_report(..., archive=True)` also:

1. snapshots the headline numbers (`metrics`) and the runs this command started;
2. saves the report's content as data: `reports/history/<YYYY-MM-DD_HH-MM-SS>_<command>.js` (local time).
   It is JSON in a one-line JS wrapper, because browsers don't let a page read .json files from
   disk (file://) but do load scripts, so the pages work offline with no server;
3. appends an entry to `reports/history/index.json` (data file, time, command, exit code, runs, metrics);
4. rewrites `reports/compare.html`: every saved report with the change since the one before, and any
   two side by side with a metric-by-metric comparison.

`reports/report.html` shows the latest report and has a picker (newest first) that loads any earlier
one from its data file. `report.keep_history` caps the saved reports (oldest removed first; 0 = keep all).
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from balpipe_common.config import Settings

    from sftgen.report import ReportData

INDEX_FILE = "index.json"
DATA_SUFFIX = ".js"


def _lines(path: Path) -> int | None:
    if not path.is_file():
        return None
    with path.open("rb") as f:
        return sum(1 for line in f if line.strip())


def metrics(d: ReportData, s: Settings) -> dict[str, float | int]:
    """The headline numbers of a report, flat and in display order (missing parts are left out)."""
    qs, an = d.question_status, d.answer_status
    m: dict[str, float | int] = {
        "Pages": d.total_pages,
        "Failed pages": sum(v.get("failed", 0) + v.get("blocked", 0) for v in d.pages.values()),
        "Chunks": sum(n for st in d.chunks.values() for k, n in st.items() if k != "stale"),
        "Questions": d.total_questions,
        "Questions answered": qs.get("answered", 0),
        "Questions pending": qs.get("pending", 0),
        "Questions failed": qs.get("failed", 0),
        "Answers valid": an.get("valid", 0),
        "Answers invalid": an.get("invalid", 0),
        "Answers not checked": an.get("not_checked", 0),
    }
    for label, path in (("dataset.jsonl rows", s.export_path), ("SFT train rows", s.sft_dir / "train.jsonl"),
                        ("SFT eval rows", s.sft_dir / "eval.jsonl")):
        n = _lines(path)
        if n is not None:
            m[label] = n
    if d.coverage:
        m["Graph items grounded"] = d.coverage["grounded"]
    if d.repos:
        m["Repos collected"] = sum(g.get("collected", 0) for g in d.repos["groups"].values())
    if d.eval_set:
        m["Eval items"] = d.eval_set["items"]
    if d.prompts:
        m["Prompts accepted"] = d.prompts["status"].get("accepted", 0)
        m["Prompts rejected"] = d.prompts["status"].get("rejected", 0)
        m["Codebase prompts"] = d.prompts["codebase"]
    if d.answers:
        for k in ("verified", "judged", "failed"):
            m[f"Responses {k}"] = d.answers["status"].get(k, 0)
        m["Preference pairs"] = d.answers["pairs"]
    if d.codebases:
        m["Codebases active"] = d.codebases["status"].get("active", 0)
    if d.corpus:
        m["Mined fixes"] = d.corpus["fixes"]
        if d.corpus.get("manifest"):
            m["Corpus tokens"] = d.corpus["manifest"].get("tokens", 0)
    if d.dataset:
        st = d.dataset["stats"]
        m[f"Dataset {d.dataset['version']} train"] = st["train"]
        m[f"Dataset {d.dataset['version']} eval"] = st["eval"]
        m["DPO pairs"] = st["dpo_pairs"]
    m["LLM tokens"] = sum((u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0) for u in d.usage)
    m["LLM cost (USD)"] = round(sum(u["cost_usd"] or 0 for u in d.usage), 4)
    return m


def history_dir(s: Settings) -> Path:
    return s.report_path.parent / "history"


def compare_page(s: Settings) -> Path:
    return s.report_path.parent / "compare.html"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def data_js(key: str, record: dict[str, Any]) -> str:
    """A report's data as a script that registers it under `key` (loadable from file://)."""
    return (f"(window.BAL_REPORTS = window.BAL_REPORTS || {{}})[{json.dumps(key)}] = "
            f"{json.dumps(record, ensure_ascii=True)};\n")


def read_data(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.index("] = ") + 4:].rstrip().rstrip(";"))


def _migrate(hdir: Path, e: dict[str, Any]) -> dict[str, Any] | None:
    """An entry saved as a whole .html page (first version): keep its content as data, drop the page."""
    page = hdir / e["file"]
    try:
        text = page.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"<main>(.*)</main>", text, re.S)
    body = (m.group(1) if m else text).replace('href="../index.html"', 'href="compare.html"')
    new = page.with_suffix(DATA_SUFFIX).name
    _atomic_write(hdir / new, data_js(new, {"at": e["at"], "command": e["command"], "body": body}))
    page.unlink(missing_ok=True)
    return {**e, "file": new}


def load_entries(s: Settings) -> list[dict[str, Any]]:
    hdir = history_dir(s)
    try:
        entries = json.loads((hdir / INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out, migrated = [], False
    for e in entries:
        if not isinstance(e, dict) or not e.get("file"):
            continue
        if str(e["file"]).endswith(".html"):
            e, migrated = _migrate(hdir, e), True
            if e is None:
                continue
        if (hdir / e["file"]).is_file():
            out.append(e)
    if migrated:
        _atomic_write(hdir / INDEX_FILE, json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return out


def previous(s: Settings) -> dict[str, Any] | None:
    entries = load_entries(s)
    return entries[-1] if entries else None


def delta(now: dict[str, Any], before: dict[str, Any] | None) -> dict[str, float | int]:
    """Metrics that changed since `before` (new metrics count from 0)."""
    if before is None:
        return {}
    out: dict[str, float | int] = {}
    for k, v in now.items():
        diff = v - before.get(k, 0)
        if diff:
            out[k] = round(diff, 4) if isinstance(diff, float) else diff
    return out


def _slug(command: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", command.lower()).strip("-") or "run"


def next_name(s: Settings, command: str, now: datetime) -> str:
    stem = f"{now:%Y-%m-%d_%H-%M-%S}_{_slug(command)}"
    name, n = f"{stem}{DATA_SUFFIX}", 1
    while (history_dir(s) / name).exists():  # two runs in the same second
        n += 1
        name = f"{stem}-{n}{DATA_SUFFIX}"
    return name


def archive(s: Settings, body: str, *, name: str, command: str, exit_code: int, runs: list[dict[str, Any]],
            metrics_now: dict[str, Any], now: datetime, css: str) -> Path:
    """Save the report content `body` as data, record it in index.json, prune, and rebuild compare.html."""
    hdir = history_dir(s)
    at = now.isoformat(timespec="seconds")
    entries = load_entries(s)
    _atomic_write(hdir / name, data_js(name, {"at": at, "command": command, "body": body}))
    entries.append({
        "file": name, "at": at, "command": command, "exit_code": exit_code,
        "runs": [{"id": r["id"], "command": r["command"], "status": r["status"],
                  "duration_s": r.get("duration_s"), "stats": r.get("stats", {})} for r in runs],
        "metrics": metrics_now,
    })
    keep = s.report.keep_history
    if keep and len(entries) > keep:
        for old in entries[:-keep]:
            (hdir / old["file"]).unlink(missing_ok=True)
        entries = entries[-keep:]
    _atomic_write(hdir / INDEX_FILE, json.dumps(entries, indent=1, ensure_ascii=False) + "\n")
    _atomic_write(compare_page(s), render_compare(entries, css))
    legacy = s.report_path.parent / "index.html"       # the first version's history page
    if legacy.is_file() and "<title>Report history</title>" in legacy.read_text(encoding="utf-8")[:2000]:
        legacy.unlink()
    return hdir / name


# --------------------------------------------------------------------------- #
# Shared page parts
# --------------------------------------------------------------------------- #
LOADER_JS = """
window.BAL_REPORTS = window.BAL_REPORTS || {};
function loadReport(file) {
  return new Promise((ok, fail) => {
    if (window.BAL_REPORTS[file]) return ok(window.BAL_REPORTS[file]);
    const s = document.createElement('script');
    s.src = 'history/' + encodeURIComponent(file);
    s.onload = () => window.BAL_REPORTS[file] ? ok(window.BAL_REPORTS[file]) : fail(new Error(file));
    s.onerror = () => fail(new Error(file));
    document.head.appendChild(s);
  });
}
const when = at => at.replace('T', ' ').slice(0, 19);
"""

PICKER_CSS = """
.pickbar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:0 0 16px;padding:10px 14px;
background:var(--card);border:1px solid var(--line);border-radius:10px}
.pickbar label{display:flex;align-items:center;gap:8px;flex:1 1 280px;min-width:0}
.pickbar select,.pickbar button{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);
border-radius:8px;padding:6px 10px;max-width:100%;min-width:0}.pickbar select{flex:1}
.pickbar button{cursor:pointer}.old{border-color:var(--bad)}.old #pickmsg{color:var(--bad)}
"""


def _label(e: dict[str, Any]) -> str:
    return f"{e['at'].replace('T', ' ')[:19]} · {e['command']}" + (" (failed)" if e.get("exit_code") else "")


def picker_html(entries: list[dict[str, Any]], current: str | None) -> str:
    """The report picker at the top of report.html: newest first; the page itself is the first option."""
    if not entries:
        return ""
    opts = []
    newest = list(reversed(entries))
    if current and newest and newest[0]["file"] == current:
        opts.append(f'<option value="">Latest · {html.escape(_label(newest[0]))}</option>')
        newest = newest[1:]
    else:
        opts.append('<option value="">Latest (current database)</option>')
    opts += [f'<option value="{html.escape(e["file"])}">{html.escape(_label(e))}</option>' for e in newest]
    return (
        '<div class="pickbar" id="pickbar"><label for="pick"><strong>Report</strong>'
        f'<select id="pick">{"".join(opts)}</select></label>'
        '<button id="latest" hidden>Back to latest</button>'
        '<a href="compare.html">Compare reports</a><span id="pickmsg" class="muted"></span></div>'
    )


PICKER_JS = LOADER_JS + """
(() => {
  const sel = document.getElementById('pick'); if (!sel) return;
  const R = document.getElementById('report'), orig = R.innerHTML, bar = document.getElementById('pickbar');
  const msg = t => { document.getElementById('pickmsg').textContent = t; };
  async function show(file) {
    sel.value = file; document.getElementById('latest').hidden = !file; bar.classList.toggle('old', !!file);
    try { history.replaceState(null, '', file ? '#r=' + encodeURIComponent(file) : location.pathname); } catch (e) {}
    if (!file) { R.innerHTML = orig; msg(''); return; }
    msg('Loading…');
    try {
      const r = await loadReport(file);
      R.innerHTML = r.body; msg('Earlier report: ' + when(r.at) + ' · ' + r.command);
    } catch (e) { R.innerHTML = orig; msg('Could not load history/' + file); }
  }
  sel.onchange = () => show(sel.value);
  document.getElementById('latest').onclick = () => show('');
  const h = decodeURIComponent((location.hash.match(/r=([^&]+)/) || [])[1] || '');
  if (h && [...sel.options].some(o => o.value === h)) show(h);
})();
"""


# --------------------------------------------------------------------------- #
# compare.html
# --------------------------------------------------------------------------- #
COMPARE_CSS = """
.cmpbar{display:flex;flex-wrap:wrap;gap:12px;align-items:end}.cmpbar label{display:flex;flex-direction:column;
font-size:13px;color:var(--muted);gap:4px;min-width:0;flex:1 1 260px}
select,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:7px 10px;max-width:100%}button{cursor:pointer}button.on{border-color:var(--bar);color:var(--bar)}
.up{color:var(--bar2)}.down{color:var(--bad)}tr.sa td,tr.sb td{background:var(--code)}
.tag.bad{color:var(--bad);border-color:var(--bad)}
.panes{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.panes.single{grid-template-columns:1fr}
.panes.single .pb{display:none}.pane{min-width:0}.pane>.body{border:1px solid var(--line);border-radius:10px;
padding:0 14px 14px;max-height:80vh;overflow:auto;background:var(--bg)}
@media (max-width:800px){.panes{grid-template-columns:1fr}}
"""

COMPARE_JS = LOADER_JS + """
const E = JSON.parse(document.getElementById('entries').textContent);
const $ = id => document.getElementById(id);
const fmt = v => typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(4)) : (v ?? '—');
const label = (e, i) => `#${i + 1} · ${when(e.at)} · ${e.command}` + (e.exit_code ? ' (failed)' : '');
const sign = d => `<span class="${d > 0 ? 'up' : 'down'}">${d > 0 ? '+' : ''}${fmt(d)}</span>`;
function fill(sel, idx) {
  E.map((e, i) => [e, i]).reverse().forEach(([e, i]) => sel.add(new Option(label(e, i), i)));
  sel.value = idx;
}
async function pane(side, e, i) {
  $('l' + side).textContent = label(e, i);
  const body = $('b' + side); body.innerHTML = '<p class="muted">Loading…</p>';
  try { body.innerHTML = (await loadReport(e.file)).body; }
  catch (err) { body.innerHTML = '<p class="muted">Could not load history/' + e.file + '</p>'; }
}
function compare() {
  const ia = +$('a').value, ib = +$('b').value, a = E[ia], b = E[ib];
  const keys = [...new Set([...Object.keys(a.metrics), ...Object.keys(b.metrics)])];
  const rows = keys.map(k => {
    const va = a.metrics[k], vb = b.metrics[k];
    const d = Math.round(((typeof va === 'number' ? va : 0) - (typeof vb === 'number' ? vb : 0)) * 1e4) / 1e4;
    if ($('only').checked && !d && va !== undefined && vb !== undefined) return '';
    return `<tr><td>${k}</td><td class="n">${fmt(vb)}</td><td class="n">${fmt(va)}</td>` +
      `<td class="n">${d ? sign(d) : '<span class="muted">0</span>'}</td></tr>`;
  }).join('');
  $('cmp').innerHTML = rows || '<tr><td colspan="4" class="muted">No differences in the headline numbers.</td></tr>';
  document.querySelectorAll('#runs tbody tr').forEach(tr => {
    tr.classList.toggle('sa', +tr.dataset.i === ia); tr.classList.toggle('sb', +tr.dataset.i === ib);
  });
  try { history.replaceState(null, '', `#a=${ia}&b=${ib}`); } catch (e) {}
  pane('a', a, ia); if (!$('panes').classList.contains('single')) pane('b', b, ib);
}
function setMode(single) {
  $('panes').classList.toggle('single', single);
  $('m1').classList.toggle('on', single); $('m2').classList.toggle('on', !single);
  if (!single) pane('b', E[+$('b').value], +$('b').value);
}
if (E.length) {
  const h = Object.fromEntries(location.hash.slice(1).split('&').map(x => x.split('=')));
  const ok = v => v !== undefined && v !== '' && +v >= 0 && +v < E.length;
  fill($('a'), ok(h.a) ? h.a : E.length - 1);
  fill($('b'), ok(h.b) ? h.b : Math.max(0, E.length - 2));
  $('a').onchange = compare; $('b').onchange = compare; $('only').onchange = compare;
  $('m1').onclick = () => setMode(true); $('m2').onclick = () => setMode(false);
  $('swap').onclick = () => { const t = $('a').value; $('a').value = $('b').value; $('b').value = t; compare(); };
  document.querySelectorAll('[data-pick]').forEach(btn => btn.onclick = () => {
    $(btn.dataset.pick).value = btn.closest('tr').dataset.i; compare(); $('compare').scrollIntoView();
  });
  $('panes').classList.toggle('single', E.length < 2);
  $('m1').classList.toggle('on', E.length < 2); $('m2').classList.toggle('on', E.length >= 2);
  compare();
}
"""

KEY_METRICS = ["Questions", "Questions answered", "Answers valid", "dataset.jsonl rows", "Prompts accepted",
               "Responses verified", "LLM cost (USD)"]


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _num(v: Any) -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.4f}"
    return f"{int(v):,}" if isinstance(v, (int, float)) else "—"


def _change(d: Any) -> str:
    if not d:
        return ""
    return f' <span class="{"up" if d > 0 else "down"}">{"+" if d > 0 else ""}{_num(d)}</span>'


def _run_stats(runs: list[dict[str, Any]]) -> str:
    out = []
    for r in runs:
        st = ", ".join(f"{k} {v}" for k, v in (r.get("stats") or {}).items() if isinstance(v, (int, float)))
        bad = r.get("status") != "succeeded"
        out.append(f'<span class="tag{" bad" if bad else ""}">#{_e(r["id"])} {_e(r["command"])}'
                   f'{" · " + _e(r["status"]) if bad else ""}</span> <span class="muted">{_e(st)}</span>')
    return "<br>".join(out) or '<span class="muted">no pipeline run (report only)</span>'


def render_compare(entries: list[dict[str, Any]], css: str) -> str:
    keys = [k for k in KEY_METRICS if any(k in e["metrics"] for e in entries)]
    rows = []
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        before = entries[i - 1]["metrics"] if i else None
        cells = "".join(f'<td class="n">{_num(e["metrics"].get(k))}'
                        f'{_change(e["metrics"].get(k, 0) - before.get(k, 0)) if before and k in e["metrics"] else ""}'
                        "</td>" for k in keys)
        status = '<span class="tag bad">failed</span>' if e.get("exit_code") else '<span class="tag">ok</span>'
        rows.append(
            f'<tr data-i="{i}"><td class="n">{i + 1}</td><td style="white-space:nowrap">{_e(_label(e)[:19])}</td>'
            f'<td><a href="report.html#r={_e(e["file"])}">{_e(e["command"])}</a> {status}'
            f'<div style="margin-top:4px">{_run_stats(e.get("runs", []))}</div></td>{cells}'
            '<td style="white-space:nowrap"><button data-pick="a">A</button> <button data-pick="b">B</button></td></tr>')
    head = "".join(f'<th class="n">{_e(k)}</th>' for k in keys)
    data = json.dumps(entries, ensure_ascii=False).replace("</", "<\\/")
    body = (
        '<h2 id="compare">Compare</h2><div class="card"><div class="cmpbar">'
        '<label>A (newer)<select id="a"></select></label><label>B (older)<select id="b"></select></label>'
        '<div><button id="swap">Swap</button> <button id="m1">A only</button> <button id="m2">Side by side</button>'
        ' <label style="display:inline-flex;flex-direction:row;align-items:center;gap:6px;min-width:0">'
        '<input type="checkbox" id="only" checked> Only changed</label></div></div>'
        '<div class="scroll" style="margin-top:12px"><table><thead><tr><th>Metric</th><th class="n">B</th>'
        '<th class="n">A</th><th class="n">A − B</th></tr></thead><tbody id="cmp"></tbody></table></div></div>'
        '<div id="panes" class="panes"><div class="pane"><p><strong>A</strong> <span id="la" class="muted"></span>'
        '</p><div class="body" id="ba"></div></div><div class="pane pb"><p><strong>B</strong> '
        '<span id="lb" class="muted"></span></p><div class="body" id="bb"></div></div></div>'
        f"<h2>All reports ({len(entries)})</h2>"
        '<div class="card scroll"><table id="runs"><thead><tr><th class="n">#</th><th>When</th><th>Command and runs</th>'
        f'{head}<th>Compare</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="muted">Numbers are totals in the database after that command; coloured numbers are the change '
        "since the report before it.</p>"
    ) if entries else '<div class="card"><p class="muted">No reports saved yet. Run any sftgen command.</p></div>'
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Compare reports</title><style>{css}{COMPARE_CSS}</style></head><body><main>"
        "<h1>Compare pipeline reports</h1>"
        '<p class="muted">A report is saved after every sftgen command. Pick any two to compare. '
        '<a href="report.html">Latest report</a></p>'
        f"{body}"
        f'<script type="application/json" id="entries">{data}</script><script>{COMPARE_JS}</script>'
        "</main></body></html>"
    )
