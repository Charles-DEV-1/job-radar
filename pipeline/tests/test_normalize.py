from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from test_filters import FILTERS

from pipeline.dedupe import hash_job
from pipeline.filters import JobFilter
from pipeline.models import SourceConfig
from pipeline.normalize import decoded_html, normalize_job, normalize_jobs, timestamp

ATS = ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters")
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def recorded_raw(ats: str, recorded: dict[str, Any]) -> dict[str, Any]:
    if ats == "smartrecruiters":
        return deepcopy(recorded["smartrecruiters-detail"])
    key = {"greenhouse": "greenhouse-content", "workable": "workable-full"}.get(
        ats, ats
    )
    payload = recorded[key]
    jobs = (
        payload
        if isinstance(payload, list)
        else payload["offers" if ats == "recruitee" else "jobs"]
    )
    return deepcopy(jobs[0])


@pytest.mark.parametrize("ats", ATS)
def test_normalize_recorded_responses(ats: Any, recorded: dict[str, Any]) -> None:
    raw = recorded_raw(ats, recorded)
    job = normalize_job(
        raw, SourceConfig("Recorded Company", ats, "board", "https://example.com"), NOW
    )
    assert job.external_id == str(raw["shortcode"] if ats == "workable" else raw["id"])
    assert (
        job.title
        == raw[
            "text"
            if ats == "lever"
            else "name"
            if ats == "smartrecruiters"
            else "title"
        ]
    )
    assert job.description_html and job.description_text
    assert job.location_raw
    assert job.apply_url.startswith("https://")
    assert job.posted_at and job.posted_at.tzinfo == UTC
    assert job.first_seen_at == NOW
    if ats == "lever":
        assert job.posted_at.timestamp() == pytest.approx(raw["createdAt"] / 1000)
        assert raw["lists"][0]["text"] in job.description_text
    if ats == "greenhouse":
        assert "<p" in job.description_html.lower()
        assert "&lt;p" not in job.description_html.lower()
        assert job.department
    if ats == "smartrecruiters":
        assert job.apply_url == raw["applyUrl"]
        assert job.employment_type == raw["typeOfEmployment"]["label"]


@pytest.mark.parametrize("ats", ATS)
@pytest.mark.parametrize("odd", [None, 42, False, {}, [None, {}, 9]])
def test_odd_locations_and_missing_dates_do_not_crash(
    ats: Any, odd: Any, recorded: dict[str, Any]
) -> None:
    raw = recorded_raw(ats, recorded)
    for key in (
        "location",
        "locations",
        "secondaryLocations",
        "country",
        "city",
        "state",
    ):
        raw[key] = odd
    if ats == "lever":
        raw["categories"] = {"location": odd, "allLocations": odd}
    for key in (
        "createdAt",
        "first_published",
        "publishedAt",
        "published_on",
        "published_at",
        "releasedDate",
    ):
        raw.pop(key, None)
    job = normalize_job(
        raw, SourceConfig("Company", ats, "board", "https://example.com"), NOW
    )
    assert isinstance(job.location_raw, str)
    assert job.posted_at is None


@pytest.mark.parametrize("ats", ATS)
def test_missing_identity_aborts_instead_of_inventing(
    ats: Any, recorded: dict[str, Any]
) -> None:
    raw = recorded_raw(ats, recorded)
    raw.pop("shortcode" if ats == "workable" else "id")
    with pytest.raises(ValueError, match="stable external ID"):
        normalize_job(
            raw, SourceConfig("Company", ats, "board", "https://example.com"), NOW
        )


def test_double_escaped_html_and_invalid_timestamps() -> None:
    assert decoded_html("&amp;lt;p&amp;gt;Hello&amp;lt;/p&amp;gt;") == "<p>Hello</p>"
    for value in (None, "bad date", {}, 10**40):
        assert timestamp(value) is None
        assert timestamp(value, milliseconds=True) is None


def test_stored_apply_link_does_not_trigger_scraping(recorded: dict[str, Any]) -> None:
    raw = recorded_raw("greenhouse", recorded)
    raw["absolute_url"] = "https://www.linkedin.com/jobs/view/123"
    job = normalize_job(
        raw, SourceConfig("Company", "greenhouse", "board", "https://example.com"), NOW
    )
    assert job.apply_url == raw["absolute_url"]


def test_workable_location_variants_keep_all_countries_and_stable_hashes(
    recorded: dict[str, Any],
) -> None:
    raw = recorded["workable-location-variants"]
    source = SourceConfig("FairMoney", "workable", "fairmoney", "https://example.com")
    first = {job.external_id: hash_job(job) for job in normalize_jobs(raw, source, NOW)}
    second = {
        job.external_id: hash_job(job)
        for job in normalize_jobs(list(reversed(raw)), source, NOW)
    }
    assert len(first) == 2
    engineer = first["F297960A25"]
    assert "Lagos" in engineer.location_raw and "Abuja" in engineer.location_raw
    international = first["A922F33517"]
    assert all(
        country in international.location_raw
        for country in ("India", "Nigeria", "South Africa")
    )
    classified = JobFilter(FILTERS).classify(international)
    assert "nigeria" in classified.location_tags
    for external_id, job in first.items():
        assert job.content_hash == second[external_id].content_hash
        assert job.canonical_key == second[external_id].canonical_key


def test_location_merge_rejects_conflicting_material_fields(
    recorded: dict[str, Any],
) -> None:
    raw = deepcopy(recorded["workable-location-variants"][:2])
    assert raw[0]["shortcode"] == raw[1]["shortcode"]
    raw[1]["description"] += "<p>Changed requirements</p>"
    with pytest.raises(ValueError, match="conflicting postings"):
        normalize_jobs(
            raw,
            SourceConfig("FairMoney", "workable", "fairmoney", "https://example.com"),
            NOW,
        )
