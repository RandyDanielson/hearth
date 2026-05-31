"""Secret storage abstraction for the Ecobee Comfort Optimizer.

Two implementations back the same :class:`SecretStore` protocol:

* :class:`KeyVaultSecretStore` — production: Azure Key Vault accessed with a
  managed identity (``DefaultAzureCredential``). Supports ``set`` so the rotated
  Ecobee refresh token can be persisted back for the next run.
* :class:`EnvSecretStore` — local development: reads values from environment
  variables. ``set`` only updates the in-process environment and logs a
  warning, since there is nowhere durable to write.

Key Vault secret names (e.g. ``ecobee-refresh-token``) map to environment
variable names by upper-casing and replacing ``-`` with ``_`` (e.g.
``ECOBEE_REFRESH_TOKEN``), which matches the names documented in the README.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class SecretStore(Protocol):
    """Minimal read/write secret store interface."""

    def get(self, name: str) -> str:
        """Return the secret value for ``name``; raise ``KeyError`` if missing."""
        ...

    def set(self, name: str, value: str) -> None:
        """Persist ``value`` under ``name`` (used to store a rotated refresh token)."""
        ...


def _env_var_name(secret_name: str) -> str:
    """Map a Key Vault secret name to its environment-variable fallback name."""
    return secret_name.upper().replace("-", "_")


class EnvSecretStore:
    """Environment-variable backed secret store for local development."""

    def get(self, name: str) -> str:
        env_name = _env_var_name(name)
        value = os.environ.get(env_name)
        if not value:
            raise KeyError(
                f"Secret {name!r} not found or empty (expected env var {env_name!r})"
            )
        return value

    def set(self, name: str, value: str) -> None:
        env_name = _env_var_name(name)
        os.environ[env_name] = value
        logger.warning(
            "EnvSecretStore.set(%s): updated in-process env var %s only; this will "
            "NOT persist across runs. Use Key Vault (set KEY_VAULT_URI) to durably "
            "persist a rotated refresh token, or update local.settings.json manually.",
            name,
            env_name,
        )


class KeyVaultSecretStore:
    """Azure Key Vault backed secret store (managed identity)."""

    def __init__(self, vault_uri: str, *, client: Any = None) -> None:
        if client is None:
            # Lazy import so importing this module doesn't require the Azure SDKs;
            # keeps unit tests that use EnvSecretStore / fakes lightweight.
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=vault_uri, credential=credential)
        self._client = client

    def get(self, name: str) -> str:
        return self._client.get_secret(name).value

    def set(self, name: str, value: str) -> None:
        self._client.set_secret(name, value)
        logger.info("Persisted secret %s to Key Vault.", name)


def build_secret_store(key_vault_uri: str | None) -> SecretStore:
    """Return a Key Vault store when ``key_vault_uri`` is set, else an env store."""
    if key_vault_uri:
        logger.info("Using Key Vault secret store.")
        return KeyVaultSecretStore(key_vault_uri)
    logger.info("KEY_VAULT_URI not set; using environment-variable secret store.")
    return EnvSecretStore()
