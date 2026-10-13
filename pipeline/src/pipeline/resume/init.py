"""Offline conversion and a diff-style DOCX round-trip check."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from pipeline.detect_ats import REPO_ROOT
from pipeline.resume.layout import (
    LayoutError,
    compare,
    create_template,
    draft_master,
    render,
)
from pipeline.resume.schema import load_master
from pipeline.resume.validate import unchanged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a private resume without an LLM"
    )
    data = REPO_ROOT / "data"
    parser.add_argument("--master-docx", type=Path, default=data / "master_resume.docx")
    parser.add_argument("--master-pdf", type=Path)
    parser.add_argument(
        "--convert-pdf",
        action="store_true",
        help="Convert PDF locally; visually review its reconstructed layout",
    )
    parser.add_argument("--master-json", type=Path, default=data / "master_resume.json")
    parser.add_argument("--template", type=Path, default=data / "resume_template.docx")
    parser.add_argument("--layout", type=Path, default=data / "resume_layout.json")
    parser.add_argument(
        "--output", type=Path, default=data / "output/render-check.docx"
    )
    parser.add_argument("--render-check", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild template/layout; preserve an existing master JSON",
    )
    parser.add_argument("--target-pages", type=int)
    args = parser.parse_args(argv)
    try:
        if args.convert_pdf:
            from pipeline.resume.convert_pdf import convert
            from pipeline.resume.import_pdf import import_pdf

            pdf = args.master_pdf or data / "master_resume.pdf"
            if args.render_check:
                raise LayoutError("Run PDF conversion separately from --render-check")
            if args.master_json.exists() and load_master(args.master_json).reviewed:
                raise LayoutError(
                    "PDF layout requires review; preserve approved inputs"
                )
            draft, report = import_pdf(pdf)
            if not args.master_json.exists():
                args.master_json.parent.mkdir(parents=True, exist_ok=True)
                args.master_json.write_text(
                    draft.model_dump_json(indent=2), encoding="utf-8"
                )
            convert(pdf, args.master_docx)
            (args.master_json.parent / "resume_review.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            print("+ Private editable DOCX converted locally from PDF")
            print("! Compare with the source PDF before approving facts and layout")
        if not args.master_docx.is_file():
            pdf = args.master_pdf or data / "master_resume.pdf"
            if pdf.is_file() and not args.render_check:
                from pipeline.resume.import_pdf import import_pdf

                if args.master_json.exists():
                    raise LayoutError(
                        "Master JSON already exists; preserve your review edits"
                    )
                master, report = import_pdf(pdf)
                args.master_json.parent.mkdir(parents=True, exist_ok=True)
                args.master_json.write_text(
                    master.model_dump_json(indent=2), encoding="utf-8"
                )
                (args.master_json.parent / "resume_review.json").write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
                print("+ Extracted a private, unreviewed master JSON from your PDF")
                for issue in report["issues"]:
                    print("! " + issue)
                print("= Rerun --convert-pdf to prepare an editable layout for review")
                return 0
            raise LayoutError(
                "Place the original at data/master_resume.docx or use --master-docx"
            )
        if args.target_pages is not None and args.target_pages < 1:
            raise LayoutError("Target page count must be positive")
        master = (
            load_master(args.master_json)
            if args.master_json.is_file()
            else draft_master(args.master_docx)
        )
        if not args.render_check:
            if (args.template.exists() or args.layout.exists()) and not args.force:
                raise LayoutError(
                    "Template/layout already exists; use --render-check or --force"
                )
            if not args.master_json.exists():
                args.master_json.parent.mkdir(parents=True, exist_ok=True)
                args.master_json.write_text(
                    master.model_dump_json(indent=2), encoding="utf-8"
                )
            layout = create_template(
                args.master_docx, args.template, args.layout, master
            )
            layout["target_pages"] = args.target_pages
            args.layout.write_text(json.dumps(layout, indent=2), encoding="utf-8")
            print(
                f"+ Text placeholders: {sum(len(b['runs']) for b in layout['blocks'])}"
            )
            print(
                "+ Original fonts, styles, tables, section order, "
                "headers and footers retained"
            )
        render(
            master,
            unchanged(master),
            args.master_docx,
            args.template,
            args.layout,
            args.output,
        )
        differences = compare(args.master_docx, args.output)
        if differences:
            for difference in differences:
                print(difference)
            raise LayoutError(
                "Round-trip structure/style/text check failed; review mapping"
            )
        print("= Round-trip paragraph/style/text sequence and DOCX package match")
        print(f"+ Render-check DOCX: {args.output}")
        if not master.reviewed:
            print(
                "! Verify draft contact, roles, dates, skills and education; "
                "then set reviewed=true"
            )
        print("! Compare the render-check DOCX visually in your usual Word renderer")
        print("Render again: python -m pipeline.resume.init --render-check")
        return 0
    except ValidationError:
        print("Resume setup failed: master JSON does not match the strict schema")
    except (OSError, ValueError, KeyError):
        # Library/XML exceptions can carry private paragraph values. Our own
        # LayoutError messages contain only operational instructions/check names.
        import sys

        exc = sys.exception()
        print(
            "Resume setup failed: "
            + (
                str(exc)
                if isinstance(exc, LayoutError)
                else "invalid or unavailable resume inputs"
            )
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
