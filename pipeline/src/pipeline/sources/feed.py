"""Explicitly supplied, verified job-only RSS/Atom and JSON feeds."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import fromstring
from ruamel.yaml import YAML

from pipeline.llm.client import Posting, Usage
from pipeline.models import Job, SourceConfig
from pipeline.normalize import html_text
from pipeline.sources.careers_page import cached_jobs, posting_job
from pipeline.sources.pages import PageFetcher, clean_url
from pipeline.storage import job_record


def load_feeds(path: Path) -> list[SourceConfig]:
    if not path.exists():
        return []
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
        raise ValueError("feeds.yaml must contain a feeds list")
    sources: list[SourceConfig] = []
    for entry in data["feeds"]:
        if not isinstance(entry, dict):
            raise ValueError("Each feed must be a mapping")
        for key in ("name", "company", "domain", "url"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError("Feed requires name, company, domain and url")
        if type(entry.get("enabled")) is not bool:
            raise ValueError("Feed enabled must be a boolean")
        if entry["enabled"] and (
            entry.get("verified") is not True
            or entry.get("scope") not in {"nigeria", "africa"}
        ):
            raise ValueError("Enable only verified Nigeria/Africa job feeds")
        url = clean_url(entry["url"])
        sources.append(
            SourceConfig(
                entry["company"],
                "feed",
                hashlib.sha256(url.encode()).hexdigest(),
                url,
                entry["enabled"],
                entry["domain"],
                "feed",
            )
        )
    return sources


def parse_feed(body: str) -> list[dict[str, Any]]:
    if body.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(body)
        except ValueError:
            raise ValueError("Feed returned invalid JSON") from None
        items = data if isinstance(data, list) else data.get("jobs", data.get("items"))
        if not isinstance(items, list):
            raise ValueError("JSON feed must contain jobs/items array")
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid JSON feed item")
            result.append(
                {
                    "title": item.get("title"),
                    "location": item.get("location"),
                    "apply_url": item.get("apply_url", item.get("url")),
                    "description_snippet": item.get(
                        "description_text",
                        item.get(
                            "content_text",
                            item.get("content_html", item.get("description")),
                        ),
                    ),
                }
            )
        return result
    try:
        root = fromstring(body)
    except Exception:
        raise ValueError("Feed returned invalid or unsafe XML") from None
    kind = root.tag.split("}")[-1]
    if kind not in {"rss", "feed", "RDF"}:
        raise ValueError("Response is not an RSS/Atom job feed")
    result = []
    for item in root.iter():
        if item.tag.split("}")[-1] not in {"item", "entry"}:
            continue
        fields = {child.tag.split("}")[-1]: child for child in item}
        link = fields.get("link")
        url = None
        # Atom may have self/enclosure links before its alternate detail URL.
        for child in item:
            if (
                child.tag.split("}")[-1] == "link"
                and child.get("rel", "alternate") == "alternate"
            ):
                url = child.get("href") or child.text
                break
        if url is None and link is not None:
            url = link.text
        desc = next(
            (
                fields[name].text
                for name in ("encoded", "content", "description", "summary")
                if name in fields
            ),
            None,
        )
        result.append(
            {
                "title": fields["title"].text if "title" in fields else None,
                "location": fields["location"].text if "location" in fields else None,
                "apply_url": url,
                "description_snippet": desc,
            }
        )
    return result


class FeedAdapter:
    def __init__(self, pages: PageFetcher) -> None:
        self.pages = pages

    def fetch(
        self,
        source: SourceConfig,
        previous: dict[str, Any],
        observed: datetime,
        usage: Usage,
    ) -> tuple[list[Job], dict[str, Any], list[str]]:
        body, _url = self.pages.plain(source.url)
        items = parse_feed(body)
        if not items and previous.get("jobs"):
            raise ValueError("Previously nonempty feed returned zero jobs")
        jobs = {job.external_id: job for job in cached_jobs(previous)}
        for item in items:
            try:
                posting = Posting.model_validate(item)
                if not posting.title.strip():
                    raise ValueError()
                posting.apply_url = clean_url(posting.apply_url)
            except Exception:
                raise ValueError(
                    "Feed posting lacks a valid title or absolute apply URL"
                ) from None
            description = html_text(posting.description_snippet or "")
            job = posting_job(posting, source, observed, description)
            jobs[job.external_id] = job
        state = {
            "jobs": [job_record(j) for j in jobs.values()],
            "hash": hashlib.sha256(
                json.dumps(items, sort_keys=True).encode()
            ).hexdigest(),
        }
        return list(jobs.values()), state, []
