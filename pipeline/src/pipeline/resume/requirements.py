"""Literal, conservative note options; no inferred qualifications or new facts."""

import re
from typing import Any

from pipeline.resume.privacy import tailoring_facts
from pipeline.resume.schema import MasterResume
from pipeline.resume.validate import TECH, evidence_map, plain

# Avoid asserting a gap merely because a common spelling differs. These aliases
# suppress uncertain gaps only; they never authorize a matched claim or a skill.
ALIASES = (
    ("go", "golang"),
    ("javascript", "js"),
    ("typescript", "ts"),
    ("node", "nodejs", "node.js"),
    ("postgres", "postgresql"),
    ("kubernetes", "k8s"),
    ("react", "reactjs", "react.js"),
)


def options(
    master: MasterResume, description: str, allowed_ids: set[str]
) -> dict[str, Any]:
    """Offer at most eight matches/gaps each, quoting actual technology mentions.

    The LLM can select these short notes or leave arrays empty. A missing literal
    is a spelling-based evidence gap, not proof of lacking a broader capability.
    All results still pass the unchanged validator before rendering.
    """
    all_evidence = {
        key: plain(value).casefold() for key, value in evidence_map(master).items()
    }
    facts, references = tailoring_facts(master)
    permitted: dict[str, str] = {}
    for key, pointer in references.items():
        if key not in allowed_ids:
            continue
        value: Any = facts
        for part in pointer.lstrip("/").split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
        permitted[key] = plain(value).casefold()
    terms = set(TECH) | {skill for group in master.skills for skill in group.items}
    job = plain(description)
    quotes: dict[str, tuple[int, str]] = {}
    for term in sorted(terms):
        if not term.strip() or len(term) > 240:
            continue
        match = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", job, re.I)
        if match:
            quotes.setdefault(match.group().casefold(), (match.start(), match.group()))
    matches: list[dict[str, Any]] = []
    gaps: list[str] = []
    for _, quote in sorted(quotes.values()):
        phrase = quote.casefold()
        ids = [
            key
            for key, value in permitted.items()
            if phrase in value and phrase in all_evidence.get(key, "")
        ]
        if ids:
            if len(matches) < 8:
                matches.append({"requirement": quote, "evidence_ids": ids[:1]})
            continue
        if any(phrase in value for value in all_evidence.values()):
            continue  # Private/immutable evidence exists; do not call it a gap.
        variants = next((group for group in ALIASES if phrase in group), ())
        if any(
            re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", value)
            for alias in variants
            for value in all_evidence.values()
        ):
            continue
        if len(gaps) < 8:
            gaps.append(quote)
    return {"matched": matches, "gaps": gaps}
