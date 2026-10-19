"""Editable, explainable role and location classification; no compensation checks."""

import re
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from pipeline.models import Job

type FilterResult = Literal["include", "maybe", "exclude"]


def matches(value: str, terms: list[str]) -> list[str]:
    return [
        term
        for term in terms
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", value, re.IGNORECASE)
    ]


class JobFilter:
    def __init__(self, path: Path) -> None:
        with path.open(encoding="utf-8") as handle:
            self.config: Any = YAML(typ="safe").load(handle)
        if not isinstance(self.config, dict):
            raise ValueError("Filter configuration must be a mapping")
        self.roles = self.config["roles"]
        self.geo = self.config["locations"]
        self.seniority = self.config["seniority"]
        for key in ("exclude", "maybe"):
            self._terms(self.seniority, key)
        for key in (
            "engineering_titles",
            "include",
            "maybe",
            "exclude",
            "training",
            "description_include",
            "description_titles",
        ):
            self._terms(self.roles, key)
        for key in (
            "african_regions",
            "remote_terms",
            "remote_regions",
            "non_african",
            "eligibility_patterns",
        ):
            self._terms(self.geo, key)
        if not isinstance(self.geo.get("countries"), dict):
            raise ValueError("locations.countries must be a mapping")
        for country in self.geo["countries"].values():
            if not isinstance(country, dict) or not isinstance(
                country.get("name"), str
            ):
                raise ValueError("Each country needs a name, aliases, and cities")
            self._terms(country, "aliases")
            self._terms(country, "cities")
        self.regions = (
            [country["name"] for country in self.geo["countries"].values()]
            + self.geo["african_regions"]
            + self.geo["remote_regions"]
        )
        self.eligibility = [
            re.compile(pattern.replace("{region}", re.escape(region)), re.IGNORECASE)
            for pattern in self.geo["eligibility_patterns"]
            for region in self.regions
        ]

    @staticmethod
    def _terms(mapping: Any, key: str) -> None:
        values = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ValueError(f"Filter list {key} must contain nonempty strings")

    def _role(self, job: Job) -> tuple[FilterResult, str]:
        core = matches(job.title, self.roles["engineering_titles"])
        if core:
            adjacent = matches(job.title, self.roles["maybe"])
            if adjacent:
                return "maybe", "adjacent engineering role: " + ", ".join(adjacent)
            return "include", "explicit engineering title: " + ", ".join(core)
        excluded = matches(job.title, self.roles["exclude"])
        if excluded:
            return "exclude", "unrelated title: " + ", ".join(excluded)
        maybe = matches(job.title, self.roles["maybe"])
        if maybe:
            return "maybe", "adjacent engineering role: " + ", ".join(maybe)
        included = matches(job.title, self.roles["include"])
        if included:
            return "include", "engineering title: " + ", ".join(included)
        technical = matches(job.description_text, self.roles["description_include"])
        training = matches(job.title, self.roles["training"])
        if training and technical:
            return "include", "engineering internship/graduate evidence: " + ", ".join(
                technical
            )
        if training:
            return (
                "maybe",
                "internship/graduate title without clear engineering context",
            )
        if technical and matches(job.title, self.roles["description_titles"]):
            return "maybe", "technical description but ambiguous title: " + ", ".join(
                technical
            )
        return "exclude", "no software-engineering role evidence in title"

    def _location(self, job: Job) -> tuple[FilterResult, str]:
        raw = job.location_raw
        remote = job.remote_flag or bool(matches(raw, self.geo["remote_terms"]))
        job.remote_flag = remote
        tags: list[str] = ["remote"] if remote else []
        african: list[str] = []
        # Bare ISO country codes are accepted; do not confuse "e.g." with Egypt.
        country_code = raw.strip().upper()
        for code, country in self.geo["countries"].items():
            found = matches(raw, country["aliases"] + country["cities"])
            if found or country_code == code:
                african.append(country["name"])
                tags.append(country["name"].casefold())
                tags.extend(city.casefold() for city in matches(raw, country["cities"]))
        regions = matches(raw, self.geo["african_regions"])
        if regions:
            african.extend(regions)
            tags.append("africa")
        broad = matches(raw, self.geo["remote_regions"])
        tags.extend(region.casefold() for region in broad)
        job.location_tags = list(dict.fromkeys(tags))
        if not raw.strip():
            return "maybe", "location is missing"
        if african:
            return "include", "African location: " + ", ".join(dict.fromkeys(african))
        if broad:
            return "include", "broad eligibility region: " + ", ".join(broad)
        non_african = matches(raw, self.geo["non_african"])
        if non_african:
            return "exclude", "only non-African locations listed: " + ", ".join(
                non_african
            )
        if remote and any(
            pattern.search(job.description_text) for pattern in self.eligibility
        ):
            return (
                "include",
                "remote description explicitly includes Africa/Nigeria/EMEA/worldwide",
            )
        return "maybe", "location or remote eligibility is ambiguous"

    def classify(self, job: Job) -> Job:
        role, role_reason = self._role(job)
        excluded = matches(job.title, self.seniority["exclude"])
        review = matches(job.title, self.seniority["maybe"])
        if excluded:
            role = "exclude"
            role_reason = (
                "outside student/junior profile; excluded seniority: "
                + ", ".join(excluded)
            )
        elif review and role != "exclude":
            role = "maybe"
            role_reason = (
                "seniority requires review for student/junior profile: "
                + ", ".join(review)
            )
        location, location_reason = self._location(job)
        job.filter_result = (
            "exclude"
            if "exclude" in (role, location)
            else "maybe"
            if "maybe" in (role, location)
            else "include"
        )
        job.filter_reason = f"Role: {role_reason}. Location: {location_reason}."
        return job
