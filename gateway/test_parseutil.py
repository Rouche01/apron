"""Shared parseutil helpers."""

from __future__ import annotations

import pytest

from parseutil import env_int, env_nonneg_int, opt_bool, opt_str


def test_opt_str_and_bool():
    row = {
        "a": "  x  ",
        "b": "",
        "c": None,
        "d": True,
        "e": "yes",
        "f": "off",
        "g": 1,
    }
    assert opt_str(row, "a") == "x"
    assert opt_str(row, "b") is None
    assert opt_str(row, "c") is None
    assert opt_str(row, "missing") is None
    assert opt_bool(row, "d") is True
    assert opt_bool(row, "e") is True
    assert opt_bool(row, "f") is False
    assert opt_bool(row, "g") is True
    assert opt_bool(row, "missing", default=True) is True


def test_env_int(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PU_TEST", raising=False)
    assert env_int("PU_TEST", 7) == 7
    monkeypatch.setenv("PU_TEST", "42")
    assert env_int("PU_TEST", 7) == 42
    monkeypatch.setenv("PU_TEST", "nope")
    assert env_int("PU_TEST", 7) == 7
    monkeypatch.setenv("PU_NEG", "-3")
    assert env_nonneg_int("PU_NEG", 0) == 0
    monkeypatch.setenv("PU_POS", "5")
    assert env_nonneg_int("PU_POS", 0) == 5
