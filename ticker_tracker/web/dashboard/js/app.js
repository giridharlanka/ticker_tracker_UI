/**
 * Ticker Tracker dashboard — API wiring.
 */

const FINANCE_IDS = ["yahoo", "alpha_vantage", "twelve_data", "finnhub", "polygon"];
const OUTPUT_FMT_IDS = ["xlsx", "html"];

let configCache = null;
let lastFile = null;
/** Set true after a successful Analyse; server still has HTML for /api/email-report. */
let reportAvailableForEmail = false;

function $(id) {
  return document.getElementById(id);
}

function setStatus(el, text, kind) {
  el.textContent = text || "";
  el.classList.remove("err", "ok");
  if (kind === "err") el.classList.add("err");
  if (kind === "ok") el.classList.add("ok");
}

function showRunProgress(visible) {
  const wrap = $("run_progress");
  if (wrap) wrap.hidden = !visible;
}

/**
 * @param {"idle" | "running" | "ready" | "sending" | "sent"} mode
 */
function setEmailButtonMode(mode) {
  const b = $("btn_email");
  if (!b) return;
  for (const m of ["idle", "running", "ready", "sending", "sent"]) {
    b.classList.remove("email-mode-" + m);
  }
  b.classList.add("email-mode-" + mode);

  const titles = {
    idle: "Run Analyse first — then you can email the HTML report.",
    running: "Wait for analysis to finish.",
    ready: "Send the last report HTML to addresses in Settings.",
    sending: "Sending via Gmail API…",
    sent: "Message sent.",
  };
  b.title = titles[mode] || titles.idle;

  const labels = {
    idle: "Email report",
    running: "Email report",
    ready: "Email report",
    sending: "Sending…",
    sent: "Sent ✓",
  };
  b.textContent = labels[mode] || "Email report";

  b.disabled = mode !== "ready";
}

async function fetchConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

function showConfigWarning(data) {
  const errBox = $("modal_errors");
  if (!errBox || !data?.config_warning) return;
  errBox.innerHTML = `<div class="form-error">${escapeHtml(data.config_warning)}</div>`;
}

/** Prefill Run panel paths from saved config (still stored for CLI; dashboard edits here per run). */
function applyRunPanelFromForm(form) {
  if (!form) return;
  const od = $("run_output_dir");
  if (od && form.local_report_dir !== undefined) od.value = form.local_report_dir || "";
  const hp = $("run_holdings_path");
  if (hp && form.local_holdings_path !== undefined) hp.value = form.local_holdings_path || "";
  const sn = $("run_local_sheet_name");
  if (sn && form.local_holdings_sheet_name !== undefined) {
    sn.value = form.local_holdings_sheet_name || "Holdings";
  }
}

