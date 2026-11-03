"""Minimal parser for the agent's handoff report (Outcome / Changes made /
Side-effect commands / Open issues). Phase 5 builds the full self-report scoring on it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SECTIONS = ("outcome", "changes made", "side-effect commands", "open issues")
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(outcome|changes made|side[- ]effect commands|open issues)\s*(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*(.*)$",
    re.I,
)


@dataclass
class HandoffReport:
    raw: str
    outcome: str | None                 # completed | partial | not_completed | None (missing/unparseable)
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def follows_template(self) -> bool:
        return self.outcome is not None and all(s in self.sections for s in SECTIONS)


def normalize_outcome(value: str) -> str | None:
    v = re.sub(r"[*_`#.:]", " ", value).strip().lower()
    if re.match(r"^(not\s+completed?|incomplete|not\s+done|failed)\b", v):
        return "not_completed"
    if re.match(r"^partial(ly)?\b", v):
        return "partial"
    if re.match(r"^(completed?|done|resolved|fixed)\b", v):
        return "completed"
    return None


def parse_report(text: str) -> HandoffReport:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            current = match.group(1).lower().replace("side effect", "side-effect")
            sections[current] = [match.group(2)] if match.group(2).strip() else []
        elif current is not None:
            sections[current].append(line)
    joined = {k: "\n".join(v).strip() for k, v in sections.items()}
    outcome_text = joined.get("outcome", "")
    first_line = outcome_text.splitlines()[0] if outcome_text else ""
    return HandoffReport(raw=text, outcome=normalize_outcome(first_line) if first_line else None, sections=joined)


def missing_mentions(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in terms if t.lower() not in lowered]
