"""Content-free change counts for accepted resume edits and rendered documents."""

from pathlib import Path

from pipeline.resume.layout import compare
from pipeline.resume.schema import MasterResume, Tailoring
from pipeline.resume.validate import plain


def _skill_groups(master: MasterResume, edit: Tailoring) -> list[dict[str, object]]:
    positions = {skill: index for index, skill in enumerate(edit.skills_order)}
    groups = sorted(
        master.skills,
        key=lambda group: min(
            (positions.get(skill, len(positions)) for skill in group.items),
            default=len(positions),
        ),
    )
    return [
        {
            "group": group.name,
            "skills": [skill for skill in edit.skills_order if skill in group.items],
        }
        for group in groups
    ]


def human_report(master: MasterResume, edit: Tailoring) -> dict[str, object]:
    return {
        "summary_before": master.summary,
        "summary_after": edit.summary,
        "skills_before": [
            {"group": group.name, "skills": group.items} for group in master.skills
        ],
        "skills_after": _skill_groups(master, edit),
        "matched_requirements": [
            match.requirement for match in edit.match_notes.matched
        ],
        "gaps": edit.match_notes.gaps,
    }


def summarize(
    master: MasterResume, edit: Tailoring, original: Path, rendered: Path
) -> dict[str, int | bool]:
    roles = {role.id: role for role in master.experience}
    originals = {
        bullet.id: bullet.text for role in master.experience for bullet in role.bullets
    }
    changes: dict[str, int | bool] = {
        "document_changed": bool(compare(original, rendered)),
        "summary_changed": plain(master.summary) != plain(edit.summary),
        "skill_groups_changed": sum(
            (before["group"], before["skills"]) != (after["group"], after["skills"])
            for before, after in zip(
                [
                    {"group": group.name, "skills": group.items}
                    for group in master.skills
                ],
                _skill_groups(master, edit),
            )
        ),
        "skills_omitted": sum(len(group.items) for group in master.skills)
        - len(edit.skills_order),
        "bullet_roles_reordered": sum(
            role.bullet_order
            != [b.id for b in roles[role.id].bullets if b.id in role.bullet_order]
            for role in edit.experience
        ),
        "bullets_omitted": sum(len(role.bullets) for role in master.experience)
        - sum(len(role.bullets) for role in edit.experience),
        "bullets_rephrased": sum(
            plain(bullet.text) != plain(originals[bullet.id])
            for role in edit.experience
            for bullet in role.bullets
        ),
        "projects_reordered": edit.projects_order
        != [project.id for project in master.projects],
    }
    changes["tailoring_changed"] = bool(changes["document_changed"]) and any(
        value for key, value in changes.items() if key != "document_changed"
    )
    return changes
