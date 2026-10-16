"""Conservative board ownership checks, including actual Lever posting evidence."""

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import unquote

from pipeline.discovery import owns_url, trusted_careers_url
from pipeline.models import Company, Match, ProbeResult

LEGAL_SUFFIXES = {"inc", "ltd", "limited", "llc", "plc", "corp", "corporation"}


def normalized_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    # Parenthetical brand/annotation text must not hide an approved legal name.
    # Compare the remaining full name, never arbitrary substrings or bracket labels.
    value = re.sub(r"\([^)]*\)", " ", value)
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

    def confirmed(reason: str) -> Match:
        if probe.job_count == 0:
            reason += "; empty, verify"
        return Match(probe, "confirmed", reason, evidence_url)

    def unverified(reason: str) -> Match:
        status = "not_found" if probe.job_count == 0 else "needs_review"
        return Match(probe, status, reason, evidence_url)

    if probe.company_domains:
        matching = [
            owns_url(company, "https://" + domain) for domain in probe.company_domains
        ]
        if not all(matching):
            return unverified("Board company domain conflicts with entry")
        identity = "Board company domain matches official domain"
        empty_reason = "empty, domain match only"
    elif probe.company_names:
        matching = [name_matches(name, company) for name in probe.company_names]
        if not all(matching):
            return unverified("Board company name conflicts with entry")
        identity = "Board company name matches entry"
        empty_reason = "empty, name match only"
    else:
        identity = ""
        empty_reason = "Empty board has no verified company identity"
    if probe.job_count == 0:
        if evidence_url and owns_url(company, evidence_url):
            return confirmed("ATS link found on official company page")
        if identity:
            return Match(probe, "needs_review", empty_reason, evidence_url)
        return unverified(empty_reason)
    if probe.ats == "lever":
        if lever_mentions_company(company, probe):
            return confirmed(
                "Lever posting apply URL or description mentions company name/domain"
            )
        return unverified("Lever postings do not mention the company name/domain")
    if identity:
        return confirmed(identity)
    if evidence_url and owns_url(company, evidence_url):
        return confirmed("ATS link found on official company page")
    if evidence_url and trusted_careers_url(company, evidence_url):
        return confirmed("ATS link supplied by explicitly configured careers URL")
    return unverified("Board exists but has no verified company identity")


def lever_mentions_company(company: Company, probe: ProbeResult) -> bool:
    domain = company.domain.lower().removeprefix("www.").rstrip(".")
    patterns = [r"(?<![\w.-])(?:[\w-]+\.)*" + re.escape(domain) + r"(?![\w.-])"]
    for name in (company.name, *company.aliases):
        name = re.sub(r"\([^)]*\)", " ", name)
        words = re.findall(r"[^\W_]+", name.casefold())
        while words and words[-1] in LEGAL_SUFFIXES:
            words.pop()
        if words:
            patterns.append(
                r"(?<!\w)" + r"[\W_]+".join(map(re.escape, words)) + r"(?!\w)"
            )
    return any(
        re.search(pattern, unquote(text), re.IGNORECASE)
        for text in (*probe.sample_apply_urls, *probe.job_identity_texts)
        for pattern in patterns
    )
