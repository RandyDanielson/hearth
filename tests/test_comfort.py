"""Tests for the auto-changeover decision engine and the Ecobee JSON parser."""

from __future__ import annotations

import pytest

from optimizer.comfort import ChangeoverStrategy, ComfortAction, HvacMode, ThermostatState
from optimizer.parsing import parse_thermostat


def _state(
    temp: float,
    *,
    heat: float = 70.0,
    cool: float = 71.0,
    mode: HvacMode = HvacMode.HEAT,
    outdoor: float | None = 60.0,
    ref: str | None = "home",
) -> ThermostatState:
    return ThermostatState(
        name="Home",
        current_temp_f=temp,
        heat_setpoint_f=heat,
        cool_setpoint_f=cool,
        hvac_mode=mode,
        outdoor_temp_f=outdoor,
        current_climate_ref=ref,
    )


# --------------------------------------------------------------------------- #
# ChangeoverStrategy
# --------------------------------------------------------------------------- #
def test_too_warm_switches_to_cool():
    action = ChangeoverStrategy().decide(_state(72.5, mode=HvacMode.HEAT))
    assert action.kind == "switch_mode"
    assert action.hvac_mode == HvacMode.COOL
    assert action.reason


def test_too_cold_switches_to_heat():
    action = ChangeoverStrategy().decide(_state(68.5, mode=HvacMode.COOL))
    assert action.kind == "switch_mode"
    assert action.hvac_mode == HvacMode.HEAT
    assert action.reason


def test_in_band_is_noop():
    action = ChangeoverStrategy().decide(_state(70.5))
    assert action.is_noop
    assert action.reason


def test_too_warm_but_already_cooling_is_noop():
    action = ChangeoverStrategy().decide(_state(72.5, mode=HvacMode.COOL))
    assert action.is_noop


def test_too_cold_but_already_heating_is_noop():
    action = ChangeoverStrategy().decide(_state(68.5, mode=HvacMode.HEAT))
    assert action.is_noop


def test_outdoor_guard_blocks_cooling_when_cold_outside():
    action = ChangeoverStrategy().decide(_state(72.5, mode=HvacMode.HEAT, outdoor=40.0))
    assert action.is_noop
    assert "guard" in action.reason


def test_outdoor_guard_blocks_heating_when_hot_outside():
    action = ChangeoverStrategy().decide(_state(68.5, mode=HvacMode.COOL, outdoor=85.0))
    assert action.is_noop
    assert "guard" in action.reason


def test_guard_disabled_switches_regardless_of_outdoor():
    strategy = ChangeoverStrategy(enable_outdoor_guard=False)
    action = strategy.decide(_state(72.5, mode=HvacMode.HEAT, outdoor=40.0))
    assert action.kind == "switch_mode"
    assert action.hvac_mode == HvacMode.COOL


def test_unknown_outdoor_does_not_block_switch():
    action = ChangeoverStrategy().decide(_state(72.5, mode=HvacMode.HEAT, outdoor=None))
    assert action.kind == "switch_mode"
    assert action.hvac_mode == HvacMode.COOL


def test_just_inside_band_does_not_switch():
    # Exactly at the threshold edges should NOT trigger (strict crossing).
    cool_edge = ChangeoverStrategy().decide(_state(71.9, cool=71.0))  # 71.9 < 71+1
    heat_edge = ChangeoverStrategy().decide(_state(69.1, heat=70.0))  # 69.1 > 70-1
    assert cool_edge.is_noop
    assert heat_edge.is_noop


def test_wider_climate_band_is_respected():
    # Home set to a comfort band (heat 68 / cool 76): 72F sits comfortably inside.
    action = ChangeoverStrategy().decide(_state(72.0, heat=68.0, cool=76.0))
    assert action.is_noop


def test_custom_switch_band():
    # With a 2F band, 72.5 is not yet warm enough (needs >= 73) to switch.
    action = ChangeoverStrategy(switch_band_f=2.0).decide(_state(72.5, cool=71.0))
    assert action.is_noop


# --------------------------------------------------------------------------- #
# parse_thermostat
# --------------------------------------------------------------------------- #
def _sample_thermostat() -> dict:
    return {
        "name": "Living Room",
        "runtime": {"actualTemperature": 723, "desiredHeat": 700, "desiredCool": 710},
        "settings": {"hvacMode": "heat"},
        "program": {
            "currentClimateRef": "home",
            "climates": [
                {"climateRef": "home", "heatTemp": 700, "coolTemp": 710},
                {"climateRef": "away", "heatTemp": 620, "coolTemp": 820},
            ],
        },
        "weather": {"forecasts": [{"temperature": 555}]},
    }


def test_parse_thermostat_happy_path():
    state = parse_thermostat(_sample_thermostat())
    assert state.name == "Living Room"
    assert state.current_temp_f == 72.3
    assert state.heat_setpoint_f == 70.0
    assert state.cool_setpoint_f == 71.0
    assert state.hvac_mode == HvacMode.HEAT
    assert state.outdoor_temp_f == 55.5
    assert state.current_climate_ref == "home"


def test_parse_thermostat_outdoor_sentinel_becomes_none():
    therm = _sample_thermostat()
    therm["weather"]["forecasts"][0]["temperature"] = -5002
    assert parse_thermostat(therm).outdoor_temp_f is None


def test_parse_thermostat_missing_weather_is_none():
    therm = _sample_thermostat()
    del therm["weather"]
    assert parse_thermostat(therm).outdoor_temp_f is None


def test_parse_thermostat_falls_back_to_desired_setpoints():
    therm = _sample_thermostat()
    therm["program"]["currentClimateRef"] = "custom"  # not present in climates
    state = parse_thermostat(therm)
    assert state.heat_setpoint_f == 70.0  # from runtime.desiredHeat
    assert state.cool_setpoint_f == 71.0  # from runtime.desiredCool


def test_parse_thermostat_unknown_mode_becomes_off():
    therm = _sample_thermostat()
    therm["settings"]["hvacMode"] = "something"
    assert parse_thermostat(therm).hvac_mode == HvacMode.OFF


def test_parse_thermostat_aux_heat_mode():
    therm = _sample_thermostat()
    therm["settings"]["hvacMode"] = "auxHeatOnly"
    assert parse_thermostat(therm).hvac_mode == HvacMode.AUX


def test_parse_thermostat_missing_temperature_raises():
    therm = _sample_thermostat()
    del therm["runtime"]["actualTemperature"]
    with pytest.raises(ValueError):
        parse_thermostat(therm)


def test_parse_then_decide_end_to_end():
    # 72.3F with home band 70/71 and heat mode -> switch to cool.
    state = parse_thermostat(_sample_thermostat())
    action: ComfortAction = ChangeoverStrategy().decide(state)
    assert action.kind == "switch_mode"
    assert action.hvac_mode == HvacMode.COOL
