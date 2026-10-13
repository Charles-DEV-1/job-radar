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
from pipeline.resume.changes import summarize
from pipeline.resume.layout import LayoutError, load_layout, render
from pipeline.resume.pdf import Pagination
from pipeline.resume.privacy import ContactRedactor
from pipeline.resume.prompt import compact_json, request
from pipeline.resume.schema import MasterResume, Tailoring
from pipeline.resume.validate import TruthError, validate_tailoring


class TailoringFailure(ValueError):
    pass


class OutputLimitFailure(TailoringFailure):
    """A known truncated response must not spend quota on identical retries."""


class TailoringClient(ExtractionClient):
    def propose(
        self,
        master: MasterResume,
        job: dict[str, Any],
        usage: Usage,
        check: Callable[[Tailoring], None],
    ) -> Tailoring:
        if not master.reviewed:
            raise TailoringFailure("Master facts require owner review")
        redactor = ContactRedactor(master.contact)
        system, payload, minimum = request(master, job)
        if len(compact_json(payload)) > 100_000:
            raise TailoringFailure(
                "Master/job input exceeds bounded tailoring input size"
            )
        last_error = "No valid output"
        for attempt in range(3):
            usage.attempts += 1
            received = False
            try:
                # Scrub the complete request on EVERY attempt: contact details may
                # be repeated in allowed prose, the job, or validator feedback.
                reply = self.backend.complete(
                    redactor.text(system),
                    compact_json(redactor.payload(payload)),
                    min_output_tokens=minimum,
                )
                received = True
                account_reply(reply, usage, self.input_price, self.output_price)
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
                proposed = validate_tailoring(master, proposed, job["description_text"])
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
            if not received:
                usage.uncounted_attempts += 1
                usage.cost_usd = None
            payload["validator_errors"] = [last_error]
            if attempt < 2:
                self.sleep(0.5 * 2**attempt)
        raise TailoringFailure("Validation failed after 3 attempts: " + last_error)


@dataclass
class TailorResult:
    path: Path
    filename: str
    status: Literal["ready", "failed"]
    match_notes: dict[str, Any] | None
    error: str | None
    warnings: list[str]
    changes: dict[str, int | bool] = field(default_factory=dict)
    report_path: Path | None = None


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
    pagination = pagination or Pagination()
    layout = load_layout(layout_path, master, original, template)

    def check(candidate: Tailoring) -> None:
        destination.unlink(missing_ok=True)
        destination.with_suffix(".pdf").unlink(missing_ok=True)
        render(master, candidate, original, template, layout_path, destination)
        warnings[:] = pagination.check(
            original, destination, layout.get("target_pages")
        )

    try:
        proposed = client.propose(master, job, usage, check)
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
        )
    except TailoringFailure as exc:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.with_suffix(".pdf").unlink(missing_ok=True)
        # Byte-identical master fallback is always available locally and is uploaded
        # as the downloadable file while the job remains visibly failed.
        shutil.copyfile(original, destination)
        result = TailorResult(
            destination, destination.name, "failed", None, str(exc), warnings
        )
    write_report(result)
    return result
