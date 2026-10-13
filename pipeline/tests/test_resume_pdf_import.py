"""PDF reconstruction is local, bounded, private, and never silently approved."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from resume_helpers import synthetic_resume

from pipeline.resume import convert_pdf, import_pdf
from pipeline.resume.init import main
from pipeline.resume.layout import LayoutError, compare


def test_converter_uses_private_staging_no_shell_and_discards_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, fixture, _, _ = synthetic_resume(tmp_path)
    pdf, destination = tmp_path / "source.pdf", tmp_path / "converted.docx"
    pdf.write_bytes(b"synthetic source")

    def worker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert kwargs["timeout"] == 60 and kwargs["capture_output"]
        assert "shell" not in kwargs and args[-2] == str(pdf.resolve())
        shutil.copyfile(fixture, args[-1])
        return subprocess.CompletedProcess(args, 0, b"private output", b"")

    monkeypatch.setattr(convert_pdf.subprocess, "run", worker)
    convert_pdf.convert(pdf, destination)
    assert destination.read_bytes() == fixture.read_bytes()
    with pytest.raises(LayoutError, match="already exists"):
        convert_pdf.convert(pdf, destination)


def test_converter_timeout_retains_source_and_no_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = tmp_path / "source.pdf", tmp_path / "target.docx"
    source.write_bytes(b"private source")

    def timeout(*args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired("converter", 60, output="private output")

    monkeypatch.setattr(convert_pdf.subprocess, "run", timeout)
    with pytest.raises(LayoutError, match="timed out") as error:
        convert_pdf.convert(source, target)
    assert "private" not in str(error.value)
    assert source.read_bytes() == b"private source" and not target.exists()


def test_pdf_setup_keeps_facts_unapproved_and_preserves_existing_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, fixture, _, _ = synthetic_resume(tmp_path)
    master.reviewed = False
    facts = tmp_path / "facts.json"
    facts.write_text(master.model_dump_json(indent=2), encoding="utf-8")
    original_facts = facts.read_bytes()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic PDF")
    monkeypatch.setattr(
        import_pdf,
        "import_pdf",
        lambda _: (master, {"pages": 1, "issues": ["Review reconstructed layout"]}),
    )
    monkeypatch.setattr(
        convert_pdf, "convert", lambda _, target: shutil.copyfile(fixture, target)
    )
    baseline, template, output = [
        tmp_path / f"{p}.docx" for p in ("baseline", "pdf-template", "check")
    ]
    assert (
        main(
            [
                "--convert-pdf",
                "--master-pdf",
                str(source),
                "--master-docx",
                str(baseline),
                "--master-json",
                str(facts),
                "--template",
                str(template),
                "--layout",
                str(tmp_path / "imported-layout.json"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert facts.read_bytes() == original_facts
    assert json.loads(facts.read_text(encoding="utf-8"))["reviewed"] is False
    assert compare(baseline, output) == []
    assert source.read_bytes() == b"synthetic PDF"