function applyFormToModal(data) {
  const form = data.form;
  if (!form) return;

  const srcGoogle = $("src_google");
  const srcLocal = $("src_local");
  if (form.holdings_source === "local_file") {
    srcLocal.checked = true;
  } else {
    srcGoogle.checked = true;
  }

  $("google_sheets_id").value = form.google_sheets_id || "";
  $("holdings_sheet_name").value = form.holdings_sheet_name || "Holdings";
  $("emails").value = form.emails || "";
  $("base_currency").value = form.base_currency || "";
  $("fx_source").value = form.fx_source || "frankfurter";
  $("fx_api_key").value = "";
  $("fx_key_action").value = form.fx_key_action || "keep";
  if ($("analysis_enabled")) {
    $("analysis_enabled").checked = form.analysis_enabled !== false;
  }
  if ($("llm_provider")) $("llm_provider").value = form.llm_provider || "ollama";
  if ($("ollama_url")) $("ollama_url").value = form.ollama_url || "http://localhost:11434";
  if ($("analysis_model")) $("analysis_model").value = form.analysis_model || "qwen2.5:7b";
  if ($("gemini_model")) $("gemini_model").value = form.gemini_model || "gemini-2.0-flash";
  if ($("llm_include_holding_context")) {
    $("llm_include_holding_context").checked = !!form.llm_include_holding_context;
  }
  if ($("fmp_key_action")) $("fmp_key_action").value = form.fmp_key_action || "keep";
  if ($("key_fmp")) $("key_fmp").value = "";
  $("market_overrides").value = form.market_overrides || "";
  syncAnalysisOptionsVisibility();
  $("upload_to_drive").checked = !!form.upload_to_drive;
  $("run_on_startup").checked = !!form.run_on_startup;

  for (const fmt of OUTPUT_FMT_IDS) {
    const cb = $(`output_${fmt}`);
    if (cb) cb.checked = (form.output_formats || []).includes(fmt);
  }

  for (const sid of FINANCE_IDS) {
    const fin = $(`fin_${sid}`);
    if (fin) fin.checked = (form.finance_selected || []).includes(sid);
    if (sid !== "yahoo") {
      const ka = $(`key_action_${sid}`);
      if (ka) ka.value = (form.finance_key_action && form.finance_key_action[sid]) || "keep";
      const key = $(`key_${sid}`);
      if (key) key.value = "";
    }
  }

  const cols = [
    "ticker",
    "exchange",
    "shares",
    "cost_basis",
    "purchase_currency",
    "currency_override",
  ];
  for (const c of cols) {
    const el = $(`col_${c}`);
    if (el) el.value = form[`col_${c}`] || "";
  }

  const ks = data.key_statuses || {};
  $("key_status_fx").textContent = `FX API key: ${ks.fx || "—"}`;
  const fmpSt = $("key_status_fmp");
  if (fmpSt) fmpSt.textContent = `FMP API key: ${ks.fmp || "—"}`;
  const gemSt = $("key_status_gemini");
  if (gemSt) gemSt.textContent = `Gemini API key: ${ks.gemini || "—"}`;
  for (const sid of FINANCE_IDS) {
    if (sid === "yahoo") continue;
    const p = $(`key_status_${sid}`);
    if (p) p.textContent = `${sid} API key: ${ks[sid] || "—"}`;
  }
}

function collectConfigPayload() {
  const finance_selected = [];
  for (const sid of FINANCE_IDS) {
    const fin = $(`fin_${sid}`);
    if (fin && fin.checked) finance_selected.push(sid);
  }
  const finance_key_action = {};
  const finance_keys = {};
  for (const sid of FINANCE_IDS) {
    if (sid === "yahoo") continue;
    const ka = $(`key_action_${sid}`);
    const key = $(`key_${sid}`);
    if (ka) finance_key_action[sid] = ka.value;
    if (key) finance_keys[sid] = key.value;
  }
  const output_formats = [];
  for (const fmt of OUTPUT_FMT_IDS) {
    const cb = $(`output_${fmt}`);
    if (cb && cb.checked) output_formats.push(fmt);
  }

  const holdings_source = $("src_local").checked ? "local_file" : "google_sheets";

  const colFields = [
    "ticker",
    "exchange",
    "shares",
    "cost_basis",
    "purchase_currency",
    "currency_override",
  ];
  const columns = {};
  for (const c of colFields) {
    const el = $(`col_${c}`);
    if (el && el.value.trim()) columns[c] = el.value.trim();
  }

  return {
    holdings_source,
    google_sheets_id: $("google_sheets_id").value.trim(),
    holdings_sheet_name: $("holdings_sheet_name").value.trim(),
    emails: $("emails").value,
    base_currency: $("base_currency").value.trim(),
    fx_source: $("fx_source").value.trim(),
    fx_api_key: $("fx_api_key").value,
    fx_key_action: $("fx_key_action").value,
    analysis_enabled: $("analysis_enabled") ? $("analysis_enabled").checked : true,
    llm_provider: ($("llm_provider") && $("llm_provider").value) || "ollama",
    ollama_url: ($("ollama_url") && $("ollama_url").value.trim()) || "http://localhost:11434",
    analysis_model: ($("analysis_model") && $("analysis_model").value.trim()) || "qwen2.5:7b",
    gemini_model: ($("gemini_model") && $("gemini_model").value.trim()) || "gemini-2.0-flash",
    llm_include_holding_context: $("llm_include_holding_context")
      ? $("llm_include_holding_context").checked
      : false,
    fmp_key_action: $("fmp_key_action") ? $("fmp_key_action").value : "keep",
    key_fmp: $("key_fmp") ? $("key_fmp").value : "",
    gemini_key_action: $("gemini_key_action") ? $("gemini_key_action").value : "keep",
    key_gemini: $("key_gemini") ? $("key_gemini").value : "",
    market_overrides: $("market_overrides").value,
    run_on_startup: $("run_on_startup").checked,
    upload_to_drive: $("upload_to_drive").checked,
    output_formats: output_formats.length ? output_formats : ["xlsx"],
    finance_selected,
    finance_key_action,
    finance_keys,
    columns,
  };
}

