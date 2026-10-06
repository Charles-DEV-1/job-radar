"""Verify the installed src-layout package can be imported."""

from importlib import import_module


def test_package_is_importable() -> None:
    import_module("pipeline.detect_ats")
