"""Coverage planner: how many prompts each graph item needs, and of which kinds.

* **Scope:** the nodes in `prompts.nodes` (the pilot: the HTTP cluster), or every active node.
* **Budget:** prompts per item by the node's priority (`prompts.budgets`: core 10, important 6,
  long tail 3).
* **Slots:** the budget is split over the task mix (`prompts.task_mix`, largest remainder) and
  the item's own difficulties (round-robin), so an item tagged beginner+intermediate gets both.
* **Gaps:** a slot is filled by an accepted prompt. Rejected and duplicate prompts leave the slot
  open for a gap-fill round, up to `prompts.max_rounds` writer calls per item.

No model calls here: `plan()` is what `plan-prompts` prints and `generate-prompts` works through.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from balpipe_common.config import ConfigError, PromptsCfg, Settings
from balpipe_common.db import Database

PRIORITY_ORDER = {"core": 0, "important": 1, "long_tail": 2}


@dataclass
class ItemPlan:
    item: dict[str, Any]
    node: dict[str, Any]
    slots: list[tuple[str, str]]                   # planned (task_type, difficulty)
    accepted: Counter[tuple[str, str]] = field(default_factory=Counter)
    rounds: int = 0                                # writer calls made so far

    @property
    def ref(self) -> str:
        return str(self.item["ref"])

    @property
    def missing(self) -> list[tuple[str, str]]:
        """Planned slots not yet filled by an accepted prompt, in plan order."""
        left = Counter(self.slots) - self.accepted
        out = []
        for slot in self.slots:
            if left[slot] > 0:
                out.append(slot)
                left[slot] -= 1
        return out


def task_sequence(n: int, mix: dict[str, float]) -> list[str]:
    """n task types in proportion to the mix (largest remainder), interleaved."""
    total = sum(mix.values()) or 1.0
    exact = {t: n * w / total for t, w in mix.items()}
    counts = {t: math.floor(v) for t, v in exact.items()}
    for t in sorted(exact, key=lambda t: (-(exact[t] - counts[t]), list(mix).index(t)))[: n - sum(counts.values())]:
        counts[t] += 1
    out: list[str] = []
    while len(out) < n:
        for t in mix:
            if counts[t] > 0:
                out.append(t)
                counts[t] -= 1
    return out


def slots_for(priority: str, difficulties: list[str], cfg: PromptsCfg) -> list[tuple[str, str]]:
    n = cfg.budgets.get(priority, 0)
    diffs = difficulties or ["intermediate"]
    return [(task, diffs[i % len(diffs)]) for i, task in enumerate(task_sequence(n, cfg.task_mix))]


def load_nodes(s: Settings) -> dict[str, dict[str, Any]]:
    """Node id -> {title, summary, modules, out_of_scope, priority} from graph/graph.json."""
    data = json.loads(s.path(s.graph.path).read_text(encoding="utf-8"))
    return {n["id"]: {"id": n["id"], "title": n.get("title", ""), "summary": n.get("summary", ""),
                      "modules": n.get("modules") or [], "out_of_scope": n.get("out_of_scope") or [],
                      "priority": n.get("priority", ""), "type": n.get("type", "")}
            for n in data.get("nodes", [])}


def plan(s: Settings, db: Database) -> list[ItemPlan]:
    cfg = s.prompts
    if not db.scalar("SELECT COUNT(*) FROM graph_items"):
        raise ConfigError("the graph isn't loaded: run `sftgen graph sync` first")
    nodes = load_nodes(s)
    unknown = [n for n in cfg.nodes if n not in nodes]
    if unknown:
        raise ConfigError(f"prompts.nodes has ids that aren't in the graph: {unknown}")
    scope = cfg.nodes or sorted(nodes)
    order = {n: i for i, n in enumerate(scope)}
    marks = ",".join("?" for _ in scope)
    items = [dict(r) for r in db.query(
        f"SELECT * FROM graph_items WHERE node_id IN ({marks}) AND status != 'retired'", tuple(scope))]
    items.sort(key=lambda r: (PRIORITY_ORDER.get(r["priority"], 9), order[r["node_id"]], r["ref"]))
    accepted: dict[str, Counter[tuple[str, str]]] = {}
    for r in db.query("SELECT ref, task_type, difficulty, COUNT(*) AS n FROM prompts WHERE set_name = ? "
                      "AND status = 'accepted' AND eval_overlap IS NULL AND context_mode = 'standalone' GROUP BY 1, 2, 3", (cfg.set_name,)):
        accepted.setdefault(r["ref"], Counter())[(r["task_type"], r["difficulty"])] += r["n"]
    rounds = {r[0]: r[1] for r in db.query("SELECT ref, MAX(round) FROM prompts WHERE set_name = ? "
                                           "AND context_mode = 'standalone' GROUP BY 1",
                                           (cfg.set_name,))}
    plans = []
    for it in items:
        slots = slots_for(it["priority"], json.loads(it["difficulty"] or "[]"), cfg)
        if slots:
            plans.append(ItemPlan(it, nodes[it["node_id"]], slots, accepted.get(it["ref"], Counter()),
                                  rounds.get(it["ref"], 0)))
    return plans


def summary(plans: list[ItemPlan], cfg: PromptsCfg) -> dict[str, Any]:
    by_node: dict[str, dict[str, int]] = {}
    for p in plans:
        d = by_node.setdefault(p.item["node_id"], {"items": 0, "budget": 0, "accepted": 0, "missing": 0,
                                                   "given_up": 0})
        d["items"] += 1
        d["budget"] += len(p.slots)
        d["accepted"] += sum(min(n, Counter(p.slots)[k]) for k, n in p.accepted.items())
        miss = len(p.missing)
        d["missing"] += miss
        if miss and p.rounds >= cfg.max_rounds:
            d["given_up"] += miss
    totals = {k: sum(d[k] for d in by_node.values()) for k in ("items", "budget", "accepted", "missing", "given_up")}
    return {"set": cfg.set_name, "nodes": by_node, **totals}
