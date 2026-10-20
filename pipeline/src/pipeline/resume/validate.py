"""Deterministic provenance and bounds; no semantic trust in LLM output."""

import re
from collections import Counter
from dataclasses import dataclass

from pipeline.resume.schema import MasterResume, Tailoring, TailoringProposal

FIGURES = re.compile(
    r"(?<!\w)(?:[$£€₦]\s*)?\d+(?:[.,]\d+)*(?:\s*(?:%|k\b|million\b|billion\b))?",
    re.I,
)
NAMES = re.compile(r"\b[A-Z][A-Za-z0-9+#.\-]*\b|\b\w*(?:\+\+|#)\b")
TECH = {
    "aws",
    "azure",
    "gcp",
    "redis",
    "kafka",
    "docker",
    "kubernetes",
    "python",
    "java",
    "javascript",
    "typescript",
    "react",
    "node",
    "nodejs",
    "golang",
    "rust",
    "postgresql",
    "mysql",
    "mongodb",
    "terraform",
    "graphql",
    "django",
    "fastapi",
    "elixir",
    "ruby",
    "rails",
    "git",
    "sql",
    "nosql",
    "linux",
}
SENIORITY = {
    "led",
    "managed",
    "architected",
    "mentored",
    "expert",
    "senior",
    "principal",
    "owned",
}


def plain(text: str) -> str:
    return " ".join(text.split())


def terms(text: str) -> set[str]:
    return set(re.findall(r"[\w+#.-]+", text.casefold()))


def figures(text: str) -> Counter[str]:
    return Counter(
        re.sub(r"\s+", "", m.group()).casefold() for m in FIGURES.finditer(text)
    )


@dataclass(frozen=True)
class Limits:
    max_summary_chars: int
    bullet_chars: dict[str, int]
    max_bullets: dict[str, int]

    @classmethod
    def from_master(cls, master: MasterResume) -> "Limits":
        return cls(
            len(master.summary),
            {
                b.id: len(b.text)
                for e in master.experience + master.projects
                for b in e.bullets
            },
            {e.id: len(e.bullets) for e in master.experience},
        )


class TruthError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(dict.fromkeys(errors))
        # Errors contain field IDs/check names, never resume or job text.
        super().__init__("; ".join(self.errors))


def slot_length_errors(master: MasterResume, output: Tailoring) -> list[str]:
    """Check text lengths before rendering into fixed DOCX editable regions."""
    errors = []
    if len(output.summary) > len(master.summary):
        errors.append("summary: length overflow")
    original_segments = master.summary.split("\n")
    output_segments = output.summary.split("\n")
    if len(output_segments) != len(original_segments) or any(
        len(value) > len(original_segments[index])
        for index, value in enumerate(output_segments[: len(original_segments)])
    ):
        errors.append("summary: editable paragraph slot overflow")
    known = {
        bullet.id: bullet.text for role in master.experience for bullet in role.bullets
    }
    for role in output.experience:
        for bullet in role.bullets:
            original = known.get(bullet.id)
            if original is not None and len(bullet.text) > len(original):
                errors.append(f"{bullet.id}: length overflow")
    return errors


def evidence_map(master: MasterResume) -> dict[str, str]:
    evidence = {"summary": master.summary}
    evidence.update({"skill:" + s: s for g in master.skills for s in g.items})
    for role in master.experience:
        for field in ("employer", "title", "location", "dates"):
            value = getattr(role, field)
            if value:
                evidence[f"role:{role.id}:{field}"] = value
        evidence.update({b.id: b.text for b in role.bullets})
    for project in master.projects:
        evidence[f"project:{project.id}:name"] = project.name
        if project.description:
            evidence[f"project:{project.id}:description"] = project.description
        for i, line in enumerate(project.detail_lines):
            evidence[f"project:{project.id}:detail:{i}"] = line
        evidence.update({b.id: b.text for b in project.bullets})
    for i, education in enumerate(master.education):
        for field in ("institution", "degree", "dates", "location"):
            value = getattr(education, field)
            if value:
                evidence[f"education:{i}:{field}"] = value
    for i, certification in enumerate(master.certifications):
        for field in ("name", "issuer", "dates"):
            value = getattr(certification, field)
            if value:
                evidence[f"certification:{i}:{field}"] = value
    return evidence


