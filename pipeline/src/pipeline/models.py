"""Discovery and canonical job types; adapters retain provider-shaped raw JSON."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

type RawJob = dict[str, Any]
type ATSName = Literal[
    "greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters"
]
type SourceName = ATSName | Literal["careers_page", "feed"]
type DetectionStatus = Literal["confirmed", "needs_review", "not_found"]
type ProbeStatus = Literal["found", "not_found", "error", "disabled"]


@dataclass(frozen=True)
class Company:
    name: str
    aliases: tuple[str, ...]
    domain: str
    careers_url: str | None
    category: str
    country: str
    enabled: bool


@dataclass
class ProbeResult:
    ats: ATSName
    slug: str
    status: ProbeStatus
    job_count: int = 0
    company_names: list[str] = field(default_factory=list)
    company_domains: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    sample_apply_urls: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0


@dataclass(frozen=True)
class BoardLink:
    ats: ATSName
    slug: str
    evidence_url: str | None = None


@dataclass
class DiscoveryResult:
    links: list[BoardLink] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Match:
    probe: ProbeResult
    status: DetectionStatus
    reason: str
    evidence_url: str | None = None


@dataclass
class DetectionResult:
    company: Company
    ats: str
    ats_slug: str | None
    detection_status: DetectionStatus
    job_count: int | None
    reason: str
    matches: list[Match] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceConfig:
    company_name: str
    ats: SourceName
    slug: str
    url: str
    enabled: bool = True
    company_domain: str = ""
    source_type: Literal["ats", "careers_page", "feed"] = "ats"


@dataclass
class Job:
    source: SourceName
    source_company_slug: str
    external_id: str
    company: str
    title: str
    location_raw: str
    location_tags: list[str]
    remote_flag: bool
    department: str | None
    employment_type: str | None
    description_text: str
    description_html: str
    apply_url: str
    posted_at: datetime | None
    first_seen_at: datetime
    content_hash: str = ""
    canonical_key: str = ""
    filter_result: Literal["include", "maybe", "exclude"] = "maybe"
    filter_reason: str = "Not classified"
    status: str = "new"
    source_type: Literal["ats", "careers_page", "feed"] = "ats"


@dataclass(frozen=True)
class FetchCounts:
    jobs_found: int
    inserted_jobs: int
    new_jobs: int
    updated_jobs: int
    duplicates: int
    closed_jobs: int
    backfill: bool


@dataclass
class SourceOutcome:
    source: SourceConfig
    status: Literal["ok", "error"]
    counts: FetchCounts | None = None
    error: str | None = None
