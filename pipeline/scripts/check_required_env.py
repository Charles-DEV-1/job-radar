"""Validate required scheduled-workflow configuration without echoing values."""

import os
import platform

REQUIRED_ENV = (
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


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    print(f"Python version: {platform.python_version()}")
    if missing:
        print("Missing required secrets/environment variables: " + ", ".join(missing))
        return 1
    print("Required secrets/environment variables: present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
