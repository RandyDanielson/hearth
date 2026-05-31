"""Tests for the thin Ecobee REST client.

Covers selection building, request shaping (URLs, headers, bodies, °F->tenths),
retry/backoff behavior, and the typed-exception mapping. All HTTP is mocked
with respx — no live Ecobee calls.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from optimizer.ecobee_client import (
    EcobeeAuthError,
    EcobeeClient,
    EcobeeRateLimitError,
    EcobeeRequestError,
    EcobeeTransientError,
    build_selection,
    to_ecobee_temp,
)

API_HOST = "api.ecobee.com"
THERMOSTAT_PATH = "/1/thermostat"

_OK = {"status": {"code": 0, "message": ""}}


def _client(**kwargs) -> EcobeeClient:
    # Instant "sleep" so retry tests don't actually wait.
    kwargs.setdefault("sleep", lambda _seconds: None)
    return EcobeeClient(lambda: "TKN", **kwargs)


def _get_route():
    return respx.get(host=API_HOST, path=THERMOSTAT_PATH)


def _post_route():
    return respx.post(host=API_HOST, path=THERMOSTAT_PATH)


def _last_body(route) -> dict:
    return json.loads(route.calls.last.request.content)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_build_selection_defaults_include_weather_and_program():
    selection = build_selection("X123")["selection"]
    assert selection["selectionType"] == "registered"
    assert selection["selectionMatch"] == "X123"
    assert selection["includeRuntime"] is True
    assert selection["includeProgram"] is True
    assert selection["includeWeather"] is True
    assert selection["includeSettings"] is True


def test_build_selection_can_disable_flags():
    selection = build_selection(sensors=False, weather=False)["selection"]
    assert selection["selectionMatch"] == ""
    assert selection["includeSensors"] is False
    assert selection["includeWeather"] is False


@pytest.mark.parametrize("temp_f,expected", [(70.0, 700), (71.0, 710), (68.5, 685), (72.49, 725)])
def test_to_ecobee_temp(temp_f, expected):
    assert to_ecobee_temp(temp_f) == expected


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@respx.mock
def test_get_thermostats_sends_selection_and_auth():
    payload = {**_OK, "thermostatList": [{"name": "Home"}]}
    route = _get_route().mock(return_value=httpx.Response(200, json=payload))

    selection = build_selection("ABC")
    data = _client().get_thermostats(selection)

    assert data["thermostatList"][0]["name"] == "Home"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer TKN"
    # Selection is passed URL-encoded under the `json` query param.
    assert json.loads(request.url.params["json"]) == selection


@respx.mock
def test_non_zero_status_code_raises_request_error():
    _get_route().mock(
        return_value=httpx.Response(200, json={"status": {"code": 3, "message": "bad"}})
    )
    with pytest.raises(EcobeeRequestError):
        _client().get_thermostats(build_selection())


@respx.mock
def test_auth_status_code_raises_auth_error():
    _get_route().mock(
        return_value=httpx.Response(200, json={"status": {"code": 14, "message": "expired"}})
    )
    with pytest.raises(EcobeeAuthError):
        _client().get_thermostats(build_selection())


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
@respx.mock
def test_set_hvac_mode_posts_update_thermostat_body():
    route = _post_route().mock(return_value=httpx.Response(200, json=_OK))

    _client().set_hvac_mode(selection_match="ABC", mode="cool")

    request = route.calls.last.request
    assert request.url.params["format"] == "json"
    assert request.headers["content-type"] == "application/json;charset=UTF-8"
    body = _last_body(route)
    assert body["thermostat"]["settings"]["hvacMode"] == "cool"
    assert body["selection"] == {"selectionType": "registered", "selectionMatch": "ABC"}


def test_set_hvac_mode_rejects_invalid_mode():
    with pytest.raises(ValueError):
        _client().set_hvac_mode(selection_match="", mode="frosty")


@respx.mock
def test_set_hold_builds_function_with_tenths_and_hold_type():
    route = _post_route().mock(return_value=httpx.Response(200, json=_OK))

    _client().set_hold(selection_match="", heat_temp_f=70.0, cool_temp_f=71.0)

    fn = _last_body(route)["functions"][0]
    assert fn["type"] == "setHold"
    assert fn["params"]["holdType"] == "nextTransition"
    assert fn["params"]["heatHoldTemp"] == 700
    assert fn["params"]["coolHoldTemp"] == 710
    assert "holdClimateRef" not in fn["params"]


@respx.mock
def test_set_hold_includes_climate_ref_when_given():
    route = _post_route().mock(return_value=httpx.Response(200, json=_OK))

    _client().set_hold(
        selection_match="", heat_temp_f=68.0, cool_temp_f=74.0, hold_climate_ref="home"
    )

    fn = _last_body(route)["functions"][0]
    assert fn["params"]["holdClimateRef"] == "home"


@respx.mock
def test_resume_program_body():
    route = _post_route().mock(return_value=httpx.Response(200, json=_OK))

    _client().resume_program(selection_match="ABC")

    fn = _last_body(route)["functions"][0]
    assert fn["type"] == "resumeProgram"
    assert fn["params"]["resumeAll"] is True


# --------------------------------------------------------------------------- #
# Retries / error mapping
# --------------------------------------------------------------------------- #
@respx.mock
def test_retries_on_500_then_succeeds():
    route = _get_route().mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=_OK)]
    )

    _client().get_thermostats(build_selection())

    assert route.call_count == 2


@respx.mock
def test_rate_limit_raises_after_retries():
    route = _get_route().mock(return_value=httpx.Response(429))

    with pytest.raises(EcobeeRateLimitError):
        _client(max_retries=2).get_thermostats(build_selection())

    assert route.call_count == 3  # initial + 2 retries


@respx.mock
def test_auth_error_not_retried():
    route = _get_route().mock(return_value=httpx.Response(401, text="nope"))

    with pytest.raises(EcobeeAuthError):
        _client().get_thermostats(build_selection())

    assert route.call_count == 1  # 401 is not retried


@respx.mock
def test_other_4xx_raises_request_error():
    _get_route().mock(return_value=httpx.Response(400, text="bad request"))

    with pytest.raises(EcobeeRequestError):
        _client().get_thermostats(build_selection())


@respx.mock
def test_network_error_raises_transient_after_retries():
    route = _get_route().mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(EcobeeTransientError):
        _client(max_retries=1).get_thermostats(build_selection())

    assert route.call_count == 2  # initial + 1 retry
