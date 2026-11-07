"use strict";

const $ = (s) => document.querySelector(s);
const fmtInt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const fmt2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// Each window has its own id, so several windows can show different
// contracts. "?w=..." in the address forces a new id (New window button).
let CLIENT = (() => {
  const fromUrl = new URLSearchParams(location.search).get("w");
  const id = fromUrl || sessionStorage.getItem("tlClient") ||
    Math.random().toString(36).slice(2, 10);
  sessionStorage.setItem("tlClient", id);
  return id;
})();
let state = null;       // /api/state
let last = null;        // last snapshot payload
let lastTopTs = null;   // to detect new bars
let chart = null, series = {};

// ---------------------------------------------------------------- helpers
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
const n = (v) => (v === null || v === undefined || v === "" ? "" : fmtInt.format(v));
const p = (v) => (v === null || v === undefined ? "" : fmt2.format(v));
const signed = (v) => (v === null || v === undefined ? "" : (v > 0 ? "+" : "") + fmtInt.format(v));
function short(v) {
  const a = Math.abs(v), s = v < 0 ? "-" : "+";
  if (a >= 1e7) return s + (a / 1e7).toFixed(2) + " Cr";
  if (a >= 1e5) return s + (a / 1e5).toFixed(2) + " L";
  return (v > 0 ? "+" : "") + fmtInt.format(v);
}
function showError(msg) {
  const e = $("#error");
  e.textContent = msg || "";
  e.classList.toggle("hidden", !msg);
}

// ---------------------------------------------------------------- controls
async function loadState(preferFeed) {
  state = await api("/api/state");
  $("#version").textContent = state.version ? `TradeLive v${state.version}` : "";
  const feedSel = $("#feed");
  feedSel.innerHTML = state.feeds.map((f) =>
    `<option value="${f.name}">${f.name === "replay" ? "Replay (saved files)" : (f.label || f.name) + " (live)"}</option>`).join("");
  // Keep the source the user is working with (e.g. right after logging in);
  // otherwise fall back to the last one used.
  const wanted = preferFeed || (last && last.feed) || state.settings.last_feed || "replay";
  feedSel.value = state.feeds.some((f) => f.name === wanted) ? wanted : "replay";
  setWindow(state.settings.window);
  setTopVol(state.settings.top_volumes);
  setTf(state.settings.timeframe);
  await onFeedChange();
}

function currentFeed() { return state.feeds.find((f) => f.name === $("#feed").value); }

