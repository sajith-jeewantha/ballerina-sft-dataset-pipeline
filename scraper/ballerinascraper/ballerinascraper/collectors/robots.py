"""Minimal robots.txt matcher (RFC 9309).

Python's `urllib.robotparser` uses only the first `User-agent: *` group, but
real files (ballerina.io's included) repeat it. Here every group that applies
to us is merged, and the longest matching rule wins (`Allow` on ties).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _pattern(path: str) -> re.Pattern[str]:
    anchored = path.endswith("$")
    body = re.escape(path.rstrip("$")).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    rules: list[tuple[str, bool]] = field(default_factory=list)  # (path pattern, allow)

    @classmethod
    def parse(cls, text: str, user_agent: str) -> RobotsRules:
        token = user_agent.split("/", 1)[0].strip().lower()
        specific: list[tuple[str, bool]] = []
        generic: list[tuple[str, bool]] = []
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:  # a new group starts
                    agents, in_rules = [], False
                agents.append(value.lower())
            elif key in ("allow", "disallow"):
                in_rules = True
                if not value:
                    continue  # "Disallow:" with no path allows everything
                rule = (value, key == "allow")
                if token in agents:
                    specific.append(rule)
                elif "*" in agents:
                    generic.append(rule)
        return cls(specific or generic)

    def allowed(self, path: str) -> bool:
        best_len, best_allow = -1, True
        for pattern, allow in self.rules:
            if _pattern(pattern).match(path):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len, best_allow = length, allow
        return best_allow
