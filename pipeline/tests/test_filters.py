import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.filters import JobFilter
from pipeline.models import Job, SourceConfig
from pipeline.normalize import normalize_job

FILTERS = Path(__file__).resolve().parents[2] / "data/filters.yaml"


def example_job(
    title: str = "Backend Engineer",
    location: str = "Lagos, Nigeria",
    description: str = "",
    remote: bool = False,
    external_id: str = "1",
) -> Job:
    return Job(
        "greenhouse",
        "board",
        external_id,
        "Example Company",
        title,
        location,
        [],
        remote,
        None,
        None,
        description,
        "",
        "https://example.com/jobs/" + external_id,
        None,
        datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("title", "location", "description", "remote", "expected"),
    [
        ("Backend Engineer", "Lagos, Nigeria", "", False, "include"),
        ("Back-end Developer", "Abuja", "", False, "include"),
        (
            "Software Engineering Intern",
            "Port Harcourt",
            "software development",
            False,
            "include",
        ),
        ("Graduate Trainee", "Kenya", "backend development", False, "include"),
        ("Python Developer", "Remote Africa", "", False, "include"),
        ("API Engineer", "EMEA", "", True, "include"),
        ("Java Engineer", "Worldwide", "", True, "include"),
        ("Go Engineer", "Nairobi", "", False, "include"),
        ("Node.js Engineer", "Cape Town", "", False, "include"),
        ("Platform Engineer", "Cairo", "", False, "include"),
        (
            "Backend Engineer",
            "Remote",
            "You can work remotely from Nigeria.",
            False,
            "include",
        ),
        ("SRE", "Nigeria", "", False, "maybe"),
        ("DevOps Engineer", "Accra", "", False, "maybe"),
        ("Data Engineer", "Lagos", "", False, "maybe"),
        ("Intern", "Nigeria", "", False, "maybe"),
        ("Backend Engineer", "", "", False, "maybe"),
        (
            "Backend Engineer",
            "Remote",
            "We serve customers in Nigeria.",
            False,
            "maybe",
        ),
        ("Backend Engineer", "Remote", "We go to the office sometimes.", True, "maybe"),
        (
            "Backend Engineer",
            "Remote - New York",
            "We serve customers in Africa.",
            True,
            "exclude",
        ),
        (
            "Backend Engineer",
            "New York, onsite",
            "We have offices in Lagos.",
            False,
            "exclude",
        ),
        ("Sales Engineer", "Lagos", "software development", False, "exclude"),
        ("Marketing Intern", "Nigeria", "", False, "exclude"),
        ("Finance Graduate", "Nigeria", "", False, "exclude"),
        ("Customer Support Agent", "Nigeria", "", False, "exclude"),
        ("HR Manager", "Nigeria", "", False, "exclude"),
        ("Backend Engineer", "Somewhere undisclosed", "", False, "maybe"),
    ],
)
def test_filter_cases(
    title: str, location: str, description: str, remote: bool, expected: str
) -> None:
    job = JobFilter(FILTERS).classify(example_job(title, location, description, remote))
    assert job.filter_result == expected
    assert "Role:" in job.filter_reason and "Location:" in job.filter_reason


def test_compensation_never_changes_classification() -> None:
    filters = JobFilter(FILTERS)
    for description in (
        "Unpaid internship",
        "Paid role",
        "Salary $0",
        "Compensation negotiable",
    ):
        assert (
            filters.classify(
                example_job("Backend Intern", description=description)
            ).filter_result
            == "include"
        )


def test_locations_produce_tags_and_do_not_match_substrings() -> None:
    filters = JobFilter(FILTERS)
    job = filters.classify(example_job(location="Remote Lagos, Nigeria"))
    assert job.remote_flag and {"lagos", "nigeria", "remote"} <= set(job.location_tags)
    assert filters.classify(example_job(location="NG")).filter_result == "include"
    assert (
        filters.classify(example_job(location="Remote, e.g. Berlin")).filter_result
        == "exclude"
    )


def test_manually_reviewed_real_jobs() -> None:
    path = Path(__file__).parent / "fixtures/real_filter_samples.json"
    samples = json.loads(path.read_text(encoding="utf-8"))["samples"]
    filters = JobFilter(FILTERS)
    for sample in samples:
        source = SourceConfig(**sample["source"])
        job = filters.classify(normalize_job(sample["raw"], source, datetime.now(UTC)))
        assert job.filter_result == sample["expected"], (
            job.title,
            job.location_raw,
            job.filter_reason,
        )