async function onFeedChange(userPicked) {
  const f = currentFeed();
  $("#connectBtn").classList.toggle("hidden", f.name === "replay");
  $("#connectBtn").textContent = f.ready ? "Reconnect" : "Connect broker";
  const box = $("#pickers");
  box.innerHTML = "";
  if (f.name === "replay") {
    const items = await api("/api/instruments?feed=replay");
    if (!items.length) { box.innerHTML = `<i>Put .xlsx files in ${state.data_dir}</i>`; return; }
    box.innerHTML = `<label>File <select id="pFile">${items.map((i) =>
      `<option value="${encodeURIComponent(i.key)}">${i.label.replace("Replay: ", "")}</option>`).join("")}</select></label>
      <button id="pPlay" title="Start the replay from the beginning">▶ Play</button>`;
    const sel = $("#pFile");
    const lastReplay = (state.settings.last_by_feed || {}).replay;
    const prev = (lastReplay && lastReplay.key) ||
      (state.settings.last_feed === "replay" ? state.settings.last_instrument : "");
    if (prev && items.some((i) => i.key === prev)) sel.value = encodeURIComponent(prev);
    const play = () => select("replay", decodeURIComponent(sel.value));
    sel.onchange = play;
    $("#pPlay").onclick = play;
    // Switching the Source to Replay starts it right away (a single-file list
    // never fires "change", so it would otherwise look like nothing happens).
    if (userPicked && (!last || last.feed !== "replay")) play();
    return;
  }
  if (!f.ready) { box.innerHTML = "<i>Click “Connect broker” to log in</i>"; return; }
  // Symbol: type to search any F&O index or stock (e.g. NIFTY, RELIANCE).
  let symbols = [];
  try { symbols = await api(`/api/symbols?feed=${f.name}`); } catch (e) { showError(e.message); }
  const lastMeta = ((state.settings.last_by_feed || {})[f.name] || {}).meta || {};
  box.innerHTML = `
    <label>Symbol <input id="pSym" list="symList" value="${lastMeta.symbol || "NIFTY"}" size="12"
      autocomplete="off" title="Type an index or F&amp;O stock, e.g. NIFTY or RELIANCE"></label>
    <datalist id="symList">${symbols.map((s) =>
      `<option value="${s.symbol}">${s.kind === "index" ? "Index" : "Stock"}</option>`).join("")}</datalist>
    <label>Expiry <select id="pExp"></select></label>
    <label>Strike <select id="pStrike"></select></label>
    <span class="seg" id="pType"><button data-v="CE" class="on">CE</button><button data-v="PE">PE</button><button data-v="FUT">FUT</button><button data-v="EQ" title="Cash share (no OI)">Cash</button></span>
    <button id="pGo">Watch</button>`;
  let items = [];
  const symIn = $("#pSym"), expSel = $("#pExp"), strSel = $("#pStrike");
  let type = ["CE", "PE", "FUT", "EQ"].includes(lastMeta.option_type) ? lastMeta.option_type : "CE";
  const setType = (t) => {
    type = t;
    document.querySelectorAll("#pType button").forEach((x) => x.classList.toggle("on", x.dataset.v === t));
  };
  // Underlying price for picking the at-the-money strike: nearest futures.
  const underlying = () => {
    const futs = items.filter((i) => i.option_type === "FUT" && i.ref_price > 0)
      .sort((a, b) => a.expiry.localeCompare(b.expiry));
    if (futs.length) return futs[0].ref_price;
    const eq = items.find((i) => i.option_type === "EQ" && i.ref_price > 0);
    return eq ? eq.ref_price : 0;
  };
  const fillExp = () => {
    const hasEq = items.some((i) => i.option_type === "EQ");
    $("#pType button[data-v=EQ]").classList.toggle("hidden", !hasEq);
    if (type === "EQ" && !hasEq) setType("CE");
    if (type === "EQ") {
      expSel.innerHTML = ""; expSel.disabled = true; fillStrikes(); return;
    }
    expSel.disabled = false;
    const exps = [...new Set(items.filter((i) => i.option_type === type).map((i) => i.expiry))].sort();
    expSel.innerHTML = exps.map((e) => `<option>${e}</option>`).join("");
    if (lastMeta.expiry && exps.includes(lastMeta.expiry) && lastMeta.symbol === symIn.value) expSel.value = lastMeta.expiry;
    fillStrikes();
  };
  const fillStrikes = () => {
    if (type === "FUT" || type === "EQ") { strSel.innerHTML = ""; strSel.disabled = true; return; }
    strSel.disabled = false;
    const ks = [...new Set(items.filter((i) => i.expiry === expSel.value && i.option_type === type).map((i) => i.strike))].sort((a, b) => a - b);
    strSel.innerHTML = ks.map((k) => `<option>${k}</option>`).join("");
    const u = underlying();
    let idx = Math.floor(ks.length / 2);
    if (u > 0 && ks.length) idx = ks.reduce((best, k, j) => Math.abs(k - u) < Math.abs(ks[best] - u) ? j : best, 0);
    strSel.selectedIndex = idx;
    if (lastMeta.symbol === symIn.value && ks.includes(lastMeta.strike) && expSel.value === lastMeta.expiry) {
      strSel.value = String(lastMeta.strike);
    }
  };
  const loadSym = async () => {
    const sym = symIn.value.trim().toUpperCase();
    symIn.value = sym;
    if (!symbols.some((s) => s.symbol === sym)) { items = []; fillExp(); showError(sym ? `“${sym}” is not an F&O symbol on ${f.label || f.name}` : ""); return; }
    showError("");
    items = await api(`/api/instruments?feed=${f.name}&symbol=${encodeURIComponent(sym)}`);
    fillExp();
  };
  symIn.onchange = loadSym;
  expSel.onchange = fillStrikes;
  document.querySelectorAll("#pType button").forEach((b) => b.onclick = () => { setType(b.dataset.v); fillExp(); });
  $("#pGo").onclick = () => {
    const inst = items.find((i) => i.option_type === type &&
      (type === "EQ" || i.expiry === expSel.value) &&
      (type === "FUT" || type === "EQ" || String(i.strike) === strSel.value));
    if (inst) select(f.name, inst.key); else showError("Contract not found");
  };
  setType(type);
  await loadSym();
}

async function select(feed, key) {
  try { showError(""); lastTopTs = null; await api("/api/select", { feed, key, client: CLIENT }); }
  catch (e) { showError(e.message); }
}

function setTf(v) {
  document.querySelectorAll("#tf button").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === v));
}

