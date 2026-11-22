"""The dataset card: `CARD.md` for people, `card.json` for the report and tools.

It lists what the dataset holds (sources, task types, difficulty, context mode, verification), coverage
per graph node and per item, the licences of everything inside it, the graph and toolchain versions,
the models that wrote it, decontamination against the eval set, and the gaps left for gap-fill.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.ids import utc_now_iso
from balpipe_common.licences import LicenceRegister

# documentation sources -> the repository whose licence covers them (licence register)
DOCS_REPOS = {"bbe": "ballerina-platform/ballerina-distribution", "learn": "ballerina-platform/ballerina-dev-website",
              "spec": "ballerina-platform/ballerina-spec", "central": "ballerina-platform/module-* (Central API docs)",
              "github": "ballerina-platform (repository READMEs)"}


def _graph(s: Settings, db: Database) -> dict[str, Any]:
    src = s.path(s.graph.source_file)
    info = json.loads(src.read_text(encoding="utf-8")) if src.is_file() else {}
    return {"version": db.scalar("SELECT graph_version FROM graph_items LIMIT 1"), "repo": info.get("repo"),
            "commit": info.get("commit"), "sha256": info.get("sha256"), "schema": info.get("schema_version")}


def _licences(s: Settings, meta: list[dict[str, Any]], corpus: dict[str, Any] | None) -> dict[str, Any]:
    reg = LicenceRegister(s.path(s.repos.register_file))
    docs = Counter(m["docs_source"] for m in meta if m.get("docs_source"))
    docs_rows = []
    for src, n in sorted(docs.items()):
        repo = DOCS_REPOS.get(src, src)
        row = reg.get(repo) if "/" in repo and "*" not in repo and "(" not in repo else None
        docs_rows.append({"source": src, "samples": n, "repo": repo,
                          "licence": (row or {}).get("spdx") or "per repository",
                          "verdict": (row or {}).get("verdict") or ""})
    code = Counter(m["licence"] or "?" for m in meta if m.get("repo"))
    return {"docs": docs_rows, "embedded_code": dict(code.most_common()),
            "corpus": {k: v["tokens"] for k, v in (corpus or {}).get("by_licence", {}).items()}}


def _models(meta: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for m in meta:
        for role in ("teacher", "writer", "reviewer"):
            if m.get(role):
                out[role].add(m[role])
    return {k: sorted(v) for k, v in out.items()}


def _eval_info(s: Settings, db: Database) -> dict[str, Any]:
    suites = {r[0]: r[1] for r in db.query("SELECT suite, COUNT(*) FROM eval_items WHERE status = 'active' "
                                           "GROUP BY 1 ORDER BY 1")}
    fps = db.scalar("SELECT COUNT(*) FROM fingerprints") or 0
    return {"dir": str(s.eval_dir.relative_to(s.data_root)), "suites": suites, "fingerprints": fps}


def _nodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for r in rows:
        n = nodes.setdefault(r["node"], {"node": r["node"], "title": r["node_title"], "priority": r["priority"],
                                         "in_scope": r["in_scope"], "items": 0, "covered": 0, "samples": 0})
        n["items"] += 1
        n["covered"] += r["samples"] > 0
        n["samples"] += r["samples"]
    return sorted((n for n in nodes.values() if n["samples"] or n["in_scope"]),
                  key=lambda n: (-n["samples"], n["node"]))


def _md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|",
            *("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows)]


def _dist(title: str, c: dict[str, int]) -> list[str]:
    total = sum(c.values()) or 1
    return [f"**{title}**", "", *_md_table(["", "Samples", "Share"],
                                           [[k, f"{v:,}", f"{100 * v / total:.0f}%"] for k, v in c.items()]), ""]


def write_card(s: Settings, db: Database, root: Path, *, kept: list[Any], meta: list[dict[str, Any]],
               splits: dict[str, str], stats: dict[str, Any], coverage_rows: list[dict[str, Any]],
               gaps: list[dict[str, Any]], attribution: list[dict[str, Any]], corpus: dict[str, Any] | None,
               decontam: dict[str, Any], sets: list[str]) -> dict[str, Any]:
    cfg = s.assemble
    count = lambda key: dict(Counter(m[key] or "—" for m in meta).most_common())  # noqa: E731
    by_priority = defaultdict(lambda: {"items": 0, "covered": 0, "samples": 0, "in_scope": 0, "in_scope_covered": 0})
    for r in coverage_rows:
        b = by_priority[r["priority"]]
        b["items"] += 1
        b["covered"] += r["samples"] > 0
        b["samples"] += r["samples"]
        b["in_scope"] += r["in_scope"]
        b["in_scope_covered"] += r["in_scope"] and r["samples"] > 0
    card = {
        "version": cfg.version, "created_at": utc_now_iso(), "swan_lake": s.sources.swan_lake,
        "student": "Qwen/Qwen3.8-27B", "format": "chat messages (system, user, assistant)",
        "system_prompt": s.sft.system_prompt.strip(), "prompt_sets": sets, "graph": _graph(s, db),
        "models": _models(meta), "stats": stats,
        "composition": {k: count(k) for k in ("source", "task_type", "difficulty", "context_mode", "verification",
                                              "split")},
        "priority": dict(by_priority), "nodes": _nodes(coverage_rows),
        "licences": _licences(s, meta, corpus), "attribution": attribution,
        "eval": _eval_info(s, db), "decontamination": decontam,
        "gaps": {"items": len(gaps), "missing": sum(g["missing"] for g in gaps),
                 "by_priority": dict(Counter(g["priority"] for g in gaps))},
        "corpus": {k: corpus.get(k) for k in ("version", "files", "tokens", "weighted_tokens", "repos",
                                               "generated_weight")} if corpus else None,
        "config": cfg.model_dump(),
    }
    (root / "card.json").write_text(json.dumps(card, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "CARD.md").write_text(render(card, coverage_rows), encoding="utf-8")
    return card


def render(c: dict[str, Any], coverage_rows: list[dict[str, Any]]) -> str:
    st, g = c["stats"], c["graph"]
    models = c["models"]
    lines = [
        f"# Ballerina SFT dataset {c['version']}", "",
        f"Supervised fine-tuning data for **{c['student']}** on Ballerina Swan Lake **{c['swan_lake']}**, "
        f"built {c['created_at'][:10]} by the ballerina-sft-dataset-pipeline (`sftgen assemble`).", "",
        "## Contents", "",
        *_md_table(["File", "What", "Rows"], [
            ["`sft/train.jsonl`", "training samples, `{\"id\", \"messages\"}`", f"{st['train']:,}"],
            ["`sft/eval.jsonl`", "validation split (by codebase / page / prompt group)", f"{st['eval']:,}"],
            ["`metadata.jsonl`", "per sample: source, graph items (`covers`), task, difficulty, context mode, "
                                 "verification, licence", f"{st['train'] + st['eval']:,}"],
            ["`dpo/pairs.jsonl`", "preference pairs (passing repair vs first failed answer), TRL format",
             f"{st['dpo_pairs']:,}"],
            ["`cpt/`", "raw Ballerina code corpus for continued pretraining (`weight` per file)",
             f"{st['cpt_files']:,} files, ~{st['cpt_tokens'] / 1e6:.1f}M tokens" if st["cpt_files"] else "not included"],
            ["`coverage.csv`", "samples per graph item", ""],
            ["`gaps.jsonl`", "in-scope items under their prompt budget (gap-fill list)", f"{c['gaps']['items']:,}"],
            ["`ATTRIBUTION.md`", "third-party code inside samples", f"{len(c['attribution'])} repos"],
        ]), "",
        "**Format.** Each row's `messages` is `system · user · assistant`. Apply the Qwen3.8 chat template "
        "(`tokenizer.apply_chat_template`); the answers have no reasoning trace, so train in non-thinking mode "
        "(`enable_thinking=False`). Use the same system prompt at inference:", "",
        "> " + c["system_prompt"].replace("\n", "\n> "), "",
        "## How it was made", "",
        f"* **Prompts:** item-driven per knowledge-graph item, written by {', '.join(models.get('writer', [])) or '—'} "
        f"and reviewed by {', '.join(models.get('reviewer', [])) or '—'} (prompt sets: {', '.join(c['prompt_sets'])}). "
        "Codebase tasks come from verified states (compile error, unfinished feature, version drift, failing test) "
        "of real projects in the codebase pool.",
        f"* **Answers:** teacher {', '.join(models.get('teacher', [])) or '—'}. Code is checked with `bal` "
        f"{c['swan_lake']} and repaired with the real compiler errors; prose is checked by a judge of a different "
        "model family.",
        f"* **Knowledge graph:** `{g.get('version')}`"
        + (f" ({g['repo']} @ `{g['commit'][:12]}`, schema {g.get('schema')}, sha256 `{(g.get('sha256') or '')[:16]}…`)"
           if g.get("repo") and g.get("commit") else "") + ".",
        f"* **Dedup:** {st['duplicates']['exact']} exact and {st['duplicates']['near']} near-duplicate prompts removed.",
        f"* **Balance:** at most {', '.join(f'{k} {v}' for k, v in c['config']['caps'].items())} samples per graph "
        f"item, spread over task, difficulty and context mode ({st['over_cap']:,} samples over the caps left out).",
        "",
        "## Composition", "",
        *_dist("Source", c["composition"]["source"]),
        *_dist("Task type", c["composition"]["task_type"]),
        *_dist("Difficulty", c["composition"]["difficulty"]),
        *_dist("Context mode", c["composition"]["context_mode"]),
        *_dist("Verification", c["composition"]["verification"]),
        "## Coverage", "",
        *_md_table(["Priority", "Items", "With samples", "In scope", "In scope with samples", "Samples"], [
            [p, f"{b['items']:,}", f"{b['covered']:,}", f"{b['in_scope']:,}", f"{b['in_scope_covered']:,}",
             f"{b['samples']:,}"] for p, b in sorted(c["priority"].items())]), "",
        f"{c['gaps']['items']:,} in-scope items are under their prompt budget ({c['gaps']['missing']:,} samples short); "
        "`gaps.jsonl` lists them for gap-fill (`sftgen generate-prompts`, then `answer-prompts`).", "",
        "**Per node**", "",
        *_md_table(["Node", "Priority", "Items", "With samples", "Samples"],
                   [[f"`{n['node']}`", n["priority"], n["items"], n["covered"], n["samples"]] for n in c["nodes"]]),
        "", "<details><summary>Per item (items with samples or in scope)</summary>", "",
        *_md_table(["Item", "Priority", "Samples", "Train", "Eval", "Codebase", "Tasks"], [
            [f"`{r['ref']}`", r["priority"], r["samples"], r["train"], r["eval"], r["codebase"],
             ", ".join(f"{k} {v}" for k, v in r["tasks"].items())]
            for r in coverage_rows if r["samples"] or r["in_scope"]]),
        "", "</details>", "",
        "## Licences", "",
        "**Documentation the answers are grounded in** (Q&A samples by source):", "",
        *_md_table(["Source", "Samples", "Covered by", "Licence", "Register verdict"],
                   [[d["source"], d["samples"], d["repo"], d["licence"], d["verdict"]] for d in c["licences"]["docs"]]),
        "",
        "**Third-party code inside samples** (samples by licence): "
        + (", ".join(f"{k} {v}" for k, v in c["licences"]["embedded_code"].items()) or "none")
        + ". Every repository and commit is in `ATTRIBUTION.md`.", "",
        *([f"**CPT corpus** (tokens by licence): "
           + ", ".join(f"{k} {v:,}" for k, v in c["licences"]["corpus"].items()) + "; see `cpt/ATTRIBUTION.md`.", ""]
          if c["licences"]["corpus"] else []),
        "Code repositories passed the licence gate (permissive licences only; see `docs/licence-register.csv`). "
        "A documentation source whose register verdict isn't `safe` still needs a person to confirm its licence "
        "before the dataset is shared. Benchmarks (HumanEval, MBPP) are used only for evaluation and are not in "
        "this dataset.", "",
        "## Evaluation and decontamination", "",
        f"The held-out eval set is in `{c['eval']['dir']}` "
        f"({', '.join(f'{k} {v}' for k, v in c['eval']['suites'].items()) or 'none'}). "
        f"Every sample was checked against {c['eval']['fingerprints']:,} eval fingerprints (exact, MinHash and "
        f"containment): {st['eval_overlap_dropped']:,} dropped, **{st['eval_overlap_in_export']} overlapping in the "
        "files written**. Eval-split codebases never feed training samples.", "",
        "## Limitations", "",
        "* Answers are model-written. Code was compiled (tests type-checked; executed only when `answers.run_tests` "
        "was on in a sandbox), and prose was judged, but neither guarantees the behaviour is right.",
        "* Coverage follows the knowledge graph; items without grounding sources have few or no samples.",
        "* Q&A samples (`source = question`) were not reviewed by a judge; code in them is compile-checked when "
        "`validate` ran.",
    ]
    if c["composition"]["source"].get("mined_fix"):
        lines.append("* Mined fixes (`source = mined_fix`) come from commit history and were not built at their commit.")
    return "\n".join(lines) + "\n"
