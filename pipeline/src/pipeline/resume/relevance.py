"""Deterministic job-to-master relevance ordering without adding resume facts."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from pipeline.resume.description import compact_description
from pipeline.resume.requirements import options
from pipeline.resume.schema import MasterResume, MatchNotes, RewrittenBullet, Tailoring
from pipeline.resume.validate import evidence_map

TOKEN = re.compile(r"[a-z0-9+#]+", re.I)
SKILL_PART = re.compile(r"[,()/;&]+")
REPO_ROOT = Path(__file__).resolve().parents[4]
STOP_WORDS = {
    "about",
    "across",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "the",
    "their",
    "this",
    "to",
    "we",
    "with",
    "you",
    "your",
}


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN.findall(text)]


def _skill_anchors(skill: str) -> list[tuple[str, ...]]:
    anchors = []
    for part in SKILL_PART.split(skill):
        tokens = tuple(_tokens(part))
        if tokens:
            anchors.append(tokens)
    return anchors


def _contains(query: list[str], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    for index in range(len(query)):
        if tuple(query[index : index + width]) != phrase:
            continue
        # Do not read the `.js` suffix of Node.js/Next.js as a standalone JS
        # requirement; doing so can outrank the actual framework requirement.
        if (
            phrase == ("js",)
            and index > 0
            and query[index - 1] in {"node", "next", "express"}
        ):
            continue
        return True
    return False


@lru_cache(maxsize=1)
def _skill_alias_groups() -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Load reviewed spelling aliases; they match existing facts, never add skills."""
    path = REPO_ROOT / "data" / "skill_aliases.yaml"
    if not path.exists():
        return ()
    with path.open(encoding="utf-8") as handle:
        config = YAML(typ="safe").load(handle) or {}
    groups = config.get("aliases", [])
    return tuple(
        tuple(phrase for item in group if (phrase := tuple(_tokens(str(item)))))
        for group in groups
        if isinstance(group, list)
    )


