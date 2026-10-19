from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from resume_helpers import ControlledBackend, synthetic_resume

from pipeline.llm.client import Usage
from pipeline.resume.pdf import Pagination
from pipeline.resume.tailor import TailoringClient, tailor_job
from pipeline.resume.validate import audit_proposal, unchanged
from pipeline.run_tailor import Outcome, print_outcome


def tailored(
    tmp_path: Path,
    *,
    skills: list[dict[str, Any]],
    bullets: list[str],
    title: str,
    description: str,
):
    master, original, template, layout = synthetic_resume(
        tmp_path, skills=skills, experience_bullets=bullets
    )
    pagination = Pagination("controlled-renderer", discover=False)
    pagination.convert = lambda *_: 1
    client = TailoringClient(
        ControlledBackend([unchanged(master).model_dump()]), sleep=lambda _: None
    )
    result = tailor_job(
        master,
        {
            "id": "004ebdc0-ca87-42a4-924f-7a2f7f5c2982",
            "company": "Example Company",
            "title": title,
            "description_text": description,
        },
        original,
        template,
        layout,
        tmp_path / "output",
        client,
        Usage(),
        pagination,
    )
    return master, result, client


def test_backend_job_gets_role_summary_skill_groups_and_bullets_reordered(
    tmp_path: Path, capsys: Any
) -> None:
    master, result, client = tailored(
        tmp_path,
        skills=[
            {"name": "Languages", "items": ["Python", "JavaScript/TypeScript"]},
            {"name": "Backend", "items": ["FastAPI", "RESTful APIs"]},
            {"name": "Databases", "items": ["PostgreSQL", "Redis"]},
        ],
        bullets=[
            "Designed PostgreSQL indexes to improve query speed.",
            "Built FastAPI backend services in Python.",
            "Used Redis to cache application responses.",
        ],
        title="FastAPI Backend Engineer",
        description="Required: FastAPI, Python, PostgreSQL. Build RESTful APIs.",
    )
    assert result.status == "ready"
    assert client.last_diagnostic is None
    audit = audit_proposal(
        master,
        unchanged(master).model_dump_json(),
        "Required: FastAPI, Python, PostgreSQL. Build RESTful APIs.",
    )
    assert audit["schema"]["accepted"] is True
    assert audit["fields"]["summary"]["changed"] is False
    assert result.changes["tailoring_changed"] is True
    assert result.change_report["summary_before"] == master.summary
    assert "FastAPI Backend Engineer" in result.change_report["summary_after"]
    assert result.change_report["skills_after"][0]["group"] == "Backend"
    assert result.change_report["matched_requirements"]
    document = Document(result.path)
    assert document.tables[0].cell(0, 0).text.startswith("Backend: FastAPI")
    bullet_text = [p.text for p in document.paragraphs if p.style.name == "List Bullet"]
    assert bullet_text[0] == "Built FastAPI backend services in Python.", result.changes

    print_outcome(
        Outcome(
            "job-1",
            result.status,
            Usage(),
            path=result.path,
            report_path=result.report_path,
            changes=result.changes,
            warnings=result.warnings,
            change_report=result.change_report,
        )
    )
    printed = capsys.readouterr().out
    assert "Summary before:" in printed and "Summary after:" in printed
    assert "Skills before:" in printed and "Skills after:" in printed
    assert "Matched requirements:" in printed and "Gaps:" in printed


def test_node_react_job_reorders_frontend_skills_and_relevant_bullets(
    tmp_path: Path,
) -> None:
    _, result, _ = tailored(
        tmp_path,
        skills=[
            {"name": "Languages", "items": ["Python", "JavaScript/TypeScript"]},
            {"name": "Frontend", "items": ["React", "Next.js"]},
            {"name": "Backend", "items": ["FastAPI", "RESTful APIs"]},
        ],
        bullets=[
            "Built Python APIs for a small dashboard system.",
            "Developed React screens with TypeScript.",
            "Used SQL query indexes to improve report speed.",
        ],
        title="Full Stack Engineer (Node.js & React)",
        description=(
            "Required skills: React and TypeScript. Build accessible React components."
        ),
    )
    assert result.status == "ready"
    assert result.changes["tailoring_changed"] is True
    assert result.change_report["skills_after"][0]["group"] == "Frontend"
    assert result.change_report["skills_after"][0]["skills"][0] == "React"
    assert result.change_report["summary_after"].startswith(
        "Targeting Full Stack Engineer"
    )
    document = Document(result.path)
    assert document.tables[0].cell(0, 0).text.startswith("Frontend: React")
    bullet_text = [p.text for p in document.paragraphs if p.style.name == "List Bullet"]
    assert bullet_text[0] == "Developed React screens with TypeScript.", result.changes


def test_ios_without_matching_master_skills_uses_low_fit_master_without_llm(
    tmp_path: Path,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    backend = ControlledBackend([])
    pagination = Pagination(discover=False)
    result = tailor_job(
        master,
        {
            "id": "02bef5df-4bda-482a-8b4c-f312c4d0361f",
            "company": "Example Company",
            "title": "iOS Developer",
            "description_text": "Requirements: Swift, UIKit, and iOS development.",
        },
        original,
        template,
        layout,
        tmp_path / "output",
        TailoringClient(backend),
        Usage(),
        pagination,
    )
    assert result.status == "low_fit"
    assert result.path.read_bytes() == original.read_bytes()
    assert backend.calls == []
    assert result.changes["tailoring_changed"] is False
    assert "No job skills meaningfully overlap" in result.warnings[0]