async function openLogin() {
  const f = currentFeed();
  $("#loginTitle").textContent = `Connect to ${f.label || f.name}`;
  $("#loginHelp").textContent = f.login_help || "";
  $("#loginHelp").classList.toggle("hidden", !f.login_help);
  $("#loginFields").innerHTML = Object.entries(f.login_fields).map(([k, label]) => {
    const secret = /pass|otp|pin|secret/i.test(k);
    const val = f.saved[k] || "";
    return `<label>${label}</label><input name="${k}" ${secret ? 'type="password"' : ""} value="${val}" autocomplete="off">`;
  }).join("");
  $("#loginErr").classList.add("hidden");
  try {
    const c = await api(`/api/credentials?feed=${f.name}`);
    $("#rememberText").textContent = c.browser_login
      ? "Remember on this computer (the daily Zerodha login page then opens by itself each morning)"
      : "Remember on this computer and log in automatically every morning";
    $("#forgetBtn").classList.toggle("hidden", !c.saved);
    const info = $("#savedInfo");
    info.classList.toggle("hidden", !c.saved);
    info.textContent = c.saved ? (c.browser_login ? "A login is saved for this broker. " :
      c.auto ? "A login is saved: automatic daily login is ON. " : "A login is saved (add the TOTP secret for automatic login). ") +
      (c.status || "") + (c.encrypted ? "" : " (Not encrypted on this OS.)") : "";
  } catch (e) {}
  $("#loginDlg").showModal();
}

// Safety net while the broker's login page is open: check every 2 s whether
// the login completed (in case a live update was missed), for 5 minutes.
function waitForLogin(feedName) {
  clearInterval(window._loginPoll);
  const until = Date.now() + 5 * 60 * 1000;
  window._loginPoll = setInterval(async () => {
    if (Date.now() > until) return clearInterval(window._loginPoll);
    try {
      const st = await api("/api/state");
      const f = st.feeds.find((x) => x.name === feedName);
      if (f && f.ready) {
        clearInterval(window._loginPoll);
        if ($("#loginDlg").open) $("#loginDlg").close();
        await loadState(feedName);
      }
    } catch (e) {}
  }, 2000);
}

async function doLogin(ev) {
  ev.preventDefault();
  const f = currentFeed();
  const fields = {};
  $("#loginFields").querySelectorAll("input").forEach((i) => fields[i.name] = i.value.trim());
  fields.remember = $("#remember").checked ? "1" : "0";
  $("#loginGo").disabled = true;
  try {
    const r = await api("/api/login", { feed: f.name, fields });
    if (r.open_url) {  // e.g. Zerodha: log in on the broker's own page
      window.open(r.open_url, "_blank");
      waitForLogin(f.name);
      $("#loginErr").textContent = "Zerodha's login page opened in a new window. Log in there – " +
        "you'll be sent back and TradeLive continues automatically.";
      $("#loginErr").classList.remove("hidden");
      return;
    }
    $("#loginDlg").close();
    await loadState(f.name);
  } catch (e) {
    $("#loginErr").textContent = e.message;
    $("#loginErr").classList.remove("hidden");
  } finally { $("#loginGo").disabled = false; }
}

// ---------------------------------------------------------------- rendering
function kpi(id, value, sub, tone) {
  const el = $(id);
  el.querySelector(".v").textContent = value;
  el.querySelector(".s").textContent = sub || "";
  el.classList.toggle("good", tone === "good");
  el.classList.toggle("bad", tone === "bad");
}

const cls = (v, invert) => (v === null || v === undefined || v === "" || v === 0 ? "" :
  ((v > 0) !== !!invert ? "g" : "r"));

