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
- A **dry-run** mode (default ON) logs intended changes without writing.

> **Tip:** the automation enforces your Ecobee schedule's setpoints. For a tight
> hold near 70°F, set the active comfort setting's **Heat** and **Cool**
> setpoints close together (e.g. Heat 70 / Cool 71). A wide gap (Heat 68 /
> Cool 76) intentionally lets the temperature float within that range.

Why not Ecobee's built-in `auto` mode? It enforces a ~5°F minimum heat/cool
delta, so it can't hold a tight band — that wide swing is exactly the problem
this solves by switching the system mode instead.

## Architecture

A timer-triggered **Azure Functions** app (Python 3.12, v2 programming model,
Functions 4.x runtime), split into clean layers under `optimizer/`:

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
third-party Ecobee wrapper. The decision engine is pure and sits behind a
`ComfortStrategy` protocol, so an AI-driven strategy can be swapped in later
without touching the client or wiring.

## One-time Ecobee PIN authorization (manual)

Do this once to mint the initial **refresh token**. The automation never
performs the interactive PIN step — it only uses the refresh token to mint
short-lived access tokens (and persists the rotated refresh token each run).

> New Ecobee developer registrations may be closed (Generac ownership). If you
> already have an API key, use it. Your **API Key** is the `client_id` below.

1. **Register an app** on the Ecobee developer portal. Set the authorization
   method to **ecobee PIN** and copy the **API Key**.
2. **Request a PIN** (`scope=smartWrite` is required to change settings):
   ```bash
   curl "https://api.ecobee.com/authorize?response_type=ecobeePin&client_id=$API_KEY&scope=smartWrite"
   ```
   The response contains a `ecobeePin`, an authorization `code`, and an
   `expires_in`/`interval`.
3. **Authorize the PIN** in the Ecobee web portal: *My Apps → Add Application →*
   paste the PIN *→ Authorize*.
4. **Exchange the code for tokens** (within the interval):
   ```bash
   curl -X POST "https://api.ecobee.com/token?grant_type=ecobeePin&code=$CODE&client_id=$API_KEY"
   ```
   Save the `refresh_token` from the response.
5. **Store the secrets** — put the API key and refresh token in Key Vault
   (below), or in `local.settings.json` for local dev.

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
| `HOLD_TYPE` | `nextTransition` | Hold type for any setpoint holds |

NCRONTAB cheatsheet: hourly `0 0 * * * *`, every 30 min `0 */30 * * * *`,
every 4 hours `0 0 */4 * * *`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp local.settings.json.example local.settings.json   # fill in for local dev (gitignored)
pytest
```

For local runs, leave `KEY_VAULT_URI` empty and put the Ecobee client ID /
refresh token in the `ECOBEE_CLIENT_ID` / `ECOBEE_REFRESH_TOKEN` env-fallback
values. `local.settings.json` is gitignored — **never commit secrets**.

> Local rotation caveat: with the env fallback, a rotated refresh token only
> updates the in-process environment and won't persist across runs. Use Key
> Vault to persist rotations durably, or update `local.settings.json` manually.

Run the function host locally (requires
[Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)):

```bash
func start   # with OPTIMIZER_DRY_RUN=true it logs intended changes, no writes
```

## Key Vault setup

Store secrets in Key Vault and let the Function app read/write them via a
**managed identity**. The app needs **set** (not just get) so it can persist a
rotated refresh token.

```bash
# 1. Create the vault and add secrets
az keyvault create -n <vault> -g <rg> -l <region>
az keyvault secret set --vault-name <vault> --name ecobee-client-id     --value "$API_KEY"
az keyvault secret set --vault-name <vault> --name ecobee-refresh-token --value "$REFRESH_TOKEN"

# 2. Give the Function app a system-assigned identity
az functionapp identity assign -n <app> -g <rg>

# 3. Grant that identity get + set on secrets
#    RBAC vault:
az role assignment create --assignee <principalId> \
  --role "Key Vault Secrets Officer" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.KeyVault/vaults/<vault>"
#    (or access-policy vault: az keyvault set-policy -n <vault> --object-id <principalId> --secret-permissions get set)

# 4. Point the app at the vault
az functionapp config appsettings set -n <app> -g <rg> \
  --settings KEY_VAULT_URI="https://<vault>.vault.azure.net/"
```

## Deployment

CI is in `.github/workflows/`:

- **`tests.yml`** runs `pytest` on every push and pull request.
- **`deploy.yml`** deploys to Azure Functions on push to `main` using **OIDC
  federated credentials** (no stored publish profile).

One-time Azure + GitHub setup:

1. **Create an app registration** for GitHub to authenticate as, give its
   service principal access to the Function app's resource group (e.g.
   `Contributor`), and add a **federated credential** for this repo:
   - Issuer: `https://token.actions.githubusercontent.com`
   - Subject: `repo:RandyDanielson/hearth:ref:refs/heads/main`
   - Audience: `api://AzureADTokenExchange`
2. **Add GitHub repo secrets:** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`.
3. **Set the app name** in `deploy.yml` (`AZURE_FUNCTIONAPP_NAME`).
4. **Configure app settings** on the Function app (`KEY_VAULT_URI`,
   `OPTIMIZER_SCHEDULE`, `OPTIMIZER_DRY_RUN`, comfort tunables, …).
5. **Push to `main`.** Tests run, then Azure (Oryx) restores `requirements.txt`
   and deploys.

> **First deploy:** keep `OPTIMIZER_DRY_RUN=true`. Watch the logs (read →
> decided → "DRY-RUN would switch HVAC mode …"). When the decisions look right,
> set `OPTIMIZER_DRY_RUN=false` to let it actually switch the system mode.

## Tests

```bash
pytest
```

All tests mock HTTP — there are **no live Ecobee calls** in the test suite.
Coverage spans `auth` (refresh + rotation), `ecobee_client` (selection building,
request shaping, retries, error mapping), `comfort` (the changeover rule table),
`parsing`, and the `function_app` dry-run gate.
