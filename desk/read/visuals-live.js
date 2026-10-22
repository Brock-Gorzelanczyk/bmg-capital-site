/* Opt-in live report panels. The caller owns the crawler, its arrivals and the recording.
   This module lays out supplied evidence; it never reads a filing or computes a financial value. */
(function(){
"use strict";
if (window.BMGLiveCrawl) return;
const layouts = [
  {id: "evidence", name: "Evidence board", line: "A stream of findings with their source receipts."},
  {id: "scoreboard", name: "Scoreboard", line: "Reported figures land in a compact grid."},
  {id: "spotlight", name: "Spotlight", line: "The featured finding leads, with the rest close by."}
];
const colors = {default:"#6ff4ff",electric:"#bd8cff",acid:"#b7ff3c",sunset:"#ffae45",arctic:"#65baff",laser:"#ff5c6b",
  magenta:"#ff3df2",gold:"#ffd23f",toxic:"#39ff6a",tron:"#00ffd5",bubblegum:"#ff8fd1",ticker:"#00e676",terminal:"#ffa028",
  chrome:"#e8eef5",coral:"#ff7f6a",ultraviolet:"#8a6bff",synthwave:"#ff2e97",icefire:"#9be7ff",aurora:"#4dffb8",cobalt:"#3d7bff",rosegold:"#ffb59e"};
const kinds = {figure:"Figure",ratio:"Ratio",observation:"Observation",trade:"Reported transaction",holding:"Reported holding",guidance:"Guidance"};
const text = x => x == null ? "" : typeof x === "object" ? "" : String(x);
const esc = x => text(x).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const period = x => x && typeof x === "object" ? text(x.label || x.short || x.end) : text(x);
function sourceLabel(s){
  if (typeof s === "string") return s.trim();
  if (Array.isArray(s)) return [...new Set(s.map(sourceLabel).filter(Boolean))].join("; ");
  if (!s || typeof s !== "object") return "";
  const a = [], p = s.printed_page, pdf = s.pdf_page || s.page;
  if (p != null && text(p)) a.push("p." + text(p) + (pdf && text(pdf) !== text(p) ? " (PDF p." + text(pdf) + ")" : ""));
  else if (pdf) a.push("PDF p." + text(pdf));
  if (Array.isArray(s.input_pages)){
    const pages = [...new Set(s.input_pages.filter(p => Number.isInteger(p) && p > 0))];
    if (pages.length) a.push("Input PDF " + (pages.length === 1 ? "p." : "pages ") + pages.join(", "));
  }
  if (s.accession) a.push("SEC " + text(s.accession));
  if (s.anchor) a.push("XBRL " + text(s.anchor));
  if (s.location) a.push(text(s.location));
  if (!a.length && Number.isFinite(s.offset) && s.offset >= 0) a.push("Document offset " + s.offset);
  if (s.citation) a.push(text(s.citation));
  if (s.sources) a.push(sourceLabel(s.sources));
  return [...new Set(a.filter(Boolean))].join(" · ");
}
function normalize(x){
  x = x || {};
  const citation = [...new Set([sourceLabel(x.source), sourceLabel(x.sources), x.page ? "PDF p." + x.page : ""].filter(Boolean))].join("; ");
  const raw = x.value != null ? x.value : x.display;
  const supplied = raw !== null && raw !== undefined && text(raw).trim() !== "" && !/^(n\/a|nan|infinity|undefined|null|not available|not supplied)$/i.test(text(raw).trim());
  const quote = (x.kind === "observation" || x.kind === "guidance") && !supplied && citation ? text(x.text || x.headline) : "";
  const value = supplied && citation ? text(raw) : quote || "Not supplied";
  const d = x.detail || {}, detail = x.kind === "trade" ? [d.action,d.shares ? text(d.shares) + " shares" : "",d.price ? "at " + text(d.price) : "",d.title]
    : x.kind === "holding" ? [d.class,d.put_call,d.shares ? "Reported amount: " + text(d.shares) : "",d.weight,d.change] : [];
  const note = text(x.text) || detail.map(text).filter(Boolean).join(" · ");
  return {key: text(x.key || x.id), kind: Object.hasOwn(kinds,x.kind) ? x.kind : "figure", label: text(x.label || x.name || x.headline) || "Finding",
    value, text: quote || note === value ? "" : note, period: period(x.period), source: citation || "Source not supplied", featured: !!x.featured,
    basis: text(x.basis || x.source && x.source.formula), calculated: x.kind === "ratio" || /^calculat|\bcalculation\b/i.test(text(x.basis)) || x.calculated === true || !!(x.source && x.source.calculated), missing: (!supplied && !quote) || !citation};
}
function appearance(look){
  look = look || {};
  const input = look.main || look.accent || look.color;
  return {accent: /^#[a-f0-9]{6}$/i.test(input || "") ? input : colors[look.palette] || colors.default,
    light: look.dark === false || look.theme === "light"};
}
const CSS = `
.vl-root,.vl-ending{--vl-accent:#6ff4ff;--vl-bg:#090d13;--vl-card:#131a24;--vl-ink:#f4f7fb;--vl-muted:#afbdcd;--vl-line:#354151;
  box-sizing:border-box;background:var(--vl-bg);color:var(--vl-ink);font:500 12px/1.35 system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;text-align:left;isolation:isolate;color-scheme:dark}
.vl-root *,.vl-ending *{box-sizing:border-box}.vl-light{--vl-bg:#f6f8fb;--vl-card:#fff;--vl-ink:#151d2b;--vl-muted:#4d5f72;--vl-line:#becbd8;color-scheme:light}
.vl-root{height:100%;min-height:0;display:flex;flex-direction:column;container-type:inline-size;padding:10px 12px 9px;overflow:hidden}
.vl-header{display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--vl-line);padding-bottom:7px;flex:none}
.vl-brand{font-size:12px;font-weight:800;white-space:nowrap}.vl-mark{color:var(--vl-accent);margin-right:5px}.vl-company{min-width:0;font-size:10px;color:var(--vl-muted);margin-left:auto;text-align:right;overflow-wrap:anywhere}
.vl-title{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--vl-muted);margin:7px 0 6px;font-weight:700;flex:none}
.vl-list{min-height:0;overflow:auto;overscroll-behavior:contain;scrollbar-width:thin;display:flex;flex-direction:column;gap:6px;padding:1px 2px 5px 0}
.vl-card{position:relative;min-width:0;padding:8px 10px;border:1px solid var(--vl-line);border-radius:11px;background:var(--vl-card);flex:none;animation:vl-arrive .32s cubic-bezier(.16,1,.3,1) both;overflow-wrap:anywhere}
.vl-label{display:block;font-size:11px;font-weight:650;line-height:1.25;margin:0 0 4px}.vl-value{display:block;font-weight:800;font-size:19px;line-height:1.12;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;white-space:normal}
.vl-meta{font-size:9px;color:var(--vl-muted);margin:5px 0 0;line-height:1.35;overflow-wrap:anywhere}.vl-source{display:block;font-size:8px;line-height:1.4;color:var(--vl-muted);margin-top:3px;overflow-wrap:anywhere;white-space:normal}
.vl-kind{display:block;color:var(--vl-muted);font-size:8px;letter-spacing:.05em;text-transform:uppercase;margin-bottom:4px}.vl-note{font-size:10px;line-height:1.35;color:var(--vl-muted);margin:5px 0 0}
.vl-missing .vl-value{color:var(--vl-muted);font-size:15px}.vl-empty{color:var(--vl-muted);padding:8px 0;font-size:11px;margin:0}
.vl-evidence .vl-card{border-left:3px solid var(--vl-accent);padding-left:11px}.vl-evidence .vl-label{padding-right:0}.vl-evidence .vl-value{font-size:20px}
.vl-scoreboard .vl-list{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);align-content:start}.vl-scoreboard .vl-card{padding:9px;border-top:2px solid var(--vl-accent)}
.vl-scoreboard .vl-value{font-size:18px}.vl-scoreboard .vl-label{font-size:10px}.vl-scoreboard .vl-meta{font-size:8px}
.vl-spotlight .vl-list{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);align-content:start}.vl-spotlight .vl-card{padding:8px}.vl-spotlight .vl-value{font-size:16px}
.vl-spotlight.vl-has-feature .vl-card[data-featured=true],.vl-spotlight:not(.vl-has-feature) .vl-card:first-child{grid-column:1/-1;order:-1;border-color:var(--vl-accent);padding:11px 13px}
.vl-spotlight.vl-has-feature .vl-card[data-featured=true] .vl-value,.vl-spotlight:not(.vl-has-feature) .vl-card:first-child .vl-value{font-size:28px}
.vl-root .vl-list .vl-card[data-kind=observation] .vl-value,.vl-root .vl-list .vl-card[data-kind=guidance] .vl-value{font-size:14px;line-height:1.35}
.vl-ending{position:absolute;inset:0;padding:12% 11.6% 11% 6.4%;overflow:auto;container-type:inline-size}
.vl-ending h2{font:800 clamp(20px,6cqw,32px)/1.12 system-ui,sans-serif;margin:18px 0 7px;overflow-wrap:anywhere}.vl-ending .vl-subtitle{font-size:12px;color:var(--vl-muted);margin:0 0 16px;overflow-wrap:anywhere}
.vl-end-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.vl-end-card{padding:12px;border:1px solid var(--vl-line);border-radius:14px;background:var(--vl-card);overflow-wrap:anywhere}
.vl-end-card .vl-label{font-size:12px}.vl-end-card .vl-value{font-size:clamp(18px,5.5cqw,30px)}.vl-end-card .vl-source{font-size:10px}.vl-end-card .vl-meta{font-size:10px}.vl-end-card .vl-note{font-size:11px}
.vl-ending .vl-brand{font-size:15px}.vl-ending .vl-title{font-size:11px;margin-top:20px}.vl-ending .vl-coverage{font-size:10px;color:var(--vl-muted);margin-top:14px}
.vl-ending.vl-evidence .vl-end-grid{grid-template-columns:minmax(0,1fr)}.vl-ending.vl-evidence .vl-end-card{border-left:3px solid var(--vl-accent);padding:10px 12px}
.vl-ending.vl-evidence .vl-end-card .vl-value{font-size:23px}.vl-ending.vl-spotlight .vl-end-card:first-child{grid-column:1/-1;border-color:var(--vl-accent)}
.vl-ending.vl-spotlight .vl-end-card:first-child .vl-value{font-size:clamp(26px,8cqw,43px)}
.vl-ending .vl-end-grid .vl-end-card[data-kind=observation] .vl-value,.vl-ending .vl-end-grid .vl-end-card[data-kind=guidance] .vl-value{font-size:15px;line-height:1.4}
@keyframes vl-arrive{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}
@media(prefers-reduced-motion:reduce){.vl-card{animation:none}}
`;
let mounted = null, config = {layout:"evidence",look:{},company:{}};
function installStyle(doc){
  if (doc.getElementById("bmg-live-crawl-style")) return;
  const el = doc.createElement("style"); el.id = "bmg-live-crawl-style"; el.textContent = CSS; doc.head.appendChild(el);
}
function companyName(co){ return typeof co === "string" ? co : text(co && (co.name || co.company || co.ticker)); }
function mount(panelEl, options){
  unmount();
  if (!panelEl || !panelEl.ownerDocument) throw new Error("Live crawl needs a report panel.");
  options = options || {}; config = {layout:layouts.some(x => x.id === options.layout) ? options.layout : "evidence",look:options.look || {},company:options.company || {}};
  const doc = panelEl.ownerDocument, a = appearance(config.look); installStyle(doc);
  const root = doc.createElement("section"); root.className = "vl-root vl-" + config.layout + (a.light ? " vl-light" : "");
  root.style.setProperty("--vl-accent",a.accent); root.setAttribute("aria-label","Live crawl report");
  const header = doc.createElement("header"); header.className = "vl-header";
  const brand = doc.createElement("b"); brand.className = "vl-brand"; brand.textContent = "BMG Capital";
  const company = doc.createElement("span"); company.className = "vl-company"; company.textContent = companyName(config.company);
  header.appendChild(brand); header.appendChild(company);
  const title = doc.createElement("div"); title.className = "vl-title"; title.textContent = layouts.find(x => x.id === config.layout).name + " · Filing findings";
  const list = doc.createElement("div"); list.className = "vl-list"; list.setAttribute("aria-label","Arrived findings");
  const empty = doc.createElement("p"); empty.className = "vl-empty"; empty.textContent = "Findings appear here as the crawler reaches them.";
  list.appendChild(empty); root.appendChild(header); root.appendChild(title); root.appendChild(list); panelEl.appendChild(root);
  mounted = {root,list,empty,doc,items:new Map()}; return root;
}
function add(item){
  if (!mounted) return null;
  const x = normalize(item); if (!x.key) return null;
  const m = mounted; let e = m.items.get(x.key);
  if (!e){
    const card = m.doc.createElement("article"); card.className = "vl-card"; card.setAttribute("data-live-key",x.key);
    const nodes = {};
    for (const [name,tag] of [["kind","span"],["label","span"],["value","strong"],["note","p"],["meta","div"],["source","small"]]){
      const node = m.doc.createElement(tag); node.className = "vl-" + name; card.appendChild(node); nodes[name] = node;
    }
    if (m.empty){ m.empty.remove(); m.empty = null; }
    m.list.insertBefore(card,m.list.firstChild); m.list.scrollTop = 0; e = {card,nodes}; m.items.set(x.key,e);
  }
  e.card.setAttribute("data-kind",x.kind);
  e.card.setAttribute("data-featured",String(x.featured)); e.card.classList.toggle("vl-missing",x.missing);
  e.nodes.kind.textContent = kinds[x.kind] + (x.calculated ? " · Calculated" : "");
  e.nodes.label.textContent = x.label; e.nodes.value.textContent = x.value;
  e.nodes.note.textContent = x.text; e.nodes.note.hidden = !x.text;
  e.nodes.meta.textContent = [x.period,x.basis].filter(Boolean).join(" · "); e.nodes.meta.hidden = !e.nodes.meta.textContent;
  e.nodes.source.textContent = x.source;
  e.featured = x.featured; m.root.classList.toggle("vl-has-feature",Array.from(m.items.values()).some(n => n.featured));
  return e.nodes.value;
}
function target(key){
  if (!mounted || !mounted.items.has(text(key))) return null;
  const node = mounted.items.get(text(key)).nodes.value, list = mounted.list;
  // Measure only on a flight request. Confine the instant scroll to this panel.
  const r = node.getBoundingClientRect(), v = list.getBoundingClientRect(), inset = Math.min(8,(v.bottom - v.top) / 4);
  if (r.top < v.top + inset) list.scrollTop += r.top - v.top - inset;
  else if (r.bottom > v.bottom - inset) list.scrollTop += Math.min(r.top - v.top - inset,r.bottom - v.bottom + inset);
  return node;
}
function endItem(x){
  x = x || {};
  return normalize({...x,key:x.key || x.id || x.label || "ending",source:x.source || (x.page ? {pdf_page:x.page} : null),
    kind:x.kind || (!(text(x.value) || text(x.display)) && x.text ? "observation" : "figure")});
}
function endCard(item){
  const x = endItem(item);
  return `<article data-kind="${x.kind}" class="vl-end-card${x.missing ? " vl-missing" : ""}"><span class="vl-label">${esc(x.label)}</span><strong class="vl-value">${esc(x.value)}</strong>${x.text ? `<p class="vl-note">${esc(x.text)}</p>` : ""}<div class="vl-meta">${esc([x.period,x.basis,x.calculated ? "Calculated" : ""].filter(Boolean).join(" · "))}</div><small class="vl-source">${esc(x.source)}</small></article>`;
}
function unique(items){
  const seen = new Set(); return items.filter(x => { const k = [text(x.label),text(x.value != null ? x.value : x.display),period(x.period),sourceLabel(x.source)].join("|"); if (seen.has(k)) return false; seen.add(k); return true; });
}
function ending(model, company){
  model = model || {}; const co = company || config.company || {}, a = appearance(config.look);
  // Legacy endings keep the caller's complete original report until it supplies normalized evidence.
  if (!model.keys && !model.figures && !model.groups && (model.report || model.summary)) return "";
  const keys = unique((model.keys || model.figures || []).filter(Boolean));
  const groups = (model.groups || []).filter(g => g && Array.isArray(g.items)), all = unique(groups.flatMap(g => g.items).filter(Boolean));
  const summary = [], seen = new Set();
  for (let i = 0; summary.length < 4 && groups.some(g => g.items.length > i); i++) for (const g of groups){
    const x = g.items[i]; if (!x || summary.length === 4) continue;
    const k = [text(x.label),text(x.value != null ? x.value : x.display),period(x.period),sourceLabel(x.source)].join("|");
    if (!seen.has(k)){ seen.add(k); summary.push(x); }
  }
  if (!summary.length) summary.push(...keys.slice(0,4));
  const first = keys.length ? keys.slice(0,4) : summary.slice(0,4), count = all.length || keys.length;
  const title = text(model.name) || companyName(co) || "Filing report", identity = model.identity || {};
  const subtitle = [model.form || co.form,model.subtitle || period(identity.period || co.period)].filter(Boolean).join(" · ");
  const body = (items,head) => `<div class="vl-brand">BMG Capital</div><h2>${esc(title)}</h2><p class="vl-subtitle">${esc(subtitle)}</p><div class="vl-title">${esc(head)}</div><div class="vl-end-grid">${items.length ? items.map(endCard).join("") : `<p class="vl-empty">No sourced findings supplied.</p>`}</div>`;
  const notes = (model.notes || []).map(x => `<p class="vl-coverage">${esc(x)}</p>`).join("");
  const coverage = count > summary.length ? `<p class="vl-coverage">Showing ${summary.length} of ${count} supplied findings. The full report keeps the rest.</p>` : "";
  return `<style>${CSS}</style><section class="vl-ending vl-${config.layout}${a.light ? " vl-light" : ""}" style="--vl-accent:${a.accent}"><div class="rk-keys">${body(first,text(model.title) || "Key findings")}</div><div class="rk-one" hidden>${body(summary,text(model.summary_title) || "Filing summary")}${coverage}${notes}</div></section>`;
}
function unmount(){ if (mounted){ mounted.root.remove(); mounted.items.clear(); mounted = null; } }
window.BMGLiveCrawl = {layouts,mount,add,target,ending,unmount};
})();
