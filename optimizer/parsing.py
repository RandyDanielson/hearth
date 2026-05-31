"""Map a raw Ecobee thermostat JSON object into a :class:`ThermostatState`.

Kept separate from :mod:`optimizer.comfort` so the decision engine stays free of
Ecobee's wire format. Temperatures arrive as integer tenths of a degree
Fahrenheit and are normalized to floats here.
"""

from __future__ import annotations

import logging
from typing import Any

from optimizer.comfort import HvacMode, ThermostatState

logger = logging.getLogger(__name__)

# Plausible outdoor range; Ecobee uses sentinels (e.g. -5002) when unavailable.
_MIN_PLAUSIBLE_F = -100.0
_MAX_PLAUSIBLE_F = 200.0


def parse_thermostat(therm: dict[str, Any], *, name: str | None = None) -> ThermostatState:
    """Build a :class:`ThermostatState` from one Ecobee ``thermostatList`` entry."""
    resolved_name = name or therm.get("name") or therm.get("identifier") or "thermostat"

    runtime = therm.get("runtime") or {}
    current_temp_f = _tenths_to_f(runtime.get("actualTemperature"))
    if current_temp_f is None:
        raise ValueError(f"Thermostat {resolved_name!r} missing runtime.actualTemperature")

    program = therm.get("program") or {}
    climate_ref = program.get("currentClimateRef")
    heat_sp, cool_sp = _active_setpoints(program, climate_ref, runtime, resolved_name)

    settings = therm.get("settings") or {}
    hvac_mode = _parse_mode(settings.get("hvacMode"))

    outdoor_temp_f = _outdoor_temp_f(therm.get("weather"))

    return ThermostatState(
        name=resolved_name,
        current_temp_f=current_temp_f,
        heat_setpoint_f=heat_sp,
        cool_setpoint_f=cool_sp,
        hvac_mode=hvac_mode,
        outdoor_temp_f=outdoor_temp_f,
        current_climate_ref=climate_ref,
    )


def _active_setpoints(
    program: dict[str, Any],
    climate_ref: str | None,
    runtime: dict[str, Any],
    name: str,
) -> tuple[float, float]:
    """Setpoints of the active climate, falling back to the effective desired values."""
    for climate in program.get("climates") or []:
        if climate.get("climateRef") == climate_ref:
            heat = _tenths_to_f(climate.get("heatTemp"))
            cool = _tenths_to_f(climate.get("coolTemp"))
            if heat is not None and cool is not None:
                return heat, cool

    heat = _tenths_to_f(runtime.get("desiredHeat"))
    cool = _tenths_to_f(runtime.get("desiredCool"))
    if heat is None or cool is None:
        raise ValueError(
            f"Thermostat {name!r}: could not determine active heat/cool setpoints"
        )
    logger.warning(
        "Thermostat %r: active climate %r not found; using effective desired setpoints.",
        name,
        climate_ref,
    )
    return heat, cool


def _parse_mode(value: Any) -> HvacMode:
    if not value:
        return HvacMode.OFF
    try:
        return HvacMode(value)
    except ValueError:
        logger.warning("Unknown hvacMode %r; treating as OFF.", value)
        return HvacMode.OFF


def _outdoor_temp_f(weather: dict[str, Any] | None) -> float | None:
    if not weather:
        return None
    forecasts = weather.get("forecasts") or []
    if not forecasts:
        return None
    temp = _tenths_to_f(forecasts[0].get("temperature"))
    if temp is None or not (_MIN_PLAUSIBLE_F <= temp <= _MAX_PLAUSIBLE_F):
        return None
    return temp


def _tenths_to_f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 10.0
    except (TypeError, ValueError):
        return None
