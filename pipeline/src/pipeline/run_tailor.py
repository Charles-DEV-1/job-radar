"""Stage 4 only: claim eligible jobs, tailor, privately upload, record outcomes."""

import argparse
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import ValidationError

from pipeline.detect_ats import REPO_ROOT
from pipeline.detector import RunRecorder
from pipeline.http import HTTPClient
from pipeline.llm.client import Usage
from pipeline.resume.layout import LayoutError, load_layout
from pipeline.resume.pdf import Pagination
from pipeline.resume.schema import MasterResume, load_master
from pipeline.resume.store import ResumeStore, SupabaseResumeStore
from pipeline.resume.tailor import (
    TailoringClient,
    TailorResult,
    tailor_job,
    write_report,
)
from pipeline.storage import StoreError, SupabaseStore


@dataclass
class Outcome:
    job_id: str
    status: str
    usage: Usage
    error: str | None = None
    path: Path | None = None
    report_path: Path | None = None
    changes: dict[str, int | bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    change_report: dict[str, object] = field(default_factory=dict)


def print_outcome(outcome: Outcome) -> None:
    label = "Low fit" if outcome.status == "low_fit" else outcome.status
    if label == "ready":
        if outcome.changes.get("tailoring_changed"):
            label += " (tailored)"
        elif outcome.changes.get("document_changed"):
            label += " (formatting only)"
        elif outcome.changes.get("document_changed") is False:
            label += " (unchanged master)"
    print(
        f"{outcome.job_id}: {label}" + ("; " + outcome.error if outcome.error else "")
    )
    if outcome.path:
        print(f"DOCX: {outcome.path.resolve()}")
    if outcome.report_path:
        print(f"Change report: {outcome.report_path.resolve()}")
    if outcome.changes:
        print(
            "Changes: "
            + ", ".join(f"{key}={value}" for key, value in outcome.changes.items())
        )
    for warning in outcome.warnings:
        print("Note: " + warning)
    if outcome.change_report:
        print("Summary before: " + str(outcome.change_report["summary_before"]))
        print("Summary after:  " + str(outcome.change_report["summary_after"]))
        for key in ("skills_before", "skills_after"):
            groups = outcome.change_report[key]
            text = "; ".join(
                f"{group['group']}: {', '.join(group['skills'])}" for group in groups
            )
            print(
                ("Skills before: " if key.endswith("before") else "Skills after:  ")
                + text
            )
        matched = outcome.change_report["matched_requirements"]
        gaps = outcome.change_report["gaps"]
        print(
            "Matched requirements: "
            + ("; ".join(matched) if matched else "none recorded")
        )
        print("Gaps: " + ("; ".join(gaps) if gaps else "none recorded"))


def safe_error(exc: Exception) -> str:
    if isinstance(exc, (StoreError, LayoutError)):
        return str(exc)
    return "Tailoring operation failed (" + type(exc).__name__ + ")"


def record_total(
    outcomes: list[Outcome], recorder: RunRecorder, error: str | None = None
) -> None:
    total = Usage(
        attempts=sum(o.usage.attempts for o in outcomes),
        input_tokens=sum(o.usage.input_tokens for o in outcomes),
        output_tokens=sum(o.usage.output_tokens for o in outcomes),
        uncounted_attempts=sum(o.usage.uncounted_attempts for o in outcomes),
    )
    if outcomes and all(o.usage.cost_usd is not None for o in outcomes):
        total.cost_usd = sum(o.usage.cost_usd or 0 for o in outcomes)
    recorder.write(
        {
            "event": "tailor_run",
            "jobs": len(outcomes),
            "ready": sum(o.status == "ready" for o in outcomes),
            "tailored": sum(
                o.status == "ready" and bool(o.changes.get("tailoring_changed"))
                for o in outcomes
            ),
            "unchanged": sum(
                o.status == "ready" and o.changes.get("document_changed") is False
                for o in outcomes
            ),
            "failed": sum(o.status == "failed" for o in outcomes),
            "llm": total.record(),
            "error": error,
        }
    )


def run_jobs(
    store: ResumeStore,
    client: TailoringClient,
    master: MasterResume,
    original: Path,
    template: Path,
    layout: Path,
    output: Path,
    recorder: RunRecorder,
    limit: int = 3,
    include_maybe: bool = False,
    pagination: Pagination | None = None,
) -> list[Outcome]:
    if not 1 <= limit <= 20:
        raise ValueError("Tailoring run limit must be between 1 and 20")
    if not master.reviewed:
        raise ValueError(
            "Review master_resume.json and set reviewed=true before tailoring"
        )
    load_layout(layout, master, original, template)
    store.assert_private()
    outcomes: list[Outcome] = []
    for _ in range(limit):
        claim_id = str(uuid4())
        try:
            job = store.claim(claim_id, include_maybe)
        except Exception as exc:
            record_total(outcomes, recorder, safe_error(exc))
            raise
        if job is None:
            break
        usage = Usage()
        result: TailorResult | None = None
        remote_path: str | None = None
        error: str | None = None
        status = "failed"
        accepted = False
        try:
            result = tailor_job(
                master,
                job,
                original,
                template,
                layout,
                output / claim_id,
                client,
                usage,
                pagination,
                attempt_id=claim_id,
            )
            remote_path = f"{job['id']}/{claim_id}/{result.filename}"
            store.upload(remote_path, result.path)
            status, error = result.status, result.error
        except Exception as exc:
            error = safe_error(exc)
            remote_path = None
            if result:
                # An upload failure cannot produce a cloud download, but still
                # leave an exact master locally rather than a questionable file.
                try:
                    shutil.copyfile(original, result.path)
                    result.status, result.error = "failed", error
                    result.match_notes = None
                    result.changes = {}
                    write_report(result)
                except OSError:
                    error += "; local master fallback unavailable"
        metadata = {
            "llm": usage.record(),
            "warnings": result.warnings if result else [],
            "fallback": status in {"failed", "low_fit"}
            or any(
                "LLM proposal rejected or unavailable" in warning
                for warning in (result.warnings if result else [])
            ),
            "changes": result.changes if result else {},
        }
        try:
            accepted = store.finish(
                job,
                claim_id,
                status,
                remote_path,
                result.filename if result and remote_path else None,
                result.match_notes if result and status == "ready" else None,
                error,
                metadata,
            )
            if not accepted:
                status, error = "failed", "Job changed or tailoring claim is stale"
                if remote_path:
                    store.remove(remote_path)
        except Exception as exc:
            status, error = "failed", safe_error(exc)
            # Keep an uploaded object after an ambiguous RPC response: the RPC may
            # have committed. Never delete a file that a completed job references.
        outcome = Outcome(
            job["id"],
            status,
            usage,
            error,
            result.path if result else None,
            result.report_path if result else None,
            result.changes if result else {},
            result.warnings if result else [],
            result.change_report if result else {},
        )
        outcomes.append(outcome)
        recorder.write(
            {
                "event": "tailor",
                "claim_id": claim_id,
                "job_id": job["id"],
                "status": status,
                "accepted": accepted,
                "error": error,
                **metadata,
            }
        )
    record_total(outcomes, recorder)
    return outcomes


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(
        description="Truthful resume tailoring into private Supabase Storage"
    )
    data = REPO_ROOT / "data"
    parser.add_argument("--master-docx", type=Path, default=data / "master_resume.docx")
    parser.add_argument("--master-json", type=Path, default=data / "master_resume.json")
    parser.add_argument("--template", type=Path, default=data / "resume_template.docx")
    parser.add_argument("--layout", type=Path, default=data / "resume_layout.json")
    parser.add_argument("--output-dir", type=Path, default=data / "output")
    parser.add_argument("--report-file", type=Path, default=data / "tailor-runs.jsonl")
    parser.add_argument(
        "--limit", type=int, default=os.environ.get("RESUME_MAX_PER_RUN", "3")
    )
    parser.add_argument("--include-maybe", action="store_true")
    parser.add_argument(
        "--diagnose-job",
        help=(
            "Make one read-only tailoring request and print raw response "
            "plus field validation"
        ),
    )
    args = parser.parse_args(argv)
    store: SupabaseStore | None = None
    try:
        if not 1 <= args.limit <= 20:
            raise ValueError("Tailoring run limit must be between 1 and 20")
        master = load_master(args.master_json)
        if not master.reviewed:
            raise ValueError(
                "Review master_resume.json and set reviewed=true before tailoring"
            )
        load_layout(args.layout, master, args.master_docx, args.template)
        with HTTPClient() as http:
            # Configure before claiming any jobs: missing LLM credentials leave
            # pending jobs untouched and make no provider calls.
            client = TailoringClient.from_environment(http)
            store = SupabaseStore.from_environment(http)
            if args.diagnose_job:
                response = (
                    store.client.table("jobs")
                    .select("id,title,company,description_text")
                    .eq("id", args.diagnose_job)
                    .limit(1)
                    .execute()
                )
                if not response.data:
                    raise StoreError("Diagnostic job was not found")
                job = response.data[0]
                diagnostic_usage = Usage()
                try:
                    client.propose(
                        master,
                        job,
                        diagnostic_usage,
                        lambda _: None,
                        capture_diagnostic=True,
                    )
                except Exception:
                    pass
                if not client.last_diagnostic:
                    raise StoreError("No model response was received for diagnosis")
                print(
                    json.dumps(
                        {
                            "job_id": job["id"],
                            "job_title": job["title"],
                            "usage": diagnostic_usage.record(),
                            **client.last_diagnostic,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            outcomes = run_jobs(
                SupabaseResumeStore(store),
                client,
                master,
                args.master_docx,
                args.template,
                args.layout,
                args.output_dir,
                RunRecorder(args.report_file),
                args.limit,
                args.include_maybe,
            )
        tailored_count = sum(
            o.status == "ready" and bool(o.changes.get("tailoring_changed"))
            for o in outcomes
        )
        print(
            f"Tailoring run: {len(outcomes)} jobs; "
            f"{sum(o.status == 'ready' for o in outcomes)} ready; "
            f"{sum(o.status == 'failed' for o in outcomes)} failed; "
            f"{sum(o.status == 'low_fit' for o in outcomes)} low fit; "
            f"{tailored_count} tailored"
        )
        for outcome in outcomes:
            print_outcome(outcome)
        print("Usage/cost recorded in the private tailoring run log")
        return 1 if any(o.status == "failed" for o in outcomes) else 0
    except ValidationError:
        print("Tailoring setup failed: master JSON violates its strict schema")
    except (OSError, ValueError, StoreError) as exc:
        message = (
            str(exc)
            if isinstance(exc, (StoreError, LayoutError)) or type(exc) is ValueError
            else "Resume inputs unavailable; run python -m pipeline.resume.init"
        )
        print("Tailoring setup failed: " + message)
    finally:
        if store:
            store.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
