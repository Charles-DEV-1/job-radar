from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pipeline.resume.inputs import INPUT_FILES

ROOT = Path(__file__).resolve().parents[2]


def test_environment_preflight_lists_missing_names_but_never_values() -> None:
    env = os.environ.copy()
    required = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_MODEL",
        "RESEND_API_KEY",
        "RESEND_FROM_EMAIL",
        "NOTIFY_EMAIL",
        "DASHBOARD_URL",
    )
    for name in required:
        env.pop(name, None)
    env["LLM_API_KEY"] = "sensitive-test-value"
    env["SUPABASE_URL"] = " "

    result = subprocess.run(
        [sys.executable, "pipeline/scripts/check_required_env.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Python version:" in result.stdout
    assert "SUPABASE_SERVICE_KEY" in result.stdout
    assert "LLM_API_KEY" not in result.stdout
    assert "sensitive-test-value" not in result.stdout + result.stderr


def test_environment_preflight_passes_when_all_required_values_exist() -> None:
    env = os.environ.copy()
    for name in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_MODEL",
        "RESEND_API_KEY",
        "RESEND_FROM_EMAIL",
        "NOTIFY_EMAIL",
        "DASHBOARD_URL",
    ):
        env[name] = "configured-test-value"

    result = subprocess.run(
        [sys.executable, "pipeline/scripts/check_required_env.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Python version:" in result.stdout
    assert "Required secrets/environment variables: present" in result.stdout
    assert "configured-test-value" not in result.stdout + result.stderr


def test_private_resume_preflight_reports_availability_without_contents() -> None:
    from pipeline.resume.inputs import resume_inputs_available

    class Bucket:
        items = [{"name": name} for name in INPUT_FILES.values()]

        def list(self, path: str, options: dict[str, int]) -> list[dict[str, str]]:
            assert path == "pipeline-inputs"
            assert options == {"limit": 100}
            return self.items

    class Storage:
        bucket = Bucket()

        def from_(self, _bucket: str) -> Bucket:
            return self.bucket

    class Client:
        storage = Storage()

    class Store:
        client = Client()

    class PrivateStore:
        store = Store()

        def assert_private(self) -> None:
            pass

    private = PrivateStore()
    assert resume_inputs_available(private)  # type: ignore[arg-type]
    Client.storage.bucket.items = [{"name": "master_resume.json"}]
    assert not resume_inputs_available(private)  # type: ignore[arg-type]