function render(p) {
  last = p;
  if (state && p.feeds_ready) {
    const changed = state.feeds.some((f) => p.feeds_ready[f.name] !== undefined && p.feeds_ready[f.name] !== f.ready);
    if (changed && !window._reloadingState) {
      window._reloadingState = true;
      const cur = $("#feed").value;
      const loggedIn = Object.keys(p.feeds_ready).find((n) => p.feeds_ready[n] &&
        !(state.feeds.find((f) => f.name === n) || {}).ready);
      if (loggedIn && $("#loginDlg").open) $("#loginDlg").close();
      loadState(loggedIn || cur).finally(() => { window._reloadingState = false; });
    }
  }
  showError(p.error || "");
  $("#notice").textContent = p.notice || "";
  $("#notice").classList.toggle("hidden", !p.notice);
  if (state && p.feed && p.feed !== "replay" && !window._autoReloaded) {
    const f = state.feeds.find((x) => x.name === p.feed);
    if (f && !f.ready) {  // logged in automatically since the page loaded
      window._autoReloaded = true;
      loadState(p.feed).catch(() => {});
    }
  }
  $("#instName").textContent = p.instrument || "Choose an instrument";
  document.title = p.instrument ? `${p.instrument} – TradeLive` : "TradeLive";
  const s = p.summary || {};
  const srcLabel = p.feed === "replay" ? "REPLAY" : p.feed ? "LIVE" : "";
  $("#srcBadge").textContent = srcLabel;
  $("#srcBadge").className = "badge " + (srcLabel === "LIVE" ? "live" : srcLabel ? "replay" : "hidden");
  $("#lastTs").textContent = s.ts ? `last bar ${s.ts}` : "";
  if (!s.ts) {
    for (const id of ["#kOi", "#kOiChg", "#kFlow", "#kVwap", "#kPsar", "#kSig", "#kVol"]) kpi(id, "–", "", "");
  }
  if (p.settings) {
    setTf(p.settings.timeframe); setWindow(p.settings.window);
    if (p.settings.top_volumes !== undefined) setTopVol(p.settings.top_volumes);
  }
  if (s.ts) {
    const w = s.window;
    const allRows = !p.settings || !p.settings.window;
    const winLabel = allRows ? `all ${w} bars` : `over last ${w} bars`;
    if (s.oi === null || s.oi === undefined) {
      kpi("#kOi", "No OI yet", "OI is recorded during market hours", "");
    } else {
      const exact = s.oi >= 1e5 ? fmtInt.format(s.oi) : "";
      const lastDoi = s.d_oi !== null && s.d_oi !== undefined ? `last bar ${short(s.d_oi)}` : "latest bar";
      kpi("#kOi", shortVol(s.oi), exact ? `${exact} · ${lastDoi}` : lastDoi, "");
    }
    if (s.oi_change === null || s.oi_change === undefined) {
      kpi("#kOiChg", "No OI yet", winLabel, "");
    } else {
      const pct = s.oi_change_pct !== null && s.oi_change_pct !== undefined
        ? `${s.oi_change_pct > 0 ? "+" : ""}${s.oi_change_pct.toFixed(2)}% · ` : "";
      kpi("#kOiChg", short(s.oi_change), `${pct}${winLabel}`, s.oi_change > 0 ? "good" : s.oi_change < 0 ? "bad" : "");
    }
    if (s.net_flow === null || s.net_flow === undefined) {
      kpi("#kFlow", "No OI yet", "OI is recorded while the app runs during market hours", "");
    } else {
      kpi("#kFlow", short(s.net_flow), winLabel, s.net_flow > 0 ? "good" : s.net_flow < 0 ? "bad" : "");
    }
    kpi("#kVwap", s.above_vwap ? "Above" : "Below", `VWAP/Close ${p2(s.vwap_ratio)}`, s.above_vwap ? "good" : "bad");
    kpi("#kPsar", s.psar_bias === "BULL" ? "Bullish" : "Bearish", "SAR " + (s.psar_bias === "BULL" ? "below" : "above") + " price", s.psar_bias === "BULL" ? "good" : "bad");
    kpi("#kSig", s.signal || "–", s.signal ? `${s.signal_name} · ${s.streak} bar${s.streak > 1 ? "s" : ""} in a row` : "no change",
      s.signal === "LB" || s.signal === "SC" ? "good" : s.signal ? "bad" : "");
    kpi("#kVol", s.bars_since_vol_spike === 0 ? "Now" : `${s.bars_since_vol_spike} bars ago`, "biggest volume in window", "");
  }

  renderWatch(p);
  renderSigStats(s.signal_stats);
  const rows = p.rows || [];
  const w = s.window || rows.length;
  const html = [];
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    const inWin = i >= rows.length - w;
    html.push(`<tr class="${inWin ? "inwin" : ""}${i === rows.length - 1 && r.ts !== lastTopTs && lastTopTs ? " new" : ""}">
      <td>${r.ts.slice(5)}</td><td>${p2(r.open)}</td><td>${p2(r.high)}</td><td>${p2(r.low)}</td><td>${p2(r.close)}</td>
      <td class="${volCls(r)}" title="${r.vol_rank ? "Volume rank #" + r.vol_rank + " in counted rows" : ""}">${r.vol_rank > 1 ? `<sup class="rk">${r.vol_rank}</sup>` : ""}${n(r.volume)}</td><td>${p2(r.psar)}</td>
      <td class="${r.oi_max ? "r" : r.oi_min ? "g" : ""}">${n(r.oi)}</td><td>${n(r.obv)}</td><td>${p2(r.vwap)}</td>
      <td class="sep ${r.vwap_ratio > 1 ? "r" : r.vwap_ratio < 1 ? "g" : ""}">${p2(r.vwap_ratio)}</td>
      <td class="${cls(r.d_obv)}">${signed(r.d_obv)}</td>
      <td class="${cls(r.oi_up)}">${signed(r.oi_up)}</td>
      <td class="${cls(r.oi_down)}">${signed(r.oi_down)}</td>
      <td class="${cls(r.net_flow)}">${signed(r.net_flow)}</td>
      <td>${r.signal ? `<b class="sig ${r.signal}" title="${r.signal_name}">${r.signal}</b>` : ""}</td></tr>`);
  }
  $("#grid tbody").innerHTML = html.length ? html.join("") :
    (p.id ? `<tr><td colspan="16" class="empty">${p.error ? "" : p.loaded
      ? "No trades yet today for this contract – rows appear with the first trade."
      : "Loading today's data…"}</td></tr>` : "");

  const top = rows.length ? rows[rows.length - 1].ts : null;
  if (top && lastTopTs && top !== lastTopTs) maybeAlert(rows, s);
  lastTopTs = top;
  if (chart) drawChart(rows);
}
function p2(v) { return p(v); }
// #1 volume keeps the sheet's yellow; #2..#N get amber.
function volCls(r) { return r.vol_rank === 1 || r.vol_max ? "y" : r.vol_rank > 1 ? "v2" : ""; }
function setTopVol(v) {
  const sel = $("#topVol"), val = String(v === undefined ? 5 : v);
  if (![...sel.options].some((o) => o.value === val)) {
    const o = document.createElement("option"); o.value = val; o.textContent = val; sel.appendChild(o);
  }
  sel.value = val;
  $("#topVolLegend").textContent = Number(val) > 1 ? `top ${val} volumes (#2–#${val})` : "";
  $("#topVolLegendWrap").classList.toggle("hidden", Number(val) <= 1);
}

