"""Ecobee OAuth token management.

Refreshes short-lived access tokens (~1 hour) using the long-lived refresh
token, and handles Ecobee's refresh-token **rotation**: each refresh returns a
NEW refresh token that must be persisted back to the secret store for the next
run. The access token is cached in-memory for the duration of a single run.

The HTTP client and the secret store are both injectable, so this module is
pure and unit-testable with no live calls.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from optimizer.secrets import SecretStore

logger = logging.getLogger(__name__)

ECOBEE_TOKEN_URL = "https://api.ecobee.com/token"

# Treat the token as expired slightly early so an in-flight run never uses a
# token that lapses mid-request.
_EXPIRY_BUFFER_SECONDS = 60

_HTTP_TIMEOUT_SECONDS = 30.0


class AuthError(Exception):
    """Raised when an Ecobee access token cannot be obtained."""


@dataclass
class TokenSet:
    """A refreshed Ecobee token bundle."""

    access_token: str
    expires_in: int
    refresh_token: str
    obtained_at: float

    def is_valid(self, *, now: float | None = None) -> bool:
        """Whether the access token is still usable (with a safety buffer)."""
        current = time.time() if now is None else now
        return current < (self.obtained_at + self.expires_in - _EXPIRY_BUFFER_SECONDS)


class TokenManager:
    """Manages Ecobee access tokens, refreshing and rotating as needed."""

    def __init__(
        self,
        store: SecretStore,
        client_id_secret: str,
        refresh_token_secret: str,
        *,
        http: httpx.Client | None = None,
        token_url: str = ECOBEE_TOKEN_URL,
    ) -> None:
        self._store = store
        self._client_id_secret = client_id_secret
        self._refresh_token_secret = refresh_token_secret
        self._http = http
        self._token_url = token_url
        self._token_set: TokenSet | None = None

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing at most once per run."""
        if self._token_set is not None and self._token_set.is_valid():
            return self._token_set.access_token
        self._token_set = self._refresh()
        return self._token_set.access_token

    def _refresh(self) -> TokenSet:
        client_id = self._store.get(self._client_id_secret)
        refresh_token = self._store.get(self._refresh_token_secret)

        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }

        logger.info("Refreshing Ecobee access token.")
        owns_client = self._http is None
        client = self._http or httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS)
        try:
            response = client.post(self._token_url, params=params)
        except httpx.HTTPError as exc:
            raise AuthError(f"Network error refreshing Ecobee token: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        if response.status_code != 200:
            # Ecobee returns {"error": ..., "error_description": ...}; the body
            # carries no secrets, so it is safe to surface for diagnostics.
            raise AuthError(
                f"Ecobee token refresh failed: HTTP {response.status_code} {response.text!r}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AuthError("Ecobee token refresh returned a non-JSON response") from exc

        missing = [k for k in ("access_token", "expires_in", "refresh_token") if not data.get(k)]
        if missing:
            # Do not log the full payload — it contains the access token.
            raise AuthError(f"Ecobee token response missing fields: {missing}")

        try:
            expires_in = int(data["expires_in"])
        except (TypeError, ValueError) as exc:
            raise AuthError(
                f"Ecobee token response has invalid expires_in: {data.get('expires_in')!r}"
            ) from exc

        new_refresh_token = data["refresh_token"]
        self._maybe_rotate(refresh_token, new_refresh_token)

        return TokenSet(
            access_token=data["access_token"],
            expires_in=expires_in,
            refresh_token=new_refresh_token,
            obtained_at=time.time(),
        )

    def _maybe_rotate(self, old_refresh_token: str, new_refresh_token: str) -> None:
        """Persist the refresh token back to the store if Ecobee rotated it."""
        if new_refresh_token != old_refresh_token:
            logger.info("Ecobee refresh token rotated; persisting new token to secret store.")
            self._store.set(self._refresh_token_secret, new_refresh_token)
