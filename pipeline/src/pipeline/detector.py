"""Isolate sources and record every attempt without storing job descriptions."""

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pipeline.discovery import CareersDiscovery
from pipeline.models import (
    Company,
    DetectionResult,
    DiscoveryResult,
    Match,
    ProbeResult,
)
from pipeline.slugs import generate_slugs
from pipeline.sources.ats.base import ATSAdapter
from pipeline.verification import verify_match


class RunRecorder:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = str(uuid4())

    def write(self, record: dict[str, Any]) -> None:
        record = {
            "run_id": self.run_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            **record,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Detector:
    def __init__(
        self,
        adapters: list[ATSAdapter],
        discovery: CareersDiscovery,
        recorder: RunRecorder,
    ) -> None:
        self.adapters = {adapter.name: adapter for adapter in adapters}
        self.discovery = discovery
        self.recorder = recorder
        self.cache: dict[tuple[str, str], ProbeResult] = {}

    def _probe(self, company: Company, adapter: ATSAdapter, slug: str) -> ProbeResult:
        key = (adapter.name, slug)
        cached = key in self.cache
        if not cached:
            started = time.monotonic()
            try:
                probe = adapter.probe(slug)
            except Exception as exc:
                # Record unexpected adapter bugs too, then continue other sources.
                probe = ProbeResult(
                    adapter.name,
                    slug,
                    "error",
                    error=f"Unexpected {type(exc).__name__}",
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
            self.cache[key] = probe
        probe = self.cache[key]
        self.recorder.write(
            {
                "event": "probe",
                "company": company.name,
                "cached": cached,
                **asdict(probe),
            }
        )
        return probe

    def detect(self, company: Company) -> DetectionResult:
        started = time.monotonic()
        try:
            discovery = self.discovery.discover(company)
        except Exception as exc:
            discovery = DiscoveryResult(
                errors=[f"Unexpected discovery {type(exc).__name__}"]
            )
        errors = list(discovery.errors)
        self.recorder.write(
            {
                "event": "discovery",
                "company": company.name,
                "status": "error" if discovery.errors else "ok",
                "errors": discovery.errors,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "board_link_count": len(discovery.links),
            }
        )
        matches: list[Match] = []
        tried: set[tuple[str, str]] = set()

        def attempt(
            adapter: ATSAdapter, slug: str, evidence: str | None = None
        ) -> None:
            key = (adapter.name, slug)
            if key in tried:
                return
            tried.add(key)
            probe = self._probe(company, adapter, slug)
            if probe.status == "found":
                matches.append(verify_match(company, probe, evidence))
            elif probe.status == "not_found" and evidence:
                errors.append(
                    f"{adapter.name}/{slug}: official ATS link found, "
                    "but the verified public endpoint returned a missing board"
                )
            elif probe.error:
                errors.append(f"{adapter.name}/{slug}: {probe.error}")

        # Official links take priority, preserve case, and avoid unnecessary guessing.
        for link in sorted(discovery.links, key=lambda link: link.evidence_url is None):
            adapter = self.adapters.get(link.ats)
            if adapter is not None:
                attempt(adapter, link.slug, link.evidence_url)
        if not any(match.status == "confirmed" for match in matches):
            for adapter in self.adapters.values():
                if not adapter.enabled:
                    attempt(adapter, generate_slugs(company)[0])
                    continue
                for slug in generate_slugs(company):
                    attempt(adapter, slug)
                    if any(match.status == "confirmed" for match in matches):
                        break
                if any(match.status == "confirmed" for match in matches):
                    break
        if matches:
            matches.sort(
                key=lambda match: (
                    match.status != "confirmed",
                    match.evidence_url is None,
                )
            )
            best = matches[0]
            result = DetectionResult(
                company,
                best.probe.ats,
                best.probe.slug,
                best.status,
                best.probe.job_count,
                best.reason,
                matches,
                errors,
            )
        elif errors:
            result = DetectionResult(
                company,
                "unknown",
                None,
                "needs_review",
                None,
                "Discovery incomplete; see recorded errors",
                errors=errors,
            )
        else:
            result = DetectionResult(
                company,
                "none",
                None,
                "not_found",
                None,
                "No supported public ATS board found",
            )
        self.recorder.write(
            {
                "event": "company",
                "company": company.name,
                "ats": result.ats,
                "ats_slug": result.ats_slug,
                "status": result.detection_status,
                "job_count": result.job_count,
                "errors": errors,
                "reason": result.reason,
                "matches": [
                    {
                        "ats": match.probe.ats,
                        "slug": match.probe.slug,
                        "status": match.status,
                        "reason": match.reason,
                        "evidence_url": match.evidence_url,
                    }
                    for match in result.matches
                ],
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        return result
