"""Stage 1 types; raw postings remain in each provider's JSON format."""

from dataclasses import dataclass, field
from typing import Any, Literal

type RawJob = dict[str, Any]
type ATSName = Literal[
    "greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters"
]
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
