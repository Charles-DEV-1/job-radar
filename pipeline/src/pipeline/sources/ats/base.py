"""Common probe and raw retrieval behavior for verified public ATS endpoints."""

import re
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from pipeline.http import HTTPClient
from pipeline.models import ATSName, ProbeResult, RawJob


class BoardNotFound(Exception):
    """The provider explicitly returned a missing board."""


class ATSAdapter(ABC):
    name: ATSName
    enabled = True
    disabled_reason = "Endpoint has not been verified live"
    collection_key: str | None = "jobs"
    board_name_key: str | None = None

    def __init__(self, http: HTTPClient) -> None:
        self.http = http

    @abstractmethod
    def endpoint(self, slug: str) -> str:
        """Return the live-verified public endpoint pattern."""

    @staticmethod
    def safe_slug(slug: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", slug):
            raise ValueError("Invalid ATS slug")
        return quote(slug, safe="")

    def _payload(self, url: str) -> Any:
        if not self.enabled:
            raise RuntimeError(self.disabled_reason)
        response = self.http.get(url)
        if response.status_code in {404, 410}:
            raise BoardNotFound
        response.raise_for_status()
        return response.json()

    def _jobs(self, payload: Any) -> list[RawJob]:
        if self.collection_key is None:
            jobs = payload
        else:
            if not isinstance(payload, dict) or self.collection_key not in payload:
                raise ValueError(f"Missing {self.collection_key} array")
            jobs = payload[self.collection_key]
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise ValueError("Expected an array of job objects")
        return jobs

    def _count(self, payload: Any, jobs: list[RawJob]) -> int:
        return len(jobs)

    def probe(self, slug: str) -> ProbeResult:
        started = time.monotonic()
        result = ProbeResult(self.name, slug, "error")
        try:
            if not self.enabled:
                result.status = "disabled"
                result.error = self.disabled_reason
                return result
            payload = self._payload(self.endpoint(slug))
            jobs = self._jobs(payload)
            result.status = "found"
            result.job_count = self._count(payload, jobs)
            self._identity(payload, jobs, result)
            for job in jobs[:3]:
                title = job.get("title") or job.get("text") or job.get("name")
                if isinstance(title, str):
                    result.sample_titles.append(title)
                url = next(
                    (
                        job[key]
                        for key in (
                            "applyUrl",
                            "application_url",
                            "careers_apply_url",
                            "absolute_url",
                            "hostedUrl",
                            "jobUrl",
                            "careers_url",
                            "url",
                            "ref",
                        )
                        if isinstance(job.get(key), str)
                    ),
                    None,
                )
                if url:
                    result.sample_apply_urls.append(url)
        except BoardNotFound:
            result.status = "not_found"
        except httpx.TransportError as exc:
            result.status = "error"
            result.error = type(exc).__name__ + " while requesting public board"
        except httpx.HTTPStatusError as exc:
            result.status = "error"
            result.error = f"HTTP {exc.response.status_code} from public board"
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            result.status = "error"
            result.error = f"Invalid board response: {exc}"
        finally:
            result.duration_ms = round((time.monotonic() - started) * 1000, 2)
        return result

    def _identity(self, payload: Any, jobs: list[RawJob], result: ProbeResult) -> None:
        names: list[str] = []
        domains: list[str] = []
        objects = [payload] if isinstance(payload, dict) else []
        objects += jobs
        for obj in objects:
            keys = ["company_name", "companyName"]
            if obj is payload and self.board_name_key:
                keys.append(self.board_name_key)
            for key in keys:
                if isinstance(obj.get(key), str) and obj[key].strip():
                    names.append(obj[key].strip())
            company = obj.get("company")
            if isinstance(company, str) and company.strip():
                names.append(company.strip())
            elif isinstance(company, dict):
                if isinstance(company.get("name"), str) and company["name"].strip():
                    names.append(company["name"].strip())
                for key in ("domain", "website", "website_url"):
                    value = company.get(key)
                    if isinstance(value, str) and value.strip():
                        domains.append(self._domain(value))
            for key in ("company_domain", "companyDomain", "company_website"):
                if isinstance(obj.get(key), str) and obj[key].strip():
                    domains.append(self._domain(obj[key]))
        result.company_names = list(dict.fromkeys(names))
        result.company_domains = list(dict.fromkeys(domains))

    @staticmethod
    def _domain(value: str) -> str:
        return (
            urlsplit(value if "://" in value else "https://" + value).hostname or ""
        ).lower()

    def fetch_jobs(self, slug: str) -> list[RawJob]:
        # Propagate errors so a caller cannot confuse them with an empty board.
        return self._jobs(self._payload(self.endpoint(slug)))