// ---------------------------------------------------------------- signal summary
function shortVol(v) {
  const a = Math.abs(v);
  if (a >= 1e7) return (v / 1e7).toFixed(2) + " Cr";
  if (a >= 1e5) return (v / 1e5).toFixed(2) + " L";
  return fmtInt.format(v);
}
function renderSigStats(st) {
  const wrap = $("#sigStats");
  if (!st || !st.with_signal) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  for (const k of ["LB", "SC", "SB", "LU"]) {
    const x = st[k];
    const pct = st.rows ? Math.round(x.bars / st.rows * 100) : 0;
    $("#st" + k).innerHTML = x.bars
      ? `<b>${x.bars}</b> bars <span class="muted">(${pct}%)</span><br>OI ${short(x.oi)} · Vol ${shortVol(x.volume)}`
      : `<span class="muted">none</span>`;
  }
  const share = st.bull_share;
  const bull = $("#sbBull"), bear = $("#sbBear");
  if (share === null || share === undefined) {
    bull.style.width = "50%"; bear.style.width = "50%";
    bull.textContent = `Price-up bars (LB+SC): ${st.bull_bars}`; bear.textContent = `Price-down bars (SB+LU): ${st.bear_bars}`;
  } else {
    const b = Math.round(share * 100);
    bull.style.width = Math.max(b, 0) + "%"; bear.style.width = Math.max(100 - b, 0) + "%";
    bull.textContent = b >= 12 ? `Price-up bars (LB+SC): ${st.bull_bars} bars, ${b}% of OI moved` : "";
    bear.textContent = 100 - b >= 12 ? `Price-down bars (SB+LU): ${st.bear_bars} bars, ${100 - b}% of OI moved` : "";
  }
  wrap.title = `Signals over the ${st.rows} counted rows. Bar = share of OI moved (size of OI change) on price-up bars (LB+SC) ` +
    `vs price-down bars (SB+LU). Net OI flow = OI change on up-bars minus OI change on down-bars = (LB + SC) − (SB + LU).`;
}

