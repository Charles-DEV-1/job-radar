"""Optional bounded, local PDF reconstruction. Visual review remains required."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from pipeline.resume.layout import LayoutError

WORKER = """
import logging
import sys
logging.disable(logging.CRITICAL)
from pdf2docx import Converter
converter = Converter(sys.argv[1])
try:
    converter.convert(sys.argv[2], multi_processing=False)
finally:
    converter.close()
"""


def convert(source: Path, destination: Path, *, timeout: int = 60) -> None:
    if not source.is_file() or destination.exists():
        raise LayoutError("PDF source missing or DOCX destination already exists")
    with TemporaryDirectory(prefix="job-radar-pdf-import-") as directory:
        staged = Path(directory) / "converted.docx"
        try:
            process = subprocess.run(
                [sys.executable, "-c", WORKER, str(source.resolve()), str(staged)],
                capture_output=True,
                timeout=timeout,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            raise LayoutError("Local PDF conversion timed out") from None
        if process.returncode or not staged.is_file():
            raise LayoutError(
                "Local PDF conversion failed; install pipeline[pdf-import] "
                "or convert manually in Word"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged, destination)
