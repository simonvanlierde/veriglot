"""Smoke test: replace with real tests."""

import veriglot


def test_imports() -> None:
    """The package imports."""
    assert veriglot.__name__ == "veriglot"