// ---------------------------------------------------------------- watchlist
const SIG_ORDER = { SB: 0, LU: 1, SC: 2, LB: 3 };
const watchSeen = {};  // id -> {ts, signal, above}
function renderWatch(p) {
  const list = p.watchlist || [];
  const recent = p.recent || [];
  $("#watchWrap").classList.toggle("hidden", !list.length && !recent.length);
  $("#watch").classList.toggle("hidden", !list.length);
  const rl = $("#recentLine");
  rl.classList.toggle("hidden", !recent.length);
  rl.innerHTML = recent.length ? `<span title="Opened today; still recording in the background so nothing is lost">Also recording:</span>` +
    recent.map((r) => `<span class="chip2"><button data-id="${encodeURIComponent(r.id)}" title="Open">${r.label}</button>` +
      `<button class="cx" data-stop="${encodeURIComponent(r.id)}" title="Stop recording this one">✕</button></span>`).join("") +
    `<button class="wclear" id="recentClear" title="Stop recording all of these in the background">Stop all</button>` : "";
  $("#watchCount").textContent = list.length ? `${list.length} / ${p.max_watch}` : "";
  const pin = $("#pinBtn");
  pin.classList.toggle("hidden", !p.id);
  pin.classList.toggle("on", !!p.pinned);
  pin.textContent = p.pinned ? "★ In watchlist" : "☆ Add to watchlist";
  const how = $("#watchSort").value;
  const sorted = [...list];
  const num = (v, d) => (v === null || v === undefined ? d : v);
  if (how === "signal") sorted.sort((a, b) => num(SIG_ORDER[a.signal], 9) - num(SIG_ORDER[b.signal], 9) || num(b.streak, 0) - num(a.streak, 0));
  if (how === "oichg") sorted.sort((a, b) => num(b.oi_change, -1e18) - num(a.oi_change, -1e18));
  if (how === "flow") sorted.sort((a, b) => num(b.net_flow, -1e18) - num(a.net_flow, -1e18));
  if (how === "spike") sorted.sort((a, b) => num(a.bars_since_vol_spike, 1e9) - num(b.bars_since_vol_spike, 1e9));
  if (how === "chg") sorted.sort((a, b) => num(b.chg_pct, -1e9) - num(a.chg_pct, -1e9));
  const html = sorted.map((w) => {
    const prev = watchSeen[w.id];
    const changed = prev && w.ts && prev.ts !== w.ts && prev.signal !== w.signal && w.signal;
    if (prev && w.ts && prev.ts !== w.ts) watchAlert(w, prev);
    watchSeen[w.id] = { ts: w.ts, signal: w.signal, above: w.above_vwap };
    if (!w.ts) {
      return `<tr data-id="${encodeURIComponent(w.id)}" class="${w.selected ? "sel" : ""}"><td>${w.label}</td>
        <td colspan="10" class="err">${w.error || (w.loaded ? "No trades yet today for this contract – rows appear with the first trade." : "loading…")}</td>
        <td class="x"><button data-x="1" title="Remove from watchlist">✕</button></td></tr>`;
    }
    const tone = (v) => (v > 0 ? "g" : v < 0 ? "r" : "");
    const oiChgPct = w.oi_change_pct !== null && w.oi_change_pct !== undefined
      ? ` (${w.oi_change_pct > 0 ? "+" : ""}${w.oi_change_pct.toFixed(2)}%)` : "";
    return `<tr data-id="${encodeURIComponent(w.id)}" class="${w.selected ? "sel" : ""}${changed ? " flash" : ""}">
      <td>${w.label}${w.error ? ` <span title="${w.error}">⚠️</span>` : ""}</td>
      <td>${p2(w.close)}</td>
      <td class="${tone(w.chg_pct)}">${w.chg_pct === null || w.chg_pct === undefined ? "" : (w.chg_pct > 0 ? "+" : "") + w.chg_pct.toFixed(2) + "%"}</td>
      <td class="${w.above_vwap ? "g" : "r"}">${w.above_vwap ? "Above" : "Below"}</td>
      <td class="${w.psar_bias === "BULL" ? "g" : "r"}">${w.psar_bias === "BULL" ? "Bull" : "Bear"}</td>
      <td>${w.signal ? `<b class="sig ${w.signal}" title="${w.signal_name}">${w.signal}</b> ×${w.streak}` : (w.oi_seen ? "" : '<span class="muted">no OI yet</span>')}</td>
      <td title="${w.oi !== null && w.oi !== undefined ? fmtInt.format(w.oi) : ""}">${w.oi === null || w.oi === undefined ? "" : shortVol(w.oi)}</td>
      <td class="${tone(w.oi_change)}" title="${w.oi_change !== null && w.oi_change !== undefined ? signed(w.oi_change) + oiChgPct : ""}">${w.oi_change === null || w.oi_change === undefined ? "" : short(w.oi_change)}</td>
      <td class="${tone(w.net_flow)}">${w.net_flow === null || w.net_flow === undefined ? "" : short(w.net_flow)}</td>
      <td class="${w.bars_since_vol_spike === 0 ? "y" : ""}">${w.bars_since_vol_spike === 0 ? "now" : w.bars_since_vol_spike + " bars ago"}</td>
      <td class="muted">${w.ts.slice(11)}</td>
      <td class="x"><button data-x="1" title="Remove from watchlist">✕</button></td></tr>`;
  }).join("");
  $("#watch tbody").innerHTML = html;
}
function watchAlert(w, prev) {
  if (!$("#sound").checked) return;
  const crossed = prev.above !== undefined && prev.above !== w.above_vwap;
  if (w.streak === 3 || crossed) beep(w.signal === "LB" || w.signal === "SC" || (crossed && w.above_vwap) ? 880 : 440);
}
$("#watch tbody").addEventListener("click", (ev) => {
  const tr = ev.target.closest("tr");
  if (!tr || !tr.dataset.id) return;
  const id = decodeURIComponent(tr.dataset.id);
  if (ev.target.dataset.x) {
    api("/api/watchlist", { id, pinned: false }).catch((e) => showError(e.message));
  } else {
    lastTopTs = null;
    api("/api/select", { id, client: CLIENT }).catch((e) => showError(e.message));
  }
});
$("#recentLine").addEventListener("click", (ev) => {
  if (ev.target.id === "recentClear") {
    api("/api/watchlist", { clear_recent: true }).catch((e) => showError(e.message));
    return;
  }
  const x = ev.target.closest("button[data-stop]");
  if (x) {
    api("/api/watchlist", { clear_recent: true, id: decodeURIComponent(x.dataset.stop) })
      .catch((e) => showError(e.message));
    return;
  }
  const b = ev.target.closest("button[data-id]");
  if (!b) return;
  lastTopTs = null;
  api("/api/select", { id: decodeURIComponent(b.dataset.id), client: CLIENT }).catch((e) => showError(e.message));
});
$("#helpBtn").onclick = () => $("#helpDlg").showModal();
$("#helpClose").onclick = () => $("#helpDlg").close();
$("#helpDlg").addEventListener("click", (ev) => { if (ev.target.id === "helpDlg") $("#helpDlg").close(); });
if (!localStorage.getItem("helpSeen")) {  // first start: show the guide once
  localStorage.setItem("helpSeen", "1");
  setTimeout(() => $("#helpDlg").showModal(), 800);
}
$("#watchSort").value = localStorage.getItem("watchSort") || "added";
$("#watchSort").onchange = () => { localStorage.setItem("watchSort", $("#watchSort").value); if (last) renderWatch(last); };
$("#watchClear").onclick = () => {
  if (!confirm("Remove all contracts from the watchlist and stop all background recording?")) return;
  api("/api/watchlist", { clear: true, and_recent: true }).catch((e) => showError(e.message));
};
$("#pinBtn").onclick = () => {
  if (!last || !last.id) return;
  api("/api/watchlist", { id: last.id, pinned: !last.pinned }).catch((e) => showError(e.message));
};

