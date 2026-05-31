# hearth

Comfort-optimizing automation for Ecobee thermostats — polls on a schedule and
adjusts the HVAC system mode automatically so the temperature you set is
actually held.

## What it does

The thermostat is often left in a single system mode (heat-only or cool-only),
so the indoor temperature drifts *past* the setpoint with nothing to pull it
back:

- In **heat** mode on a mild day, the house warms past your target and stays
  there — no cooling kicks in to bring it down.
- In **cool** mode overnight, the AC overshoots and the house drifts below your
  target by morning — no heat kicks in to bring it back up.

This automation solves that with **auto-changeover**: on a timer it reads the
thermostat, looks at the current temperature versus the *active comfort
setting's* heat/cool setpoints, and **switches the system mode (heat ↔ cool)**
so the temperature is held from both directions.

- Switches to **cooling** when `temp ≥ cool setpoint + band`.
- Switches to **heating** when `temp ≤ heat setpoint − band`.
- Leaves the mode alone while the temperature is comfortably inside the band
  (natural hysteresis — no rapid flapping).
- An optional **outdoor guard** avoids running the AC in winter (or heat in a
  heat wave).

> **Tip:** the automation enforces your Ecobee schedule's setpoints. For a tight
> hold near 70°F, set the active comfort setting's **Heat** and **Cool**
> setpoints close together (e.g. Heat 70 / Cool 71). A wide gap (Heat 68 /
> Cool 76) intentionally lets the temperature float within that range.

## Architecture

A timer-triggered **Azure Functions** app (Python 3.12, v2 programming model),
split into clean layers under `optimizer/`:

| Layer | File | Responsibility |
|---|---|---|
| Config | `optimizer/config.py` | Load settings from app settings / env |
| Secrets | `optimizer/secrets.py` | Key Vault (managed identity) + env fallback |
| Auth | `optimizer/auth.py` | Ecobee OAuth refresh + refresh-token rotation |
| Client | `optimizer/ecobee_client.py` | Thin Ecobee REST client (no business logic) |
| Decision | `optimizer/comfort.py` | Pure auto-changeover strategy |
| Parsing | `optimizer/parsing.py` | Ecobee JSON → `ThermostatState` |
| Entry | `function_app.py` | Timer trigger: read → decide → act → log |

We talk to the **official Ecobee REST API directly** (via `httpx`) — no
third-party Ecobee wrapper.

## Status

🚧 Scaffold in place. Implementation is landing in build-order steps:
auth → client → comfort → wiring → CI/deploy. Sections below are filled in as
those steps complete.

## One-time Ecobee PIN authorization (manual)

> _Detailed steps added in the final build step._ Summary: register an app on
> the Ecobee developer portal (API key / client ID), use the **PIN
> authorization** flow once to mint the initial **refresh token**, and store the
> client ID + refresh token in Key Vault. The automation never performs the
> interactive PIN step — it only uses the refresh token to mint short-lived
> access tokens.

## App settings

| Setting | Default | Purpose |
|---|---|---|
| `OPTIMIZER_SCHEDULE` | `0 0 * * * *` | Timer cron (NCRONTAB, hourly) |
| `OPTIMIZER_DRY_RUN` | `true` | Log intended changes without writing |
| `KEY_VAULT_URI` | _(empty)_ | Key Vault URL; env fallback when empty |
| `ECOBEE_CLIENT_ID_SECRET` | `ecobee-client-id` | Key Vault secret name |
| `ECOBEE_REFRESH_TOKEN_SECRET` | `ecobee-refresh-token` | Key Vault secret name |
| `ECOBEE_CLIENT_ID` / `ECOBEE_REFRESH_TOKEN` | _(empty)_ | Local-dev env fallback |
| `ECOBEE_SELECTION_MATCH` | _(empty = all registered)_ | Thermostat filter |
| `SWITCH_BAND_F` | `1.0` | °F past setpoint before switching |
| `ENABLE_OUTDOOR_GUARD` | `true` | Seasonal guard on/off |
| `COOL_MIN_OUTDOOR_F` / `HEAT_MAX_OUTDOOR_F` | `50` / `80` | Guard thresholds (°F) |

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp local.settings.json.example local.settings.json   # fill in for local dev (gitignored)
pytest
```

> `local.settings.json` is gitignored — never commit secrets. For local runs,
> set `KEY_VAULT_URI` empty and put the Ecobee client ID / refresh token in the
> `ECOBEE_CLIENT_ID` / `ECOBEE_REFRESH_TOKEN` env-fallback values.

Optional, to run the function host locally (requires Azure Functions Core Tools):

```bash
func start   # with OPTIMIZER_DRY_RUN=true it logs intended changes, no writes
```

## Key Vault setup

> _Detailed steps added in the final build step._ Summary: create a Key Vault,
> store `ecobee-client-id` and `ecobee-refresh-token`, enable a **managed
> identity** on the Function app, and grant it `get`/`set` on secrets (the app
> needs `set` to persist the rotated refresh token).

## Deployment

> _Detailed steps added in the final build step._ Summary: a GitHub Actions
> workflow deploys on push to `main` using **OIDC federated credentials**
> (`azure/login` + `azure/functions-action`). Dry-run defaults **ON** for the
> first deploy.

## Tests

```bash
pytest
```

All tests mock HTTP — there are **no live Ecobee calls** in the test suite.
