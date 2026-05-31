"""Ecobee Comfort Optimizer — application package.

Layers (kept deliberately separate):
    config       — application settings loaded from env / app settings
    secrets      — secret store abstraction (Key Vault + env fallback)
    auth         — Ecobee OAuth token manager (refresh + rotation)
    ecobee_client— thin Ecobee REST client (no business logic)
    comfort      — pure decision engine (auto-changeover strategy)
    parsing      — Ecobee JSON -> ThermostatState mapping
"""