def _wording_errors(
    output: str,
    original: str,
    allowed: list[str],
    label: str,
    master_words: set[str],
    max_chars: int,
    generated: bool = False,
) -> list[str]:
    errors: list[str] = []
    if any(ord(c) < 32 and c not in original for c in output) or any(
        c in output for c in ("{{", "}}", "{%", "%}")
    ):
        errors.append(f"{label}: control characters/template markup forbidden")
    if len(output) > max_chars:
        errors.append(f"{label}: length overflow")
    if not output.strip() and original.strip():
        errors.append(f"{label}: empty wording")
    if figures(output) - figures(original):
        errors.append(f"{label}: invented or changed metric")
    if not generated and (terms(output) & SENIORITY) - terms(original):
        errors.append(f"{label}: unsupported seniority claim")
    if generated:
        return errors
    new_tech = (terms(output) & TECH) - master_words
    new_names = {n.casefold() for n in NAMES.findall(output)} - master_words
    # Sentence-initial ordinary words are still subject to the stronger exact
    # approved-wording check below. Do not incorrectly call them technologies.
    if new_tech:
        errors.append(f"{label}: technology not in master")
    ordinary_starters = {
        "built",
        "developed",
        "used",
        "created",
        "implemented",
        "supported",
        "improved",
        "optimized",
        "collaborated",
        "worked",
    }
    if new_names - ordinary_starters:
        errors.append(f"{label}: new proper noun or unapproved wording")
    if plain(output) not in {plain(t) for t in [original, *allowed]}:
        errors.append(f"{label}: rephrasing lacks owner-approved provenance")
    return errors


def validate_tailoring(
    master: MasterResume,
    output: Tailoring,
    description: str,
    limits: Limits | None = None,
    generated_summary: str | None = None,
) -> Tailoring:
    limits = limits or Limits.from_master(master)
    errors: list[str] = []
    if not master.reviewed:
        errors.append("master: review required before tailoring")
    errors.extend(slot_length_errors(master, output))
    all_skills = {s.casefold(): s for g in master.skills for s in g.items}
    selected = [s.casefold() for s in output.skills_order]
    for i, skill in enumerate(selected):
        if skill not in all_skills:
            errors.append(
                f"skills_order: invented skill at index {i}; "
                "copy one exact master skill item, never a group name or synonym"
            )
    if len(selected) != len(set(selected)):
        errors.append("skills_order: duplicate skill")
    roles = {e.id: e for e in master.experience}
    role_ids = [e.id for e in output.experience]
    if set(role_ids) != set(roles) or len(role_ids) != len(set(role_ids)):
        errors.append("experience: unknown, missing, or duplicate role ID")
    project_ids = [p.id for p in master.projects]
    if set(output.projects_order) != set(project_ids) or len(
        output.projects_order
    ) != len(project_ids):
        errors.append("projects_order: must be a permutation of master project IDs")
    evidence = evidence_map(master)
    facts_text = " ".join(evidence.values())
    master_words = terms(facts_text)
    errors += _wording_errors(
        output.summary,
        master.summary,
        [
            *master.approved_summary_rephrasings,
            *([generated_summary] if generated_summary is not None else []),
        ],
        "summary",
        master_words,
        limits.max_summary_chars,
        generated_summary is not None and output.summary == generated_summary,
    )
    for edit in output.experience:
        role = roles.get(edit.id)
        if role is None:
            continue
        bullets = {b.id: b for b in role.bullets}
        ids = [b.id for b in edit.bullets]
        if len(ids) != len(set(ids)) or len(edit.bullet_order) != len(
            set(edit.bullet_order)
        ):
            errors.append(f"{edit.id}: duplicate bullet ID")
        if set(ids) != set(edit.bullet_order):
            errors.append(f"{edit.id}: bullet_order must match rewritten bullet IDs")
        if len(ids) > limits.max_bullets[edit.id]:
            errors.append(f"{edit.id}: bullet count overflow")
        for rewritten in edit.bullets:
            original = bullets.get(rewritten.id)
            if original is None:
                errors.append(f"{edit.id}: unknown or wrong-role bullet ID")
                continue
            errors += _wording_errors(
                rewritten.text,
                original.text,
                original.approved_rephrasings,
                rewritten.id,
                master_words,
                limits.bullet_chars[original.id],
            )
    notes = output.match_notes
    if len(notes.matched) + len(notes.gaps) > 20:
        errors.append("match_notes: too many requirements")
    normalized_job = plain(description).casefold()
    matched: set[str] = set()
    for i, match in enumerate(notes.matched):
        requirement = plain(match.requirement).casefold()
        if (
            not requirement
            or len(match.requirement) > 240
            or requirement not in normalized_job
        ):
            errors.append(f"match_notes: requirement must quote the job (matched[{i}])")
        if not match.evidence_ids or any(i not in evidence for i in match.evidence_ids):
            errors.append(f"match_notes: invalid evidence ID (matched[{i}])")
        elif not all(
            requirement in plain(evidence[i]).casefold() for i in match.evidence_ids
        ):
            errors.append(
                "match_notes: matched requirement not supported by cited evidence "
                f"(matched[{i}]); select an exact supplied note option"
            )
        matched.add(requirement)
    for i, gap in enumerate(notes.gaps):
        requirement = plain(gap).casefold()
        if not requirement or len(gap) > 240 or requirement not in normalized_job:
            errors.append(
                f"match_notes: gap must quote the job (gaps[{i}]); "
                "copy a supplied gap option without paraphrasing"
            )
        if requirement in matched or any(
            requirement in plain(v).casefold() for v in evidence.values()
        ):
            errors.append("match_notes: gap conflicts with master evidence")
    if errors:
        raise TruthError(errors)
    # Canonical casing, never LLM-owned formatting or immutable facts.
    return output.model_copy(update={"skills_order": [all_skills[s] for s in selected]})


