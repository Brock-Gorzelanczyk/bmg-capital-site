/* BMG terminal shell for the brain's pages: the top bar (Back, the pages, clock, where prices come from) and the ticker
   tape (companies reporting soon, the Crawler's latest reads, last prices). Read only: it calls GET endpoints of the
   local Crawler and never writes. Prices and report dates are SECONDARY context, never a source for a figure. */
(() => {
"use strict";
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
async function getJSON(url, opt) {
  const r = await fetch(url, Object.assign({cache: "no-store"}, opt || {}));
  let d = null; try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) throw new Error((d && (d.error || d.reason)) || `${url} answered ${r.status}`);
  return d;
}
const money = v => {
  if (typeof v !== "number" || !isFinite(v)) return "";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  return `${s}$${a.toLocaleString()}`;
};
const pct = v => (typeof v === "number" && isFinite(v)) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : "";
const cls = v => (typeof v === "number" && v !== 0) ? (v > 0 ? "up" : "down") : "";
const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const erLabel = e => { if (!e) return ""; const d = new Date(e.date + "T12:00:00"); return `${DAYS[d.getDay()]} ${e.date.slice(5).replace("-", "/")}${e.hour ? (e.hour === "before open" ? " BMO" : e.hour === "after close" ? " AMC" : "") : ""}`; };

const PAGES = [["home", "/", "Home"], ["crawler", "/desk/read/", "Crawler"], ["brain", "/desk/brain/", "Brain"],
               ["mastermind", "/desk/brain/mastermind/", "Mastermind"], ["desk", "/desk/", "Desk"]];

function nav(active) {
  const top = document.createElement("header");
  top.id = "bmg-top";
  top.innerHTML = `<span class="logo">BMG</span><a id="bmg-back" href="/desk/read/">&larr; Back</a>` +
    PAGES.map(([k, h, l]) => `<a href="${h}"${k === active ? ' class="on" aria-current="page"' : ""}>${l}</a>`).join("") +
    `<span class="sp"></span><span class="clock" id="bmg-clock"></span><span class="feed" id="bmg-feed"></span>`;
  document.body.prepend(top);
  top.querySelector("#bmg-back").addEventListener("click", e => {
    let same = false;
    try { const u = new URL(document.referrer); same = u.origin === location.origin && u.pathname + u.search !== location.pathname + location.search; } catch (_) {}
    if (same && history.length > 1) { e.preventDefault(); history.back(); }
  });
  const ck = top.querySelector("#bmg-clock");
  const tick = () => { try { ck.textContent = new Date().toLocaleTimeString("en-US", {timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}) + " NY"; } catch (_) {} };
  tick(); setInterval(tick, 1000);
  getJSON("/api/market/status").then(s => {
    top.querySelector("#bmg-feed").innerHTML = s.finnhub_key ? `PRICES <b>LIVE</b> FINNHUB` : `PRICES <b class="delayed">DELAYED</b> YAHOO`;
    top.querySelector("#bmg-feed").title = s.how_to_go_live || s.quotes_source;
  }).catch(() => {});
}

// ------------------------------------------------------------------ quotes (shared cache, one request per batch)
const QUOTES = {};
async function quotes(tickers) {
  const want = [...new Set(tickers.filter(Boolean))].filter(t => !QUOTES[t] || Date.now() - QUOTES[t]._at > 55000);
  for (let i = 0; i < want.length; i += 60) {
    try {
      const d = await getJSON("/api/market/quotes?tickers=" + encodeURIComponent(want.slice(i, i + 60).join(",")));
      for (const [t, q] of Object.entries(d.quotes || {})) if (q && !q.error) QUOTES[t] = Object.assign({_at: Date.now()}, q);
    } catch (e) { break; }
  }
  const out = {}; for (const t of tickers) if (QUOTES[t]) out[t] = QUOTES[t];
  return out;
}

let EARN = null;
async function earnings(days) {
  if (EARN) return EARN;
  try { EARN = await getJSON(`/api/market/earnings?days=${days || 14}`); } catch (e) { EARN = {events: [], error: e.message}; }
  EARN.by = {}; for (const e of EARN.events || []) if (!EARN.by[e.ticker]) EARN.by[e.ticker] = e;
  return EARN;
}

// ------------------------------------------------------------------ ticker tape
async function tape(opts) {
  opts = opts || {};
  const bar = document.createElement("div");
  bar.id = "bmg-tape"; bar.setAttribute("aria-label", "Ticker tape: companies reporting soon, latest reads, last prices");
  bar.innerHTML = `<span class="lbl">BMG</span><div class="rail"><div class="run"></div></div>`;
  document.body.appendChild(bar);
  const run = bar.querySelector(".run");
  let reads = [];
  try { reads = ((await getJSON("/api/reads")).reads || []).slice(0, 14); } catch (e) { reads = []; }
  const er = await earnings(14);
  const soon = (er.events || []).slice(0, 16);
  async function draw() {
    const extra = (typeof opts.tickers === "function" ? opts.tickers() : opts.tickers) || [];
    const tick = [...new Set([...extra, ...soon.map(e => e.ticker), ...reads.map(r => (r.ticker || "").toUpperCase())])].filter(Boolean).slice(0, 60);
    const q = await quotes(tick);
    const items = [];
    const qt = t => q[t] ? ` <span class="num">${q[t].price.toFixed(2)}</span> <span class="num ${cls(q[t].change_pct)}">${pct(q[t].change_pct)}</span>` : "";
    for (const e of soon) items.push(`<a class="it" href="/desk/brain/company/?t=${encodeURIComponent(e.ticker)}"><span class="tag er">ER</span><span class="k">${esc(e.ticker)}</span>${esc(erLabel(e))}${qt(e.ticker)}</a>`);
    for (const r of reads) { const t = (r.ticker || "").toUpperCase(); if (!t) continue;
      items.push(`<a class="it" href="/desk/brain/company/?t=${encodeURIComponent(t)}"><span class="tag">READ</span><span class="k">${esc(t)}</span>${esc(r.form || "")} ${esc((r.period || "").replace(/^(Fiscal year|Quarter) ended /, ""))}${qt(t)}</a>`); }
    for (const t of extra) if (!soon.some(e => e.ticker === t) && !reads.some(r => (r.ticker || "").toUpperCase() === t) && q[t])
      items.push(`<a class="it" href="/desk/brain/company/?t=${encodeURIComponent(t)}"><span class="k">${esc(t)}</span>${qt(t)}</a>`);
    if (!items.length) items.push(`<span class="it mute">No reads or report dates yet. Read a filing in the Crawler.</span>`);
    const html = items.join("");
    run.innerHTML = html + html;               // twice, so the loop is seamless
    run.style.setProperty("--dur", Math.max(40, items.length * 6) + "s");
  }
  await draw();
  setInterval(() => { if (!document.hidden) draw(); }, 60000);
}

window.BMG = {esc, getJSON, money, pct, cls, nav, tape, quotes, earnings, erLabel};
})();
