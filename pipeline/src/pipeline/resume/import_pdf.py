"""Literal PDF facts draft; never pretend PDF extraction preserves DOCX layout."""

import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from pipeline.resume.layout import HEADINGS, LayoutError, stable_id
from pipeline.resume.schema import MasterResume


def split_skills(text: str) -> list[str]:
    # Commas inside SQL (PostgreSQL, MySQL) are part of a single source skill.
    parts: list[str] = []
    start = depth = 0
    for i, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char in ",;" and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    return [s for s in [*parts, text[start:].strip()] if s]


def parse_pdf_text(text: str) -> tuple[MasterResume, list[str]]:
    sections: dict[str, list[str]] = {"contact": []}
    section = "contact"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label = HEADINGS.get(line.casefold())
        if label:
            section = label
            sections.setdefault(section, [])
        else:
            sections.setdefault(section, []).append(line)
    contact = sections.get("contact", [])
    if not contact:
        raise LayoutError("PDF has no extractable resume text; use the original DOCX")
    data: dict[str, Any] = {
        "contact": {"name": contact[0], "lines": contact[1:]},
        "summary": " ".join(sections.get("summary", [])),
        "skills": [],
        "experience": [],
        "projects": [],
        "education": [],
        "certifications": [],
        "reviewed": False,
    }
    for line in sections.get("skills", []):
        name, sep, items = line.partition(":")
        data["skills"].append(
            {
                "name": name if sep else "Skills",
                "items": split_skills(items if sep else line),
                "raw": line,
            }
        )
    issues = [
        "PDF facts are a draft; verify all fields and wrapped bullets "
        "against the original",
        "PDF conversion requires visual layout review before template approval",
    ]
    for section in ("experience", "projects"):
        entry: dict[str, Any] | None = None
        last_bullet: dict[str, Any] | None = None
        for i, line in enumerate(sections.get(section, [])):
            bullet = re.match(r"^[●•▪]\s+(.*)$", line)
            if bullet and entry is not None:
                last_bullet = {
                    "id": stable_id("bullet", i, line),
                    "text": bullet.group(1),
                }
                entry["bullets"].append(last_bullet)
                continue
            # A PDF column gap supplies a heading/date boundary, not inferred dates.
            columns = re.split(r"\s{2,}", line)
            is_header = (
                len(columns) > 1
                and re.search(r"\b\d{4}\b|Present", columns[-1]) is not None
                or section == "projects"
                and line.startswith("Additional:")
            )
            if is_header:
                title, dates = columns[0], columns[-1] if len(columns) > 1 else None
                if section == "experience":
                    job_title, sep, employer = title.partition(" — ")
                    if not sep:
                        job_title, sep, employer = title.partition(" – ")
                    location_match = re.search(r"\(([^)]+)\)$", employer)
                    entry = {
                        "id": stable_id("role", i, line),
                        "employer": employer[: location_match.start()].strip()
                        if location_match
                        else employer,
                        "title": job_title if sep else "",
                        "dates": dates or "",
                        "location": location_match.group(1) if location_match else None,
                        "heading": line,
                        "bullets": [],
                    }
                    if not sep:
                        issues.append(
                            "Separate employer/title in an experience header manually"
                        )
                    if dates and re.search(
                        r"Present\S|Present\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
                        r"Sep|Oct|Nov|Dec)",
                        dates,
                    ):
                        issues.append(
                            "An experience date contains overlapping PDF text; "
                            "verify before setting reviewed=true"
                        )
                else:
                    entry = {
                        "id": stable_id("project", i, line),
                        "name": title,
                        "dates": dates,
                        "detail_lines": [],
                        "bullets": [],
                    }
                data[section].append(entry)
                last_bullet = None
            elif entry is not None and last_bullet is not None:
                last_bullet["text"] += " " + line
            elif entry is not None and section == "projects":
                entry["detail_lines"].append(line)
            else:
                issues.append(f"Unassigned {section} line requires manual review")
    for line in sections.get("education", []):
        columns = re.split(r"\s{2,}", line)
        if len(columns) > 1:
            institution, _, location = columns[0].partition(" — ")
            data["education"].append(
                {
                    "institution": institution,
                    "degree": "",
                    "location": location or None,
                    "dates": columns[-1],
                    "raw": line,
                }
            )
        elif data["education"]:
            previous = data["education"][-1]
            previous["degree"] += (" " if previous["degree"] else "") + line
        else:
            data["education"].append(
                {"institution": line, "degree": "", "dates": "", "raw": line}
            )
    data["certifications"] = [
        {"name": line, "raw": line} for line in sections.get("certifications", [])
    ]
    return MasterResume.model_validate(data), list(dict.fromkeys(issues))


def import_pdf(path: Path) -> tuple[MasterResume, dict[str, Any]]:
    reader = PdfReader(path)
    text = "\n".join(
        page.extract_text(extraction_mode="layout") for page in reader.pages
    )
    master, issues = parse_pdf_text(text)
    return master, {
        "source_format": "pdf",
        "pages": len(reader.pages),
        "issues": issues,
    }
