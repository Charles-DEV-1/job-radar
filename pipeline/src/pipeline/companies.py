"""Validate company input and update detector fields without losing YAML comments."""

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from pipeline.http import checked_url
from pipeline.models import Company, DetectionResult

CATEGORIES = {"fintech", "bank", "ai", "saas", "telecom", "other"}
ATS_VALUES = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "smartrecruiters",
    "none",
    "unknown",
}
STATUSES = {"confirmed", "needs_review", "not_found"}


class CompanyFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.indent(mapping=2, sequence=4, offset=2)
        with path.open(encoding="utf-8") as handle:
            self.document: Any = self.yaml.load(handle)
        if not isinstance(self.document, dict) or not isinstance(
            self.document.get("companies"), list
        ):
            raise ValueError("companies.yaml must contain a companies list")
        self.companies: list[Company] = []
        seen: set[str] = set()
        for entry in self.document["companies"]:
            company = self._validate(entry)
            if company.name.casefold() in seen:
                raise ValueError(f"Duplicate company name: {company.name}")
            seen.add(company.name.casefold())
            self.companies.append(company)

    @staticmethod
    def _validate(entry: Any) -> Company:
        if not isinstance(entry, dict):
            raise ValueError("Each company must be a mapping")
        for key in ("name", "domain", "category", "country"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"Company field {key} must be a nonempty string")
        name = entry["name"].strip()
        domain = entry["domain"].lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", domain):
            raise ValueError(f"{name}: domain must be a hostname, without a URL path")
        checked_url(f"https://{domain}")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise ValueError(f"{name}: aliases must be a list of nonempty strings")
        if type(entry.get("enabled")) is not bool:
            raise ValueError(f"{name}: enabled must be a boolean")
        if entry["category"] not in CATEGORIES:
            raise ValueError(f"{name}: invalid category")
        if not re.fullmatch(r"[A-Z]{2}", entry["country"]):
            raise ValueError(f"{name}: country must be a two-letter uppercase code")
        careers = entry.get("careers_url")
        if careers is not None:
            if not isinstance(careers, str):
                raise ValueError(f"{name}: careers_url must be a URL or null")
            checked_url(careers)
        if entry.get("ats", "unknown") not in ATS_VALUES:
            raise ValueError(f"{name}: invalid ats")
        if entry.get("detection_status", "needs_review") not in STATUSES:
            raise ValueError(f"{name}: invalid detection_status")
        if entry.get("ats_slug") is not None and not isinstance(entry["ats_slug"], str):
            raise ValueError(f"{name}: ats_slug must be a string or null")
        return Company(
            name,
            tuple(alias.strip() for alias in aliases),
            domain,
            careers,
            entry["category"],
            entry["country"],
            entry["enabled"],
        )

    def write(self, results: list[DetectionResult]) -> None:
        by_name = {result.company.name: result for result in results}
        for entry in self.document["companies"]:
            result = by_name.get(entry["name"].strip())
            if result is not None:
                entry["ats"] = result.ats
                entry["ats_slug"] = result.ats_slug
                entry["detection_status"] = result.detection_status
        # Serialize fully before replacing the original; failures leave it intact.
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                self.yaml.dump(self.document, handle)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
