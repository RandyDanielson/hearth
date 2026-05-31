"""Tests for the secret store abstraction and the Ecobee token manager.

All HTTP is mocked with respx — there are no live Ecobee calls.
"""

from __future__ import annotations

import os

import httpx
import pytest
import respx

from optimizer.auth import AuthError, TokenManager, TokenSet
from optimizer.secrets import (
    EnvSecretStore,
    KeyVaultSecretStore,
    build_secret_store,
)

CLIENT_ID_SECRET = "ecobee-client-id"
REFRESH_SECRET = "ecobee-refresh-token"
TOKEN_HOST = "api.ecobee.com"
TOKEN_PATH = "/token"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeStore:
    """In-memory SecretStore that records writes."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.sets: list[tuple[str, str]] = []

    def get(self, name: str) -> str:
        return self.values[name]

    def set(self, name: str, value: str) -> None:
        self.values[name] = value
        self.sets.append((name, value))


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeKeyVaultClient:
    """Stand-in for azure.keyvault.secrets.SecretClient."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, str]] = []

    def get_secret(self, name: str) -> _FakeSecret:
        return _FakeSecret(self.store[name])

    def set_secret(self, name: str, value: str) -> None:
        self.store[name] = value
        self.sets.append((name, value))


def _token_response(*, refresh_token: str = "RT_OLD", access_token: str = "ACCESS") -> dict:
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3599,
        "refresh_token": refresh_token,
        "scope": "smartWrite",
    }


def _route():
    return respx.route(method="POST", host=TOKEN_HOST, path=TOKEN_PATH)


def _manager(store: FakeStore) -> TokenManager:
    return TokenManager(store, CLIENT_ID_SECRET, REFRESH_SECRET)


# --------------------------------------------------------------------------- #
# TokenManager
# --------------------------------------------------------------------------- #
@respx.mock
def test_refresh_returns_access_token_and_persists_rotated_refresh_token():
    store = FakeStore({CLIENT_ID_SECRET: "CID", REFRESH_SECRET: "RT_OLD"})
    route = _route().mock(
        return_value=httpx.Response(200, json=_token_response(refresh_token="RT_NEW"))
    )

    token = _manager(store).get_access_token()

    assert token == "ACCESS"
    assert route.called
    # Rotation persisted back to the store exactly once.
    assert store.values[REFRESH_SECRET] == "RT_NEW"
    assert store.sets == [(REFRESH_SECRET, "RT_NEW")]
    # The request carried the stored credentials as query params.
    params = route.calls.last.request.url.params
    assert params["grant_type"] == "refresh_token"
    assert params["refresh_token"] == "RT_OLD"
    assert params["client_id"] == "CID"


@respx.mock
def test_no_rotation_when_refresh_token_unchanged():
    store = FakeStore({CLIENT_ID_SECRET: "CID", REFRESH_SECRET: "RT_SAME"})
    _route().mock(return_value=httpx.Response(200, json=_token_response(refresh_token="RT_SAME")))

    _manager(store).get_access_token()

    assert store.sets == []  # no spurious write
    assert store.values[REFRESH_SECRET] == "RT_SAME"


@respx.mock
def test_access_token_cached_within_run():
    store = FakeStore({CLIENT_ID_SECRET: "CID", REFRESH_SECRET: "RT"})
    route = _route().mock(return_value=httpx.Response(200, json=_token_response()))
    tm = _manager(store)

    first = tm.get_access_token()
    second = tm.get_access_token()

    assert first == second
    assert route.call_count == 1  # only refreshed once


@respx.mock
def test_auth_error_on_401():
    store = FakeStore({CLIENT_ID_SECRET: "CID", REFRESH_SECRET: "RT"})
    _route().mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))

    with pytest.raises(AuthError):
        _manager(store).get_access_token()


@respx.mock
def test_auth_error_on_network_failure():
    store = FakeStore({CLIENT_ID_SECRET: "CID", REFRESH_SECRET: "RT"})
    _route().mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(AuthError):
        _manager(store).get_access_token()


@respx.mock
def test_auth_error_on_missing_fields():
    store = FakeStore({CLIENT_ID_SECRET: "CID", REFRESH_SECRET: "RT"})
    _route().mock(return_value=httpx.Response(200, json={"access_token": "A"}))

    with pytest.raises(AuthError):
        _manager(store).get_access_token()


def test_tokenset_is_valid_respects_buffer():
    ts = TokenSet(access_token="A", expires_in=3599, refresh_token="R", obtained_at=1_000.0)
    assert ts.is_valid(now=1_000.0) is True
    # Within the 60s expiry buffer -> treated as invalid.
    assert ts.is_valid(now=1_000.0 + 3599 - 30) is False


# --------------------------------------------------------------------------- #
# Secret stores
# --------------------------------------------------------------------------- #
def test_env_secret_store_reads_and_maps_names(monkeypatch):
    monkeypatch.setenv("ECOBEE_REFRESH_TOKEN", "env-rt")
    assert EnvSecretStore().get("ecobee-refresh-token") == "env-rt"


def test_env_secret_store_missing_raises(monkeypatch):
    monkeypatch.delenv("ECOBEE_CLIENT_ID", raising=False)
    with pytest.raises(KeyError):
        EnvSecretStore().get("ecobee-client-id")


def test_env_secret_store_set_updates_process_env(monkeypatch):
    monkeypatch.delenv("ECOBEE_REFRESH_TOKEN", raising=False)
    EnvSecretStore().set("ecobee-refresh-token", "rotated")
    assert os.environ["ECOBEE_REFRESH_TOKEN"] == "rotated"


def test_keyvault_store_get_set_with_injected_client():
    client = FakeKeyVaultClient()
    client.store[CLIENT_ID_SECRET] = "CID"
    store = KeyVaultSecretStore("https://example.vault.azure.net/", client=client)

    assert store.get(CLIENT_ID_SECRET) == "CID"
    store.set(REFRESH_SECRET, "RT2")
    assert client.sets == [(REFRESH_SECRET, "RT2")]


def test_build_secret_store_uses_env_when_no_uri():
    assert isinstance(build_secret_store(None), EnvSecretStore)
    assert isinstance(build_secret_store(""), EnvSecretStore)


def test_build_secret_store_uses_keyvault_when_uri_set(monkeypatch):
    # Patch in a fake so we exercise the selection branch without constructing a
    # real Azure credential (DefaultAzureCredential pulls in the Azure SDK stack).
    created: dict[str, str] = {}

    class FakeKeyVaultStore:
        def __init__(self, uri: str) -> None:
            created["uri"] = uri

    monkeypatch.setattr("optimizer.secrets.KeyVaultSecretStore", FakeKeyVaultStore)

    store = build_secret_store("https://example.vault.azure.net/")

    assert isinstance(store, FakeKeyVaultStore)
    assert created["uri"] == "https://example.vault.azure.net/"


@respx.mock
def test_rotation_round_trips_through_env_store(monkeypatch):
    # End-to-end: env-backed store, rotation persists to the process env.
    monkeypatch.setenv("ECOBEE_CLIENT_ID", "CID")
    monkeypatch.setenv("ECOBEE_REFRESH_TOKEN", "RT_OLD")
    _route().mock(return_value=httpx.Response(200, json=_token_response(refresh_token="RT_NEW")))

    TokenManager(EnvSecretStore(), CLIENT_ID_SECRET, REFRESH_SECRET).get_access_token()

    assert os.environ["ECOBEE_REFRESH_TOKEN"] == "RT_NEW"
