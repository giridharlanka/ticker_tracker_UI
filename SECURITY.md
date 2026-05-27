# Security

## Where secrets live

| Secret | Storage | Notes |
|--------|-----------|--------|
| Fernet salt for `config.enc` | OS keychain: service `ticker-tracker`, user `config-key` | Combined with a machine fingerprint to derive the encryption key. |
| Google OAuth refresh/access tokens | OS keychain: service `ticker-tracker-google`, user `oauth-token` | JSON payload (access, refresh, expiry). |
| Paid FX API key (generic slot) | OS keychain: service `ticker-tracker`, user `fx-api-key` | Used when `fx_source` is not the free Frankfurter default. |
| Finance API keys (per source) | OS keychain: service `ticker-tracker`, user `finance-api-<source>` | e.g. `finance-api-alpha_vantage`. |
| Twelve Data API key | OS keychain: service `ticker-tracker-twelvedata`, user `api-key` | Written when Twelve Data is enabled in setup. |
| Open Exchange Rates app ID | OS keychain: service `ticker-tracker-oxr`, user `api-key` | Written when OXR is selected as FX source. |
| FMP API key | OS keychain: service `ticker-tracker-fmp`, user `api-key` | Optional fundamentals enrichment. |
| Gemini API key | OS keychain: service `ticker-tracker-gemini`, user `api-key` | Or `GEMINI_API_KEY` / `GOOGLE_API_KEY` in the environment. Prompts exclude holdings by default. |

## What is and is not on disk

- **On disk (encrypted):** `config.enc` — sheet IDs, column letters, `finance_sources`, `base_currency`, `email_ids` (addresses only, not mail passwords), `fx_source` name, market overrides, flags. **No** raw API keys or OAuth tokens inside this file.
- **On disk (sensitive client id):** `credentials.json` — Google Cloud **OAuth client** id/secret from the Desktop (or Web) app type. This is **not** an end-user password; treat it like a deployable client credential. Do not commit it; `.gitignore` excludes it from the repo tree when placed under the app config directory.
- **Not stored by this app:** Google account passwords, broker passwords, or third-party master keys outside the keychain rows above.

## Revoking Google OAuth access

1. Visit [Google Account → Security → Third-party apps with account access](https://myaccount.google.com/permissions) (or “Manage third-party access”).
2. Remove access for **Ticker Tracker** (or the name you gave the OAuth consent screen).
3. Locally, delete the keychain entry `ticker-tracker-google` / `oauth-token` (or run the app and sign in again after deleting `credentials.json` and re-running OAuth).

## Rotating finance / FX API keys

1. Obtain a new key from the provider’s dashboard.
2. Run **`ticker-tracker-setup`** (or `--setup` from the main app) and re-enter keys for the affected sources, **or** update the keychain manually with the same service/username scheme as in the table above.
3. Revoke the old key at the provider so it cannot be abused if leaked.

## Web setup UI

The optional Flask wizard binds to **127.0.0.1** by default. Do not expose it on `0.0.0.0` or port-forward it unless you understand the risk (local CSRF / LAN sniffing).

## Responsible disclosure

If you find a security vulnerability, please **report it privately** to the repository maintainers (use GitHub **Security → Advisories** or direct contact if published in the repo profile) with enough detail to reproduce before public disclosure. We will work on a fix and coordinate disclosure timing.

## Secret scanning

This repository uses **TruffleHog** in CI to reduce the chance of accidental credential commits. Local runs are optional:

```bash
docker run --rm -v "$PWD:/pwd" -w /pwd ghcr.io/trufflesecurity/trufflehog:latest filesystem . --fail
```
