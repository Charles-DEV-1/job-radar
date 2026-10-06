"""Conservative ownership checks; never infer ownership from slug or job text."""

import re
import unicodedata
from difflib import SequenceMatcher

from pipeline.discovery import owns_url
from pipeline.models import Company, Match, ProbeResult

LEGAL_SUFFIXES = {"inc", "ltd", "limited", "llc", "plc", "corp", "corporation"}


def normalized_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", value.casefold())
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


def name_matches(value: str, company: Company) -> bool:
    candidate = normalized_name(value)
    for name in (company.name, *company.aliases):
        expected = normalized_name(name)
        if candidate and candidate == expected:
            return True
        # Short brands and substring collisions must match exactly. Longer names
        # permit only a small spelling difference, never arbitrary containment.
        if (
            min(len(candidate), len(expected)) >= 7
            and abs(len(candidate) - len(expected)) <= 1
        ):
            if SequenceMatcher(None, candidate, expected).ratio() >= 0.94:
                return True
    return False


def verify_match(
    company: Company, probe: ProbeResult, evidence_url: str | None = None
) -> Match:
    if probe.status != "found":
        raise ValueError("Only an existing board can be verified")
    if probe.company_domains:
        matching = [
            owns_url(company, "https://" + domain) for domain in probe.company_domains
        ]
        if not all(matching):
            return Match(
                probe, "needs_review", "Board company domain conflicts with entry"
            )
        return Match(
            probe,
            "confirmed",
            "Board company domain matches official domain",
            evidence_url,
        )
    if probe.company_names:
        matching = [name_matches(name, company) for name in probe.company_names]
        if not all(matching):
            return Match(
                probe, "needs_review", "Board company name conflicts with entry"
            )
        return Match(
            probe, "confirmed", "Board company name matches entry", evidence_url
        )
    if evidence_url and owns_url(company, evidence_url):
        return Match(
            probe, "confirmed", "ATS link found on official company page", evidence_url
        )
    return Match(
        probe, "needs_review", "Board exists but has no verified company identity"
    )
