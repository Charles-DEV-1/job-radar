"""Optional PDF export; changed documents require verified pagination."""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfReader

from pipeline.resume.layout import LayoutError, compare


def libreoffice() -> str | None:
    configured = os.environ.get("LIBREOFFICE_EXECUTABLE")
    if configured:
        if not Path(configured).is_file():
            raise LayoutError("LIBREOFFICE_EXECUTABLE does not exist")
        return configured
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        directory = os.environ.get(variable)
        candidate = (
            Path(directory) / "LibreOffice/program/soffice.exe" if directory else None
        )
        if candidate and candidate.is_file():
            return str(candidate)
    return None


class Pagination:
    def __init__(self, executable: str | None = None, discover: bool = True) -> None:
        self.executable = executable or (libreoffice() if discover else None)

    def convert(self, source: Path, destination: Path) -> int:
        if not self.executable:
            raise LayoutError("PDF renderer unavailable; pagination cannot be verified")
        with TemporaryDirectory(prefix="job-radar-pdf-") as directory:
            work = Path(directory)
            profile = (work / "profile").as_uri()
            process = subprocess.run(
                [
                    self.executable,
                    f"-env:UserInstallation={profile}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(work),
                    str(source.resolve()),
                ],
                capture_output=True,
                timeout=60,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            pdf = work / (source.stem + ".pdf")
            if process.returncode or not pdf.is_file():
                raise LayoutError("LibreOffice PDF export failed")
            pages = len(PdfReader(pdf).pages)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf, destination)
            return pages

    def check(
        self, original: Path, rendered: Path, target_pages: int | None = None
    ) -> list[str]:
        if not self.executable:
            if compare(original, rendered):
                raise LayoutError(
                    "Changed resume requires LibreOffice to verify target page count"
                )
            return ["PDF skipped: renderer unavailable; original DOCX preserved"]
        with TemporaryDirectory(prefix="job-radar-baseline-") as directory:
            baseline = self.convert(original, Path(directory) / "baseline.pdf")
        actual = self.convert(rendered, rendered.with_suffix(".pdf"))
        if actual != baseline or target_pages is not None and actual != target_pages:
            rendered.with_suffix(".pdf").unlink(missing_ok=True)
            raise LayoutError("Rendered resume page count differs from target/original")
        return []
