"""Upload reviewed local resume inputs to the existing private Storage bucket."""

import argparse
from pathlib import Path

from dotenv import load_dotenv

from pipeline.detect_ats import REPO_ROOT
from pipeline.http import HTTPClient
from pipeline.resume.inputs import INPUT_FILES, INPUT_PREFIX
from pipeline.resume.store import SupabaseResumeStore
from pipeline.storage import SupabaseStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload private resume inputs for scheduled GitHub Actions runs"
    )
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    args = parser.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env", override=False)
    files = {filename: args.data_dir / filename for filename in INPUT_FILES.values()}
    missing = [name for name, path in files.items() if not path.is_file()]
    if missing:
        print("Resume input upload failed: required local input file is missing")
        return 2

    store: SupabaseStore | None = None
    try:
        with HTTPClient() as http:
            store = SupabaseStore.from_environment(http)
            private = SupabaseResumeStore(store)
            private.assert_private()
            for filename, document in files.items():
                private.upload_input(f"{INPUT_PREFIX}/{filename}", document)
                print(f"Uploaded private resume input: {filename}")
        return 0
    except Exception as exc:
        # Storage/client exceptions can contain credentials or request payloads.
        print(f"Resume input upload failed ({type(exc).__name__})")
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