// ---------------------------------------------------------------- alerts
let audioCtx = null;
// With several windows open, only one of them ("the beeper") plays sounds.
function isBeeper() {
  const now = Date.now();
  let cur = null;
  try { cur = JSON.parse(localStorage.getItem("tlBeeper") || "null"); } catch (e) {}
  if (!cur || cur.id === CLIENT || now - cur.t > 5000) {
    localStorage.setItem("tlBeeper", JSON.stringify({ id: CLIENT, t: now }));
    return true;
  }
  return false;
}
setInterval(() => { if ($("#sound").checked) isBeeper(); }, 2000);
function beep(freq, force) {
  if (!force && !isBeeper()) return;
  audioCtx = audioCtx || new AudioContext();
  const o = audioCtx.createOscillator(), g = audioCtx.createGain();
  o.frequency.value = freq; o.connect(g); g.connect(audioCtx.destination);
  g.gain.setValueAtTime(0.2, audioCtx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.4);
  o.start(); o.stop(audioCtx.currentTime + 0.4);
}
function maybeAlert(rows, s) {
  if (!$("#sound").checked || rows.length < 2) return;
  const a = rows[rows.length - 1], b = rows[rows.length - 2];
  const crossed = (a.vwap_ratio < 1) !== (b.vwap_ratio < 1);
  if (s.streak === 3 || crossed) beep(s.signal === "LB" || s.signal === "SC" || (crossed && a.vwap_ratio < 1) ? 880 : 440);
}

// ---------------------------------------------------------------- chart
function toTime(ts) { return Math.floor(Date.parse(ts.replace(" ", "T") + ":00Z") / 1000); }
function initChart() {
  if (!window.LightweightCharts) { showError("Chart library failed to load"); return; }
  const el = $("#chart");
  chart = LightweightCharts.createChart(el, { autoSize: true, timeScale: { timeVisible: true, secondsVisible: false },
    rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0.3 } } });
  series.candle = chart.addCandlestickSeries();
  series.vwap = chart.addLineSeries({ color: "#2563eb", lineWidth: 2, title: "VWAP" });
  series.psar = chart.addLineSeries({ color: "#a855f7", lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 2, title: "PSAR" });
  series.vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" } });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
  series.oi = chart.addLineSeries({ priceScaleId: "oi", color: "#f59e0b", lineWidth: 2, title: "OI" });
  chart.priceScale("oi").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 }, visible: false });
  if (last) drawChart(last.rows || []);
}
function drawChart(rows) {
  const t = rows.map((r) => toTime(r.ts));
  series.candle.setData(rows.map((r, i) => ({ time: t[i], open: r.open, high: r.high, low: r.low, close: r.close })));
  series.vwap.setData(rows.map((r, i) => ({ time: t[i], value: r.vwap })));
  series.psar.setData(rows.filter((r) => r.psar !== null).map((r) => ({ time: toTime(r.ts), value: r.psar })));
  series.vol.setData(rows.map((r, i) => ({ time: t[i], value: r.volume, color: (r.d_obv || 0) >= 0 ? "#86efac" : "#fca5a5" })));
  series.oi.setData(rows.map((r, i) => ({ time: t[i], value: r.oi })));
  // Mark only the counted rows so the chart stays readable.
  const w = (last && last.summary && last.summary.window) || rows.length;
  series.candle.setMarkers(rows.slice(-w).filter((r) => r.signal === "LB" || r.signal === "SB").map((r) => ({
    time: toTime(r.ts), position: r.signal === "LB" ? "belowBar" : "aboveBar",
    color: r.signal === "LB" ? "#15803d" : "#b91c1c", shape: r.signal === "LB" ? "arrowUp" : "arrowDown", text: r.signal,
  })));
}

