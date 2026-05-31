"""Thin client for the official Ecobee REST API.

This layer only shapes requests, performs HTTP (with retries on transient
errors), and maps failures to typed exceptions. It contains **no business
logic** — deciding *what* to do lives in :mod:`optimizer.comfort`.

Endpoints used (base ``https://api.ecobee.com``):

* ``GET  /1/thermostat?json=<selection>``         — read state
* ``POST /1/thermostat?format=json``              — write (update settings /
  ``setHold`` / ``resumeProgram``)

Access tokens are supplied lazily via a ``token_provider`` callable (typically
``TokenManager.get_access_token``) so this client never touches OAuth itself.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

ECOBEE_API_BASE = "https://api.ecobee.com"
_THERMOSTAT_PATH = "/1/thermostat"
_HTTP_TIMEOUT_SECONDS = 30.0

VALID_HVAC_MODES = frozenset({"heat", "cool", "auto", "off", "auxHeatOnly"})

# Ecobee response status codes that indicate an auth problem even on HTTP 200.
_AUTH_STATUS_CODES = frozenset({14, 16})


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class EcobeeError(Exception):
    """Base class for Ecobee client errors."""


class EcobeeAuthError(EcobeeError):
    """Authentication/authorization failure (HTTP 401/403 or auth status code)."""


class EcobeeRateLimitError(EcobeeError):
    """Rate limit exceeded (HTTP 429) after exhausting retries."""


class EcobeeTransientError(EcobeeError):
    """Transient failure (network error or HTTP 5xx) after exhausting retries."""


class EcobeeRequestError(EcobeeError):
    """Non-retryable client error (other 4xx or non-zero API status code)."""


# --------------------------------------------------------------------------- #
# Selection / helpers
# --------------------------------------------------------------------------- #
def build_selection(
    selection_match: str = "",
    *,
    runtime: bool = True,
    settings: bool = True,
    equipment_status: bool = True,
    program: bool = True,
    events: bool = True,
    sensors: bool = True,
    weather: bool = True,
) -> dict[str, Any]:
    """Build a read ``selection`` object for ``GET /1/thermostat``.

    ``weather`` is needed for the outdoor temperature the comfort engine uses for
    its seasonal guard; ``program`` provides the active climate's setpoints.
    """
    return {
        "selection": {
            "selectionType": "registered",
            "selectionMatch": selection_match,
            "includeRuntime": runtime,
            "includeSettings": settings,
            "includeEquipmentStatus": equipment_status,
            "includeProgram": program,
            "includeEvents": events,
            "includeSensors": sensors,
            "includeWeather": weather,
        }
    }


def _registered_selection(selection_match: str) -> dict[str, str]:
    return {"selectionType": "registered", "selectionMatch": selection_match}


def to_ecobee_temp(temp_f: float) -> int:
    """Convert °F to Ecobee's integer tenths-of-a-degree representation (70.0 -> 700)."""
    return int(round(temp_f * 10))


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class EcobeeClient:
    """Minimal Ecobee REST client with retries and typed errors."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        http: httpx.Client | None = None,
        base_url: str = ECOBEE_API_BASE,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token_provider = token_provider
        self._http = http or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._sleep = sleep

    # -- public API -------------------------------------------------------- #
    def get_thermostats(self, selection: dict[str, Any]) -> dict[str, Any]:
        """Read thermostat data for the given ``selection`` object."""
        return self._request("GET", _THERMOSTAT_PATH, params={"json": json.dumps(selection)})

    def set_hvac_mode(self, *, selection_match: str, mode: str) -> dict[str, Any]:
        """Set the persistent system mode (heat/cool/auto/off/auxHeatOnly)."""
        if mode not in VALID_HVAC_MODES:
            raise ValueError(
                f"Invalid hvacMode {mode!r}; expected one of {sorted(VALID_HVAC_MODES)}"
            )
        body = {
            "selection": _registered_selection(selection_match),
            "thermostat": {"settings": {"hvacMode": mode}},
        }
        return self._write(body)

    def set_hold(
        self,
        *,
        selection_match: str,
        heat_temp_f: float,
        cool_temp_f: float,
        hold_type: str = "nextTransition",
        hold_climate_ref: str | None = None,
    ) -> dict[str, Any]:
        """Set a temperature hold. Temps are °F; converted to Ecobee tenths."""
        params: dict[str, Any] = {
            "holdType": hold_type,
            "heatHoldTemp": to_ecobee_temp(heat_temp_f),
            "coolHoldTemp": to_ecobee_temp(cool_temp_f),
        }
        if hold_climate_ref is not None:
            params["holdClimateRef"] = hold_climate_ref
        body = {
            "selection": _registered_selection(selection_match),
            "functions": [{"type": "setHold", "params": params}],
        }
        return self._write(body)

    def resume_program(self, *, selection_match: str, resume_all: bool = True) -> dict[str, Any]:
        """Resume the thermostat's scheduled program (clearing holds)."""
        body = {
            "selection": _registered_selection(selection_match),
            "functions": [{"type": "resumeProgram", "params": {"resumeAll": resume_all}}],
        }
        return self._write(body)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EcobeeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals --------------------------------------------------------- #
    def _write(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", _THERMOSTAT_PATH, params={"format": "json"}, json_body=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token_provider()}"}
        content = None
        if json_body is not None:
            content = json.dumps(json_body)
            headers["Content-Type"] = "application/json;charset=UTF-8"

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(
                    method, url, params=params, content=content, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise EcobeeTransientError(f"Network error calling Ecobee: {exc}") from exc

            status = response.status_code
            if status == 200:
                return self._parse_ok(response)
            if status in (401, 403):
                raise EcobeeAuthError(
                    f"Ecobee auth failed: HTTP {status} {response.text!r}"
                )
            if status == 429:
                if attempt < self._max_retries:
                    self._sleep(self._retry_after(response, attempt))
                    continue
                raise EcobeeRateLimitError("Ecobee rate limit exceeded (HTTP 429)")
            if 500 <= status < 600:
                last_error = EcobeeTransientError(f"Ecobee server error HTTP {status}")
                if attempt < self._max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise last_error
            raise EcobeeRequestError(f"Ecobee request failed: HTTP {status} {response.text!r}")

        # Unreachable in practice; guards against a logic slip in the loop.
        raise EcobeeTransientError("Ecobee request failed after retries") from last_error

    def _parse_ok(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise EcobeeError("Ecobee returned a non-JSON 200 response") from exc
        status = data.get("status") or {}
        code = status.get("code", 0)
        if code != 0:
            message = status.get("message", "")
            if code in _AUTH_STATUS_CODES:
                raise EcobeeAuthError(f"Ecobee API error {code}: {message}")
            raise EcobeeRequestError(f"Ecobee API error {code}: {message}")
        return data

    def _backoff(self, attempt: int) -> float:
        return self._backoff_factor * (2 ** attempt)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(int(header))
            except ValueError:
                pass
        return self._backoff(attempt)
