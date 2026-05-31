"""Azure Functions v2 entry point for the Ecobee Comfort Optimizer.

This is the scaffold stub. The full wiring (read thermostat -> decide via the
comfort engine -> act -> log, with the dry-run gate) is implemented in a later
build step. For now this defines the ``FunctionApp`` instance and the timer
trigger so the project structure imports and deploys cleanly.

The timer cadence is driven by the ``OPTIMIZER_SCHEDULE`` app setting via
Azure's ``%SETTING%`` substitution syntax (default: hourly).
"""

from __future__ import annotations

import logging

import azure.functions as func

app = func.FunctionApp()


@app.timer_trigger(
    schedule="%OPTIMIZER_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def optimize(timer: func.TimerRequest) -> None:
    """Timer-triggered optimizer entry point (wiring pending — see build step 5)."""
    if timer.past_due:
        logging.warning("Timer is past due.")
    logging.info("Ecobee Comfort Optimizer scaffold invoked; full wiring pending.")
