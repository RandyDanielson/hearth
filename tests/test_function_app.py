"""Tests for the function_app wiring, focused on the dry-run gate.

Uses a fake Ecobee client so no HTTP or Azure host is involved.
"""

from __future__ import annotations

from optimizer.config import AppConfig
from function_app import run_optimizer


class FakeClient:
    def __init__(self, thermostats: list[dict]) -> None:
        self._thermostats = thermostats
        self.mode_calls: list[tuple[str, str]] = []
        self.hold_calls: list[dict] = []
        self.resume_calls: list[dict] = []

    def get_thermostats(self, selection: dict) -> dict:
        return {"status": {"code": 0}, "thermostatList": self._thermostats}

    def set_hvac_mode(self, *, selection_match: str, mode: str) -> dict:
        self.mode_calls.append((selection_match, mode))
        return {"status": {"code": 0}}

    def set_hold(self, **kwargs) -> dict:
        self.hold_calls.append(kwargs)
        return {"status": {"code": 0}}

    def resume_program(self, **kwargs) -> dict:
        self.resume_calls.append(kwargs)
        return {"status": {"code": 0}}


def _cfg(*, dry_run: bool) -> AppConfig:
    return AppConfig(
        schedule="0 0 * * * *",
        dry_run=dry_run,
        key_vault_uri=None,
        secret_name_client_id="ecobee-client-id",
        secret_name_refresh_token="ecobee-refresh-token",
        selection_match="",
        switch_band_f=1.0,
        enable_outdoor_guard=True,
        cool_min_outdoor_f=50.0,
        heat_max_outdoor_f=80.0,
        hold_type="nextTransition",
    )


def _warm_thermostat() -> dict:
    # 72.3F, home band 70/71, currently in heat mode -> strategy wants cool.
    return {
        "name": "Home",
        "runtime": {"actualTemperature": 723},
        "settings": {"hvacMode": "heat"},
        "program": {
            "currentClimateRef": "home",
            "climates": [{"climateRef": "home", "heatTemp": 700, "coolTemp": 710}],
        },
        "weather": {"forecasts": [{"temperature": 600}]},
    }


def _in_band_thermostat() -> dict:
    therm = _warm_thermostat()
    therm["runtime"]["actualTemperature"] = 705  # 70.5F, comfortably in band
    return therm


def test_dry_run_does_not_write():
    client = FakeClient([_warm_thermostat()])
    run_optimizer(_cfg(dry_run=True), client=client)
    assert client.mode_calls == []  # nothing written in dry-run


def test_live_run_switches_mode():
    client = FakeClient([_warm_thermostat()])
    run_optimizer(_cfg(dry_run=False), client=client)
    assert client.mode_calls == [("", "cool")]


def test_noop_writes_nothing_even_when_live():
    client = FakeClient([_in_band_thermostat()])
    run_optimizer(_cfg(dry_run=False), client=client)
    assert client.mode_calls == []
    assert client.hold_calls == []
    assert client.resume_calls == []


def test_no_thermostats_is_safe():
    client = FakeClient([])
    run_optimizer(_cfg(dry_run=False), client=client)
    assert client.mode_calls == []
