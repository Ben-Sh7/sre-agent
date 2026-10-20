"""Smoke test: the package is installed and imports. Real tests come with real code."""

import importlib


def test_package_imports() -> None:
    module = importlib.import_module("sre_agent")

    assert module.__name__ == "sre_agent"
