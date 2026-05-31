"""Application configuration loaded from environment / Azure app settings.

All knobs have safe defaults; notably ``dry_run`` defaults **ON** so a fresh
deploy never writes to the thermostat until explicitly turned off.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE = "0 0 * * * *"  # hourly (NCRONTAB: second minute hour day month dow)


@dataclass(frozen=True)
class AppConfig:
    """Resolved configuration for one optimizer run."""

    schedule: str
    dry_run: bool
    key_vault_uri: str | None
    secret_name_client_id: str
    secret_name_refresh_token: str
    selection_match: str
    switch_band_f: float
    enable_outdoor_guard: bool
    cool_min_outdoor_f: float
    heat_max_outdoor_f: float
    hold_type: str

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build configuration from environment variables / app settings."""
        key_vault_uri = (os.environ.get("KEY_VAULT_URI") or "").strip() or None
        return cls(
            schedule=os.environ.get("OPTIMIZER_SCHEDULE", DEFAULT_SCHEDULE),
            dry_run=_get_bool("OPTIMIZER_DRY_RUN", True),
            key_vault_uri=key_vault_uri,
            secret_name_client_id=os.environ.get("ECOBEE_CLIENT_ID_SECRET", "ecobee-client-id"),
            secret_name_refresh_token=os.environ.get(
                "ECOBEE_REFRESH_TOKEN_SECRET", "ecobee-refresh-token"
            ),
            selection_match=os.environ.get("ECOBEE_SELECTION_MATCH", ""),
            switch_band_f=_get_float("SWITCH_BAND_F", 1.0),
            enable_outdoor_guard=_get_bool("ENABLE_OUTDOOR_GUARD", True),
            cool_min_outdoor_f=_get_float("COOL_MIN_OUTDOOR_F", 50.0),
            heat_max_outdoor_f=_get_float("HEAT_MAX_OUTDOOR_F", 80.0),
            hold_type=os.environ.get("HOLD_TYPE", "nextTransition"),
        )


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r; using default %s", name, raw, default)
        return default