function syncLlmProviderFields() {
  const provider = ($("llm_provider") && $("llm_provider").value) || "ollama";
  const ollamaBlock = $("llm_ollama_fields");
  const geminiBlock = $("llm_gemini_fields");
  if (ollamaBlock) ollamaBlock.hidden = provider !== "ollama";
  if (geminiBlock) geminiBlock.hidden = provider !== "gemini";
}

async function syncAnalysisOptionsVisibility() {
  const enabled = $("analysis_enabled") && $("analysis_enabled").checked;
  const block = $("analysis_options");
  if (!block) return;
  block.querySelectorAll("input, select, textarea").forEach((el) => {
    el.disabled = !enabled;
  });
  block.style.opacity = enabled ? "1" : "0.55";
  syncLlmProviderFields();
}

async function openSettings() {
  $("modal_errors").innerHTML = "";
  $("modal_ok").style.display = "none";
  $("modal_backdrop").classList.add("open");
  try {
    configCache = await fetchConfig();
    applyFormToModal(configCache);
    showConfigWarning(configCache);
  } catch (e) {
    $("modal_errors").innerHTML = `<div class="form-error">${escapeHtml(String(e))}</div>`;
  }
}

function closeSettings() {
  $("modal_backdrop").classList.remove("open");
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function saveSettings() {
  const errBox = $("modal_errors");
  const okBox = $("modal_ok");
  errBox.innerHTML = "";
  okBox.style.display = "none";
  try {
    const payload = collectConfigPayload();
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const issues = data.errors || [data.error || "Save failed"];
      errBox.innerHTML = `<div class="form-error"><strong>Fix:</strong><ul>${issues.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul></div>`;
      return;
    }
    okBox.style.display = "block";
    okBox.textContent = "Saved.";
    configCache = data;
    if (data.form) {
      applyFormToModal(data);
      applyRunPanelFromForm(data.form);
    }
  } catch (e) {
    errBox.innerHTML = `<div class="form-error">${escapeHtml(String(e))}</div>`;
  }
}

function analyseSource() {
  return $("run_source_upload").checked ? "upload" : "google_sheets";
}

async function nativePickFolder() {
  const status = $("run_status");
  try {
    const r = await fetch("/api/native-pick-folder", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(status, d.error || r.statusText, "err");
      return;
    }
    if (d.path) $("run_output_dir").value = d.path;
  } catch (e) {
    setStatus(status, String(e), "err");
  }
}

async function nativePickHoldingsFile() {
  const status = $("run_status");
  try {
    const r = await fetch("/api/native-pick-file", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(status, d.error || r.statusText, "err");
      return;
    }
    if (d.path) {
      $("run_holdings_path").value = d.path;
      lastFile = null;
      const fi = $("file_input");
      if (fi) fi.value = "";
      const fl = $("file_label");
      if (fl) fl.textContent = "Using path on disk (not browser upload)";
    }
  } catch (e) {
    setStatus(status, String(e), "err");
  }
}

let analysePollTimer = null;
/** Last preview HTML applied to the iframe (avoids redundant reloads / flicker). */
let lastPreviewHtml = "";

function showPreviewProgress(visible) {
  const panel = $("preview_progress");
  const wrap = document.querySelector(".preview-frame-wrap");
  if (panel) panel.hidden = !visible;
  if (wrap) wrap.classList.toggle("preview-running", visible);
  const hint = $("preview_hint");
  if (hint) {
    hint.textContent = visible
      ? "Live progress from the backend — report fills in as data is ready"
      : "HTML output matches the generated report file";
  }
  const title = $("preview_title");
  if (title) title.textContent = visible ? "Analysis in progress" : "Report preview";
}