// ---------------------------------------------------------------- live socket
let shuttingDown = false;
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  const ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 20000);
  ws.onopen = () => {
    $("#status").className = "dot ok";
    ws.send(JSON.stringify({ type: "hello", client: CLIENT }));
  };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "shutdown") {
      shuttingDown = true;
      clearInterval(ping);
      window.close();
      return;
    }
    if (m.type === "new_id") {  // duplicated tab: this window gets its own id
      CLIENT = m.client;
      sessionStorage.setItem("tlClient", CLIENT);
      $("#exportBtn").href = `/api/export.xlsx?client=${encodeURIComponent(CLIENT)}`;
      return;
    }
    if (m.type === "snapshot") render(m);
  };
  ws.onclose = () => {
    clearInterval(ping);  // one keep-alive timer per connection, no pile-up
    $("#status").className = "dot bad";
    if (!shuttingDown) setTimeout(connect, 2000);
  };
}

// ---------------------------------------------------------------- wire up
$("#feed").onchange = () => onFeedChange(true);
$("#topVol").onchange = () => {
  setTopVol($("#topVol").value);
  api("/api/settings", { top_volumes: Number($("#topVol").value) });
};
$("#connectBtn").onclick = openLogin;
$("#loginForm").addEventListener("submit", (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  doLogin(ev);
});
function setWindow(v) {
  const sel = $("#window"), val = String(v || 0);
  if (![...sel.options].some((o) => o.value === val)) {
    const o = document.createElement("option");
    o.value = val; o.textContent = val;
    sel.insertBefore(o, sel.querySelector('option[value="custom"]'));
  }
  sel.value = val;
  sel.dataset.current = val;
}
$("#window").onchange = () => {
  const sel = $("#window");
  let v = sel.value;
  if (v === "custom") {
    const ans = prompt("How many recent rows should be counted? (0 = all rows)", sel.dataset.current || "0");
    const num = parseInt(ans, 10);
    if (ans === null || isNaN(num) || num < 0) { sel.value = sel.dataset.current || "0"; return; }
    v = String(num);
  }
  setWindow(v);
  api("/api/settings", { window: Number(v) });
};
document.querySelectorAll("#tf button").forEach((b) => b.onclick = () => {
  setTf(Number(b.dataset.v)); api("/api/settings", { timeframe: Number(b.dataset.v) });
});
$("#chartBtn").onclick = () => {
  const wrap = $("#chartWrap");
  wrap.classList.toggle("hidden");
  $("#chartBtn").textContent = wrap.classList.contains("hidden") ? "Show chart" : "Hide chart";
  if (!chart && !wrap.classList.contains("hidden")) initChart();
};
$("#sound").checked = localStorage.getItem("sound") === "1";
$("#sound").onchange = () => { localStorage.setItem("sound", $("#sound").checked ? "1" : "0"); if ($("#sound").checked) beep(660, true); };

$("#forgetBtn").onclick = async () => {
  if (!confirm("Delete the saved login from this computer?")) return;
  await fetch(`/api/credentials?feed=${currentFeed().name}`, { method: "DELETE" });
  $("#forgetBtn").classList.add("hidden");
  $("#savedInfo").textContent = "Saved login deleted.";
};
api("/api/autostart").then((a) => {
  $("#autostartWrap").classList.toggle("hidden", !a.supported);
  $("#autostart").checked = a.enabled;
}).catch(() => {});
$("#autostart").onchange = () => api("/api/autostart", { enabled: $("#autostart").checked })
  .catch((e) => { showError(e.message); $("#autostart").checked = !$("#autostart").checked; });

$("#exportBtn").href = `/api/export.xlsx?client=${encodeURIComponent(CLIENT)}`;
$("#newWinBtn").onclick = () => {
  const id = Math.random().toString(36).slice(2, 10);
  window.open(`/?w=${id}`, `tl_${id}`, "popup,width=1500,height=950");
};
loadState().catch((e) => showError(e.message));
connect();
