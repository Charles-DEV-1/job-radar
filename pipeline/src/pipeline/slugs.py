"""Small, deterministic candidate lists; a slug is never ownership evidence."""

import re
import unicodedata

import tldextract

from pipeline.models import Company

# Use the bundled suffix snapshot: no hidden external request or cache directory.
DOMAIN_PARTS = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def generate_slugs(company: Company, limit: int = 6) -> list[str]:
    if limit < 1:
        raise ValueError("slug limit must be positive")
    domain = DOMAIN_PARTS(company.domain).domain
    values = [domain, company.name, *company.aliases]
    candidates: list[str] = []
    for value in values:
        ascii_value = (
            unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        )
        hyphenated = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
        for slug in (hyphenated, hyphenated.replace("-", "")):
            if slug and slug not in candidates:
                candidates.append(slug)
                if len(candidates) == limit:
                    return candidates
    return candidates