function resetPreviewProgress() {
  const fill = $("preview_progress_fill");
  const pctEl = $("preview_progress_pct");
  const track = $("preview_progress_track");
  const msg = $("preview_progress_msg");
  const log = $("preview_progress_log");
  if (fill) fill.style.width = "0%";
  if (pctEl) pctEl.textContent = "0%";
  if (track) track.setAttribute("aria-valuenow", "0");
  if (msg) msg.textContent = "";
  if (log) log.textContent = "";
  const liveWrap = $("preview_live_wrap");
  const tbody = $("preview_live_tbody");
  if (liveWrap) liveWrap.hidden = true;
  if (tbody) tbody.innerHTML = "";
}

function getIframeActiveTab(iframe) {
  try {
    const doc = iframe.contentDocument;
    const active = doc?.querySelector(".tt-tab.active");
    return active?.dataset?.tab || null;
  } catch {
    return null;
  }
}

function getIframeHoldingsView(iframe) {
  try {
    return iframe.contentWindow?.sessionStorage?.getItem("tt-holdings-view");
  } catch {
    return null;
  }
}

function applyPreviewHtml(iframe, html) {
  if (!iframe || !html || html === lastPreviewHtml) return;
  const preservedTab = getIframeActiveTab(iframe);
  const preservedHoldingsView = getIframeHoldingsView(iframe);
  lastPreviewHtml = html;
  iframe.srcdoc = html;
  if (preservedTab || preservedHoldingsView) {
    iframe.addEventListener(
      "load",
      () => {
        try {
          if (preservedTab) {
            iframe.contentWindow?.postMessage({ type: "tt-show-tab", tab: preservedTab }, "*");
          }
          if (preservedHoldingsView) {
            iframe.contentWindow?.postMessage(
              { type: "tt-holdings-view", view: preservedHoldingsView },
              "*",
            );
          }
        } catch {
          /* ignore */
        }
      },
      { once: true },
    );
  }
}

function updatePreviewProgress(snap) {
  const pct = Math.max(0, Math.min(100, Number(snap.pct) || 0));
  const fill = $("preview_progress_fill");
  const pctEl = $("preview_progress_pct");
  const track = $("preview_progress_track");
  if (fill) fill.style.width = `${pct}%`;
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (track) track.setAttribute("aria-valuenow", String(pct));
  const msg = $("preview_progress_msg");
  if (msg) msg.textContent = snap.message || "";
  const log = $("preview_progress_log");
  if (log && Array.isArray(snap.log)) {
    log.textContent = snap.log.join("\n");
    log.scrollTop = log.scrollHeight;
  }
  const iframe = $("report_frame");
  if (iframe && snap.preview_html) {
    applyPreviewHtml(iframe, snap.preview_html);
  }
  const signals = snap.live_signals;
  const liveWrap = $("preview_live_wrap");
  const tbody = $("preview_live_tbody");
  if (signals && signals.length && liveWrap && tbody) {
    liveWrap.hidden = false;
    tbody.innerHTML = signals
      .map(
        (row) =>
          `<tr><td>${escapeHtml(row.ticker)}</td><td>${escapeHtml(row.signal)}</td><td>${escapeHtml(row.confidence)}</td></tr>`,
      )
      .join("");
  }
  const label = $("run_progress_label");
  if (label) label.textContent = snap.message || "Analysing portfolio…";
}

function stopAnalysePoll() {
  if (analysePollTimer !== null) {
    window.clearInterval(analysePollTimer);
    analysePollTimer = null;
  }
}

