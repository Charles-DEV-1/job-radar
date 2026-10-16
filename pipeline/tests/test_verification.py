from dataclasses import replace

import pytest

from pipeline.models import Company, ProbeResult
from pipeline.slugs import generate_slugs
from pipeline.verification import verify_match


def test_slugs_include_domain_name_and_aliases(company: Company) -> None:
    entry = replace(
        company, name="Fair Money", aliases=("FM Africa",), domain="www.fairmoney.io"
    )
    assert generate_slugs(entry) == ["fairmoney", "fair-money", "fm-africa", "fmafrica"]


def test_slug_limit_and_unicode(company: Company) -> None:
    entry = replace(
        company,
        name="Élan & Cloud",
        aliases=("One Two", "Three Four"),
        domain="elan.com.ng",
    )
    assert generate_slugs(entry, limit=3) == ["elan", "elan-cloud", "elancloud"]


def test_domain_root_uses_public_suffix_and_ignores_subdomains(
    company: Company,
) -> None:
    entry = replace(company, domain="careers.fairmoney.com.ng")
    assert generate_slugs(entry)[0] == "fairmoney"


@pytest.mark.parametrize("name", ["FairMoney", "Fair Money", "FairMoney Limited"])
def test_company_name_verification(company: Company, name: str) -> None:
    probe = ProbeResult(
        "workable", "fairmoney", "found", job_count=1, company_names=[name]
    )
    assert verify_match(company, probe).status == "confirmed"


def test_long_name_allows_a_small_spelling_difference(company: Company) -> None:
    entry = replace(company, name="FairMoney Technologies", aliases=())
    probe = ProbeResult(
        "workable",
        "fairmoney",
        "found",
        job_count=1,
        company_names=["FairMoney Technologie"],
    )
    assert verify_match(entry, probe).status == "confirmed"


@pytest.mark.parametrize(
    "wrong", ["FairMoney Loans", "Money", "Fairbank", "FairMoney Kenya"]
)
def test_wrong_company_is_never_confirmed(company: Company, wrong: str) -> None:
    probe = ProbeResult(
        "workable", "fairmoney", "found", job_count=1, company_names=[wrong]
    )
    assert verify_match(company, probe).status == "needs_review"
    assert (
        verify_match(company, probe, "https://fairmoney.io/careers").status
        == "needs_review"
    )


def test_same_slug_without_identity_needs_review(company: Company) -> None:
    probe = ProbeResult("lever", "fairmoney", "found", job_count=1)
    assert verify_match(company, probe).status == "needs_review"


def test_official_link_verifies_an_empty_successful_board(company: Company) -> None:
    probe = ProbeResult("ashby", "unexpected-slug", "found", job_count=0)
    match = verify_match(company, probe, "https://careers.fairmoney.io/jobs")
    assert match.status == "confirmed" and "empty, verify" in match.reason
    assert match.evidence_url


@pytest.mark.parametrize(
    "url",
    [
        "https://fairmoney.io.evil.com",
        "https://evilfairmoney.io",
        "https://jobs.lever.co/fairmoney",
    ],
)
def test_external_link_cannot_supply_official_proof(company: Company, url: str) -> None:
    assert (
        verify_match(
            company, ProbeResult("lever", "fairmoney", "found", job_count=1), url
        ).status
        == "needs_review"
    )


def test_domain_match_and_conflict(company: Company) -> None:
    probe = ProbeResult(
        "workable",
        "fairmoney",
        "found",
        job_count=1,
        company_domains=["www.fairmoney.io"],
    )
    assert verify_match(company, probe).status == "confirmed"
    probe.company_domains = ["wrong-company.com"]
    probe.job_count = 1
    probe.company_names = ["FairMoney"]
    assert (
        verify_match(company, probe, "https://fairmoney.io/").status == "needs_review"
    )


def test_short_similar_brands_do_not_fuzzy_match(company: Company) -> None:
    entry = replace(company, name="Wave", aliases=())
    for wrong in ("Wav", "Flutterwave", "Wave Financial"):
        probe = ProbeResult(
            "lever", "wave", "found", job_count=1, company_names=[wrong]
        )
        assert verify_match(entry, probe).status == "needs_review"
