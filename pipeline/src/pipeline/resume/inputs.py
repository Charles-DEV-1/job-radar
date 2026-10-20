"""Private master-resume inputs used by scheduled tailoring runs."""

from pathlib import Path

from pipeline.resume.store import BUCKET, SupabaseResumeStore

INPUT_FILES = {
    "master_json": "master_resume.json",
    "master_docx": "master_resume.docx",
    "template": "resume_template.docx",
    "layout": "resume_layout.json",
}
INPUT_PREFIX = "pipeline-inputs"


def download_resume_inputs(
    store: SupabaseResumeStore, destination: Path
) -> dict[str, Path]:
    """Stage all required files locally; caller owns and removes destination."""
    store.assert_private()
    paths: dict[str, Path] = {}
    for key, filename in INPUT_FILES.items():
        local_path = destination / filename
        store.download_input(f"{INPUT_PREFIX}/{filename}", local_path)
        paths[key] = local_path
    return paths


def resume_inputs_available(store: SupabaseResumeStore) -> bool:
    """Check object names only; never download or expose resume file contents."""
    store.assert_private()
    objects = store.store.client.storage.from_(BUCKET).list(
        INPUT_PREFIX, {"limit": 100}
    )
    if not isinstance(objects, list):
        return False
    available = {item.get("name") for item in objects if isinstance(item, dict)}
    return set(INPUT_FILES.values()).issubset(available)
