"""Comfort decision engine (pure, no I/O).

Given a snapshot of thermostat state, decide whether to change the HVAC system
mode so the temperature is actively held within the active comfort setting's
band — the **auto-changeover** behavior that keeps the house from drifting past
the setpoint while stuck in a single mode.

The :class:`ComfortStrategy` protocol is the seam where an AI-driven strategy
could later replace :class:`ChangeoverStrategy` without touching the client or
the function wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


class HvacMode(str, Enum):
    """Ecobee system modes."""

    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    OFF = "off"
    AUX = "auxHeatOnly"


@dataclass(frozen=True)
class ThermostatState:
    """Inputs to the comfort decision, already unit-normalized to °F."""

    name: str
    current_temp_f: float
    heat_setpoint_f: float  # active climate's heat setpoint
    cool_setpoint_f: float  # active climate's cool setpoint
    hvac_mode: HvacMode
    outdoor_temp_f: float | None
    current_climate_ref: str | None


ActionKind = Literal["switch_mode", "set_hold", "resume", "noop"]


@dataclass(frozen=True)
class ComfortAction:
    """The decided action. ``reason`` is always populated for structured logging."""

    kind: ActionKind
    reason: str
    hvac_mode: HvacMode | None = None
    heat_setpoint_f: float | None = None
    cool_setpoint_f: float | None = None

    @property
    def is_noop(self) -> bool:
        return self.kind == "noop"


class ComfortStrategy(Protocol):
    """Decision strategy interface; swap in an AI strategy later."""

    def decide(self, state: ThermostatState) -> ComfortAction:
        ...


@dataclass
class ChangeoverStrategy:
    """Deterministic auto-changeover strategy.

    Switches to **cooling** when the indoor temperature rises a full
    ``switch_band_f`` above the active cool setpoint, and to **heating** when it
    falls a full band below the active heat setpoint. While the temperature is
    inside the band, the mode is left alone (this provides natural hysteresis,
    so the mode only flips on a genuine crossing).

    The optional outdoor guard suppresses switching in the wrong season: no
    cooling when it is colder than ``cool_min_outdoor_f`` outside, and no heating
    when it is hotter than ``heat_max_outdoor_f`` outside.
    """

    switch_band_f: float = 1.0
    enable_outdoor_guard: bool = True
    cool_min_outdoor_f: float = 50.0
    heat_max_outdoor_f: float = 80.0

    def decide(self, state: ThermostatState) -> ComfortAction:
        band = self.switch_band_f
        temp = state.current_temp_f
        heat_sp = state.heat_setpoint_f
        cool_sp = state.cool_setpoint_f
        outdoor = state.outdoor_temp_f

        # --- Too warm: want cooling ---------------------------------------- #
        if temp >= cool_sp + band:
            if (
                self.enable_outdoor_guard
                and outdoor is not None
                and outdoor < self.cool_min_outdoor_f
            ):
                return ComfortAction(
                    "noop",
                    reason=(
                        f"indoor {temp:.1f}F is above cool setpoint {cool_sp:.1f}F+{band:.1f}F "
                        f"but outdoor {outdoor:.1f}F < {self.cool_min_outdoor_f:.0f}F guard; "
                        f"not switching to cool."
                    ),
                )
            if state.hvac_mode == HvacMode.COOL:
                return ComfortAction(
                    "noop",
                    reason=(
                        f"indoor {temp:.1f}F above cool setpoint {cool_sp:.1f}F and already "
                        f"in cool mode; no change."
                    ),
                )
            return ComfortAction(
                "switch_mode",
                hvac_mode=HvacMode.COOL,
                heat_setpoint_f=heat_sp,
                cool_setpoint_f=cool_sp,
                reason=(
                    f"indoor {temp:.1f}F >= cool setpoint {cool_sp:.1f}F + band {band:.1f}F; "
                    f"switching HVAC {state.hvac_mode.value}->cool to bring it down."
                ),
            )

        # --- Too cold: want heating ---------------------------------------- #
        if temp <= heat_sp - band:
            if (
                self.enable_outdoor_guard
                and outdoor is not None
                and outdoor > self.heat_max_outdoor_f
            ):
                return ComfortAction(
                    "noop",
                    reason=(
                        f"indoor {temp:.1f}F is below heat setpoint {heat_sp:.1f}F-{band:.1f}F "
                        f"but outdoor {outdoor:.1f}F > {self.heat_max_outdoor_f:.0f}F guard; "
                        f"not switching to heat."
                    ),
                )
            if state.hvac_mode == HvacMode.HEAT:
                return ComfortAction(
                    "noop",
                    reason=(
                        f"indoor {temp:.1f}F below heat setpoint {heat_sp:.1f}F and already "
                        f"in heat mode; no change."
                    ),
                )
            return ComfortAction(
                "switch_mode",
                hvac_mode=HvacMode.HEAT,
                heat_setpoint_f=heat_sp,
                cool_setpoint_f=cool_sp,
                reason=(
                    f"indoor {temp:.1f}F <= heat setpoint {heat_sp:.1f}F - band {band:.1f}F; "
                    f"switching HVAC {state.hvac_mode.value}->heat to bring it up."
                ),
            )

        # --- Comfortable: leave the schedule alone ------------------------- #
        return ComfortAction(
            "noop",
            reason=(
                f"indoor {temp:.1f}F within band "
                f"[{heat_sp - band:.1f}F, {cool_sp + band:.1f}F]; no change."
            ),
        )
