# ticker-tracker

**ticker-tracker** reads your holdings from **Google Sheets**, fetches live prices (**Yahoo Finance** plus optional **Finnhub**, **Alpha Vantage**, **Twelve Data**), converts everything to your chosen **base currency** using **ECB-based FX** (Frankfurter) or **Open Exchange Rates**, and produces an **Excel report** e‑mailed through **Gmail** (optional **Google Drive** upload is configured in setup). Configuration and API keys stay on your machine: **encrypted config** plus the **OS keychain**.

---

## Prerequisites

- **Python 3.11+** (3.11 or newer recommended; matches CI and type checking).
- A **Google account** you can use for Cloud billing (no charge for the APIs used at typical personal volumes) and OAuth.
- Optional: API keys for paid finance or FX providers if you enable those sources.

---

## Google Cloud setup (step by step)

1. Open [Google Cloud Console](https://console.cloud.google.com/) and **Create project** (any name, e.g. `ticker-tracker`).
2. **APIs & Services → Library** — enable each API:
   - **Google Sheets API** (read your portfolio tab).
   - **Google Drive API** (upload the generated `.xlsx`).
   - **Gmail API** (send the report as an attachment).
3. **APIs & Services → OAuth consent screen**
   - User type: **External** (or Internal if Workspace-only).
   - Add scopes (or rely on defaults when the app requests them): the app requests  
     `spreadsheets.readonly`, `drive.file`, `gmail.send` (see `ticker_tracker/google/auth.py`).
   - Add yourself as a **test user** while the app is in *Testing* mode.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type: **Desktop app** (recommended for the installed flow).
   - Download the JSON and save it as **`credentials.json`** in the app config directory  
     (`~/Library/Application Support/ticker-tracker/` on macOS, or see `ticker_tracker.config.application_config_dir`).
5. First run of Sheets/Drive/Gmail will open a **browser OAuth** window; approve access. Tokens are stored in the **OS keychain**, not in `credentials.json`.

---

## Installation

```bash
git clone https://github.com/giridharlanka/ticker_tracker_UI.git
cd ticker_tracker_UI
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# or install the package (registers ticker-tracker CLI):
pip install .
```

**Contributors / CI parity:**

```bash
pip install -r requirements.txt -r requirements-dev.txt
# equivalent:
pip install -e ".[dev]"
```

Use `pip install -e .` if you want an editable checkout while iterating.

### Optional environment file

Copy `.env.example` to `.env` for local secrets (e.g. `GEMINI_API_KEY`). `.env` is gitignored. The dashboard loads it on startup when you run `python app.py`.

### Where data lives (not in the repo)

| Item | Typical location (macOS) |
|------|---------------------------|
| Encrypted settings | `~/Library/Application Support/ticker-tracker/config.enc` |
| Google OAuth client | `~/Library/Application Support/ticker-tracker/credentials.json` |
| API keys | macOS Keychain (see [SECURITY.md](SECURITY.md)) |

---

## Quick start (dashboard)

After installation and [Google Cloud setup](#google-cloud-setup-step-by-step):

```bash
source .venv/bin/activate
python main.py --setup          # first time: wizard + credentials.json
python app.py                   # http://127.0.0.1:5225/
```

1. Open **Settings** — sheet ID, columns, email, analysis provider (Ollama or Gemini).  
2. Place **`credentials.json`** in the app config directory (see table above).  
3. Click **Analyse** on the Run panel.

**Headless one-shot run:**

```bash
ticker-tracker --run
ticker-tracker --run --no-analysis   # skip LLM / fundamentals
```

Run the **setup wizard** (stores encrypted `config.enc` and keychain entries):

```bash
python main.py --setup
# or
ticker-tracker --setup
# or (same as ticker-tracker-setup)
ticker-tracker-setup
```

**Daily use:** run `ticker-tracker` or `python main.py` for the **Tk** popup (`Run` / `Skip`). Headless: `ticker-tracker --run` or `python -m ticker_tracker --run`.

**Email notifications:** every address you save in setup is sent the report on each successful run (the GUI shows the full list; it no longer limits sends to a single “profile”).

**Output files:** choose one or both formats in setup:
- `xlsx` (Excel workbook)
- `html` (standalone local HTML report)

Reports are written to your configured `local_report_dir`; if blank, the app uses your OS temp directory.

### Web dashboard (browser UI)

From the repository root, after `pip install .` (Flask is included in the default dependencies):

```bash
python app.py
```

Then open **http://127.0.0.1:5225/** (localhost only). Override the bind address or port with **`TICKER_DASHBOARD_HOST`** and **`TICKER_DASHBOARD_PORT`** if needed.

The dashboard is a **single-process** local server: it serves the HTML/CSS/JS under `ticker_tracker/web/dashboard/` and calls the same portfolio engine as the CLI.

- **Settings** — Edit and save the same options as the setup wizard (`config.enc` plus keychain API keys), including **Analysis** (enable/disable, **Ollama** or **Gemini Flash**, models, optional FMP API key). Holdings are **not** sent to the LLM by default. Local file paths and the on-disk report folder are controlled in the **Run** panel per analysis (saved values in `config.enc` are still used to prefill the Run panel and for CLI/Tk). Secret keys are never shown in full; you can **keep**, **replace**, or **clear** each key.
- **Run** — **Output folder** (where HTML/XLSX are written for that run) with optional **Browse…** (native folder picker via the local Python process). For **upload**, set a **path on this computer**, browse to a file, and/or drag-and-drop; set the **XLSX tab** name when needed. **Analyse** from **Google Sheets** uses the saved Sheet ID and column map, or from **upload** for a one-off file. The preview always shows **HTML**; an **Excel** file is written only if **`xlsx`** is enabled in Settings (same as other runs).
- **Send email after analysis** — Checked by default; uses the **Gmail API** and the addresses in Settings. For uploaded files, this opts in to mail even though CLI/Tk runs with `local_file` normally skip notifications.
- **Email report** — Sends the **HTML body** of the last successful preview to every address in Settings (no attachment).

You still need **`credentials.json`** in the app config directory (see [Google Cloud setup](#google-cloud-setup-step-by-step)) for Sheets, optional Drive upload, and Gmail.

**Review saved settings** (read-only JSON; same non-secret fields as `config.enc`):

```bash
ticker-tracker --show-config
ticker-tracker --show-config --web
# optional bind (default 127.0.0.1:8767):
ticker-tracker --show-config --web --show-config-host 127.0.0.1 --show-config-port 8768
```

**Other browser tools:** `ticker-tracker-setup --web` opens the **setup-only** form; `--show-config --web` is **read-only**. Use **`python app.py`** for the full dashboard (preview, uploads, and editable Settings).

---

## Google Sheets format

Row **1** is treated as a **header** (ignored for data). Data starts at row **2**.

| Column (logical name) | Required | Example | Description |
|----------------------|----------|---------|-------------|
| **ticker** | Yes | `AAPL` or `D05` | Symbol for the instrument. If you use **exchange**, you can use the local code (e.g. `D05` on SGX); otherwise use the full symbol your price source expects (see [Ticker format](#ticker-format--suffix-guide)). |
| **exchange** | No | `SGX` or `NYSE` | Listing venue (common names or MIC-style codes). Used to build the **price** symbol (e.g. `D05` + SGX → `D05.SI` for Yahoo) and, when the price API does not return a currency, to guess **quote currency**. |
| **shares** | Yes | `10` | Number of shares or units. |
| **cost_basis** | Yes | `150` | **Cost per share** in **purchase_currency** if that column is mapped, otherwise in your **base currency**. Total row cost is `shares × cost_basis`, then converted to base when `purchase_currency` is set. |
| **purchase_currency** | No | `SGD` | ISO 4217 code for the currency you paid in for that row’s cost. Leave blank to treat `cost_basis` as already in base currency. |
| **currency_override** | No | `HKD` | If set, forces **listing / quote** currency for that row when the suffix, exchange hint, or provider is ambiguous. See [multi-currency doc](docs/multi_currency.md#cost-basis-vs-currency-override). |

**Sample rows** (base currency e.g. **SGD**; first row uses cost in SGD via `purchase_currency`):

| ticker | exchange | shares | cost_basis | purchase_currency | currency_override |
|--------|----------|--------|------------|-------------------|---------------------|
| AAPL   |          | 5      | 240        | USD               |                     |
| D05    | SGX      | 2000   | 9.25       | SGD               |                     |
| 0005   | HKEX     | 400    | 800        | HKD               |                     |
| SHEL   | LSE      | 150    | 15         | GBP               |                     |

---

## Local holdings file format (CSV/XLSX)

You can set `holdings_source` to **`local_file`** in setup and point the app to:
- a **CSV** file (column mapping uses header names), or
- an **XLSX** file (column mapping uses column letters like `A`, `B`, `C`).

Required mapped fields:
- `ticker`
- `shares`
- `cost_basis`

Optional mapped fields:
- `exchange`
- `purchase_currency`
- `currency_override`

Notes:
- For XLSX, the sheet/tab name defaults to `Holdings` (configurable).
- For **CLI / Tk** runs with `holdings_source` **`local_file`**, **Google Drive** and **Gmail** are disabled. The **web dashboard** can still **upload** a file and optionally **send mail** after analysis (see [Web dashboard](#web-dashboard-browser-ui)).
- Relative local paths are resolved from the current working directory.

**Example mixed portfolio (listing currencies):**

| Ticker   | Listing CCY | Notes |
|----------|---------------|--------|
| **AAPL** | USD | US listing. |
| **D05.SI** | SGD | Singapore listing (DBS). |
| **0005.HK** | HKD | Hong Kong listing (HSBC). |
| **SHEL.L** | GBP | London listing; see **LSE pence** note below. |

---

## Finance sources

| Config id | Description | API key | Get started |
|-----------|-------------|---------|-------------|
| **yahoo** | Yahoo Finance (via `yfinance`) | No | [yfinance](https://github.com/ranaroussi/yfinance) |
| **finnhub** | [Finnhub](https://finnhub.io/) quote + profile (currency); **ETF profile** if company profile omits currency | Yes (keychain `finance-api-finnhub`) | Per-symbol fallback when Yahoo omits a ticker; see **Finnhub client behaviour** below. |
| **alpha_vantage** | Alpha Vantage GLOBAL_QUOTE + symbol search | Yes (keychain `finance-api-alpha_vantage`) | [Alpha Vantage](https://www.alphavantage.co/support/#api-key) |
| **twelve_data** | Twelve Data `/quote` | Yes (keychain `ticker-tracker-twelvedata`) | [Twelve Data](https://twelvedata.com/) |
| **polygon** | — | — | Reserved in setup; not implemented yet. |

**Source order (`finance_sources` in config):** list ids **top to bottom** — that is try-first → try-last. The engine **merges** prices: each source is asked only for symbols still missing; e.g. Yahoo fills most rows, then Finnhub is called for the remainder, then Alpha Vantage for any still missing. Each holding’s `PriceResult.source` records which provider supplied that quote.

**Batch behaviour:** **Finnhub**, **Alpha Vantage**, and **Twelve Data** resolve **each symbol independently** — one unknown or failing ticker does not prevent the rest of that batch from returning quotes.

**Finnhub client behaviour:** Matches the official [finnhub-python](https://github.com/Finnhub-Stock-API/finnhub-python) base URL order: **`api.finnhub.io`** first, then **`finnhub.io`**. Retries with backoff on transient errors (**429**, **502–504**, network). Quote uses **current / prior close / open / high / low** (first positive value). If the profile request fails after a good quote, the row still gets a price with **USD** as currency unless another source filled it earlier in the merge.

**One ticker shows “—” for price while others work:** the sheet symbol often does not match what APIs expect (e.g. iShares **CSPX** on the London Stock Exchange is usually quoted as **`CSPX.L`** on Yahoo / many feeds, not bare `CSPX`). Set **exchange** (e.g. LSE) or put the **full symbol** your provider uses in **ticker**. Backup sources only help when they recognise the same symbol string.

---

## FX rate sources

| Config id | API key | Default? | Notes |
|-----------|---------|----------|--------|
| **frankfurter** | No | **Yes** | ECB-oriented rates from [Frankfurter](https://www.frankfurter.app/); batch fetch per run. |
| **open_exchange_rates** | Yes (`ticker-tracker-oxr` + main FX slot) | No | [Open Exchange Rates](https://openexchangerates.org/signup); free tier is USD-based; app cross-rates to your base. |
| **fixer** / **currencylayer** | Would use main FX slot | — | Shown in setup; **adapters not implemented** — use Frankfurter or OXR. |

Details: [docs/multi_currency.md](docs/multi_currency.md#fx-rate-sources-comparison).

---

## Multi-currency & base reporting

Your **base currency** (e.g. **SGD**) is the currency of **totals**, **cost basis** (as entered), and the **Excel** report headers. Each holding’s **native** price currency comes from the provider or suffix rules; the engine **converts** native amounts to base using the FX source you picked.

Full walkthrough: **[docs/multi_currency.md](docs/multi_currency.md)**.

---

## Ticker format & suffix guide

Suffixes match **longest** pattern on the ticker (case-insensitive). Built-in defaults (overridable in setup):

| Suffix | Exchange (typical) | CCY | Example tickers |
|--------|---------------------|-----|-------------------|
| `.SI` | Singapore (SGX) | SGD | `D05.SI`, `O39.SI` |
| `.L` | London (LSE) | GBP | `SHEL.L`, `VOD.L` |
| `.HK` | Hong Kong (HKEX) | HKD | `0005.HK`, `0700.HK` |
| `.AX` | Australia (ASX) | AUD | `BHP.AX` |
| `.T` | Japan (TSE) | JPY | `7203.T` |
| `.TO` | Canada (TSX) | CAD | `SHOP.TO` |
| `.NS` | India (NSE) | INR | `RELIANCE.NS` |
| `.KL` | Malaysia (Bursa) | MYR | `MAYBANK.KL` |
| `.DE` | Xetra | EUR | `SAP.DE` |
| `.PA` | Euronext Paris | EUR | `OR.PA` |

### LSE pence (GBX) correction

Some UK feeds quote in **GBX** (pence). The app normalises **GBX → GBP** (÷100) before FX so totals stay in major pounds. If something still looks off for a `.L` line, set **currency override** and verify the symbol on your price source.

---

## Startup configuration

In the setup wizard, choose **Upload report to Google Drive** (Yes/No). When **No**, the workbook is still built and attached to the notification email only.

Enable **Run on startup** to register a **headless** run at login:

- **macOS:** `~/Library/LaunchAgents/com.ticker-tracker.portfolio.plist` + `launchctl bootstrap`.
- **Windows:** `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → `TickerTracker`.
- **Linux:** `~/.config/systemd/user/ticker-tracker.service` + `systemctl --user enable`.

The command is **`python -m ticker_tracker --run`** (same interpreter you used to install). Override with env **`TICKER_TRACKER_STARTUP_CMD`** (shell-split) if you need a specific venv path.

---

## Troubleshooting

### OAuth / Google

| Symptom | What to do |
|---------|------------|
| **`redirect_uri_mismatch`** | Desktop client should use **loopback** redirect; use the installed-app flow from this project. Recreate OAuth client as **Desktop**. |
| **`access_blocked` / “app not verified”** | On *Testing* consent screen, add your Google account as a **test user**, or publish the app (stricter verification for sensitive scopes). |
| **`invalid_client`** | `credentials.json` must match the OAuth client; re-download from Cloud Console. |
| **Sheets “not found”** | Check spreadsheet ID and that the account you OAuth’d has access. |

### Rates / providers

| Symptom | What to do |
|---------|------------|
| **Alpha Vantage “Thank you for using…”** | Free tier rate limit; wait or add a paid key / another finance source. |
| **Finnhub 429 / 5xx** | The client **retries** a few times with backoff and may switch host; if it persists, wait or upgrade the Finnhub plan. |
| **Finnhub “rejected” / HTTP 401–403** | Bad or missing API key — re-enter the key in setup (keychain `finance-api-finnhub`). |
| **Twelve Data 429 / errors** | Free tier ~**8 req/min**; reduce symbols or upgrade. |
| **Frankfurter / FX errors offline** | Check network; **forex-python** fallback may also hit the network. |
| **FX source `fixer` / `currencylayer`** | Not implemented — switch to **frankfurter** or **open_exchange_rates** in setup. |

### Config / keychain

| Symptom | What to do |
|---------|------------|
| **Cannot decrypt `config.enc`** | Wrong machine, missing keychain salt (`config-key`), or corrupt file — see [SECURITY.md](SECURITY.md). |

---

## Project layout (short)

| Path | Role |
|------|------|
| `app.py` | Start the **web dashboard** (`python app.py` → port 5225 by default). |
| `ticker_tracker/main.py` | CLI / GUI entry (`ticker-tracker`). |
| `ticker_tracker/engine.py` | Sheets → FX → prices → XLSX → optional Drive → Gmail. |
| `ticker_tracker/web/dashboard_server.py` | Flask app for the dashboard (API + static files). |
| `ticker_tracker/web/dashboard/` | Dashboard **HTML / CSS / JS** (`index.html`, `css/`, `js/`). |
| `ticker_tracker/finance/`, `ticker_tracker/fx/` | Price and FX adapters + registries. |
| `ticker_tracker/ui/popup.py` | Tk “Portfolio Tracker” window. |
| `ticker_tracker/ui/startup_registration.py` | OS login registration. |
| `docs/multi_currency.md` | Deep dive on currencies and FX. |

---

## Analysis Module

When enabled, Ticker Tracker fetches fundamental data and computes technical indicators for each holding, then synthesises a BUY/HOLD/SELL signal using an LLM. Choose **Ollama** (fully local, e.g. Qwen or Gemma) or **Gemini Flash** (Google API; ticker + fundamentals + technicals only — **no holdings in the prompt** unless you opt in).

### Requirements

**Ollama** (default provider):

- [Ollama](https://ollama.ai) with your model pulled, e.g. `ollama pull qwen2.5:7b` or `ollama pull gemma3:4b`
- Host reachable at the configured **Ollama URL** (default `http://localhost:11434`)

**Gemini Flash**:

- API key from [Google AI Studio](https://aistudio.google.com/) (keychain or `GEMINI_API_KEY` / `GOOGLE_API_KEY`)
- Model default: `gemini-2.0-flash` (free tier; analysis spaces requests ~6s apart)

### What is analysed

**Fundamentals** (via yfinance, with optional FMP Cloud fallback):

P/E, P/B, EV/EBITDA, margins, growth rates, debt ratios, analyst consensus, earnings surprise, recent news headlines.

**Technical indicators** (computed locally via pandas-ta-classic):

EMA (20/50/200), RSI(14), MACD(12/26/9), Bollinger Bands (20,2), ATR(14), OBV, volume vs 20-day average.

**LLM synthesis** (Ollama local, or Gemini cloud):

Each ticker receives a BUY/HOLD/SELL signal, confidence level, three strengths, three risks, and a two-sentence summary. The portfolio summary uses prior per-ticker signals only (not raw holdings). Optional setting adds position size to prompts (intended for local Ollama).

### Data quality notes

- **US tickers** (NYSE/NASDAQ): full fundamental data is usually available.
- **SGX/HKEx tickers**: fundamentals may be sparse via yfinance alone; add an **FMP Cloud** API key in setup for richer coverage.
- **LSE tickers** (`.L`): supported; pence correction applies as usual.
- Analysis does **not** change the Holdings or Summary sheets — it adds **Analysis** and **Portfolio Signals** sheets (and HTML sections) only.
- To disable analysis for a faster run: set `analysis_enabled` to false in the setup wizard, or run:

  ```bash
  ticker-tracker --run --no-analysis
  ```

See **[docs/analysis.md](docs/analysis.md)** for how indicators, prompts, and signals work, plus troubleshooting.

---

## Development

```bash
make install    # venv + editable install with dev + web extras
make setup      # ticker-tracker --setup (CLI wizard)
make run        # ticker-tracker (Tk popup)
python app.py   # web dashboard (see [Web dashboard](#web-dashboard-browser-ui))
make test       # pytest + coverage
make lint       # ruff check + format check
make clean      # caches + coverage artifacts
make typecheck  # mypy (same as CI typecheck job)
```

CI (GitHub Actions): **Ruff**, **mypy**, **pytest** with coverage, and **TruffleHog** secret scanning on `push` / `pull_request` to **`main`**.

---

## Security & disclosure

See **[SECURITY.md](SECURITY.md)** for where secrets live, what is on disk, how to revoke OAuth, rotate API keys, and **responsible disclosure**.

---

## License

Specify your license in `LICENSE` (e.g. MIT) when you publish the repo.
