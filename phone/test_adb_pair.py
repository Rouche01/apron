"""Unit tests for wireless adb pair helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import adbutil


def test_adb_pair_success():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "Successfully paired to 192.168.1.20:37123\n"
    proc.stderr = ""
    with patch("adbutil.subprocess.run", return_value=proc) as run:
        out = adbutil.adb_pair("192.168.1.20:37123", "123456")
    assert "Successfully paired" in out
    run.assert_called_once()
    assert run.call_args.args[0] == ["adb", "pair", "192.168.1.20:37123", "123456"]


def test_adb_pair_requires_code():
    with pytest.raises(adbutil.AdbError, match="pairing code"):
        adbutil.adb_pair("192.168.1.20:37123", "  ")


def test_adb_pair_failure():
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    proc.stderr = "error: protocol fault"
    with patch("adbutil.subprocess.run", return_value=proc):
        with pytest.raises(adbutil.AdbError, match="adb pair failed"):
            adbutil.adb_pair("192.168.1.20:37123", "123456")
