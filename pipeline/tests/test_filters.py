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


@pytest.mark.parametrize(
    "title,term",
    [
        ("Engineering Manager", "manager"),
        ("Senior Engineering Manager", "manager"),
        ("Head of Backend Engineering", "head of"),
        ("Director of Software Engineering", "director"),
        ("VP Engineering", "VP"),
        ("Vice President of Engineering", "vice president"),
        ("Principal Backend Engineer", "principal"),
        ("Staff Software Engineer", "staff"),
        ("Chief Software Engineer", "chief"),
        ("CTO", "CTO"),
    ],
)
def test_seniority_excluded_before_engineering_evidence(title: str, term: str) -> None:
    classified = JobFilter(FILTERS).classify(
        example_job(title, description="Python software development")
    )
    assert classified.filter_result == "exclude"
    assert term in classified.filter_reason
    assert "student/junior" in classified.filter_reason


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Senior Backend Engineer", "maybe"),
        ("Lead Software Developer", "maybe"),
        ("Junior Backend Engineer", "include"),
        ("Software Engineering Intern", "include"),
        ("Backend Developer for Staffing Tools", "include"),
        ("Backend Developer for Leadership Tools", "include"),
        ("Senior Sales Executive", "exclude"),
    ],
)
def test_student_seniority_and_word_boundaries(title: str, expected: str) -> None:
    assert JobFilter(FILTERS).classify(example_job(title)).filter_result == expected


def test_seniority_lists_are_editable(tmp_path: Path) -> None:
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    config = yaml.load(FILTERS.read_text(encoding="utf-8"))
    config["seniority"] = {"exclude": ["fellow"], "maybe": ["experienced"]}
    path = tmp_path / "filters.yaml"
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(config, handle)
    filters = JobFilter(path)
    assert (
        filters.classify(example_job("Fellow Backend Engineer")).filter_result
        == "exclude"
    )
    assert (
        filters.classify(example_job("Experienced Backend Engineer")).filter_result
        == "maybe"
    )
    assert (
        filters.classify(example_job("Senior Backend Engineer")).filter_result
        == "include"
    )


@pytest.mark.parametrize(
    "title",
    [
        "Senior People Partner",
        "Internal Audit Senior",
        "Junior Underwriter",
        "Growth Analyst",
        "Senior Risk Analyst - Android Lending",
        "Reconciliation & Settlement Officer",
        "Data Entry Officer (Enugu)",
        "Executive Assistant (Growth)",
        "Growth Associate (Kano State)",
        "Clinical/Health Informatics Consultant",
        "Research & Evidence Professional",
        "Public Health Implementation Officer (Oyo state)",
        "Join our Talent Network",
        "Scrum Master",
        "Portfolio Analysis & Reporting - Lead",
        "Unrecognised Business Role",
        "Go To Market Associate",
        "Platform Operations Associate",
    ],
)
def test_non_engineering_titles_do_not_leak_into_check_fit(title: str) -> None:
    # Company boilerplate can mention software without describing this role.
    job = JobFilter(FILTERS).classify(
        example_job(
            title,
            description="Our software engineering team builds software tools.",
        )
    )
    assert job.filter_result == "exclude"
    assert "Role:" in job.filter_reason


@pytest.mark.parametrize(
    "title",
    [
        "Android Engineer",
        "Senior iOS Engineer - Lagos",
        "Software Development Engineer in Test (SDET)",
        "Devops Engineer",
        "Senior Backend Engineer",
    ],
)
def test_technical_review_roles_remain_available(title: str) -> None:
    assert JobFilter(FILTERS).classify(example_job(title)).filter_result == "maybe"


def test_ambiguous_technical_titles_require_software_evidence() -> None:
    filters = JobFilter(FILTERS)
    assert (
        filters.classify(example_job("Member of Technical Team")).filter_result
        == "exclude"
    )
    assert (
        filters.classify(
            example_job(
                "Member of Technical Team",
                description="Build APIs with backend development.",
            )
        ).filter_result
        == "maybe"
    )


def test_description_title_gate_is_editable(tmp_path: Path) -> None:
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    config = yaml.load(FILTERS.read_text(encoding="utf-8"))
    config["roles"]["description_titles"] = ["apprentice"]
    path = tmp_path / "filters.yaml"
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(config, handle)
    filters = JobFilter(path)
    assert (
        filters.classify(
            example_job("Apprentice", description="Learn software development.")
        ).filter_result
        == "maybe"
    )