async function pollAnalyseJob(jobId) {
  const status = $("run_status");
  const iframe = $("report_frame");
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const r = await fetch(`/api/analyse/status/${encodeURIComponent(jobId)}`);
        const snap = await r.json().catch(() => ({}));
        if (!r.ok) {
          stopAnalysePoll();
          setStatus(status, snap.error || r.statusText, "err");
          resolve({ ok: false });
          return;
        }
        updatePreviewProgress(snap);
        if (snap.status === "done") {
          stopAnalysePoll();
          if (snap.html) applyPreviewHtml(iframe, snap.html);
          showPreviewProgress(false);
          setStatus(
            status,
            `Done. ${snap.summary?.emails_sent ? `Email sent (${snap.summary.emails_sent}). ` : ""}${snap.summary?.html_report_path || ""}`,
            "ok",
          );
          resolve({ ok: true, snap });
          return;
        }
        if (snap.status === "error") {
          stopAnalysePoll();
          showPreviewProgress(false);
          setStatus(status, snap.error || "Analysis failed.", "err");
          resolve({ ok: false });
        }
      } catch (e) {
        stopAnalysePoll();
        showPreviewProgress(false);
        setStatus(status, String(e), "err");
        resolve({ ok: false });
      }
    };
    tick();
    analysePollTimer = window.setInterval(tick, 750);
  });
}

/**
 * @returns {Promise<boolean | null>} ``true``/``false`` = run with/without analysis; ``null`` = cancelled
 */
function showAnalyseModeModal() {
  const backdrop = $("analyse_mode_backdrop");
  if (!backdrop) return Promise.resolve(true);

  return new Promise((resolve) => {
    const cleanup = () => {
      backdrop.classList.remove("open");
      $("analyse_mode_full")?.removeEventListener("click", onFull);
      $("analyse_mode_fast")?.removeEventListener("click", onFast);
      $("analyse_mode_close")?.removeEventListener("click", onCancel);
      backdrop.removeEventListener("click", onBackdrop);
    };
    const onFull = () => {
      cleanup();
      resolve(true);
    };
    const onFast = () => {
      cleanup();
      resolve(false);
    };
    const onCancel = () => {
      cleanup();
      resolve(null);
    };
    const onBackdrop = (e) => {
      if (e.target === backdrop) onCancel();
    };
    $("analyse_mode_full")?.addEventListener("click", onFull);
    $("analyse_mode_fast")?.addEventListener("click", onFast);
    $("analyse_mode_close")?.addEventListener("click", onCancel);
    backdrop.addEventListener("click", onBackdrop);
    backdrop.classList.add("open");
  });
}

/** @param {boolean} runAnalysisEnabled */
async function runAnalyse(runAnalysisEnabled) {
  const status = $("run_status");
  const iframe = $("report_frame");
  const btn = $("btn_analyse");
  let analyseSucceeded = false;

  stopAnalysePoll();
  setStatus(status, "Starting…", null);
  btn.disabled = true;
  setEmailButtonMode("running");
  showRunProgress(true);
  resetPreviewProgress();
  showPreviewProgress(true);
  lastPreviewHtml = "";
  if (iframe) iframe.srcdoc = "";

  try {
    const sendEmail = $("send_email").checked;
    const source = analyseSource();
    const outputDir = ($("run_output_dir") && $("run_output_dir").value.trim()) || "";

    let startResp;
    if (source === "upload") {
      const pathTrim = $("run_holdings_path") ? $("run_holdings_path").value.trim() : "";
      if (!pathTrim && !lastFile) {
        setStatus(status, "Set a holdings file path, browse, or upload / drag-and-drop.", "err");
        return;
      }
      const fd = new FormData();
      fd.append("source", "upload");
      fd.append("send_email", sendEmail ? "true" : "false");
      fd.append("holdings_path", pathTrim);
      fd.append("holdings_sheet_name", ($("run_local_sheet_name") && $("run_local_sheet_name").value.trim()) || "Holdings");
      fd.append("output_dir", outputDir);
      fd.append("analysis_enabled", runAnalysisEnabled ? "true" : "false");
      if (lastFile) fd.append("file", lastFile, lastFile.name);
      startResp = await fetch("/api/analyse", { method: "POST", body: fd });
    } else {
      startResp = await fetch("/api/analyse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: "google_sheets",
          send_email: sendEmail,
          output_dir: outputDir,
          analysis_enabled: runAnalysisEnabled,
        }),
      });
    }
    const startData = await startResp.json().catch(() => ({}));
    if (!startResp.ok) {
      setStatus(status, startData.error || startResp.statusText, "err");
      showPreviewProgress(false);
      return;
    }
    const jobId = startData.job_id;
    if (!jobId) {
      setStatus(status, "Server did not return a job id.", "err");
      showPreviewProgress(false);
      return;
    }
    setStatus(status, "Running…", null);
    const outcome = await pollAnalyseJob(jobId);
    analyseSucceeded = outcome.ok === true;
  } catch (e) {
    setStatus(status, String(e), "err");
    showPreviewProgress(false);
  } finally {
    stopAnalysePoll();
    showRunProgress(false);
    btn.disabled = false;
    if (!analyseSucceeded) {
      showPreviewProgress(false);
    }
    if (analyseSucceeded) {
      reportAvailableForEmail = true;
      setEmailButtonMode("ready");
    } else {
      setEmailButtonMode(reportAvailableForEmail ? "ready" : "idle");
    }
  }
}

