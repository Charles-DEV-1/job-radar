"""Bounded LLM tailoring, code validation, rendering, and exact-master fallback."""

import re
import shutil
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError

from pipeline.llm.client import (
    ExtractionClient,
    ProviderBudgetError,
    ProviderRateLimitError,
    Usage,
    account_rate_limit,
    account_reply,
)
from pipeline.llm.pacing import prompt_tokens
from pipeline.resume.changes import human_report, summarize
from pipeline.resume.description import compact_description
from pipeline.resume.layout import LayoutError, load_layout, render
from pipeline.resume.pdf import Pagination
from pipeline.resume.privacy import ContactRedactor
from pipeline.resume.prompt import compact_json, request
from pipeline.resume.relevance import deterministic_tailoring, matching_skills
from pipeline.resume.schema import MasterResume, Tailoring
from pipeline.resume.validate import (
    TruthError,
    audit_proposal,
    unchanged,
    validate_tailoring,
)


class TailoringFailure(ValueError):
    pass


class OutputLimitFailure(TailoringFailure):
    """A known truncated response must not spend quota on identical retries."""


class TailoringClient(ExtractionClient):
    last_diagnostic: dict[str, Any] | None = None

    def propose(
        self,
        master: MasterResume,
        job: dict[str, Any],
        usage: Usage,
        check: Callable[[Tailoring], None],
        warnings: list[str] | None = None,
        skill_slot_limits: list[int] | None = None,
        capture_diagnostic: bool = False,
    ) -> Tailoring:
        if not master.reviewed:
            raise TailoringFailure("Master facts require owner review")
        redactor = ContactRedactor(master.contact)
        description = compact_description(job["description_text"], job["company"])
        budget = getattr(getattr(self.backend, "budget", None), "limit", None)
        last_error = "No valid output"
        for attempt in range(3):
            usage.attempts += 1
            received = False
            try:
                while True:
                    # Rebuild options after every trim and include retry feedback in
                    # sizing. Measure exactly the redacted text the provider receives.
                    system, payload, minimum = request(
                        master, job, description=description.text
                    )
                    payload["validator_errors"] = [] if attempt == 0 else [last_error]
                    sent_system = redactor.text(system)
                    sent_payload = redactor.payload(payload)
                    content = compact_json(sent_payload)
                    estimated = prompt_tokens(sent_system, content)
                    excess = max(0, estimated + minimum - budget) if budget else 0
                    oversized = max(0, len(content) - 100_000)
                    if not excess and not oversized:
                        break
                    if not description.trim(max(excess * 3 + 96, oversized, 1)):
                        raise ProviderBudgetError(
                            "LLM budget cannot fit master facts plus minimum response "
                            f"({minimum}) tokens after job description compaction; "
                            "use a larger account-supported budget"
                        )
                if (
                    job["description_text"].strip()
                    and not sent_payload["job"]["description_text"].strip()
                ):
                    raise TailoringFailure(
                        "No job description remains after input compaction"
                    )
                # Scrub the complete request on EVERY attempt: contact details may
                # be repeated in allowed prose, the job, or validator feedback.
                reply = self.backend.complete(
                    sent_system,
                    content,
                    min_output_tokens=minimum,
                )
                received = True
                account_reply(reply, usage, self.input_price, self.output_price)
                if capture_diagnostic:
                    self.last_diagnostic = audit_proposal(
                        master, reply.content, sent_payload["job"]["description_text"]
                    )
                if not reply.usage_known:
                    raise TailoringFailure("Provider response is missing token usage")
                if not reply.complete:
                    if reply.finish_reason in {"length", "max_tokens"}:
                        raise OutputLimitFailure(
                            "Provider hit the output token limit before "
                            "finishing JSON; "
                            "use a larger account-supported budget or reduce input. "
                            "Master fallback retained; identical retries skipped"
                        )
                    raise TailoringFailure("Provider returned unfinished output")
                try:
                    proposed = Tailoring.model_validate_json(reply.content)
                except ValidationError:
                    raise TailoringFailure(
                        "Strict tailoring schema failed; immutable edits "
                        "and extra fields forbidden"
                    ) from None
                proposed = validate_tailoring(
                    master, proposed, sent_payload["job"]["description_text"]
                )
                proposed = deterministic_tailoring(
                    master, job, proposed, skill_slot_limits
                )
                proposed = validate_tailoring(
                    master,
                    proposed,
                    sent_payload["job"]["description_text"],
                    generated_summary=proposed.summary,
                )
                check(proposed)
                return proposed
            except ProviderRateLimitError as exc:
                account_rate_limit(exc, usage)
                raise TailoringFailure(str(exc)) from None
            except OutputLimitFailure:
                raise
            except ProviderBudgetError as exc:
                usage.uncounted_attempts += 1
                usage.cost_usd = None
                raise TailoringFailure(str(exc)) from None
            except (TruthError, LayoutError, TailoringFailure) as exc:
                last_error = redactor.text(str(exc))
            except Exception as exc:
                # SDK exceptions may contain credentials, prompts, or response bodies.
                last_error = (
                    "Tailoring provider/render failed (" + type(exc).__name__ + ")"
                )
            finally:
                warning = description.warning()
                if warnings is not None and warning:
                    warnings[:] = [
                        item
                        for item in warnings
                        if not item.startswith("Job description compacted:")
                    ]
                    warnings.append(warning)
            if not received:
                usage.uncounted_attempts += 1
                usage.cost_usd = None
            if attempt < 2:
                self.sleep(0.5 * 2**attempt)
        raise TailoringFailure("Validation failed after 3 attempts: " + last_error)


