"""Tabbed portfolio HTML report (Excel sheet parity + Bloomberg-style drill-down)."""

from __future__ import annotations

import html
import json
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from ticker_tracker.analysis.base import FundamentalsResult, LLMAnalysis, TechnicalResult
from ticker_tracker.calculator import purchase_amount_lines_by_ccy
from ticker_tracker.fx.base import FXRate
from ticker_tracker.report_builder import _ANALYSIS_HEADERS, _HEATMAP_COLUMNS, _heatmap_signals

_TAB_IDS = ("holdings", "summary", "metadata", "analysis", "signals")


def _esc(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def _fmt_num(value: Any) -> str:
    if value is None or value == "—":
        return "—"
    if isinstance(value, int | float):
        return f"{float(value):,.2f}"
    return _esc(value)


def _fmt_shares(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, int | float):
        return f"{float(value):,.4f}".rstrip("0").rstrip(".")
    return _esc(value)


def _fmt_pct_html(value: Any) -> str:
    if not isinstance(value, int | float):
        return _esc(value)
    x = float(value)
    s = f"{x:,.2f}%"
    esc = html.escape(s)
    if x > 0:
        return f'<span class="pos">{esc}</span>'
    if x < 0:
        return f'<span class="neg">{esc}</span>'
    return esc


def _signal_class(signal: str) -> str:
    s = (signal or "").upper()
    if s == "BUY":
        return "signal-buy"
    if s == "SELL":
        return "signal-sell"
    if s == "HOLD":
        return "signal-hold"
    return "signal-muted"


def _summary_table_rows(summary: Mapping[str, Any], base_upper: str) -> list[list[str]]:
    bu = base_upper.upper()
    rows: list[list[str]] = []
    cost_map: dict[str, float] = dict(summary.get("totals_purchase_cost_by_ccy") or {})
    val_map: dict[str, float] = dict(summary.get("totals_purchase_value_by_ccy") or {})
    gl_map: dict[str, float] = dict(summary.get("totals_purchase_gl_by_ccy") or {})

    def fmt_base(v: Any) -> str:
        if isinstance(v, int | float):
            return f"{float(v):,.2f} {bu}"
        return _esc(v)

    if not cost_map and summary.get("totals_purchase_cost_formatted"):
        rows.append(
            [
                "Total invested",
                fmt_base(summary.get("total_cost_basis_base")),
                _esc(summary.get("totals_purchase_cost_formatted")),
            ]
        )
        rows.append(
            [
                "Current value",
                fmt_base(summary.get("total_current_value_base")),
                _esc(summary.get("totals_purchase_value_formatted")),
            ]
        )
        rows.append(
            [
                "Gain / loss",
                fmt_base(summary.get("total_gain_loss_base")),
                _esc(summary.get("totals_purchase_gl_formatted")),
            ]
        )
    else:
        for label, base_key, pmap in (
            ("Total invested", "total_cost_basis_base", cost_map),
            ("Current value", "total_current_value_base", val_map),
            ("Gain / loss", "total_gain_loss_base", gl_map),
        ):
            lines = purchase_amount_lines_by_ccy(pmap)
            rows.append([label, fmt_base(summary.get(base_key)), _esc(lines[0] if lines else "—")])
            for ln in lines[1:]:
                rows.append(["", "", _esc(ln)])

    tr = summary.get("total_return_pct")
    rows.append(["Total return %", _fmt_pct_html(tr), _fmt_pct_html(tr)])
    cnt = int(summary.get("distinct_ticker_count") or summary.get("holding_count") or 0)
    rows.append(["Holdings count (distinct tickers)", _esc(cnt), "—"])
    for note in summary.get("assumption_notes") or []:
        rows.append([_esc(note), "", ""])
    return rows


def _table(headers: list[str], rows: list[list[str]], *, extra_class: str = "") -> str:
    cls = f"tt-table {extra_class}".strip()
    out = [f'<table class="{cls}"><thead><tr>']
    for h in headers:
        out.append(f"<th>{_esc(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _metadata_rows(metadata: Mapping[str, Any], base: str) -> list[list[str]]:
    rows: list[list[str]] = [
        ["Run timestamp (UTC)", _esc(metadata.get("run_timestamp_utc", ""))],
        ["Base currency", _esc(base.upper())],
        ["FX source", _esc(metadata.get("fx_source", ""))],
    ]
    for fx in metadata.get("fx_rates") or []:
        if isinstance(fx, FXRate):
            rows.append(
                [
                    "FX rate",
                    f"{fx.from_currency} → {fx.to_currency}",
                    f"{fx.rate} ({fx.source}, {fx.fetched_at.astimezone(UTC).isoformat()})",
                ]
            )
        elif isinstance(fx, dict):
            rows.append(
                [
                    "FX rate",
                    f"{fx.get('from_currency')} → {fx.get('to_currency')}",
                    f"{fx.get('rate')} ({fx.get('source')}, {fx.get('fetched_at')})",
                ]
            )
    rows.append(
        ["Price fetch failed", _esc(", ".join(metadata.get("price_fetch_failed") or []) or "—")]
    )
    rows.append(
        ["FX unavailable", _esc(", ".join(metadata.get("fx_unavailable_tickers") or []) or "—")]
    )
    rows.append(
        [
            "Cost FX unavailable",
            _esc(", ".join(metadata.get("cost_fx_unavailable_tickers") or []) or "—"),
        ]
    )
    return rows


def _finance_source_rows(metadata: Mapping[str, Any]) -> list[list[str]]:
    return [
        [_esc(t), _esc(src)]
        for t, src in sorted((metadata.get("finance_source_by_ticker") or {}).items())
    ]


def _analysis_cells(
    ticker: str,
    *,
    fund: FundamentalsResult | None,
    tech: TechnicalResult | None,
    analysis: LLMAnalysis | None,
    pending: bool,
) -> list[str]:
    if pending and fund is None and tech is None:
        return [_esc(ticker)] + ["<span class='pending'>Loading…</span>"] * (
            len(_ANALYSIS_HEADERS) - 1
        )

    signal = (
        analysis.signal
        if analysis and analysis.llm_available
        else ("LLM OFFLINE" if analysis is None and not pending else "—")
    )
    sig_html = f'<span class="{_signal_class(signal)}">{_esc(signal)}</span>'

    def _f(v: float | None, fmt: str = ".2f", suffix: str = "") -> str:
        return f"{v:{fmt}}{suffix}" if v is not None else "—"

    return [
        _esc(ticker),
        sig_html,
        _esc(analysis.confidence if analysis and analysis.llm_available else "—"),
        _f(tech.rsi_14 if tech else None, ".1f"),
        _f(tech.macd_histogram if tech else None, ".4f"),
        (
            f"{tech.price_vs_ema200_pct:+.1f}%"
            if tech and tech.price_vs_ema200_pct is not None
            else "—"
        ),
        _f(fund.pe_ratio if fund else None),
        _f(fund.pb_ratio if fund else None),
        (f"{fund.net_margin * 100:.1f}%" if fund and fund.net_margin is not None else "—"),
        (
            f"{fund.revenue_growth_yoy * 100:.1f}%"
            if fund and fund.revenue_growth_yoy is not None
            else "—"
        ),
        _f(fund.debt_to_equity if fund else None),
        _esc(fund.analyst_recommendation if fund and fund.analyst_recommendation else "—"),
        _f(fund.target_price if fund else None),
        (
            f"{fund.last_earnings_surprise_pct:+.1f}%"
            if fund and fund.last_earnings_surprise_pct is not None
            else "—"
        ),
        _esc(" | ".join(analysis.strengths) if analysis and analysis.llm_available else "—"),
        _esc(" | ".join(analysis.risks) if analysis and analysis.llm_available else "—"),
        _esc(
            analysis.summary
            if analysis and analysis.llm_available
            else ("LLM offline — per-ticker analysis unavailable." if not pending else "—")
        ),
        (
            "FULL"
            if fund and fund.fundamentals_available
            else (f"SPARSE — {ticker}" if fund else "—")
        ),
    ]


def _ticker_detail_payload(
    ticker: str,
    *,
    holding: Mapping[str, Any] | None,
    fund: FundamentalsResult | None,
    tech: TechnicalResult | None,
    analysis: LLMAnalysis | None,
) -> dict[str, Any]:
    def _ser_fund(f: FundamentalsResult | None) -> dict[str, Any]:
        if f is None:
            return {}
        return {
            "pe_ratio": f.pe_ratio,
            "pb_ratio": f.pb_ratio,
            "ev_ebitda": f.ev_ebitda,
            "dividend_yield": f.dividend_yield,
            "net_margin": f.net_margin,
            "revenue_growth_yoy": f.revenue_growth_yoy,
            "debt_to_equity": f.debt_to_equity,
            "roe": f.roe,
            "analyst_recommendation": f.analyst_recommendation,
            "target_price": f.target_price,
            "last_earnings_surprise_pct": f.last_earnings_surprise_pct,
            "recent_headlines": list(f.recent_headlines or [])[:5],
            "warnings": list(f.warnings or []),
            "fundamentals_available": f.fundamentals_available,
        }

    def _ser_tech(t: TechnicalResult | None) -> dict[str, Any]:
        if t is None:
            return {}
        return {
            "rsi_14": t.rsi_14,
            "macd_histogram": t.macd_histogram,
            "price_vs_ema200_pct": t.price_vs_ema200_pct,
            "ema_20": t.ema_20,
            "ema_50": t.ema_50,
            "ema_200": t.ema_200,
            "signals": [{"name": s.name, "bullish": s.bullish} for s in (t.signals or [])],
            "warnings": list(t.warnings or []),
        }

    def _ser_llm(a: LLMAnalysis | None) -> dict[str, Any]:
        if a is None:
            return {}
        return {
            "signal": a.signal,
            "confidence": a.confidence,
            "strengths": list(a.strengths),
            "risks": list(a.risks),
            "summary": a.summary,
            "llm_available": a.llm_available,
        }

    h = holding or {}
    return {
        "ticker": ticker,
        "holding": {
            "shares": h.get("shares"),
            "report_ccy": h.get("report_ccy"),
            "cost_basis_purchase": h.get("cost_basis_purchase"),
            "current_value_purchase": h.get("current_value_purchase"),
            "gain_loss_purchase": h.get("gain_loss_purchase"),
            "gain_loss_pct_purchase": h.get("gain_loss_pct_purchase"),
        },
        "fundamentals": _ser_fund(fund),
        "technicals": _ser_tech(tech),
        "llm": _ser_llm(analysis),
    }


_TABBED_CSS = """
:root {
  --bg: #0a0e14;
  --panel: #121820;
  --panel2: #1a2332;
  --border: #2a3544;
  --text: #e8edf4;
  --muted: #8b9cb3;
  --accent: #ff6b00;
  --pos: #3fb950;
  --neg: #f85149;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--text); }
.tt-report { display: flex; flex-direction: column; min-height: 100vh; }
.tt-top { padding: 0.6rem 1rem; background: var(--panel2); border-bottom: 1px solid var(--border); }
.tt-top h1 { margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: 0.02em; }
.tt-top .sub { color: var(--muted); font-size: 0.75rem; margin-top: 0.15rem; }
.tt-tabs { display: flex; gap: 0; background: var(--panel); border-bottom: 1px solid var(--border); overflow-x: auto; }
.tt-tab {
  padding: 0.55rem 1rem; border: none; background: transparent; color: var(--muted);
  font-size: 0.78rem; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent;
  white-space: nowrap;
}
.tt-tab:hover { color: var(--text); }
.tt-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tt-tab .badge { font-size: 0.65rem; margin-left: 0.35rem; opacity: 0.85; }
.tt-tab[data-ready="0"] .badge { color: var(--muted); }
.tt-body { display: flex; flex: 1; min-height: 0; }
.tt-panels { flex: 1; overflow: auto; padding: 0.75rem 1rem; }
.tt-panel { display: none; }
.tt-panel.active { display: block; }
.tt-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.tt-table th, .tt-table td { border: 1px solid var(--border); padding: 0.35rem 0.5rem; text-align: left; }
.tt-table th { background: var(--panel2); color: var(--muted); font-weight: 600; position: sticky; top: 0; }
.tt-table tbody tr.tt-holding-row { cursor: pointer; }
.tt-table tbody tr.tt-holding-row:hover { background: rgba(255,107,0,0.08); }
.tt-table tbody tr.tt-holding-row.selected { outline: 1px solid var(--accent); background: rgba(255,107,0,0.12); }
.tt-table .num { font-variant-numeric: tabular-nums; font-family: ui-monospace, Menlo, monospace; }
.pos { color: var(--pos); font-weight: 600; }
.neg { color: var(--neg); font-weight: 600; }
.signal-buy { color: var(--pos); font-weight: 700; }
.signal-sell { color: var(--neg); font-weight: 700; }
.signal-hold { color: #d4a72c; font-weight: 700; }
.signal-muted { color: var(--muted); }
.pending { color: var(--muted); font-style: italic; }
.tt-detail {
  width: 340px; max-width: 42vw; border-left: 1px solid var(--border);
  background: var(--panel); overflow-y: auto; padding: 0.75rem; flex-shrink: 0;
}
.tt-detail.hidden { display: none; }
.tt-detail h2 { margin: 0 0 0.5rem; font-size: 1.1rem; color: var(--accent); }
.tt-detail h3 { margin: 0.75rem 0 0.35rem; font-size: 0.72rem; text-transform: uppercase; color: var(--muted); }
.tt-detail dl { margin: 0; font-size: 0.78rem; }
.tt-detail dt { color: var(--muted); margin-top: 0.35rem; }
.tt-detail dd { margin: 0.1rem 0 0; font-family: ui-monospace, Menlo, monospace; }
.tt-detail ul { margin: 0.25rem 0; padding-left: 1.1rem; font-size: 0.78rem; }
.tt-detail .close { float: right; background: transparent; border: 1px solid var(--border); color: var(--text); cursor: pointer; padding: 0.2rem 0.45rem; border-radius: 3px; font-size: 0.7rem; }
.tt-empty { color: var(--muted); font-style: italic; padding: 1rem 0; }
.tt-subhead { font-size: 0.85rem; color: var(--muted); margin: 1rem 0 0.35rem; }
.tt-perf-gainers tbody { background: rgba(63,185,80,0.12); }
.tt-perf-losers tbody { background: rgba(248,81,73,0.12); }
.tt-heatmap .bull { background: rgba(63,185,80,0.25); }
.tt-heatmap .bear { background: rgba(248,81,73,0.25); }
.tt-holdings-toolbar {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 0.5rem; margin-bottom: 0.5rem;
}
.tt-holdings-note { font-size: 0.72rem; color: var(--muted); margin: 0; flex: 1; min-width: 12rem; }
.tt-view-toggle { display: inline-flex; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.tt-view-btn {
  padding: 0.35rem 0.65rem; border: none; background: var(--panel2); color: var(--muted);
  font-size: 0.72rem; font-weight: 600; cursor: pointer;
}
.tt-view-btn.active { background: var(--accent); color: #0a0e14; }
.tt-view-btn:hover:not(.active) { color: var(--text); }
.holdings-view { display: none; }
.holdings-view.active { display: block; }
.tt-table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0.5rem 0; }
@media (max-width: 768px) {
  .tt-tabs { flex-wrap: nowrap; }
  .tt-tab { padding: 0.5rem 0.65rem; font-size: 0.7rem; }
  .tt-body { flex-direction: column; }
  .tt-detail {
    width: 100%; max-width: none; border-left: none;
    border-top: 1px solid var(--border); max-height: 42vh;
  }
  .tt-panels { padding: 0.5rem; }
  .tt-table { font-size: 0.72rem; min-width: 520px; }
  .tt-top h1 { font-size: 0.9rem; }
}
@media (max-width: 480px) {
  .tt-tab .badge { display: block; margin: 0.15rem 0 0; }
}
"""

_TABBED_JS = """
(function () {
  const data = JSON.parse(document.getElementById("tt-ticker-data").textContent || "{}");
  const panels = document.querySelectorAll(".tt-panel");
  const tabs = document.querySelectorAll(".tt-tab");
  const detail = document.getElementById("tt-detail");
  const detailBody = document.getElementById("tt-detail-body");
  let selectedRow = null;

  function showTab(id) {
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === id));
    panels.forEach((p) => p.classList.toggle("active", p.id === "tab-" + id));
    try { sessionStorage.setItem("tt-active-tab", id); } catch (e) {}
  }
  tabs.forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  try {
    const saved = sessionStorage.getItem("tt-active-tab");
    if (saved) showTab(saved);
  } catch (e) {}
  window.addEventListener("message", (ev) => {
    if (ev.data && ev.data.type === "tt-show-tab" && ev.data.tab) showTab(ev.data.tab);
    if (ev.data && ev.data.type === "tt-holdings-view" && ev.data.view) {
      const btn = document.querySelector('.tt-view-btn[data-view="' + ev.data.view + '"]');
      if (btn) btn.click();
    }
  });

  function fmt(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
    return String(v);
  }

  function renderDetail(ticker) {
    const d = data[ticker];
    if (!d) {
      detailBody.innerHTML = "<p class='tt-empty'>No analysis data for this ticker yet.</p>";
      return;
    }
    const h = d.holding || {};
    const f = d.fundamentals || {};
    const t = d.technicals || {};
    const l = d.llm || {};
    let html = "";
    html += "<dl>";
    html += "<dt>Shares</dt><dd>" + fmt(h.shares) + "</dd>";
    html += "<dt>Cost basis (purch.)</dt><dd>" + fmt(h.cost_basis_purchase) + " " + (h.report_ccy || "") + "</dd>";
    html += "<dt>Current value</dt><dd>" + fmt(h.current_value_purchase) + "</dd>";
    html += "<dt>G/L %</dt><dd>" + fmt(h.gain_loss_pct_purchase) + "</dd>";
    html += "</dl>";
    if (l.signal) {
      html += "<h3>AI signal</h3><p><strong>" + l.signal + "</strong> (" + (l.confidence || "—") + ")</p>";
      if (l.summary) html += "<p>" + l.summary + "</p>";
      if (l.strengths && l.strengths.length) {
        html += "<h3>Strengths</h3><ul>" + l.strengths.map((s) => "<li>" + s + "</li>").join("") + "</ul>";
      }
      if (l.risks && l.risks.length) {
        html += "<h3>Risks</h3><ul>" + l.risks.map((s) => "<li>" + s + "</li>").join("") + "</ul>";
      }
    }
    html += "<h3>Technicals</h3><dl>";
    html += "<dt>RSI (14)</dt><dd>" + fmt(t.rsi_14) + "</dd>";
    html += "<dt>MACD histogram</dt><dd>" + fmt(t.macd_histogram) + "</dd>";
    html += "<dt>vs EMA200</dt><dd>" + (t.price_vs_ema200_pct != null ? t.price_vs_ema200_pct.toFixed(1) + "%" : "—") + "</dd>";
    html += "</dl>";
    if (t.signals && t.signals.length) {
      html += "<ul>" + t.signals.map((s) => "<li>" + s.name + "</li>").join("") + "</ul>";
    }
    html += "<h3>Fundamentals</h3><dl>";
    html += "<dt>P/E</dt><dd>" + fmt(f.pe_ratio) + "</dd>";
    html += "<dt>P/B</dt><dd>" + fmt(f.pb_ratio) + "</dd>";
    html += "<dt>Net margin</dt><dd>" + (f.net_margin != null ? (f.net_margin * 100).toFixed(1) + "%" : "—") + "</dd>";
    html += "<dt>Rev growth YoY</dt><dd>" + (f.revenue_growth_yoy != null ? (f.revenue_growth_yoy * 100).toFixed(1) + "%" : "—") + "</dd>";
    html += "<dt>Analyst</dt><dd>" + (f.analyst_recommendation || "—") + "</dd>";
    html += "<dt>Target</dt><dd>" + fmt(f.target_price) + "</dd>";
    html += "</dl>";
    if (f.recent_headlines && f.recent_headlines.length) {
      html += "<h3>Headlines</h3><ul>" + f.recent_headlines.map((x) => "<li>" + x + "</li>").join("") + "</ul>";
    }
    detailBody.innerHTML = html;
  }

  function bindHoldingRows(root) {
    (root || document).querySelectorAll(".tt-holding-row").forEach((row) => {
      row.addEventListener("click", () => {
        const ticker = row.dataset.ticker;
        if (selectedRow) selectedRow.classList.remove("selected");
        row.classList.add("selected");
        selectedRow = row;
        detail.classList.remove("hidden");
        const title = document.getElementById("tt-detail-title");
        if (title) title.textContent = ticker;
        renderDetail(ticker);
      });
    });
  }
  bindHoldingRows(document);

  function setupHoldingsViewToggle() {
    const toolbar = document.getElementById("holdings-toolbar");
    const panelC = document.getElementById("holdings-panel-consolidated");
    const panelL = document.getElementById("holdings-panel-lots");
    if (!toolbar || !panelC || !panelL) return;
    const buttons = toolbar.querySelectorAll(".tt-view-btn");
    function showView(view) {
      const isLots = view === "lots";
      panelC.classList.toggle("active", !isLots);
      panelL.classList.toggle("active", isLots);
      buttons.forEach((b) => b.classList.toggle("active", b.dataset.view === view));
      try { sessionStorage.setItem("tt-holdings-view", view); } catch (e) {}
    }
    buttons.forEach((b) => b.addEventListener("click", () => showView(b.dataset.view || "consolidated")));
    try {
      const saved = sessionStorage.getItem("tt-holdings-view");
      if (saved === "lots") showView("lots");
    } catch (e) {}
  }
  setupHoldingsViewToggle();

  document.getElementById("tt-detail-close")?.addEventListener("click", () => {
    detail.classList.add("hidden");
    if (selectedRow) selectedRow.classList.remove("selected");
    selectedRow = null;
  });
})();
"""


_EMIT_DEBOUNCE_SEC = 0.75


class TabbedReportState:
    """Mutable report state; call :meth:`publish` to push HTML to the dashboard."""

    def __init__(self, publish: Callable[[str], None] | None = None) -> None:
        self.publish = publish
        self.base: str = ""
        self.summary: Mapping[str, Any] = {}
        self.holdings: list[dict[str, Any]] = []
        self.holdings_lots: list[dict[str, Any]] = []
        self.metadata: Mapping[str, Any] = {}
        self.drive_url: str | None = None
        self.fundamentals: dict[str, FundamentalsResult] = {}
        self.technicals: dict[str, TechnicalResult] = {}
        self.analyses: dict[str, LLMAnalysis] = {}
        self.portfolio_summary: Mapping[str, str] | None = None
        self.llm_offline_hint: str = ""
        self.llm_available: bool = False
        self.analysis_enabled: bool = False
        self._tickers: list[str] = []
        self._generated_at: str | None = None
        self._emit_timer: threading.Timer | None = None
        self._last_published_html: str | None = None

    def set_core(
        self,
        *,
        base: str,
        summary: Mapping[str, Any],
        holdings: list[dict[str, Any]],
        metadata: Mapping[str, Any],
        drive_url: str | None = None,
        analysis_enabled: bool = False,
        llm_offline_hint: str = "",
        holdings_lots: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base = base
        self.summary = summary
        self.holdings = holdings
        self.holdings_lots = list(holdings_lots) if holdings_lots is not None else list(holdings)
        self.metadata = metadata
        self.drive_url = drive_url
        self.analysis_enabled = analysis_enabled
        self.llm_offline_hint = llm_offline_hint
        self._tickers = list(
            dict.fromkeys(str(h.get("ticker") or "") for h in holdings if h.get("ticker"))
        )
        if not self._generated_at:
            self._generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._emit(immediate=True)

    def set_fundamental(self, ticker: str, result: FundamentalsResult) -> None:
        self.fundamentals[ticker] = result
        self._emit()

    def set_technicals(self, technicals: dict[str, TechnicalResult]) -> None:
        self.technicals = dict(technicals)
        self._emit(immediate=True)

    def set_analysis(self, ticker: str, analysis: LLMAnalysis) -> None:
        self.analyses[ticker] = analysis
        self._emit()

    def set_portfolio_summary(
        self,
        portfolio_summary: Mapping[str, str] | None,
        *,
        llm_available: bool,
    ) -> None:
        self.portfolio_summary = portfolio_summary
        self.llm_available = llm_available
        self._emit(immediate=True)

    def flush(self) -> None:
        """Publish latest HTML immediately (e.g. before run ends)."""
        self._emit(immediate=True)

    def _emit(self, *, immediate: bool = False) -> None:
        publish = self.publish
        if publish is None:
            return
        if self._emit_timer is not None:
            self._emit_timer.cancel()
            self._emit_timer = None
        if immediate:

            def _do() -> None:
                html_out = self.render()
                if html_out != self._last_published_html:
                    self._last_published_html = html_out
                    publish(html_out)

            _do()
            return

        def _debounced() -> None:
            self._emit_timer = None
            html_out = self.render()
            if html_out != self._last_published_html:
                self._last_published_html = html_out
                publish(html_out)

        self._emit_timer = threading.Timer(_EMIT_DEBOUNCE_SEC, _debounced)
        self._emit_timer.daemon = True
        self._emit_timer.start()

    def render(self) -> str:
        return build_portfolio_tabbed_html(
            base=self.base,
            summary=self.summary,
            holdings=self.holdings,
            holdings_lots=self.holdings_lots,
            metadata=self.metadata,
            drive_url=self.drive_url,
            fundamentals=self.fundamentals or None,
            technicals=self.technicals or None,
            analyses=self.analyses or None,
            portfolio_summary=self.portfolio_summary,
            llm_offline_hint=self.llm_offline_hint,
            llm_available=self.llm_available,
            analysis_enabled=self.analysis_enabled,
            tickers_ordered=self._tickers,
            generated_at=self._generated_at,
        )


def _summary_rankings_html(holdings: list[dict[str, Any]]) -> str:
    """Top / bottom performers by purchase currency (matches email report)."""
    from ticker_tracker.engine import _perf_table_rows, _rank_best_worst_by_currency

    perf_cols = [
        "Ticker",
        "Shares",
        "Cost basis (purch.)",
        "Latest value (purch.)",
        "G/L (purch.)",
        "G/L %",
    ]
    ranked = _rank_best_worst_by_currency(holdings, n=5)
    if not ranked:
        return (
            "<h3 class='tt-subhead'>Top 5 gainers / losers (by return %, per currency)</h3>"
            "<p class='tt-empty'>No holdings with valid prices, FX, and positive cost basis for ranking.</p>"
        )
    parts: list[str] = []
    for ccy, (best, worst) in ranked.items():
        parts.append(
            f"<h3 class='tt-subhead'>Top 5 gainers ({_esc(ccy)}, by return %, distinct tickers)</h3>"
        )
        if best:
            parts.append(_table(perf_cols, _perf_table_rows(best), extra_class="tt-perf-gainers"))
        else:
            parts.append("<p class='tt-empty'>No eligible holdings for this currency.</p>")
        parts.append(
            f"<h3 class='tt-subhead'>Top 5 losers ({_esc(ccy)}, by return %, distinct tickers)</h3>"
        )
        if worst:
            parts.append(_table(perf_cols, _perf_table_rows(worst), extra_class="tt-perf-losers"))
        else:
            parts.append("<p class='tt-empty'>No eligible holdings for this currency.</p>")
    return "".join(parts)


def _holdings_tbody_html(holdings: list[dict[str, Any]]) -> str:
    """Clickable holdings table rows."""
    rows_out: list[str] = []
    for h in holdings:
        ticker = str(h.get("ticker") or "")
        cells = [
            f'<span class="num">{_esc(ticker)}</span>',
            f'<span class="num">{_fmt_shares(h.get("shares"))}</span>',
            f'<span class="num">{_fmt_num(h.get("cost_per_share_purchase"))}</span>',
            _esc(h.get("report_ccy")),
            f'<span class="num">{_fmt_num(h.get("cost_basis_purchase"))}</span>',
            f'<span class="num">{_fmt_num(h.get("price_per_share_purchase"))}</span>',
            f'<span class="num">{_fmt_num(h.get("current_value_purchase"))}</span>',
            f'<span class="num">{_fmt_num(h.get("gain_loss_purchase"))}</span>',
            f'<span class="num">{_fmt_pct_html(h.get("gain_loss_pct_purchase"))}</span>',
        ]
        rows_out.append(
            f'<tr class="tt-holding-row" data-ticker="{_esc(ticker)}">'
            + "".join(f"<td>{c}</td>" for c in cells)
            + "</tr>"
        )
    return "".join(rows_out)


def _holdings_table_block(holdings: list[dict[str, Any]], hold_headers: list[str]) -> str:
    return (
        '<div class="tt-table-scroll"><table class="tt-table"><thead><tr>'
        + "".join(f"<th>{_esc(h)}</th>" for h in hold_headers)
        + "</tr></thead><tbody>"
        + _holdings_tbody_html(holdings)
        + "</tbody></table></div>"
    )


def build_portfolio_tabbed_html(
    *,
    base: str,
    summary: Mapping[str, Any],
    holdings: list[dict[str, Any]],
    holdings_lots: list[dict[str, Any]] | None = None,
    metadata: Mapping[str, Any],
    drive_url: str | None = None,
    fundamentals: dict[str, FundamentalsResult] | None = None,
    technicals: dict[str, TechnicalResult] | None = None,
    analyses: dict[str, LLMAnalysis] | None = None,
    portfolio_summary: Mapping[str, str] | None = None,
    llm_offline_hint: str = "Ensure the analysis LLM is configured and reachable.",
    llm_available: bool = False,
    analysis_enabled: bool = False,
    tickers_ordered: list[str] | None = None,
    generated_at: str | None = None,
) -> str:
    """Full HTML document with Excel-matching tabs and ticker drill-down."""
    b = base.upper()
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fund_map = fundamentals or {}
    tech_map = technicals or {}
    ana_map = analyses or {}
    tickers = tickers_ordered or list(
        dict.fromkeys(str(h.get("ticker") or "") for h in holdings if h.get("ticker"))
    )

    n_fund = sum(1 for t in tickers if t in fund_map)
    n_tech = sum(1 for t in tickers if t in tech_map)
    n_llm = sum(1 for t in tickers if t in ana_map and ana_map[t].llm_available)

    def tab_btn(
        tab_id: str, label: str, ready: bool, badge: str = "", *, active: bool = False
    ) -> str:
        badge_html = f'<span class="badge">{_esc(badge)}</span>' if badge else ""
        cls = "tt-tab active" if active else "tt-tab"
        return (
            f'<button type="button" class="{cls}" data-tab="{tab_id}" data-ready="{"1" if ready else "0"}">'
            f"{_esc(label)}{badge_html}</button>"
        )

    tabs_html = [
        tab_btn("holdings", "Holdings", bool(holdings), active=True),
        tab_btn("summary", "Summary", bool(summary)),
        tab_btn("metadata", "Metadata", bool(metadata)),
    ]
    if analysis_enabled:
        tabs_html.append(
            tab_btn(
                "analysis",
                "Analysis",
                n_fund > 0,
                f"{n_fund}/{len(tickers)}" if tickers else "",
            )
        )
        tabs_html.append(
            tab_btn(
                "signals",
                "Portfolio Signals",
                n_tech > 0 or bool(portfolio_summary),
                f"LLM {n_llm}/{len(tickers)}" if tickers else "",
            )
        )

    hold_headers = [
        "Ticker",
        "Shares",
        "Cost/sh",
        "CCY",
        "Cost basis",
        "Price/sh",
        "Value",
        "G/L",
        "G/L %",
    ]
    lots = holdings_lots if holdings_lots is not None else holdings
    show_lots_toggle = len(lots) > len(holdings)
    holdings_by_ticker = {str(h.get("ticker") or ""): h for h in holdings}
    for h in lots:
        t = str(h.get("ticker") or "")
        if t and t not in holdings_by_ticker:
            holdings_by_ticker[t] = h

    if show_lots_toggle:
        holdings_toolbar = (
            '<div class="tt-holdings-toolbar" id="holdings-toolbar">'
            '<p class="tt-holdings-note">Switch between consolidated positions and every sheet row (lot).</p>'
            '<div class="tt-view-toggle" role="group" aria-label="Holdings view">'
            '<button type="button" class="tt-view-btn active" data-view="consolidated">By ticker</button>'
            f'<button type="button" class="tt-view-btn" data-view="lots">All lots ({len(lots)})</button>'
            "</div></div>"
        )
        holdings_panels = (
            f'<div id="holdings-panel-consolidated" class="holdings-view active">'
            f"{_holdings_table_block(holdings, hold_headers)}</div>"
            f'<div id="holdings-panel-lots" class="holdings-view">'
            f"{_holdings_table_block(lots, hold_headers)}</div>"
        )
    else:
        holdings_toolbar = (
            '<p class="tt-holdings-note">One row per ticker '
            "(weighted-average cost across lots).</p>"
        )
        holdings_panels = _holdings_table_block(holdings, hold_headers)

    summary_panel = _table(
        ["Metric", "Base", "Purchased"],
        _summary_table_rows(summary, b),
    )
    summary_panel += _summary_rankings_html(holdings)

    meta_panel = _table(["Field", "Value", ""], _metadata_rows(metadata, b))
    fin_rows = _finance_source_rows(metadata)
    if fin_rows:
        meta_panel += "<h3 style='margin-top:1rem;font-size:0.85rem;color:var(--muted)'>Finance source per ticker</h3>"
        meta_panel += _table(["Ticker", "Source"], fin_rows)

    analysis_rows_html: list[str] = []
    if analysis_enabled:
        for ticker in tickers:
            pending = ticker not in fund_map and ticker not in tech_map
            cells = _analysis_cells(
                ticker,
                fund=fund_map.get(ticker),
                tech=tech_map.get(ticker),
                analysis=ana_map.get(ticker),
                pending=pending and analysis_enabled,
            )
            analysis_rows_html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    if analysis_enabled:
        analysis_panel = (
            '<div class="tt-table-scroll"><table class="tt-table tt-analysis"><thead><tr>'
            + "".join(f"<th>{_esc(h)}</th>" for h in _ANALYSIS_HEADERS)
            + "</tr></thead><tbody>"
            + "".join(analysis_rows_html)
            + "</tbody></table></div>"
        )
    else:
        analysis_panel = "<p class='tt-empty'>Analysis disabled in settings.</p>"

    signals_parts: list[str] = []
    if analysis_enabled:
        sigs = [a.signal for a in ana_map.values() if a.llm_available]
        counts = Counter(sigs)
        total = len(sigs) or 1
        sig_rows = [
            ["Total holdings", str(len(tickers))],
            ["BUY", f"{counts.get('BUY', 0)} ({counts.get('BUY', 0) / total * 100:.0f}%)"],
            ["HOLD", f"{counts.get('HOLD', 0)} ({counts.get('HOLD', 0) / total * 100:.0f}%)"],
            ["SELL", f"{counts.get('SELL', 0)} ({counts.get('SELL', 0) / total * 100:.0f}%)"],
        ]
        signals_parts.append(_table(["Metric", "Value"], sig_rows))
        if llm_available and portfolio_summary:
            signals_parts.append(
                f"<p><strong>Commentary:</strong> {_esc(portfolio_summary.get('commentary', ''))}</p>"
            )
            signals_parts.append(
                f"<p><strong>Top action:</strong> {_esc(portfolio_summary.get('top_action', ''))}</p>"
            )
        else:
            signals_parts.append(f"<p class='tt-empty'>LLM offline — {_esc(llm_offline_hint)}</p>")
        heat_headers = ["Ticker", *[name for name, _ in _HEATMAP_COLUMNS]]
        heat_rows: list[list[str]] = []
        for ticker in tickers:
            tech = tech_map.get(ticker)
            grouped = _heatmap_signals(tech)
            row_cells = [_esc(ticker)]
            for col_name, _ in _HEATMAP_COLUMNS:
                matches = grouped[col_name]
                if not matches:
                    row_cells.append("—")
                else:
                    cls = "bull" if matches[0].bullish else "bear"
                    row_cells.append(f'<span class="{cls}">{_esc(matches[0].name)}</span>')
            heat_rows.append(row_cells)
        signals_parts.append("<h3 style='font-size:0.85rem;color:var(--muted)'>Signal heatmap</h3>")
        signals_parts.append(_table(heat_headers, heat_rows, extra_class="tt-heatmap"))
    else:
        signals_parts.append("<p class='tt-empty'>Analysis disabled.</p>")

    ticker_json: dict[str, Any] = {}
    for ticker in tickers:
        ticker_json[ticker] = _ticker_detail_payload(
            ticker,
            holding=holdings_by_ticker.get(ticker),
            fund=fund_map.get(ticker),
            tech=tech_map.get(ticker),
            analysis=ana_map.get(ticker),
        )

    drive_link = ""
    if drive_url:
        safe = html.escape(drive_url, quote=True)
        drive_link = f' <a href="{safe}" style="color:var(--accent)">Open in Drive</a>'

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Portfolio ({_esc(b)}) — {_esc(stamp)}</title>",
        f"<style>{_TABBED_CSS}</style></head><body>",
        '<div class="tt-report">',
        '<header class="tt-top">',
        f"<h1>Portfolio report — {_esc(b)}</h1>",
        f'<div class="sub">As of {_esc(stamp)}{drive_link} · Click a ticker in Holdings for details</div>',
        "</header>",
        '<nav class="tt-tabs">' + "".join(tabs_html) + "</nav>",
        '<div class="tt-body">',
        '<main class="tt-panels">',
        '<section id="tab-holdings" class="tt-panel active">'
        '<h3 style="font-size:0.85rem;color:var(--muted)">Holdings</h3>'
        + holdings_toolbar
        + holdings_panels
        + "</section>",
        f'<section id="tab-summary" class="tt-panel">{summary_panel}</section>',
        f'<section id="tab-metadata" class="tt-panel">{meta_panel}</section>',
    ]
    if analysis_enabled:
        parts.append(f'<section id="tab-analysis" class="tt-panel">{analysis_panel}</section>')
        parts.append(
            f'<section id="tab-signals" class="tt-panel">{"".join(signals_parts)}</section>'
        )
    parts.extend(
        [
            "</main>",
            '<aside id="tt-detail" class="tt-detail hidden">',
            '<button type="button" class="close" id="tt-detail-close">Close</button>',
            '<h2 id="tt-detail-title">Ticker</h2>',
            '<div id="tt-detail-body"></div>',
            "</aside>",
            "</div>",
            f'<script type="application/json" id="tt-ticker-data">{json.dumps(ticker_json)}</script>',
            f"<script>{_TABBED_JS}</script>",
            "</div></body></html>",
        ]
    )
    return "".join(parts)
