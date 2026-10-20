"""Check that all private resume inputs exist without reading their contents."""

from dotenv import load_dotenv

from pipeline.detect_ats import REPO_ROOT
from pipeline.http import HTTPClient
from pipeline.resume.inputs import resume_inputs_available
from pipeline.resume.store import SupabaseResumeStore
from pipeline.storage import SupabaseStore


def main() -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    store: SupabaseStore | None = None
    try:
        with HTTPClient() as http:
            store = SupabaseStore.from_environment(http)
            available = resume_inputs_available(SupabaseResumeStore(store))
        print(f"Master resume files available: {'yes' if available else 'no'}")
        return 0 if available else 1
    except Exception as exc:
        # Client exceptions may contain request data; report only the type.
        print(
            "Master resume files available: no "
            f"(Storage check failed: {type(exc).__name__})"
        )
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
