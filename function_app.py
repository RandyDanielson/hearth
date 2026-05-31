"""Azure Functions v2 entry point for the Ecobee Comfort Optimizer.

Timer-triggered wiring that ties the layers together:

    read thermostat -> parse state -> decide (comfort engine) -> act -> log

The run is idempotent (a skipped or repeated run is safe), and the **dry-run**
app setting (default ON) logs intended changes without calling write endpoints.

The schedule is driven by the ``OPTIMIZER_SCHEDULE`` app setting via Azure's
``%SETTING%`` substitution (default: hourly).
"""

from __future__ import annotations

import logging

import azure.functions as func

from optimizer.auth import TokenManager
from optimizer.comfort import ChangeoverStrategy, ComfortAction, ComfortStrategy, ThermostatState
from optimizer.config import AppConfig
from optimizer.ecobee_client import EcobeeClient, build_selection
from optimizer.parsing import parse_thermostat
from optimizer.secrets import build_secret_store

logger = logging.getLogger("optimizer.function_app")

app = func.FunctionApp()


@app.timer_trigger(
    schedule="%OPTIMIZER_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def optimize(timer: func.TimerRequest) -> None:
    """Timer-triggered optimizer run."""
    if timer.past_due:
        logger.warning("Timer is past due; running optimizer anyway.")
    try:
        run_optimizer()
    except Exception:  # noqa: BLE001 - surface failures to the host for retry/alerting
        logger.exception("Optimizer run failed.")
        raise


def run_optimizer(cfg: AppConfig | None = None, *, client: EcobeeClient | None = None) -> None:
    """Execute one optimization pass.

    ``cfg`` and ``client`` can be injected for testing; otherwise they are built
    from app settings / Key Vault.
    """
    cfg = cfg or AppConfig.from_env()
    strategy = ChangeoverStrategy(
        switch_band_f=cfg.switch_band_f,
        enable_outdoor_guard=cfg.enable_outdoor_guard,
        cool_min_outdoor_f=cfg.cool_min_outdoor_f,
        heat_max_outdoor_f=cfg.heat_max_outdoor_f,
    )
    logger.info(
        "Optimizer starting (dry_run=%s, switch_band=%.1fF, outdoor_guard=%s).",
        cfg.dry_run,
        cfg.switch_band_f,
        cfg.enable_outdoor_guard,
    )

    if client is not None:
        _run_with_client(client, strategy, cfg)
        return

    store = build_secret_store(cfg.key_vault_uri)
    token_manager = TokenManager(
        store, cfg.secret_name_client_id, cfg.secret_name_refresh_token
    )
    with EcobeeClient(token_manager.get_access_token) as owned_client:
        _run_with_client(owned_client, strategy, cfg)


def _run_with_client(
    client: EcobeeClient, strategy: ComfortStrategy, cfg: AppConfig
) -> None:
    data = client.get_thermostats(build_selection(cfg.selection_match))
    thermostats = data.get("thermostatList") or []
    logger.info("Read %d thermostat(s).", len(thermostats))
    if len(thermostats) > 1:
        logger.warning(
            "Multiple thermostats returned; writes target selection_match=%r "
            "(set ECOBEE_SELECTION_MATCH to scope to one).",
            cfg.selection_match,
        )
    for therm in thermostats:
        _process_thermostat(client, strategy, therm, cfg)


def _process_thermostat(
    client: EcobeeClient, strategy: ComfortStrategy, therm: dict, cfg: AppConfig
) -> None:
    try:
        state = parse_thermostat(therm)
    except ValueError as exc:
        logger.error("Skipping thermostat: %s", exc)
        return

    logger.info(
        "[%s] read: indoor=%.1fF climate=%s heat_sp=%.1fF cool_sp=%.1fF mode=%s outdoor=%s",
        state.name,
        state.current_temp_f,
        state.current_climate_ref,
        state.heat_setpoint_f,
        state.cool_setpoint_f,
        state.hvac_mode.value,
        f"{state.outdoor_temp_f:.1f}F" if state.outdoor_temp_f is not None else "n/a",
    )

    action = strategy.decide(state)
    logger.info("[%s] decided: %s (%s)", state.name, action.kind, action.reason)
    _apply_action(client, action, state, cfg)


def _apply_action(
    client: EcobeeClient, action: ComfortAction, state: ThermostatState, cfg: AppConfig
) -> None:
    if action.is_noop:
        return

    if action.kind == "switch_mode" and action.hvac_mode is not None:
        target = action.hvac_mode.value
        if cfg.dry_run:
            logger.info(
                "[%s] DRY-RUN would switch HVAC mode %s->%s.",
                state.name,
                state.hvac_mode.value,
                target,
            )
            return
        client.set_hvac_mode(selection_match=cfg.selection_match, mode=target)
        logger.info("[%s] switched HVAC mode %s->%s.", state.name, state.hvac_mode.value, target)
        return

    if action.kind == "set_hold" and action.heat_setpoint_f is not None:
        if cfg.dry_run:
            logger.info(
                "[%s] DRY-RUN would set hold heat=%.1fF cool=%.1fF.",
                state.name,
                action.heat_setpoint_f,
                action.cool_setpoint_f,
            )
            return
        client.set_hold(
            selection_match=cfg.selection_match,
            heat_temp_f=action.heat_setpoint_f,
            cool_temp_f=action.cool_setpoint_f,
            hold_type=cfg.hold_type,
        )
        logger.info("[%s] set hold.", state.name)
        return

    if action.kind == "resume":
        if cfg.dry_run:
            logger.info("[%s] DRY-RUN would resume program.", state.name)
            return
        client.resume_program(selection_match=cfg.selection_match)
        logger.info("[%s] resumed program.", state.name)
