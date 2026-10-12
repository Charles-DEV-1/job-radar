"""Field mappings verified against recorded provider responses."""

import html
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from pipeline.models import Job, RawJob, SourceConfig


def normalize_jobs(
    raw_jobs: list[RawJob], source: SourceConfig, seen_at: datetime
) -> list[Job]:
    """Keep stable IDs; Workable emits one otherwise identical row per location."""
    unique: dict[str, Job] = {}
    places: dict[str, set[str]] = {}
    for raw in raw_jobs:
        job = normalize_job(raw, source, seen_at)
        previous = unique.get(job.external_id)
        if previous is not None:
            equivalent = (
                replace(previous, location_raw="") == replace(job, location_raw="")
                if source.ats == "workable"
                else previous == job
            )
            if not equivalent:
                raise ValueError(
                    "Source returned conflicting postings for the same external ID"
                )
        else:
            unique[job.external_id] = job
            places[job.external_id] = set()
        if job.location_raw:
            places[job.external_id].add(job.location_raw)
    for external_id, job in unique.items():
        job.location_raw = "; ".join(sorted(places[external_id]))
    return list(unique.values())


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def label(value: Any) -> str:
    if isinstance(value, dict):
        return text(value.get("name")) or text(value.get("label"))
    return text(value)


def join_values(values: list[str], separator: str = ", ") -> str:
    return separator.join(dict.fromkeys(value for value in values if value))


def locations(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return join_values([locations(item) for item in value], "; ")
    if isinstance(value, dict):
        full = text(value.get("fullLocation")) or text(value.get("location_str"))
        if full:
            return full
        name = text(value.get("name")) or text(value.get("location"))
        country = text(value.get("country")) or text(value.get("country_name"))
        return join_values(
            [
                text(value.get("city")) or name,
                text(value.get("region")) or text(value.get("state")),
                country,
            ]
        )
    return ""


def timestamp(value: Any, *, milliseconds: bool = False) -> datetime | None:
    try:
        if milliseconds and type(value) in {int, float}:
            return datetime.fromtimestamp(value / 1000, UTC)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(
                value.strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
            )
            return (
                parsed.replace(tzinfo=UTC)
                if parsed.tzinfo is None
                else parsed.astimezone(UTC)
            )
    except (ValueError, OverflowError, OSError):
        pass
    return None


def html_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def decoded_html(value: str) -> str:
    for _ in range(3):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


def normalize_job(raw: RawJob, source: SourceConfig, seen_at: datetime) -> Job:
    ats = source.ats
    external = raw.get("shortcode") if ats == "workable" else raw.get("id")
    if (
        not isinstance(external, (str, int))
        or isinstance(external, bool)
        or not str(external).strip()
    ):
        raise ValueError(f"{ats}: posting lacks a stable external ID")
    title = text(
        raw.get(
            "text"
            if ats == "lever"
            else "name"
            if ats == "smartrecruiters"
            else "title"
        )
    )
    if not title:
        raise ValueError(f"{ats}: posting lacks a title")
    remote = False
    department = employment = ""
    description = plain = location = url = ""
    posted: datetime | None = None
    if ats == "greenhouse":
        location = locations(raw.get("location"))
        description = decoded_html(text(raw.get("content")))
        departments = raw.get("departments")
        department = (
            join_values([label(item) for item in departments], "; ")
            if isinstance(departments, list)
            else ""
        )
        url = text(raw.get("absolute_url"))
        posted = timestamp(raw.get("first_published"))
    elif ats == "lever":
        categories = (
            raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
        )
        location = locations(categories.get("allLocations")) or locations(
            categories.get("location")
        )
        department = text(categories.get("department")) or text(categories.get("team"))
        employment = text(categories.get("commitment"))
        remote = text(raw.get("workplaceType")).lower() == "remote"
        lists = raw.get("lists") if isinstance(raw.get("lists"), list) else []
        sections = []
        for section in lists:
            if isinstance(section, dict):
                sections.append(
                    "<h3>"
                    + html.escape(text(section.get("text")))
                    + "</h3>"
                    + text(section.get("content"))
                )
        description = "\n".join(
            filter(
                None,
                [
                    text(raw.get("opening")),
                    text(raw.get("descriptionBody")) or text(raw.get("description")),
                    *sections,
                    text(raw.get("additional")),
                ],
            )
        )
        plain = html_text(description) or text(raw.get("descriptionPlain"))
        url = text(raw.get("applyUrl")) or text(raw.get("hostedUrl"))
        posted = timestamp(raw.get("createdAt"), milliseconds=True)
    elif ats == "ashby":
        location = join_values(
            [locations(raw.get("location")), locations(raw.get("secondaryLocations"))],
            "; ",
        )
        department = text(raw.get("department")) or text(raw.get("team"))
        employment = text(raw.get("employmentType"))
        remote = (
            raw.get("isRemote") is True
            or text(raw.get("workplaceType")).lower() == "remote"
        )
        description = text(raw.get("descriptionHtml"))
        plain = text(raw.get("descriptionPlain"))
        url = text(raw.get("applyUrl")) or text(raw.get("jobUrl"))
        posted = timestamp(raw.get("publishedAt"))
    elif ats == "workable":
        location = locations(raw.get("locations")) or locations(raw)
        department, employment = (
            label(raw.get("department")),
            label(raw.get("employment_type")),
        )
        remote = raw.get("telecommuting") is True
        description = text(raw.get("description"))
        url = (
            text(raw.get("application_url"))
            or text(raw.get("url"))
            or text(raw.get("shortlink"))
        )
        posted = timestamp(raw.get("published_on"))
    elif ats == "recruitee":
        location = locations(raw.get("locations")) or locations(raw.get("location"))
        department, employment = (
            label(raw.get("department")),
            text(raw.get("employment_type_code")),
        )
        remote = raw.get("remote") is True
        description = "\n".join(
            filter(None, [text(raw.get("description")), text(raw.get("requirements"))])
        )
        url = text(raw.get("careers_apply_url")) or text(raw.get("careers_url"))
        posted = timestamp(raw.get("published_at"))
    elif ats == "smartrecruiters":
        location = locations(raw.get("location"))
        loc = raw.get("location")
        remote = isinstance(loc, dict) and loc.get("remote") is True
        department, employment = (
            label(raw.get("department")),
            label(raw.get("typeOfEmployment")),
        )
        ad = raw.get("jobAd") if isinstance(raw.get("jobAd"), dict) else {}
        sections = ad.get("sections") if isinstance(ad.get("sections"), dict) else {}
        description = "\n".join(
            text(section.get("text"))
            for section in sections.values()
            if isinstance(section, dict) and text(section.get("text"))
        )
        url = text(raw.get("applyUrl")) or text(raw.get("postingUrl"))
        posted = timestamp(raw.get("releasedDate"))
    if not url:
        raise ValueError(f"{ats}: posting lacks a public apply URL")
    # This is a stored browser link, not an automated request. ATS-provided
    # LinkedIn apply links can be kept without ever fetching LinkedIn.
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
    ):
        raise ValueError(f"{ats}: posting has an invalid apply URL")
    if seen_at.tzinfo is None:
        raise ValueError("seen_at must be timezone-aware")
    return Job(
        source=ats,
        source_company_slug=source.slug,
        external_id=str(external),
        company=source.company_name,
        title=title,
        location_raw=location,
        location_tags=[],
        remote_flag=bool(remote),
        department=department or None,
        employment_type=employment or None,
        description_html=description,
        description_text=plain or html_text(description),
        apply_url=url,
        posted_at=posted,
        first_seen_at=seen_at.astimezone(UTC),
    )
