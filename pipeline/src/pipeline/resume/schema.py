"""Master facts and the deliberately narrow LLM editing surface."""

import hashlib
import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Contact(StrictModel):
    name: str
    lines: list[str] = Field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)


class Bullet(StrictModel):
    id: str
    text: str
    # Optional, owner-reviewed paraphrases. Free-form semantic equivalence cannot
    # be established by a deterministic token/proper-noun check alone.
    approved_rephrasings: list[str] = Field(default_factory=list)


class SkillGroup(StrictModel):
    name: str
    items: list[str]
    raw: str | None = None


class Experience(StrictModel):
    id: str
    employer: str
    title: str
    location: str | None = None
    dates: str
    bullets: list[Bullet]
    heading: str | None = None


class Project(StrictModel):
    id: str
    name: str
    description: str | None = None
    url: str | None = None
    dates: str | None = None
    detail_lines: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Education(StrictModel):
    institution: str
    degree: str
    dates: str
    location: str | None = None
    raw: str | None = None


class Certification(StrictModel):
    name: str
    issuer: str | None = None
    dates: str | None = None
    raw: str | None = None


class MasterResume(StrictModel):
    contact: Contact
    summary: str
    skills: list[SkillGroup]
    experience: list[Experience]
    projects: list[Project]
    education: list[Education]
    certifications: list[Certification]
    reviewed: bool = False
    approved_summary_rephrasings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [entry.id for entry in self.experience + self.projects]
        ids += [
            b.id for entry in self.experience + self.projects for b in entry.bullets
        ]
        if any(not value.strip() for value in ids) or len(ids) != len(set(ids)):
            raise ValueError(
                "Master role/project/bullet IDs must be unique and nonempty"
            )
        skills = [s.casefold() for group in self.skills for s in group.items]
        if any(not s.strip() for s in skills) or len(skills) != len(set(skills)):
            raise ValueError("Master skills must be nonempty and unique")
        return self

    def facts_hash(self) -> str:
        data = self.model_dump(exclude={"reviewed", "approved_summary_rephrasings"})
        for entry in data["experience"] + data["projects"]:
            for bullet in entry["bullets"]:
                bullet.pop("approved_rephrasings", None)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class RewrittenBullet(StrictModel):
    id: str
    text: str


class TailoredExperience(StrictModel):
    id: str
    bullet_order: list[str]
    bullets: list[RewrittenBullet]


class RequirementMatch(StrictModel):
    requirement: str
    evidence_ids: list[str]


class MatchNotes(StrictModel):
    matched: list[RequirementMatch]
    gaps: list[str]


class Tailoring(StrictModel):
    summary: str
    skills_order: list[str]
    experience: list[TailoredExperience]
    projects_order: list[str]
    match_notes: MatchNotes


class SlotRewrite(StrictModel):
    """Response for one focused shortening retry; no extra fields are allowed."""

    text: str


class TailoringProposal(StrictModel):
    """The model's complete editable surface; ordering stays in deterministic code."""

    summary: str
    bullet_rewrites: list[RewrittenBullet] = Field(default_factory=list, max_length=3)


def load_master(path: Path) -> MasterResume:
    return MasterResume.model_validate_json(path.read_text(encoding="utf-8"))
