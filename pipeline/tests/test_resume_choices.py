"""Grounded choices guide the model without accepting invented output."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from resume_helpers import (
    ControlledBackend,
    job,
    synthetic_resume,
    tailoring_proposal,
    verified_pagination,
)

from pipeline.llm.client import Usage
from pipeline.resume.prompt import request
from pipeline.resume.requirements import options
from pipeline.resume.schema import Tailoring, TailoringProposal
from pipeline.resume.tailor import TailoringClient, tailor_job
from pipeline.resume.validate import TruthError, unchanged, validate_tailoring


def test_model_schema_exposes_wording_only_and_slot_limits(tmp_path: Path) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    current_job = job()
    system, payload, _ = request(master, current_job)
    assert "bullet_rewrites" in system
    assert "match_note_options" not in payload
    assert "skills_order" not in system
    assert payload["slot_character_limits"]["summary"] == len(master.summary)
    assert payload["slot_character_limits"]["bullets"]["b-1"] == len(
        master.experience[0].bullets[0].text
    )
    with pytest.raises(ValidationError):
        TailoringProposal.model_validate(
            {**tailoring_proposal(master), "skills_order": ["AWS"]}
        )


def test_note_choices_do_not_expand_claims_or_create_alias_gaps(tmp_path: Path) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    # Owner's literal Go skill does not authorize calling it Golang in output.
    master.skills[0].items.append("Go")
    payload = options(
        master, "Golang and Python; scalable distributed systems", {"b-1"}
    )
    assert payload == {
        "matched": [{"requirement": "Python", "evidence_ids": ["b-1"]}],
        "gaps": [],
    }


def test_private_education_evidence_never_becomes_an_outbound_match(
    tmp_path: Path,
) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    master.education[0].degree = "AWS coursework"
    assert options(master, "AWS", {"b-1"}) == {"matched": [], "gaps": []}
    master.experience[0].bullets[0].id = "education:0:degree"
    assert options(master, "AWS", {"education:0:degree"}) == {"matched": [], "gaps": []}


def test_match_notes_are_created_locally_and_untrusted_fields_are_rejected(
    tmp_path: Path,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    invalid = {**tailoring_proposal(master), "match_notes": {"matched": [], "gaps": []}}
    with pytest.raises(ValidationError):
        TailoringProposal.model_validate(invalid)
    backend = ControlledBackend([tailoring_proposal(master)])
    usage = Usage()
    result = tailor_job(
        master,
        current_job,
        original,
        template,
        layout,
        tmp_path / "output",
        TailoringClient(backend, sleep=lambda _: None),
        usage,
        verified_pagination(),
    )
    assert result.status == "ready" and usage.attempts == 1
    assert [match["requirement"] for match in result.match_notes["matched"]] == [
        "Python",
        "SQL",
    ]
    assert "match_note_options" not in backend.calls[0]
    assert result.warnings == []


@pytest.mark.parametrize("mutation", ["group", "synonym", "matched", "gap"])
def test_instructions_never_replace_deterministic_rejection(
    tmp_path: Path,
    mutation: str,
) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    proposal = unchanged(master).model_dump()
    if mutation == "group":
        proposal["skills_order"] = ["Languages"]
    elif mutation == "synonym":
        proposal["skills_order"] = ["Python programming"]
    elif mutation == "matched":
        proposal["match_notes"]["matched"] = [
            {"requirement": "AWS", "evidence_ids": ["b-1"]}
        ]
    else:
        proposal["match_notes"]["gaps"] = ["Cloud expertise missing"]
    with pytest.raises(TruthError):
        validate_tailoring(
            master, Tailoring.model_validate(proposal), job()["description_text"]
        )