def _variants(phrase: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    for group in _skill_alias_groups():
        if phrase in group:
            return group
    return (phrase,)


def _expanded_token_set(tokens: list[str]) -> set[str]:
    result = set(tokens)
    for group in _skill_alias_groups():
        if any(_contains(tokens, phrase) for phrase in group):
            result.add("alias:" + "/".join(group[0]))
    return result


def _skill_score(skill: str, title: list[str], description: list[str]) -> int:
    score = 0
    for phrase in _skill_anchors(skill):
        variants = _variants(phrase)
        if any(_contains(title, item) for item in variants):
            score += 5 * len(phrase)
        elif any(_contains(description, item) for item in variants):
            score += len(phrase)
    return score


def matching_skills(master: MasterResume, job: dict[str, Any]) -> list[str]:
    title = _tokens(job["title"])
    description = _tokens(job["description_text"])
    ranked = [
        (skill, _skill_score(skill, title, description), group_index, item_index)
        for group_index, group in enumerate(master.skills)
        for item_index, skill in enumerate(group.items)
    ]
    return [skill for skill, score, _, _ in ranked if score > 0]


def _role_type(title: str) -> str:
    role = title.strip()
    role = re.sub(r"\s*\([^)]*\)", "", role).strip()
    for separator in (" - ", " | "):
        role = role.split(separator, maxsplit=1)[0].strip()
    # Job titles can contain years, contract durations, or fixture IDs. Keep the
    # role wording while avoiding numbers that the resume validator treats as
    # factual metrics.
    role = re.sub(
        r"\b\d+(?:[.,]\d+)*(?:\s*(?:%|k|million|billion))?\b", "", role, flags=re.I
    )
    role = " ".join(role.split()).strip(" -|,;")
    return role or "this role"


def _safe_summary(master: MasterResume, job: dict[str, Any], skills: list[str]) -> str:
    if "\n" in master.summary:
        return master.summary
    title = _role_type(job["title"])
    summary = ""
    selected: list[str] = []
    for skill in skills:
        candidate = [*selected, skill]
        text = f"Targeting {title}: {', '.join(candidate)}"
        if len(text) <= len(master.summary):
            selected = candidate
            summary = text
    if not summary:
        return master.summary
    return summary


def _bullet_score(text: str, title: list[str], description: list[str]) -> int:
    query = _expanded_token_set([*title, *description])
    evidence = _expanded_token_set(_tokens(text))
    return sum(token not in STOP_WORDS and len(token) > 2 for token in evidence & query)


def deterministic_tailoring(
    master: MasterResume,
    job: dict[str, Any],
    candidate: Tailoring | None = None,
    skill_slot_limits: list[int] | None = None,
) -> Tailoring:
    """Keep all facts and IDs while deterministically tailoring their order."""
    candidate = candidate or Tailoring.model_validate(
        {
            "summary": master.summary,
            "skills_order": [skill for group in master.skills for skill in group.items],
            "experience": [
                {
                    "id": role.id,
                    "bullet_order": [bullet.id for bullet in role.bullets],
                    "bullets": [
                        bullet.model_dump(include={"id", "text"})
                        for bullet in role.bullets
                    ],
                }
                for role in master.experience
            ],
            "projects_order": [project.id for project in master.projects],
            "match_notes": {"matched": [], "gaps": []},
        }
    )
    title_tokens = _tokens(job["title"])
    description_tokens = _tokens(job["description_text"])
    skill_ranks = [
        (skill, _skill_score(skill, title_tokens, description_tokens), group_i, item_i)
        for group_i, group in enumerate(master.skills)
        for item_i, skill in enumerate(group.items)
    ]
    matched = [
        skill
        for skill, score, _, _ in sorted(
            skill_ranks, key=lambda item: (-item[1], item[2], item[3])
        )
        if score > 0
    ]
    if not matched:
        notes = options(
            master,
            compact_description(job["description_text"], job.get("company", "")).text,
            set(evidence_map(master)),
        )
        return candidate.model_copy(
            update={"match_notes": MatchNotes.model_validate(notes)}
        )
    group_ranks = {
        group_index: max(
            (
                score
                for _, score, rank_group, _ in skill_ranks
                if rank_group == group_index
            ),
            default=0,
        )
        for group_index in range(len(master.skills))
    }
    group_order = sorted(
        range(len(master.skills)), key=lambda index: (-group_ranks[index], index)
    )
    ordered_skills: list[str] = []
    for slot, group_index in enumerate(group_order):
        group = master.skills[group_index]
        items = [
            (skill, score, item_index)
            for skill, score, rank_group, item_index in skill_ranks
            if rank_group == group_index
        ]
        selected: list[str] = []
        for skill, _, _ in sorted(items, key=lambda item: (-item[1], item[2])):
            proposed = [*selected, skill]
            text_length = len(group.name) + 2 + len(", ".join(proposed))
            if (
                skill_slot_limits is None
                or slot >= len(skill_slot_limits)
                or text_length <= skill_slot_limits[slot]
            ):
                selected = proposed
        ordered_skills.extend(selected)
    roles = {role.id: role for role in master.experience}
    ordered_experience = []
    for edit in candidate.experience:
        role = roles[edit.id]
        original_ids = [bullet.id for bullet in role.bullets]
        rank = {
            bullet.id: _bullet_score(bullet.text, title_tokens, description_tokens)
            for bullet in role.bullets
        }
        bullet_records = {bullet.id: bullet for bullet in edit.bullets}
        for bullet in role.bullets:
            bullet_records.setdefault(
                bullet.id,
                RewrittenBullet(id=bullet.id, text=bullet.text),
            )
        texts = {bullet_id: bullet.text for bullet_id, bullet in bullet_records.items()}
        remaining = sorted(
            original_ids,
            key=lambda bullet_id: (-rank[bullet_id], original_ids.index(bullet_id)),
        )
        ordered: list[str] = []
        for slot_id in original_ids:
            slot_length = len(
                next(bullet.text for bullet in role.bullets if bullet.id == slot_id)
            )
            fitting = [
                bullet_id
                for bullet_id in remaining
                if len(texts[bullet_id]) <= slot_length
            ]
            chosen = next(
                (bullet_id for bullet_id in remaining if bullet_id in fitting), None
            )
            if chosen is None:
                continue
            ordered.append(chosen)
            remaining.remove(chosen)
        ordered_experience.append(
            edit.model_copy(
                update={
                    "bullet_order": ordered,
                    "bullets": [bullet_records[bullet_id] for bullet_id in ordered],
                }
            )
        )
    project_order = sorted(
        master.projects,
        key=lambda project: (
            -_bullet_score(
                " ".join(
                    [project.name, project.description or "", *project.detail_lines]
                    + [bullet.text for bullet in project.bullets]
                ),
                title_tokens,
                description_tokens,
            ),
            master.projects.index(project),
        ),
    )
    return candidate.model_copy(
        update={
            "summary": _safe_summary(master, job, matched),
            "skills_order": ordered_skills,
            "experience": ordered_experience,
            "projects_order": [project.id for project in project_order],
            "match_notes": MatchNotes.model_validate(
                options(
                    master,
                    compact_description(
                        job["description_text"], job.get("company", "")
                    ).text,
                    set(evidence_map(master)),
                )
            ),
        }
    )
