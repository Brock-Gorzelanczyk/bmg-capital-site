/* Posting Visuals: optional report endings made from the existing report's cited evidence.
   This module never runs the crawler, changes its gait, or adds a figure to a reader result.
   Nothing is stored until the owner explicitly adds an exported ending as a new Studio take. */
(function(){
"use strict";
if (window.BMGVisuals) return;
const W = 1080, H = 1920;
const styles = [
  {id: "map", name: "Portfolio map", line: "Reported positions become a field of company tiles.", tag: "13F"},
  {id: "evidence", name: "Evidence board", line: "Connect the findings to the evidence behind them.", tag: "Any sourced report"},
  {id: "takeover", name: "Report takeover", line: "A compact report grows into a full-screen reveal.", tag: "Any sourced report"},
  {id: "spotlight", name: "Finding spotlight", line: "Give one finding the stage, then reveal its context.", tag: "Any sourced report"},
  {id: "scoreboard", name: "Financial scoreboard", line: "Show matching current and prior reporting periods.", tag: "Matched periods"},
  {id: "transform", name: "Filing to report", line: "An actual source excerpt becomes a clear report.", tag: "Any sourced report"}
];
const accents = [{name: "Cyan", color: "#68edfa"}, {name: "Pink", color: "#ff53b4"}, {name: "Lime", color: "#c8f853"},
  {name: "Violet", color: "#bba0ff"}, {name: "Amber", color: "#ffcf70"}, {name: "Coral", color: "#ff7c79"}];
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
const str = v => v == null ? "" : String(v);
const esc = v => str(v).replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const ease = n => { n = clamp(n, 0, 1); return 1 - Math.pow(1 - n, 3); };
const ramp = (t, at, span) => ease((t - at) / (span || .3));
const periodText = p => typeof p === "string" ? p : str(p && (p.label || p.short || p.end || p.year));
function sourceText(s){
  if (!s || typeof s !== "object") return "";
  const out = [], printed = s.printed_page, pdf = s.pdf_page || s.page;
  if (printed != null && str(printed)) out.push("p." + printed + (pdf && str(pdf) !== str(printed) ? " (PDF p." + pdf + ")" : ""));
  else if (pdf) out.push("PDF p." + pdf);
  if (s.accession) out.push("SEC " + s.accession);
  if (s.anchor) out.push("anchor " + str(s.anchor).replace(/^#/, ""));
  if (s.location) out.push(typeof s.location === "string" ? s.location : "source location");
  if (!out.length && Number.isFinite(s.offset) && s.offset >= 0) out.push("document offset " + s.offset);
  if (s.section && !pdf && !s.anchor && !s.accession) out.push(str(s.section));
  return out.join(" · ");
}
function hasLocation(s){
  return !!(s && (s.pdf_page || s.printed_page || s.page || s.anchor || s.accession || s.location || Number.isFinite(s.offset) && s.offset >= 0));
}
function buildModel(raw){
  raw = raw || {};
  const legacy = raw.legacy, rep = raw.report || {}, ins = raw.insights || {}, end = ins.ending || rep.document_ending || {};
  const co = rep.company || {}, hs = ins.holdings_summary || {}, filing = raw.filing || {}, identity = end.identity || {};
  const model = {company: str(end.name || hs.manager || co.name || co.name_hint || legacy && legacy.company || filing.company || "Company report"),
    ticker: str(end.ticker || co.ticker || co.ticker_hint || legacy && legacy.ticker || filing.ticker),
    form: str(end.form || hs.form || co.form || legacy && legacy.form || filing.form),
    period: periodText(identity.period || hs.period || co.period || legacy && legacy.period || filing.period),
    kind: str(end.kind || ins.kind || "other"), accession: str(identity.accession || hs.accession || legacy && legacy.accession || filing.accession),
    filed: str(identity.filed || hs.filed || legacy && legacy.filed || filing.filed), items: [], holdings: [], comparisons: [], notes: [], legacy: !!legacy};
  const seen = new Set(), bad = x => /needs review|unavailable|not found|missing|conflict/i.test(str(x.status));
  const locate = x => ({...(x.source || {}), ...(!hasLocation(x.source) && x.page ? {pdf_page: x.page} : {})});
  const add = (x, fallbackId, group) => {
    if (!x || bad(x)) return null;
    const source = locate(x), sources = Array.isArray(x.sources) ? x.sources.filter(hasLocation).map(s => ({...s})) : [];
    if (!hasLocation(source) && !sources.length) return null;
    const value = str(x.display != null ? x.display : x.value), text = str(x.text || x.headline), label = str(x.label || x.name || x.metric || group || "Finding");
    if (!value && !text || /^(?:n\/a|not supplied|not available)$/i.test(value.trim()) && !text) return null;
    const key = [label, value, text, periodText(x.period)].join("|");
    if (seen.has(key)) return model.items.find(i => i.dedupe === key) || null;
    seen.add(key);
    const citations = [source, ...sources].map(sourceText).filter(Boolean);
    const item = {id: str(x.id || fallbackId || "item-" + model.items.length), label, value, text, period: periodText(x.period) || model.period,
      periodType: str(x.period_type || x.period && x.period.type), unit: str(x.unit), currency: str(x.currency), scale: str(x.scale),
      basis: str(x.basis || (x.kind === "calculated" ? "Calculated from cited inputs" : "Reported finding")), group: str(group),
      source, sources, citation: Array.from(new Set(citations)).join("; "), dedupe: key};
    model.items.push(item); return item;
  };
  if (legacy){
    (legacy.figures || []).forEach((x, i) => add({...x, source: {...(x.source || {}), ...(x.page ? {pdf_page: x.page} : {}), ...(x.accession ? {accession: x.accession} : {})}}, "legacy-" + i, "Saved report"));
    (legacy.findings || []).forEach((x, i) => add({...x, label: x.headline || "Finding", text: x.headline, value: "", sources: (x.pages || []).filter(Boolean).map(p => ({pdf_page: p}))}, "legacy-finding-" + i, "Saved report"));
    model.notes.push("This saved take contains a compact report. Only evidence saved with that take is available here.");
  } else {
    (end.keys || []).forEach((x, i) => add(x, "key-" + i, "Key findings"));
    (end.groups || []).forEach((g, n) => (g.items || []).forEach((x, i) => add(x, "group-" + n + "-" + i, g.title)));
    const facts = (rep.evidence || {}).facts || {}, calcs = (rep.evidence || {}).calcs || {}, all = {...facts, ...calcs};
    const rows = [...((rep.financials || {}).performance || []), ...((rep.financials || {}).calculated || []), ...((rep.position || {}).rows || [])];
    const fromFact = (f, label) => {
      if (!f) return null;
      const sources = (f.inputs || []).map(id => all[id] && all[id].source).filter(hasLocation);
      return {...f, label: label || f.name || f.metric, sources, basis: f.kind === "calculated" ? "Calculated from cited inputs" : "Reported statement figure"};
    };
    rows.forEach((r, n) => {
      const cur = all[r.current], prior = all[r.prior];
      const current = add(fromFact(cur, r.label || cur && cur.name), r.current || "row-" + n, "Statement figures");
      if (!current || !cur || !prior || bad(prior) || !hasLocation(cur.source) || !hasLocation(prior.source)) return;
      const p = cur.period || {}, q = prior.period || {};
      const unit = str(cur.unit), pu = str(prior.unit), currency = str(cur.currency), pc = str(prior.currency);
      if (!unit || unit !== pu || currency !== pc || str(cur.scale) !== str(prior.scale) || !p.type || p.type !== q.type
        || !p.end || !q.end || p.end === q.end || !periodText(p) || !periodText(q) || !prior.display) return;
      const prev = fromFact(prior, r.label || prior.name), src = {...prior.source};
      model.comparisons.push({id: "compare-" + str(r.current), label: current.label, unit, currency, scale: str(cur.scale),
        current: {...current, source: {...cur.source}, citation: sourceText(cur.source)},
        prior: {id: str(prior.id || r.prior), label: current.label, value: str(prior.display), text: "", period: periodText(q),
          source: src, sources: prev.sources, citation: sourceText(src), unit, currency, scale: str(prior.scale), basis: prev.basis},
        change: r.change && r.change.display ? str(r.change.display) : ""});
    });
    const live = rep.live || {};
    for (const s of live.sections || []) for (const r of s.rows || []){
      const id = (r.cells || [])[0], f = all[id], c = (live.cells || {})[id];
      if (f) add(fromFact(f, r.label), id, s.title);
      else if (c && c.kind !== "calculated") add({...c, label: r.label, value: c.display, source: {pdf_page: c.page, printed_page: c.printed}}, id, s.title);
    }
    if (!model.items.length) Object.entries(facts).forEach(([id, f]) => add(fromFact(f), id, "Reported figures"));
    (rep.exec_findings || []).forEach((f, i) => {
      const sources = (f.refs || []).map(id => all[id] && all[id].source).filter(hasLocation);
      add({...f, label: f.headline, text: f.headline, value: "", sources, basis: "Existing report finding"}, "finding-" + i, "Report findings");
    });
    if (ins.kind === "holdings") (ins.holdings || []).forEach((r, n) => {
      const source = {...locate(r), ...(hs.accession ? {accession: hs.accession} : {})};
      if (!hasLocation(source) || !r.issuer || !r.value || /^(n\/a|not supplied)$/i.test(str(r.value))) return;
      const amountKind = str(r.kind || ""), klass = str(r.class), put = str(r.put_call), label = r.issuer + (klass ? " · " + klass : "") + (put ? " · " + put : "");
      const h = {id: "holding-" + n, issuer: str(r.issuer), class: klass, put_call: put, amountKind, shares: str(r.shares),
        weight: str(r.weight), value: str(r.value), period: hs.period || model.period, source, citation: sourceText(source),
        label, unit: "USD", currency: "USD", basis: "Reported quarter-end holding; weight from the report", text: ""};
      h.text = (h.shares ? h.shares + (amountKind === "PRN" ? " principal amount" : put ? " underlying shares" : " shares") : "Amount not supplied") + (h.weight && h.weight !== "n/a" ? " · " + h.weight : "");
      model.holdings.push(h); add(h, h.id, "Reported positions");
    });
    model.notes = (end.notes || []).map(str);
    if (ins.kind === "holdings") model.notes.push("Reported 13F securities are a partial, delayed view, not assets under management. Quarter-end holdings do not establish trades or profits.");
  }
  model.notes = Array.from(new Set(model.notes));
  return model;
}
function weightOf(h){
  const m = /^\s*(\d+(?:\.\d+)?)\s*%\s*$/.exec(str(h.weight));
  return m && +m[1] > 0 && +m[1] <= 100 ? +m[1] : null;
}
function styleAvailability(model){
  const yes = model && model.items && model.items.length > 0;
  const out = Object.fromEntries(styles.map(s => [s.id, {enabled: !!yes, reason: yes ? "" : "Choose a report with cited findings first."}]));
  out.map = {enabled: !!(model && model.kind === "holdings" && model.holdings.some(h => weightOf(h) != null)),
    reason: "A portfolio map needs actual 13F positions with sourced reported weights. Choose a 13F holdings report."};
  out.scoreboard = {enabled: !!(model && model.comparisons && model.comparisons.length),
    reason: "This report has no sourced current/prior pair with matching period types, units and currency. No comparison is inferred."};
  if (out.map.enabled) out.map.reason = "";
  if (out.scoreboard.enabled) out.scoreboard.reason = "";
  return out;
}
// Canvas text and tile geometry are pure apart from the supplied measuring function.
function wrapText(text, maxWidth, measure){
  measure = measure || (s => str(s).length * 10);
  const lines = [];
  for (const p of str(text).split(/\n/)){
    if (!p.trim()){ lines.push(""); continue; }
    let line = "";
    for (let word of p.trim().split(/\s+/)){
      if (measure(word) > maxWidth){
        if (line){ lines.push(line); line = ""; }
        let part = "";
        for (const ch of word){ if (part && measure(part + ch) > maxWidth){ lines.push(part); part = ch; } else part += ch; }
        line = part; continue;
      }
      if (line && measure(line + " " + word) > maxWidth){ lines.push(line); line = word; }
      else line += (line ? " " : "") + word;
    }
    if (line) lines.push(line);
  }
  return lines;
}
function layoutTiles(items, box){
  box = box || {x: 0, y: 0, w: 100, h: 100};
  const valid = items.map((item, index) => ({item, index, weight: weightOf(item)})).filter(x => x.weight != null), out = [];
  function split(list, b){
    if (!list.length) return;
    if (list.length === 1){ out.push({...b, item: list[0].item, index: list[0].index}); return; }
    const total = list.reduce((s, x) => s + x.weight, 0); let sum = 0, cut = 1, best = Infinity;
    for (let n = 1; n < list.length; n++){ sum += list[n - 1].weight; const d = Math.abs(total / 2 - sum); if (d < best){ best = d; cut = n; } }
    const fraction = list.slice(0, cut).reduce((s, x) => s + x.weight, 0) / total;
    if (b.w > b.h){ split(list.slice(0, cut), {...b, w: b.w * fraction}); split(list.slice(cut), {...b, x: b.x + b.w * fraction, w: b.w * (1 - fraction)}); }
    else { split(list.slice(0, cut), {...b, h: b.h * fraction}); split(list.slice(cut), {...b, y: b.y + b.h * fraction, h: b.h * (1 - fraction)}); }
  }
  split(valid, box); return out;
}
const family = 'system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif';
function textBlock(g, text, x, y, w, h, px, color, weight, align){
  let lines = [], size = px || 28;
  do { g.font = (weight || 500) + " " + size + "px " + family; lines = wrapText(text, w, s => g.measureText(s).width); if (lines.length * size * 1.25 <= h) break; size -= 1; } while (size > 12);
  g.save(); g.beginPath(); g.rect(x, y, w, h); g.clip(); g.fillStyle = color || "#eef4f8"; g.textBaseline = "top"; g.textAlign = align || "left";
  const tx = align === "center" ? x + w / 2 : align === "right" ? x + w : x;
  for (let i = 0; i < lines.length && (i + 1) * size * 1.25 <= h + 1; i++) g.fillText(lines[i], tx, y + i * size * 1.25);
  g.restore(); return {height: Math.min(h, lines.length * size * 1.25), size, lines};
}
function rounded(g, x, y, w, h, r, fill, stroke){
  g.beginPath(); if (g.roundRect) g.roundRect(x, y, w, h, r); else g.rect(x, y, w, h);
  if (fill){ g.fillStyle = fill; g.fill(); } if (stroke){ g.strokeStyle = stroke; g.lineWidth = 2; g.stroke(); }
}
function initials(s){ return str(s).replace(/\b(?:inc|corp|corporation|company|the|holdings)\b/gi, "").trim().split(/\s+/).slice(0, 2).map(x => x[0] || "").join("").toUpperCase() || "R"; }
function drawFrame(g, model, options, time){
  const o = options || {}, accent = o.accent || accents[0].color, duration = o.duration || 10, t = o.reducedMotion ? duration : clamp(time, 0, duration);
  const avail = styleAvailability(model), style = avail[o.style] && avail[o.style].enabled ? o.style : "evidence";
  const eligible = style === "map" ? model.holdings.filter(h => weightOf(h) != null) : style === "scoreboard" ? model.comparisons : model.items;
  const selected = eligible.filter(i => !o.selected || o.selected.includes(i.id)).slice(0, style === "map" ? 8 : style === "scoreboard" ? 3 : 4);
  const featured = selected.find(i => i.id === o.feature) || selected[0];
  const b = {x: 72, y: 390, w: 936, h: 1160};
  g.clearRect(0, 0, W, H); g.fillStyle = "#080c12"; g.fillRect(0, 0, W, H);
  const glow = g.createRadialGradient(800, 450, 0, 800, 450, 1200); glow.addColorStop(0, accent + "1b"); glow.addColorStop(1, "#080c1200"); g.fillStyle = glow; g.fillRect(0, 0, W, H);
  g.strokeStyle = "#ffffff08"; g.lineWidth = 1;
  for (let x = 40; x < W; x += 70){ g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke(); }
  for (let y = 35; y < H; y += 70){ g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke(); }
  textBlock(g, "BMG CAPITAL", 72, 102, 560, 50, 27, "#edf6fa", 800);
  rounded(g, 846, 96, 162, 46, 23, accent + "18", accent + "50"); textBlock(g, "REPORT", 846, 107, 162, 30, 18, accent, 800, "center");
  const title = style === "map" ? "The portfolio, revealed." : style === "scoreboard" ? "The periods, side by side." : style === "spotlight" ? "One finding. Full focus." : style === "transform" ? "From evidence to clarity." : style === "takeover" ? "The report takes the stage." : "Follow the evidence.";
  textBlock(g, title, 72, 190, 936, 110, 52, "#f6fafc", 800);
  textBlock(g, model.company, 72, 300, o.logo ? 810 : 936, 70, 31, accent, 700);
  textBlock(g, [model.form, model.period].filter(Boolean).join(" · "), 72, 359, 936, 62, 22, "#a0afbc", 500);
  if (o.logo){ const l = o.logo; const r = Math.min(74 / l.width, 74 / l.height); g.drawImage(l, 923 + (74 - l.width * r) / 2, 292 + (74 - l.height * r) / 2, l.width * r, l.height * r); }
  function card(item, x, y, w, h, index, big){
    const a = ramp(t, .45 + index * .24, .7), lift = (1 - a) * 44; g.save(); g.globalAlpha = a; g.translate(0, lift);
    rounded(g, x, y, w, h, 25, "#111b25", accent + "50");
    rounded(g, x + 1, y + 25, 5, h - 50, 3, accent);
    const labelH = big ? 100 : 72, valueH = item.value ? big ? 180 : 125 : 0;
    textBlock(g, item.label, x + 28, y + 28, w - 56, labelH, big ? 39 : 29, "#cfdae3", 650);
    if (item.value) textBlock(g, item.value, x + 28, y + labelH + 39, w - 56, valueH, big ? 105 : 57, accent, 800);
    const contentY = y + labelH + (item.value ? valueH + 48 : 44), bottom = y + h - 140;
    if (item.text) textBlock(g, item.text, x + 28, contentY, w - 56, Math.max(0, bottom - contentY), big ? 33 : 26, "#d9e2e9", 500);
    textBlock(g, item.period || model.period, x + 28, y + h - 127, w - 56, 50, 20, "#a3b2c1", 500);
    textBlock(g, item.citation, x + 28, y + h - 74, w - 56, 55, 18, "#85a0b3", 500);
    g.restore();
  }
  function sourceBox(item, x, y, w, h, index){
    const a = ramp(t, .1 + index * .2, .65); g.save(); g.globalAlpha = a;
    rounded(g, x, y, w, h, 22, "#0b131d", "#ffffff20");
    textBlock(g, "SOURCE EXCERPT", x + 28, y + 25, w - 56, 36, 19, accent, 800);
    const quote = item && (item.source.excerpt || item.source.raw || item.text);
    textBlock(g, quote || "Report evidence: " + (item ? item.label + ": " + (item.value || item.text) : ""), x + 28, y + 80, w - 56, h - 185, 28, "#c7d2dc", 500);
    textBlock(g, item && item.citation || "", x + 28, y + h - 79, w - 56, 60, 18, "#87a0b2", 500); g.restore();
  }
  if (!selected.length){
    textBlock(g, "Choose at least one cited finding to preview this ending.", 150, 760, 780, 270, 44, "#b7c6d1", 600, "center");
  } else if (style === "map"){
    const tiles = layoutTiles(selected, {x: b.x, y: 454, w: b.w, h: 960});
    tiles.forEach((r, n) => {
      const a = ramp(t, .18 + n * .18, .8), pad = 7, x = r.x + pad, y = r.y + pad + (1 - a) * 50, w = r.w - pad * 2, h = r.h - pad * 2;
      g.save(); g.globalAlpha = a; rounded(g, x, y, w, h, 19, n === 0 ? accent + "26" : "#11202b", accent + (n === 0 ? "9f" : "45"));
      textBlock(g, initials(r.item.issuer), x + 18, y + 18, w - 36, h * .2, Math.min(56, w * .2), accent, 800);
      textBlock(g, r.item.issuer, x + 18, y + h * .28, w - 36, h * .22, 34, "#f5fafc", 800);
      textBlock(g, r.item.value, x + 18, y + h * .53, w - 36, h * .16, 47, accent, 750);
      textBlock(g, [r.item.class, r.item.put_call, r.item.amountKind, r.item.weight].filter(Boolean).join(" · "), x + 18, y + h * .73, w - 36, h * .14, 23, "#a6c2d0", 500);
      textBlock(g, r.item.source.pdf_page ? "PDF p." + r.item.source.pdf_page : r.item.citation, x + 18, y + h - Math.min(44, h * .12), w - 36, Math.min(35, h * .1), 17, "#a8bfce", 500); g.restore();
    });
    textBlock(g, "Showing " + selected.length + " of " + model.holdings.length + " reported positions. Tile sizes follow their reported weights; selected positions fill this view.", 80, 1445, 920, 108, 24, "#a5bac9", 500);
    textBlock(g, model.accession ? "SEC " + model.accession : selected[0].citation, 80, 1560, 920, 60, 21, "#7895a9", 500);
  } else if (style === "evidence"){
    const midY = 1006, positions = [[72, 460], [566, 460], [72, 1126], [566, 1126]];
    g.save(); g.globalAlpha = ramp(t, .1, 1.2); g.strokeStyle = accent + "65"; g.lineWidth = 3;
    selected.forEach((it, n) => { const p = positions[n]; g.beginPath(); g.moveTo(540, midY); g.lineTo(p[0] + 220, p[1] + (n < 2 ? 470 : 0)); g.stroke(); }); g.restore();
    rounded(g, 299, 972, 482, 70, 35, "#0b1823", accent); textBlock(g, "REPORT EVIDENCE", 310, 996, 460, 35, 22, accent, 800, "center");
    selected.forEach((it, n) => card(it, positions[n][0], positions[n][1], 442, 470, n, false));
  } else if (style === "takeover"){
    const a = ramp(t, 0, 1.6), top = 1290 - 850 * a;
    rounded(g, 56, top, 968, 1190, 34, "#0f1924", accent + "6a");
    textBlock(g, "THE FINDINGS", 91, top + 35, 890, 45, 23, accent, 800);
    selected.forEach((it, n) => card(it, 85 + n % 2 * 474, top + 110 + Math.floor(n / 2) * 512, 436, 480, n, false));
  } else if (style === "spotlight"){
    card(featured, 72, 475, 936, 640, 0, true);
    const rest = selected.filter(i => i.id !== featured.id).slice(0, 2);
    rest.forEach((it, n) => card(it, 72 + n * 494, 1150, rest.length === 1 ? 936 : 442, 440, n + 2, false));
    if (!rest.length) textBlock(g, featured.basis, 102, 1190, 876, 200, 30, "#9eb4c4", 500);
  } else if (style === "scoreboard"){
    selected.forEach((pair, n) => {
      const y = 460 + n * 370, a = ramp(t, .35 + n * .3, .7); g.save(); g.globalAlpha = a; g.translate(0, (1 - a) * 40);
      rounded(g, 72, y, 936, 340, 25, "#111b25", accent + "4b"); textBlock(g, pair.label, 100, y + 27, 880, 55, 31, "#e2eaf0", 700);
      const cols = [{it: pair.prior, x: 102, color: "#a8bdca"}, {it: pair.current, x: 558, color: accent}];
      cols.forEach(c => { textBlock(g, c.it.period, c.x, y + 99, 410, 70, 22, "#9aafbd", 500); textBlock(g, c.it.value, c.x, y + 173, 410, 83, 62, c.color, 800); textBlock(g, c.it.citation, c.x, y + 266, 410, 53, 18, "#7f9bad", 500); });
      g.strokeStyle = "#ffffff20"; g.beginPath(); g.moveTo(532, y + 101); g.lineTo(532, y + 308); g.stroke(); g.restore();
    });
    textBlock(g, "As reported, using matching period types and units. No forecast is implied.", 80, 1620, 920, 78, 24, "#9fb5c4", 500);
  } else if (style === "transform"){
    sourceBox(featured, 72, 460, 936, 430, 0);
    const a = ramp(t, .5, 1.2); g.save(); g.globalAlpha = a; g.strokeStyle = accent; g.lineWidth = 4;
    g.beginPath(); g.moveTo(540, 912); g.lineTo(540, 1003); g.lineTo(525, 985); g.moveTo(540, 1003); g.lineTo(555, 985); g.stroke(); g.restore();
    textBlock(g, "REPORT EVIDENCE", 72, 1050, 936, 48, 22, accent, 800);
    card(featured, 72, 1120, 936, 480, 3, false);
  }
  // The bottom is kept clear of social controls, and all data remains in the safe frame.
  g.fillStyle = "#080c12"; g.fillRect(0, 1710, W, 210); g.strokeStyle = accent + "38"; g.beginPath(); g.moveTo(72, 1720); g.lineTo(1008, 1720); g.stroke();
  const note = model.kind === "holdings" ? "Quarter-end 13F disclosures. Partial, delayed holdings, not AUM or evidence of trades." : "From the cited report. Reported figures and existing calculations keep their original basis.";
  textBlock(g, note, 72, 1750, 936, 95, 23, "#849baa", 500);
  return {style, selected: selected.map(i => i.id), width: W, height: H};
}

function mount(host, opts){
  opts = opts || {};
  if (!host) throw new Error("The Visuals panel needs a place to open.");
  const state = {visible: false, sources: [], sourceId: "", model: buildModel({}), selected: [], feature: "", style: "evidence", duration: 10,
    accent: accents[0].color, playing: false, time: 0, loading: false, exporting: false, logo: null, reduced: false};
  let raf = 0, last = 0, loadToken = 0, recorder = null, stream = null, cancel = false, exportBlob = null, exportMeta = null, exportUrl = "", logoUrl = "";
  const urls = new Set(), disposers = [], mm = typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;
  state.reduced = !!(mm && mm.matches);
  host.innerHTML = `<style>
    .vp-shell{--vp-accent:#68edfa;color:var(--ink,#edf4f8);font:inherit}.vp-shell *{box-sizing:border-box}.vp-shell button,.vp-shell select,.vp-shell input{font:inherit}.vp-shell button{cursor:pointer}.vp-shell button:disabled{cursor:not-allowed;opacity:.48}.vp-shell [hidden]{display:none!important}
    .vp-title{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin:24px 0}.vp-title h2{margin:0 0 8px;font-size:28px}.vp-note{color:var(--muted,#94a5b4);font-size:14px;line-height:1.6;margin:5px 0}.vp-tag{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--vp-accent);background:#0b1820;padding:5px 9px;border-radius:20px;white-space:nowrap}
    .vp-source{display:grid;grid-template-columns:minmax(180px,1fr) auto auto;gap:10px;align-items:end;padding:18px;border:1px solid var(--line,#29323a);border-radius:16px}.vp-label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}.vp-shell select{width:100%;padding:12px;border-radius:10px;border:1px solid var(--line,#38444f);background:var(--paper,#101820);color:var(--ink,#edf4f8);min-width:0}.vp-btn{border:1px solid var(--line,#38444f);border-radius:10px;padding:11px 15px;background:transparent;color:inherit;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:7px}.vp-btn.vp-primary{background:var(--vp-accent);border-color:var(--vp-accent);color:#071015;font-weight:800}.vp-btn:focus-visible,.vp-shell input:focus-visible,.vp-shell select:focus-visible,.vp-style:focus-visible{outline:3px solid var(--vp-accent);outline-offset:3px}
    .vp-grid{display:grid;grid-template-columns:minmax(300px,1fr) minmax(280px,410px);gap:30px;margin-top:24px;align-items:start}.vp-styles{display:grid;grid-template-columns:1fr 1fr;gap:10px}.vp-style{position:relative;min-width:0;padding:15px;text-align:left;border:1px solid var(--line,#2e3e49);border-radius:14px;background:transparent;color:inherit}.vp-style[aria-pressed=true]{background:#68edfa0b;border-color:var(--vp-accent);box-shadow:inset 0 0 0 1px var(--vp-accent)}.vp-style b{display:block;margin:8px 0 6px;font-size:15px}.vp-style small{display:block;font-size:12px;line-height:1.5;color:var(--muted,#94a5b4)}.vp-glyph{font-size:25px;line-height:1;color:var(--vp-accent)}.vp-style .vp-tag{font-size:9px;float:right}.vp-section{border-top:1px solid var(--line,#27353e);margin-top:22px;padding-top:20px}.vp-section h3{font-size:16px;margin:0 0 12px}.vp-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.vp-palette{display:flex;gap:9px}.vp-swatch{width:29px;height:29px;border-radius:50%;border:2px solid transparent;padding:0}.vp-swatch[aria-pressed=true]{outline:2px solid currentColor;outline-offset:3px}.vp-mini{max-width:140px}.vp-logo{width:36px;height:36px;object-fit:contain;background:#0b1420;border-radius:7px}
    .vp-items{max-height:355px;overflow:auto;border:1px solid var(--line,#293944);border-radius:12px}.vp-item{display:flex;align-items:flex-start;gap:10px;padding:12px;border-bottom:1px solid var(--line,#293944);cursor:pointer}.vp-item:last-child{border-bottom:0}.vp-item input{margin-top:4px;accent-color:var(--vp-accent)}.vp-item span{min-width:0;overflow-wrap:anywhere}.vp-item b{display:block;font-size:13px}.vp-item small{display:block;font-size:12px;line-height:1.5;color:var(--muted,#94a5b4)}.vp-item em{font-style:normal;color:var(--vp-accent)}
    .vp-preview{position:sticky;top:20px;min-width:0}.vp-screen{overflow:hidden;border:1px solid #294250;border-radius:21px;background:#080c12;box-shadow:0 12px 35px #0003;max-width:410px;margin:auto;aspect-ratio:9/16}.vp-screen canvas{display:block;width:100%;height:100%}.vp-controls{display:flex;align-items:center;gap:8px;margin:12px 0}.vp-controls input{flex:1;min-width:40px;accent-color:var(--vp-accent)}.vp-clock{font-variant-numeric:tabular-nums;font-size:12px;white-space:nowrap;min-width:63px}.vp-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px}.vp-actions .vp-primary{grid-column:1/-1}.vp-actions .vp-btn{font-size:13px}.vp-status{min-height:26px;font-size:13px;line-height:1.55;margin:12px 0;overflow-wrap:anywhere}.vp-status[data-error=true]{color:#ee876f}.vp-empty{padding:24px;border:1px dashed var(--line,#344952);border-radius:14px;margin-top:20px}.vp-done{margin-top:14px;border:1px solid var(--vp-accent);border-radius:12px;padding:13px}.vp-done video{display:block;width:100%;max-height:350px;background:#080c12;margin-bottom:12px}.vp-progress{height:5px;background:#29323a;border-radius:4px;overflow:hidden;margin-top:12px}.vp-progress i{display:block;height:100%;width:0;background:var(--vp-accent)}
    @media(max-width:950px){.vp-grid{grid-template-columns:minmax(0,1fr) minmax(250px,340px);gap:18px}.vp-style{padding:12px}.vp-style .vp-tag{display:none}.vp-source{grid-template-columns:minmax(0,1fr) auto}.vp-source>button:last-child{grid-column:1/-1}}
    @media(max-width:650px){.vp-grid{grid-template-columns:minmax(0,1fr)}.vp-preview{position:static;grid-row:1}.vp-screen{max-width:300px}.vp-title{margin:18px 0}.vp-title h2{font-size:24px}.vp-title>.vp-tag{display:none}.vp-source{padding:13px;grid-template-columns:minmax(0,1fr)}.vp-source>button:last-child{grid-column:auto}.vp-styles{gap:8px}.vp-style b{font-size:13px}.vp-style small{font-size:11px}.vp-actions{max-width:410px;margin:auto}.vp-row{gap:12px}.vp-items{max-height:260px}.vp-controls{max-width:410px;margin:12px auto}}
    @media(prefers-reduced-motion:reduce){.vp-shell *{scroll-behavior:auto!important}}
  </style><section class="vp-shell" aria-label="Posting Visuals">
    <div class="vp-title"><div><h2>Give the report a new reveal.</h2><p class="vp-note">Choose an optional ending for your next post. Your Crawler and Studio stay just as they are.</p></div><span class="vp-tag">Visuals lab</span></div>
    <div class="vp-source"><label><span class="vp-label">Use a report</span><select id="vp-source" aria-label="Report to visualize"><option value="">Loading reports...</option></select></label><button class="vp-btn" id="vp-refresh" type="button">Refresh</button><button class="vp-btn" id="vp-make" type="button">Choose a filing</button></div>
    <div class="vp-empty" id="vp-empty" hidden><b>Start with a real report.</b><p class="vp-note">Choose a filing in Make a clip and let its report finish loading, or choose a saved take with report evidence. These visuals only use findings that already have a source.</p></div>
    <div class="vp-grid"><div class="vp-settings"><div class="vp-styles" id="vp-styles">${styles.map((s, i) => `<button type="button" class="vp-style" data-vp-style="${s.id}" aria-pressed="${s.id === state.style}"><span class="vp-glyph" aria-hidden="true">${["▦", "⌘", "▤", "◉", "▥", "⇣"][i]}</span><span class="vp-tag">${s.tag}</span><b>${s.name}</b><small>${s.line}</small><small data-vp-reason="${s.id}"></small></button>`).join("")}</div>
    <div class="vp-section"><h3>Make it yours</h3><div class="vp-row"><label><span class="vp-label">Length</span><select id="vp-duration" class="vp-mini"><option value="6">6 seconds</option><option value="10" selected>10 seconds</option><option value="15">15 seconds</option></select></label><div><span class="vp-label">Accent</span><div class="vp-palette">${accents.map(a => `<button type="button" class="vp-swatch" data-vp-color="${a.color}" style="background:${a.color}" title="${a.name}" aria-label="${a.name}" aria-pressed="${a.color === state.accent}"></button>`).join("")}</div></div></div>
      <div class="vp-row" style="margin-top:16px"><label class="vp-btn" for="vp-logo">Add company logo</label><input id="vp-logo" type="file" accept="image/png,image/jpeg,image/webp" hidden><img class="vp-logo" id="vp-logo-preview" alt="Selected company logo" hidden><button type="button" class="vp-btn" id="vp-logo-clear" hidden>Remove logo</button></div><p class="vp-note">Optional image from your device. No logo is fetched online.</p></div>
    <div class="vp-section"><h3 id="vp-selection-title">Findings to show</h3><p class="vp-note" id="vp-selection-note"></p><div class="vp-items" id="vp-items"></div><label style="display:block;margin-top:13px"><span class="vp-label">Spotlight finding</span><select id="vp-feature"></select></label></div>
    <div class="vp-section"><p class="vp-note" id="vp-provenance"></p></div></div>
    <div class="vp-preview"><div class="vp-screen"><canvas id="vp-canvas" width="1080" height="1920" aria-label="Portrait report ending preview"></canvas></div><div class="vp-controls"><button class="vp-btn" id="vp-play" type="button" aria-label="Play preview">Play</button><button class="vp-btn" id="vp-replay" type="button" aria-label="Replay from start">↺</button><input type="range" id="vp-time" min="0" max="10" step="0.01" value="0" aria-label="Preview position"><span class="vp-clock" id="vp-clock">0.0 / 10 s</span></div>
    <div class="vp-actions"><button type="button" class="vp-btn vp-primary" id="vp-export">Make ending video</button><button type="button" class="vp-btn" id="vp-png">Save picture</button><button type="button" class="vp-btn" id="vp-final">Show final frame</button><button type="button" class="vp-btn" id="vp-cancel" hidden>Cancel export</button></div><p class="vp-note">1080 × 1920 portrait canvas. This creates a separate, silent report ending. Add it to Studio or download it on its own.</p><p class="vp-note" id="vp-motion" hidden>Reduced motion is on. The ending holds its finished layout.</p><div class="vp-progress" id="vp-progress" hidden><i></i></div><p class="vp-status" id="vp-status" role="status" aria-live="polite"></p><div class="vp-done" id="vp-done" hidden></div></div></div>
  </section>`;
  const $ = s => host.querySelector(s), $$ = s => Array.from(host.querySelectorAll(s)), cv = $("#vp-canvas"), g = cv.getContext("2d");
  const on = (el, type, fn) => { el.addEventListener(type, fn); disposers.push(() => el.removeEventListener(type, fn)); };
  const say = (s, error) => { $("#vp-status").textContent = s; $("#vp-status").dataset.error = !!error; };
  const source = () => state.sources.find(s => s.id === state.sourceId);
  const eligible = () => state.style === "map" ? state.model.holdings.filter(h => weightOf(h) != null) : state.style === "scoreboard" ? state.model.comparisons : state.model.items;
  const limit = () => state.style === "map" ? 8 : state.style === "scoreboard" ? 3 : 4;
  const render = () => {
    drawFrame(g, state.model, state, state.time); $("#vp-time").value = state.time; $("#vp-clock").textContent = state.time.toFixed(1) + " / " + state.duration + " s";
  };
  function pause(){ state.playing = false; if (raf) cancelAnimationFrame(raf); raf = 0; $("#vp-play").textContent = "Play"; $("#vp-play").setAttribute("aria-label", "Play preview"); }
  function tick(now){
    if (!state.visible || !state.playing || state.exporting){ raf = 0; return; }
    state.time = Math.min(state.duration, state.time + (last ? Math.min(.1, (now - last) / 1000) : 0)); last = now; render();
    if (state.time >= state.duration){ pause(); return; } raf = requestAnimationFrame(tick);
  }
  function play(){ if (state.exporting || !state.model.items.length) return; if (state.reduced){ state.time = state.duration; render(); return; } if (state.time >= state.duration) state.time = 0; state.playing = true; last = 0; $("#vp-play").textContent = "Pause"; $("#vp-play").setAttribute("aria-label", "Pause preview"); raf = requestAnimationFrame(tick); }
  function clearExport(){
    const v = $("#vp-done video"); if (v){ v.pause(); v.removeAttribute("src"); v.load(); }
    if (exportUrl){ URL.revokeObjectURL(exportUrl); urls.delete(exportUrl); exportUrl = ""; }
    exportBlob = null; exportMeta = null; $("#vp-done").hidden = true; $("#vp-done").innerHTML = "";
  }
  function updateControls(){
    const availability = styleAvailability(state.model), enabled = availability[state.style].enabled && state.selected.length > 0;
    for (const b of $$("[data-vp-style]")){ const a = availability[b.dataset.vpStyle]; b.disabled = !a.enabled || state.exporting; b.setAttribute("aria-pressed", b.dataset.vpStyle === state.style); b.title = a.reason; $("[data-vp-reason='" + b.dataset.vpStyle + "']").textContent = a.enabled ? "" : a.reason; }
    for (const id of ["vp-export", "vp-png", "vp-final", "vp-play", "vp-replay"]) $("#" + id).disabled = !enabled || state.exporting;
    for (const id of ["vp-source", "vp-refresh", "vp-make", "vp-duration", "vp-logo", "vp-logo-clear", "vp-feature", "vp-time"]) $("#" + id).disabled = state.exporting;
    $$("[data-vp-item], [data-vp-color]").forEach(b => { b.disabled = state.exporting; });
    $("#vp-cancel").hidden = !state.exporting; $("#vp-progress").hidden = !state.exporting; $("#vp-motion").hidden = !state.reduced;
  }
  function updateItems(reset){
    const list = eligible(), max = limit();
    if (reset) state.selected = list.slice(0, max).map(i => i.id);
    else state.selected = state.selected.filter(id => list.some(i => i.id === id)).slice(0, max);
    if (!state.selected.includes(state.feature)) state.feature = state.selected[0] || "";
    $("#vp-selection-title").textContent = state.style === "map" ? "Positions to show" : state.style === "scoreboard" ? "Comparisons to show" : "Findings to show";
    $("#vp-selection-note").textContent = "Choose up to " + max + ". " + list.length + " sourced " + (state.style === "map" ? "positions" : state.style === "scoreboard" ? "matched comparisons" : "findings") + " available." + (list.length > 80 ? " The first 80 are listed here." : "");
    $("#vp-items").innerHTML = list.slice(0, 80).map(i => `<label class="vp-item"><input type="checkbox" data-vp-item="${esc(i.id)}" ${state.selected.includes(i.id) ? "checked" : ""}><span><b>${esc(i.label)}</b><small><em>${esc(i.value || i.current && i.current.value || i.text)}</em>${i.period ? " · " + esc(i.period) : ""}</small><small>${esc(i.citation || i.current && i.current.citation || "")}</small></span></label>`).join("");
    $("#vp-feature").innerHTML = list.filter(i => state.selected.includes(i.id)).map(i => `<option value="${esc(i.id)}">${esc(i.label)}</option>`).join("");
    $("#vp-feature").value = state.feature;
    $("#vp-provenance").textContent = [state.model.company, state.model.form, state.model.period, state.model.accession ? "SEC " + state.model.accession : "", ...state.model.notes.slice(0, 2)].filter(Boolean).join(" · ");
    $("#vp-empty").hidden = !!state.model.items.length; updateControls(); render();
  }
  function changeSource(id){
    pause(); clearExport(); state.sourceId = id; state.model = buildModel(source() && source().result || {}); state.time = 0;
    if (!styleAvailability(state.model)[state.style].enabled) state.style = state.model.kind === "holdings" && styleAvailability(state.model).map.enabled ? "map" : "evidence";
    updateItems(true); say(state.model.items.length ? "Ready. Choose a reveal and preview it." : "No cited report findings are available yet.");
  }
  async function refresh(){
    if (state.exporting) return;
    const token = ++loadToken; state.loading = true; say("Looking for finished reports...");
    try {
      const list = await (opts.sources ? opts.sources() : []); if (token !== loadToken) return;
      state.sources = Array.isArray(list) ? list.filter(s => s && s.id && s.result) : [];
      $("#vp-source").innerHTML = state.sources.length ? state.sources.map(s => `<option value="${esc(s.id)}">${esc(s.label || s.id)}</option>`).join("") : '<option value="">No reports available yet</option>';
      const id = state.sources.some(s => s.id === state.sourceId) ? state.sourceId : state.sources[0] && state.sources[0].id || "";
      $("#vp-source").value = id; changeSource(id);
    } catch (e){ if (token === loadToken) say("Reports could not be opened: " + (e.message || e), true); }
    finally { if (token === loadToken) state.loading = false; }
  }
  function download(blob, name){
    const u = URL.createObjectURL(blob); urls.add(u); const a = document.createElement("a"); a.href = u; a.download = name; a.click();
    setTimeout(() => { URL.revokeObjectURL(u); urls.delete(u); }, 30000);
  }
  const fileStem = () => (state.model.ticker || state.model.company || "report").replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").slice(0, 65) + "-" + state.style;
  async function savePicture(){
    if (state.exporting) return; pause();
    try { const blob = await new Promise(r => cv.toBlob(r, "image/png")); if (!blob) throw new Error("No picture was produced."); download(blob, fileStem() + ".png"); say("The current preview frame is downloading as a 1080 × 1920 PNG."); } catch (e){ say(e.message || str(e), true); }
  }
  async function exportVideo(){
    if (state.exporting || !state.selected.length) return; pause(); clearExport(); cancel = false;
    if (typeof MediaRecorder === "undefined" || !cv.captureStream){ say("This browser cannot record a canvas. You can save a picture instead.", true); return; }
    state.exporting = true; updateControls(); say("Making the ending. Keep this tab visible until it finishes.");
    let chunks = [], parts = {}, error = null, started = 0, frames = 0;
    const frozen = {...state, selected: state.selected.slice()}, sourceSnapshot = source() && source().result;
    try {
      if (opts.loadStudio) { const S = await opts.loadStudio(); parts = S && S.parts || {}; }
      if (cancel || !state.visible || document.hidden) throw new Error("Export cancelled. Keep this tab visible and try again.");
      const fallback = ['video/mp4;codecs="avc1.42e01f"', "video/mp4", 'video/webm;codecs="vp9"', 'video/webm;codecs="vp8"', "video/webm"];
      const type = parts.pickType ? parts.pickType(false, 30) : fallback.find(t => MediaRecorder.isTypeSupported(t));
      if (!type) throw new Error("This browser has no supported video encoder. Save a picture instead.");
      state.time = 0; drawFrame(g, frozen.model, frozen, 0); render(); stream = cv.captureStream(30);
      recorder = new MediaRecorder(stream, {mimeType: type, videoBitsPerSecond: 16000000});
      const stopped = new Promise((resolve, reject) => { recorder.onstop = resolve; recorder.onerror = e => { error = e.error || new Error("Video recording failed."); reject(error); }; });
      recorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.start(500); started = performance.now();
      await new Promise((resolve, reject) => {
        const draw = now => {
          if (cancel || !state.visible || document.hidden){ reject(new Error("Export cancelled because the tab was hidden or closed. Your original takes are unchanged.")); return; }
          const elapsed = (now - started) / 1000; state.time = Math.min(frozen.duration, elapsed); drawFrame(g, frozen.model, frozen, state.time); frames++;
          $("#vp-progress i").style.width = Math.min(100, elapsed / frozen.duration * 100) + "%";
          $("#vp-clock").textContent = state.time.toFixed(1) + " / " + frozen.duration + " s";
          if (elapsed >= frozen.duration) resolve(); else raf = requestAnimationFrame(draw);
        }; raf = requestAnimationFrame(draw);
      });
      const mime = recorder.mimeType || type; recorder.stop(); await stopped;
      if (error) throw error;
      let blob = new Blob(chunks, {type: mime.split(";")[0]});
      if (!blob.size) throw new Error("The browser produced an empty file. Try again.");
      if (/webm/i.test(mime) && parts.fixWebmDuration) blob = await parts.fixWebmDuration(blob, frozen.duration * 1000);
      const name = fileStem() + (/mp4/i.test(mime) ? ".mp4" : ".webm"); exportBlob = blob;
      exportMeta = {name, duration: frozen.duration, snapshot: sourceSnapshot, model: frozen.model, style: frozen.style, selected: frozen.selected,
        feature: frozen.feature, accent: frozen.accent, requestedFps: 30, canvasFramesDrawn: frames, mime, reducedMotion: frozen.reduced};
      exportUrl = URL.createObjectURL(blob); urls.add(exportUrl);
      $("#vp-done").innerHTML = `<video src="${esc(exportUrl)}" controls playsinline aria-label="Exported report ending"></video><div class="vp-row"><a class="vp-btn vp-primary" href="${esc(exportUrl)}" download="${esc(name)}">Download ending</a><button type="button" class="vp-btn" id="vp-studio">Add as a new Studio take</button></div><p class="vp-note">${/mp4/i.test(mime) ? "MP4" : "WebM"} · 1080 × 1920 canvas · ${frozen.duration} seconds · ${(blob.size / 1e6).toFixed(1)} MB. Requested 30 fps; the browser determines the encoded frame rate.</p>`;
      $("#vp-done").hidden = false; $("#vp-studio").disabled = !opts.db;
      $("#vp-studio").addEventListener("click", saveToStudio, {once: true});
      say("Ready. Download the ending, or add it to Studio as a new take.");
    } catch (e){ say(e.message || str(e), true); }
    finally {
      if (raf) cancelAnimationFrame(raf); raf = 0;
      if (recorder && recorder.state !== "inactive") try { recorder.stop(); } catch (e) {}
      if (stream) stream.getTracks().forEach(t => t.stop()); stream = null; recorder = null;
      state.exporting = false; updateControls(); state.time = state.duration; render();
    }
  }
  async function saveToStudio(){
    if (!exportBlob || !exportMeta || !opts.db) return;
    const btn = $("#vp-studio"); btn.disabled = true; say("Adding a separate ending take to Studio...");
    try {
      const e = exportMeta, m = e.model, selected = m.items.filter(i => e.selected.includes(i.id));
      const id = "visual-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7), created = Date.now();
      const compact = {company: m.company, ticker: m.ticker, form: m.form, period: m.period, accession: m.accession,
        figures: selected.map(i => ({k: i.id, label: i.label, value: i.value, period: i.period, page: i.source.pdf_page || null, source: i.source, accession: i.source.accession || m.accession})),
        findings: selected.filter(i => !i.value && i.text).map(i => ({headline: i.text, pages: [i.source.pdf_page].filter(Boolean)}))};
      const take = {id, created, blob: exportBlob, size: exportBlob.size, type: exportBlob.type, duration: e.duration, name: m.company + " · " + (styles.find(s => s.id === e.style) || {}).name,
        meta: {source: "visuals", ticker: m.ticker, form: m.form, report: compact, visualReport: e.snapshot, cover: "report ending", hook: "",
          visualEnding: {version: 1, style: e.style, duration: e.duration, selected: e.selected, feature: e.feature, accent: e.accent, reducedMotion: e.reducedMotion},
          quality: {requested: {w: W, h: H, fps: 30, label: "Report ending"}, measured: {w: W, h: H}, note: "Canvas dimensions; encoded frame rate was not measured."}}};
      await opts.db.put("takes", take); if (opts.files) opts.files.set(id, exportBlob);
      say("The ending was added as a new take. Your original recordings are unchanged.");
      if (opts.openStudio) await opts.openStudio(id);
    } catch (e){ say("The ending could not be added to Studio: " + (e.message || e) + ". You can still download it.", true); btn.disabled = false; btn.addEventListener("click", saveToStudio, {once: true}); }
  }
  on($("#vp-source"), "change", e => changeSource(e.target.value)); on($("#vp-refresh"), "click", refresh); on($("#vp-make"), "click", () => opts.openMake && opts.openMake());
  on($("#vp-styles"), "click", e => { const b = e.target.closest("[data-vp-style]"); if (!b || b.disabled) return; pause(); clearExport(); state.style = b.dataset.vpStyle; state.time = 0; updateItems(true); });
  on($("#vp-items"), "change", e => { const input = e.target.closest("[data-vp-item]"); if (!input || state.exporting) return;
    if (input.checked && state.selected.length >= limit()){ input.checked = false; say("Choose up to " + limit() + " for a readable portrait ending."); return; }
    clearExport(); state.selected = input.checked ? [...state.selected, input.dataset.vpItem] : state.selected.filter(id => id !== input.dataset.vpItem); updateItems(false);
  });
  on($("#vp-feature"), "change", e => { clearExport(); state.feature = e.target.value; render(); });
  on($("#vp-duration"), "change", e => { pause(); clearExport(); state.duration = +e.target.value; state.time = Math.min(state.time, state.duration); $("#vp-time").max = state.duration; render(); });
  $$("[data-vp-color]").forEach(b => on(b, "click", () => { clearExport(); state.accent = b.dataset.vpColor; $(".vp-shell").style.setProperty("--vp-accent", state.accent); $$("[data-vp-color]").forEach(x => x.setAttribute("aria-pressed", x === b)); render(); }));
  on($("#vp-play"), "click", () => state.playing ? pause() : play()); on($("#vp-replay"), "click", () => { pause(); state.time = 0; render(); play(); });
  on($("#vp-time"), "input", e => { pause(); state.time = +e.target.value; render(); }); on($("#vp-final"), "click", () => { pause(); state.time = state.duration; render(); });
  on($("#vp-export"), "click", exportVideo); on($("#vp-png"), "click", savePicture); on($("#vp-cancel"), "click", () => { cancel = true; say("Cancelling this export..."); });
  on($("#vp-logo"), "change", async e => {
    const file = e.target.files && e.target.files[0]; if (!file) return;
    if (!/^image\/(png|jpeg|webp)$/.test(file.type) || file.size > 12e6){ say("Choose a PNG, JPG or WebP image smaller than 12 MB.", true); return; }
    const u = URL.createObjectURL(file), im = new Image();
    try { im.src = u; await im.decode(); if (logoUrl) URL.revokeObjectURL(logoUrl); logoUrl = u; state.logo = im; $("#vp-logo-preview").src = u; $("#vp-logo-preview").hidden = false; $("#vp-logo-clear").hidden = false; clearExport(); render(); }
    catch (e){ URL.revokeObjectURL(u); say("That image could not be opened. Try another image.", true); }
  });
  on($("#vp-logo-clear"), "click", () => { if (logoUrl) URL.revokeObjectURL(logoUrl); logoUrl = ""; state.logo = null; $("#vp-logo-preview").hidden = true; $("#vp-logo-clear").hidden = true; $("#vp-logo").value = ""; clearExport(); render(); });
  on(document, "visibilitychange", () => { if (document.hidden){ pause(); if (state.exporting) cancel = true; } });
  if (mm){ const motion = e => { state.reduced = e.matches; if (state.reduced){ pause(); state.time = state.duration; } updateControls(); render(); }; if (mm.addEventListener){ mm.addEventListener("change", motion); disposers.push(() => mm.removeEventListener("change", motion)); } }
  function hide(){ state.visible = false; pause(); if (state.exporting) cancel = true; const v = $("#vp-done video"); if (v) v.pause(); }
  function destroy(){ hide(); ++loadToken; if (stream) stream.getTracks().forEach(t => t.stop()); for (const off of disposers) off(); clearExport(); if (logoUrl) URL.revokeObjectURL(logoUrl); for (const u of urls) URL.revokeObjectURL(u); urls.clear(); }
  const api = {show(){ state.visible = true; render(); if (!state.sources.length && !state.loading) return refresh(); }, hide, busy: () => state.exporting || state.loading, refresh,
    state: () => ({visible: state.visible, sourceId: state.sourceId, sources: state.sources.map(s => ({id: s.id, label: s.label})), model: state.model,
      selected: state.selected.slice(), feature: state.feature, style: state.style, duration: state.duration, accent: state.accent, playing: state.playing, time: state.time, exporting: state.exporting, reducedMotion: state.reduced}), destroy};
  updateControls(); render(); return api;
}
window.BMGVisuals = {mount, version: 1, parts: {buildModel, styleAvailability, sourceText, wrapText, layoutTiles, styles, drawFrame, weightOf}};
})();
