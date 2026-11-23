"""Live device_ready enrichment for GET /v1/seats."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import physical_phone_seats as pps


def test_probe_worker_device_ready():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "ok": True,
        "bound_serial": "192.168.1.2:5555",
        "selected": {"serial": "192.168.1.2:5555", "state": "device"},
    }
    with patch("physical_phone_seats.httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = resp
        out = pps.probe_worker_device_status("http://phone-1:8003")
    assert out["device_ready"] is True
    assert out["device_state"] == "device"
    assert out["device_serial"] == "192.168.1.2:5555"
    assert out["device_probe_ok"] is True


def test_probe_worker_device_empty():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "ok": True,
        "bound_serial": None,
        "selected": None,
    }
    with patch("physical_phone_seats.httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = resp
        out = pps.probe_worker_device_status("http://phone-1:8003")
    assert out["device_ready"] is False
    assert out["device_state"] is None


def test_enrich_seats_summary():
    seats = [
        {"id": "desk1", "kind": "desktop", "worker_base_url": "http://w:8000"},
        {
            "id": "phone1",
            "kind": "phone",
            "worker_base_url": "http://phone-1:8003",
            "bound_serial": None,
        },
        {
            "id": "phone-cloud1",
            "kind": "phone",
            "provider": "phone-harness",
            "worker_base_url": "http://phone-cloud-1:8003",
        },
    ]

    def fake_probe(url: str, *, timeout: float = 2.0) -> dict:
        if "phone-1" in url:
            return {
                "device_ready": True,
                "device_state": "device",
                "device_serial": "serial-1",
                "device_probe_ok": True,
            }
        return {
            "device_ready": False,
            "device_state": None,
            "device_serial": None,
            "device_probe_ok": True,
        }

    with patch.object(pps, "probe_worker_device_status", side_effect=fake_probe):
        summary = pps.enrich_seats_device_status(seats)

    assert summary == {"physical_total": 1, "physical_ready": 1}
    assert seats[1]["device_ready"] is True
    assert seats[2]["device_ready"] is False
    assert "device_ready" not in seats[0]