def audit_proposal(master: MasterResume, raw_response: str, description: str) -> dict:
    """Explain independent field validation without changing normal log privacy."""
    try:
        output = TailoringProposal.model_validate_json(raw_response)
    except Exception:
        return {
            "raw_response": raw_response,
            "schema": {
                "accepted": False,
                "reason": "Strict proposal schema rejected the response",
            },
            "fields": {},
        }

    baseline = unchanged(master)
    summary_candidate = baseline.model_copy(update={"summary": output.summary})
    try:
        validate_tailoring(master, summary_candidate, description)
        summary_accepted, summary_reason = True, "Accepted by field validator"
    except TruthError as exc:
        summary_accepted, summary_reason = False, "; ".join(exc.errors)

    bullet_results = []
    accepted = summary_accepted
    for rewrite in output.bullet_rewrites:
        roles = []
        found = False
        for role in baseline.experience:
            bullets = []
            for bullet in role.bullets:
                if bullet.id == rewrite.id:
                    found = True
                    bullets.append(bullet.model_copy(update={"text": rewrite.text}))
                else:
                    bullets.append(bullet)
            roles.append(role.model_copy(update={"bullets": bullets}))
        if not found:
            valid, reason = False, "Unknown master bullet ID"
        else:
            candidate = summary_candidate.model_copy(update={"experience": roles})
            try:
                validate_tailoring(master, candidate, description)
                valid, reason = True, "Accepted by field validator"
            except TruthError as exc:
                valid, reason = False, "; ".join(exc.errors)
        accepted = accepted and valid
        bullet_results.append(
            {
                "id": rewrite.id,
                "proposed": rewrite.text,
                "accepted": valid,
                "reason": reason,
            }
        )
    return {
        "raw_response": raw_response,
        "schema": {
            "accepted": accepted,
            "reason": "Only the summary and up to three bullet rewrites are proposed",
        },
        "fields": {
            "summary": {
                "proposed": output.summary,
                "changed": plain(output.summary) != plain(master.summary),
                "accepted": summary_accepted,
                "reason": summary_reason,
            },
            "bullet_rewrites": bullet_results,
        },
    }


def unchanged(master: MasterResume) -> Tailoring:
    return Tailoring.model_validate(
        {
            "summary": master.summary,
            "skills_order": [s for g in master.skills for s in g.items],
            "experience": [
                {
                    "id": e.id,
                    "bullet_order": [b.id for b in e.bullets],
                    "bullets": [{"id": b.id, "text": b.text} for b in e.bullets],
                }
                for e in master.experience
            ],
            "projects_order": [p.id for p in master.projects],
            "match_notes": {"matched": [], "gaps": []},
        }
    )
