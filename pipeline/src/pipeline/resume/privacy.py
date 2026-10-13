"""Allowlisted tailoring facts and contact redaction at the request boundary."""

import re
from typing import Any

from pipeline.resume.schema import Contact, MasterResume

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
LINK = re.compile(r"(?:https?://|www\.)[^\s<>|]+", re.I)
PHONE = re.compile(r"(?<!\w)\+?\d[\d ()\-.]{6,}\d(?!\w)")


def tailoring_facts(master: MasterResume) -> tuple[dict[str, Any], dict[str, str]]:
    """Keep immutable headers, education, certificates and all URLs local."""
    facts = {
        "summary": master.summary,
        "approved_summary_rephrasings": master.approved_summary_rephrasings,
        "skills": [g.model_dump(include={"name", "items"}) for g in master.skills],
        "experience": [
            r.model_dump(include={"id", "bullets"}) for r in master.experience
        ],
        "projects": [
            p.model_dump(
                include={"id", "name", "description", "detail_lines", "bullets"}
            )
            for p in master.projects
        ],
    }
    evidence = {"summary": master.summary}
    evidence.update({"skill:" + s: s for g in master.skills for s in g.items})
    evidence.update(
        {b.id: b.text for r in master.experience + master.projects for b in r.bullets}
    )
    for project in master.projects:
        evidence[f"project:{project.id}:name"] = project.name
        if project.description:
            evidence[f"project:{project.id}:description"] = project.description
        evidence.update(
            {
                f"project:{project.id}:detail:{i}": text
                for i, text in enumerate(project.detail_lines)
            }
        )
    return facts, evidence


class ContactRedactor:
    """Remove contact values even when repeated in facts, jobs, IDs or feedback.

    Raw contact lines are supported because PDF imports may leave email/phone
    fields unset. Redacted facts still face the unchanged local truth validator;
    unusable redacted wording fails safely to the original document.
    """

    def __init__(self, contact: Contact) -> None:
        values = {contact.name, *contact.lines, *contact.links}
        values.update(v for v in (contact.email, contact.phone, contact.location) if v)
        values.update(part for part in contact.name.split() if len(part) >= 3)
        for line in contact.lines:
            values.update(EMAIL.findall(line))
            values.update(LINK.findall(line))
            values.update(PHONE.findall(line))
            for part in re.split(r"[|;\n]", line):
                values.add(re.sub(r"^[\w ]+:\s*", "", part).strip())
        self.patterns = []
        for value in sorted(values, key=len, reverse=True):
            if not value.strip():
                continue
            # Whitespace/case and common phone separators must not bypass privacy.
            digits = re.sub(r"\D", "", value)
            if PHONE.fullmatch(value.strip()) and len(digits) >= 7:
                expression = r"\+?" + r"[\s().-]*".join(digits)
            else:
                expression = r"\s+".join(re.escape(p) for p in value.split())
                if value.startswith(("https://", "http://", "www.")):
                    expression = r"(?:https?://)?(?:www\.)?" + re.escape(
                        re.sub(r"^(?:https?://)?(?:www\.)?", "", value).rstrip("/")
                    )
            # IDs may concatenate personal names/phone numbers without word
            # boundaries. Privacy takes priority over retaining such identifiers.
            self.patterns.append(re.compile(expression, re.I))

    def text(self, value: str) -> str:
        for pattern in self.patterns:
            value = pattern.sub("[private]", value)
        # Emails and web links are unnecessary for tailoring, including ones
        # embedded in project text but omitted from the structured contact block.
        value = EMAIL.sub("[private]", value)
        value = PHONE.sub(
            lambda match: (
                "[private]"
                if len(re.sub(r"\D", "", match.group())) >= 9
                else match.group()
            ),
            value,
        )
        return LINK.sub("[private]", value)

    def payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.payload(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key): self.payload(item) for key, item in value.items()}
        return value
