"""Grounded choices guide the model without accepting invented output."""

from copy import deepcopy
from pathlib import Path

import pytest
from resume_helpers import ControlledBackend, job, synthetic_resume

from pipeline.llm.client import Usage
from pipeline.resume.pdf import Pagination
from pipeline.resume.prompt import request
from pipeline.resume.requirements import options
from pipeline.resume.schema import MatchNotes, Tailoring
from pipeline.resume.tailor import TailoringClient, tailor_job
from pipeline.resume.validate import TruthError, unchanged, validate_tailoring


def test_closed_skill_schema_and_each_note_option_are_truthful(tmp_path: Path) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    current_job = job()
    system, payload, _ = request(master, current_job)
    assert '"enum":["Python","SQL"]' in system
    choices = payload["match_note_options"]
    assert choices["gaps"] == ["AWS"]
    assert {m["requirement"] for m in choices["matched"]} == {"Python", "SQL"}
    for match in choices["matched"]:
        candidate = unchanged(master).model_copy(
            update={"match_notes": MatchNotes(matched=[match], gaps=[])}
        )
        validate_tailoring(master, candidate, current_job["description_text"])
    candidate = unchanged(master).model_copy(
        update={"match_notes": MatchNotes(matched=[], gaps=choices["gaps"])}
    )
    validate_tailoring(master, candidate, current_job["description_text"])


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


def test_retry_rejects_the_reported_failures_then_accepts_grounded_choices(
    tmp_path: Path,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    good = unchanged(master).model_dump()
    good["match_notes"] = {
        "matched": [{"requirement": "Python", "evidence_ids": ["b-1"]}],
        "gaps": ["AWS"],
    }
    bad = deepcopy(good)
    bad["skills_order"].append("AWS")
    bad["match_notes"]["matched"][0]["requirement"] = "Python, SQL"
    bad["match_notes"]["gaps"] = ["No AWS experience"]
    backend = ControlledBackend([bad, good])
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
        Pagination(discover=False),
    )
    assert result.status == "ready" and usage.attempts == 2
    assert result.match_notes == good["match_notes"]
    feedback = backend.calls[1]["validator_errors"][0]
    assert "skills_order: invented skill at index 2" in feedback
    assert "matched[0]" in feedback and "gaps[0]" in feedback
    assert backend.calls[1]["match_note_options"]["gaps"] == ["AWS"]


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
