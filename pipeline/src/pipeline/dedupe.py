"""Deterministic identities and content hashes without ingestion timestamps."""

import hashlib
import json
import re
import unicodedata

from pipeline.models import Job


def normalized(value: str) -> str:
    return " ".join(
        re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())
    )


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def hash_job(job: Job) -> Job:
    job.canonical_key = digest(
        [
            normalized(job.company),
            normalized(job.title),
            normalized(job.location_raw),
        ]
    )
    job.content_hash = digest(
        {
            "company": job.company,
            "title": job.title,
            "location": job.location_raw,
            "remote": job.remote_flag,
            "department": job.department,
            "employment_type": job.employment_type,
            "description_html": job.description_html,
            "description_text": job.description_text,
        }
    )
    return job