@dataclass
class TailorResult:
    path: Path
    filename: str
    status: Literal["ready", "failed", "low_fit"]
    match_notes: dict[str, Any] | None
    error: str | None
    warnings: list[str]
    changes: dict[str, int | bool] = field(default_factory=dict)
    report_path: Path | None = None
    change_report: dict[str, Any] = field(default_factory=dict)


def write_report(result: TailorResult) -> None:
    """Keep counts next to this private artifact; never log resume wording."""
    path = result.path.with_suffix(".report.json")
    try:
        path.write_text(
            compact_json(
                {
                    "status": result.status,
                    "error": result.error,
                    "changes": result.changes,
                    "match_notes": result.match_notes,
                    "warnings": result.warnings,
                    "change_report": result.change_report,
                }
            ),
            encoding="utf-8",
        )
        result.report_path = path
    except OSError:
        result.report_path = None
        result.warnings.append("Local change report could not be written")


def filename(master: MasterResume, company: str, title: str) -> str:
    text = unicodedata.normalize(
        "NFKD", "_".join((master.contact.name, company, title))
    )
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_-.")[:96]
    return (text or "Resume") + ".docx"


def tailor_job(
    master: MasterResume,
    job: dict[str, Any],
    original: Path,
    template: Path,
    layout_path: Path,
    output: Path,
    client: TailoringClient,
    usage: Usage,
    pagination: Pagination | None = None,
    attempt_id: str | None = None,
) -> TailorResult:
    job_id = str(UUID(job["id"]))
    name = filename(master, job["company"], job["title"])
    if attempt_id:
        name = Path(name).stem + "_" + str(UUID(attempt_id))[:8] + ".docx"
    destination = output / job_id / name
    warnings: list[str] = []
    input_warnings: list[str] = []
    pagination = pagination or Pagination()
    layout = load_layout(layout_path, master, original, template)
    skill_slot_limits = [
        len(block["text"])
        for block in layout["blocks"]
        if block["binding"]["kind"] == "skills"
    ]
    matched = matching_skills(master, job)

    if not matched:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.with_suffix(".pdf").unlink(missing_ok=True)
        shutil.copyfile(original, destination)
        unchanged_result = deterministic_tailoring(master, job, unchanged(master))
        unchanged_result = validate_tailoring(
            master,
            unchanged_result,
            compact_description(job["description_text"], job["company"]).text,
        )
        result = TailorResult(
            destination,
            destination.name,
            "low_fit",
            None,
            None,
            ["No job skills meaningfully overlap with the master resume"],
            summarize(master, unchanged_result, original, destination),
            change_report=human_report(master, unchanged_result),
        )
        write_report(result)
        return result

    def check(candidate: Tailoring) -> None:
        destination.unlink(missing_ok=True)
        destination.with_suffix(".pdf").unlink(missing_ok=True)
        render(master, candidate, original, template, layout_path, destination)
        warnings[:] = pagination.check(
            original, destination, layout.get("target_pages")
        )

    try:
        proposed = client.propose(
            master, job, usage, check, input_warnings, skill_slot_limits
        )
    except TailoringFailure as exc:
        # Keep the deterministic, master-only match useful even if optional LLM
        # prose or match-note generation fails. No proposed wording is accepted.
        try:
            proposed = deterministic_tailoring(
                master, job, unchanged(master), skill_slot_limits
            )
            proposed = validate_tailoring(
                master,
                proposed,
                compact_description(job["description_text"], job["company"]).text,
                generated_summary=proposed.summary,
            )
            check(proposed)
        except (LayoutError, TailoringFailure, TruthError, ValueError) as render_exc:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.with_suffix(".pdf").unlink(missing_ok=True)
            shutil.copyfile(original, destination)
            result = TailorResult(
                destination,
                destination.name,
                "failed",
                None,
                str(render_exc),
                warnings,
            )
        else:
            warnings.append(
                "LLM proposal rejected or unavailable; applied deterministic "
                "master-only ordering instead (" + str(exc) + ")"
            )
            changes = summarize(master, proposed, original, destination)
            result = TailorResult(
                destination,
                destination.name,
                "ready",
                proposed.match_notes.model_dump(),
                None,
                warnings,
                changes,
                change_report=human_report(master, proposed),
            )
    else:
        changes = summarize(master, proposed, original, destination)
        if not changes["document_changed"]:
            warnings.append("No document changes selected; master content retained")
        elif not changes["tailoring_changed"]:
            warnings.append("Only document formatting changed; master content retained")
        result = TailorResult(
            destination,
            destination.name,
            "ready",
            proposed.match_notes.model_dump(),
            None,
            warnings,
            changes,
            change_report=human_report(master, proposed),
        )
    result.warnings.extend(input_warnings)
    write_report(result)
    return result