async function sendEmailReport() {
  const status = $("run_status");
  if (!reportAvailableForEmail) return;

  setEmailButtonMode("sending");
  try {
    const r = await fetch("/api/email-report", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(status, data.error || r.statusText, "err");
      setEmailButtonMode("ready");
      return;
    }
    setStatus(status, `Email queued: sent to ${data.sent} address(es).`, "ok");
    setEmailButtonMode("sent");
    window.setTimeout(() => {
      setEmailButtonMode("ready");
    }, 2200);
  } catch (e) {
    setStatus(status, String(e), "err");
    setEmailButtonMode("ready");
  }
}

function setupDropZone() {
  const drop = $("drop_zone");
  const input = $("file_input");
  if (!drop || !input) return;

  drop.addEventListener("click", () => input.click());
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("drag");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) setFile(f);
  });
  input.addEventListener("change", () => {
    const f = input.files && input.files[0];
    if (f) setFile(f);
  });
}

function setFile(file) {
  lastFile = file;
  const pathEl = $("run_holdings_path");
  if (pathEl) pathEl.value = "";
  const el = $("file_label");
  if (el) el.textContent = file.name;
}

function setupRunPathInput() {
  const hp = $("run_holdings_path");
  if (!hp) return;
  hp.addEventListener("input", () => {
    lastFile = null;
    const fi = $("file_input");
    if (fi) fi.value = "";
    const fl = $("file_label");
    if (fl) fl.textContent = "No file uploaded";
  });
}

function setupSourceRadios() {
  const u = $("run_source_upload");
  const g = $("run_source_google");
  const uploadSection = $("run_upload_section");
  const hint = $("sheet_hint");
  function sync() {
    const upload = u && u.checked;
    if (uploadSection) {
      uploadSection.style.display = upload ? "block" : "none";
      uploadSection.setAttribute("aria-hidden", upload ? "false" : "true");
    }
    if (hint) hint.style.display = upload ? "none" : "block";
  }
  u?.addEventListener("change", sync);
  g?.addEventListener("change", sync);
  sync();
}

document.addEventListener("DOMContentLoaded", async () => {
  setEmailButtonMode("idle");

  $("btn_settings")?.addEventListener("click", openSettings);
  $("modal_close")?.addEventListener("click", closeSettings);
  $("modal_cancel")?.addEventListener("click", closeSettings);
  $("modal_save")?.addEventListener("click", saveSettings);
  $("modal_backdrop")?.addEventListener("click", (e) => {
    if (e.target === $("modal_backdrop")) closeSettings();
  });
  $("btn_analyse")?.addEventListener("click", async () => {
    const mode = await showAnalyseModeModal();
    if (mode === null) return;
    runAnalyse(mode);
  });
  $("btn_email")?.addEventListener("click", sendEmailReport);
  $("btn_pick_output_folder")?.addEventListener("click", nativePickFolder);
  $("btn_pick_holdings_file")?.addEventListener("click", nativePickHoldingsFile);
  setupDropZone();
  setupRunPathInput();
  setupSourceRadios();
  $("analysis_enabled")?.addEventListener("change", syncAnalysisOptionsVisibility);
  $("llm_provider")?.addEventListener("change", syncLlmProviderFields);
  try {
    const data = await fetchConfig();
    applyRunPanelFromForm(data.form);
    if (data.config_warning) {
      const st = $("run_status");
      if (st) setStatus(st, data.config_warning, "err");
    }
  } catch {
    /* ignore — user may fix config later */
  }
});
