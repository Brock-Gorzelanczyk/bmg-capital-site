/* BMG Studio: the video editor in the Crawler's Posting tab, made to work like TikTok's editor.
   A take from Record mode (or any video file) plays in a 9:16 preview. Pictures and text sit on top where they are
   dragged, each shown for the stretch its bar covers on the timeline; a song from an audio file plays under the video,
   from the part chosen and at the volume set. Export plays the edit once in real time into a 1080 x 1920 video with
   the song mixed in (MP4 where the browser can write it, WebM otherwise).
   Nothing leaves this computer: takes, pictures, songs and the edit are kept in this browser (IndexedDB, through
   window.bmgStudioDB from the Crawler page), and the finished video is downloaded or saved where you choose. */
(function(){
"use strict";
if (window.BMGStudio) return;

const W = 1080, H = 1920;                         // every edit is made in this frame (9:16) and exported at it
const MIN_LEN = 0.2;                              // the shortest part, picture or text stretch (seconds)
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const uid = () => Math.random().toString(36).slice(2, 9) + Date.now().toString(36).slice(-5);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = (s, tenths) => {
  s = Math.max(0, +s || 0);
  const m = Math.floor(s / 60), r = s - m * 60;
  return m + ":" + (tenths ? (Math.floor(r * 10) / 10).toFixed(1).padStart(4, "0") : String(Math.floor(r)).padStart(2, "0"));
};
const mb = n => (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + " MB";
const once = (t, ev, ms) => new Promise(res => {
  let timer = 0;
  const done = () => { clearTimeout(timer); t.removeEventListener(ev, done); res(); };
  t.addEventListener(ev, done);
  if (ms) timer = setTimeout(done, ms);
});
const nextFrame = () => new Promise(r => requestAnimationFrame(() => r()));
// after a seek: the frame it landed on, decoded (the seeked event can come a moment before it is)
const settled = v => new Promise(r => {
  const t = setTimeout(r, 250);
  if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(() => { clearTimeout(t); r(); });
});

// ---- text: laid out once at export size, so the preview breaks lines exactly where the video does ----
const FONTS = {
  classic: {label: "Classic", f: '800 {s}px system-ui, -apple-system, "Helvetica Neue", "Segoe UI", Roboto, Arial, sans-serif', lh: 1.17},
  strong: {label: "Strong", f: '900 {s}px "Arial Black", "Helvetica Neue", Impact, system-ui, sans-serif', lh: 1.1, upper: true},
  mono: {label: "Typewriter", f: '600 {s}px "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace', lh: 1.26},
  serif: {label: "Serif", f: '700 {s}px "Source Serif 4", Georgia, "Times New Roman", serif', lh: 1.2}
};
const COLORS = ["#ffffff", "#141414", "#ffd23f", "#6ff4ff", "#ff3da6", "#39ff6a", "#ff5c3d", "#8a6bff", "#3d7bff"];
const light = hex => { const n = parseInt(hex.slice(1), 16); return (0.299 * (n >> 16) + 0.587 * (n >> 8 & 255) + 0.114 * (n & 255)) > 150; };
const measureCtx = () => measureCtx.c || (measureCtx.c = document.createElement("canvas").getContext("2d"));
function textLayout(it){
  const F = FONTS[it.style] || FONTS.classic, px = it.size * W, font = F.f.replace("{s}", px.toFixed(2));
  const c = measureCtx(); c.font = font;
  const maxW = it.wrap * W, lines = [];
  for (const para of String(it.text || " ").split("\n")){
    const words = (F.upper ? para.toUpperCase() : para).split(/\s+/).filter(Boolean);
    if (!words.length){ lines.push(""); continue; }
    let line = words[0];
    for (const w of words.slice(1)){
      const t = line + " " + w;
      if (c.measureText(t).width <= maxW) line = t; else { lines.push(line); line = w; }
    }
    lines.push(line);
  }
  const widths = lines.map(l => c.measureText(l).width);
  const lh = px * F.lh, padX = it.bg === "box" ? px * 0.36 : px * 0.16, padY = it.bg === "box" ? px * 0.2 : px * 0.14;
  const w = Math.max(px * 0.8, ...widths) + padX * 2, h = lines.length * lh + padY * 2;
  return {font, px, lines, widths, lh, padX, padY, w, h};
}
// draws the text centred on (0, 0) of the current transform, in export pixels
function drawText(g, it, L){
  const bgCol = it.color, txt = it.bg === "box" ? (light(it.color) ? "#141414" : "#ffffff") : it.color;
  g.font = L.font; g.textBaseline = "middle"; g.lineJoin = "round";
  const top = -L.h / 2 + L.padY;
  L.lines.forEach((line, i) => {
    const lw = L.widths[i], y = top + L.lh * (i + 0.5);
    const x = it.align === "left" ? -L.w / 2 + L.padX : it.align === "right" ? L.w / 2 - L.padX - lw : -lw / 2;
    if (it.bg === "box" && line){
      const r = L.px * 0.22, bx = x - L.padX * 0.62, bw = lw + L.padX * 1.24, by = y - L.lh / 2 - L.padY * 0.32, bh = L.lh + L.padY * 0.64;
      g.fillStyle = bgCol; g.beginPath();
      if (g.roundRect) g.roundRect(bx, by, bw, bh, r); else g.rect(bx, by, bw, bh);
      g.fill();
    }
    if (it.bg === "outline"){ g.strokeStyle = light(it.color) ? "#141414" : "#ffffff"; g.lineWidth = L.px * 0.14; g.strokeText(line, x, y); }
    if (it.bg === "none"){ g.shadowColor = "rgba(0,0,0,.45)"; g.shadowBlur = L.px * 0.12; g.shadowOffsetY = L.px * 0.03; }
    g.fillStyle = txt; g.fillText(line, x, y);
    g.shadowColor = "transparent"; g.shadowBlur = 0; g.shadowOffsetY = 0;
  });
}
function textCanvas(it, scale){
  const L = textLayout(it), cv = document.createElement("canvas");
  cv.width = Math.max(1, Math.ceil(L.w * scale)); cv.height = Math.max(1, Math.ceil(L.h * scale));
  const g = cv.getContext("2d"); g.scale(scale, scale); g.translate(L.w / 2, L.h / 2); drawText(g, it, L);
  return {cv, L};
}

// ---- where the video sits in the 9:16 frame: filling it (edges cropped) or whole (bars) ----
// Auto fills the frame (cropping the edges) only for a take within 3% of 9:16; a take further off shows whole, with
// bars: a Crawler take has text right to its edges (a 6% wider take lost about 15 px of its report panel each side)
function videoFill(vw, vh, fit){
  const r = vw / vh, R = W / H;
  return fit === "fill" || (fit !== "fit" && Math.abs(r - R) / R < 0.03);
}
function videoRect(vw, vh, fit){
  const r = vw / vh, R = W / H, fill = videoFill(vw, vh, fit);
  let w, h;
  if ((r > R) === fill){ h = H; w = H * r; } else { w = W; h = W / r; }
  return {x: (W - w) / 2, y: (H - h) / 2, w, h};
}

// ---- the song's gain over the edit: fade in from its start, fade out to its end ----
function gainCurve(m, end){
  const a = m.at, span = Math.max(0, end - a), fi = Math.min(m.fadeIn || 0, span / 2), fo = Math.min(m.fadeOut || 0, span / 2), v = m.vol;
  return [[a, fi > 0 ? 0 : v], [a + fi, v], [end - fo, v], [end, fo > 0 ? 0 : v]];
}
function curveAt(c, e){
  if (e <= c[0][0]) return c[0][1];
  for (let i = 1; i < c.length; i++){
    const [e1, v1] = c[i], [e0, v0] = c[i - 1];
    if (e <= e1) return e1 > e0 ? v0 + (v1 - v0) * (e - e0) / (e1 - e0) : v1;
  }
  return c[c.length - 1][1];
}
// base: the audio clock's time at edit time 0; from: the edit time the sound starts at
function applyCurve(param, c, base, from, now){
  param.cancelScheduledValues(0);
  param.setValueAtTime(curveAt(c, from), now);
  for (const [e, v] of c) if (e > from) param.linearRampToValueAtTime(v, base + e);
}

// ---- reading a video the browser recorded: its length is unknown until it has been read to the end ----
function loadedMeta(v){
  if (v.readyState >= 1) return Promise.resolve();
  return new Promise((res, rej) => {
    const ok = () => { v.removeEventListener("error", bad); res(); };
    const bad = () => {
      v.removeEventListener("loadedmetadata", ok);
      const e = v.error, why = e ? ["", "it stopped", "a network error", "it could not be decoded", "the file is empty, missing or not a video it can play"][e.code] || "error " + e.code : "";
      rej(new Error("The browser could not read this video" + (why ? ` (${why}${e && e.message ? ": " + e.message : ""})` : "") + "."));
    };
    v.addEventListener("loadedmetadata", ok, {once: true}); v.addEventListener("error", bad, {once: true});
  });
}
async function fixDuration(v){
  if (Number.isFinite(v.duration) && v.duration > 0) return v.duration;
  await new Promise(res => {
    let timer = 0;
    const done = () => {
      if (!Number.isFinite(v.duration)) return;
      clearTimeout(timer); v.removeEventListener("durationchange", done); v.removeEventListener("timeupdate", done); res();
    };
    v.addEventListener("durationchange", done); v.addEventListener("timeupdate", done);
    timer = setTimeout(() => { v.removeEventListener("durationchange", done); v.removeEventListener("timeupdate", done); res(); }, 10000);
    v.currentTime = 1e101;
  });
  v.currentTime = 0;
  await once(v, "seeked", 3000);
  return v.duration;
}

// ---- the files a browser can write, best first: H.264 and AAC in MP4 (TikTok, Instagram, QuickTime), else WebM ----
function pickType(audio, fps){
  const avc = fps > 30 ? ["avc1.64002a", "avc1.640028"] : ["avc1.640028", "avc1.4d0028", "avc1.42e01f"];
  const c = [];
  for (const v of avc) c.push(audio ? `video/mp4;codecs="${v},mp4a.40.2"` : `video/mp4;codecs="${v}"`);
  if (audio) for (const v of avc) c.push(`video/mp4;codecs="${v},opus"`);
  c.push(audio ? 'video/mp4;codecs="vp9,opus"' : 'video/mp4;codecs="vp9"', "video/mp4",
    audio ? 'video/webm;codecs="vp9,opus"' : 'video/webm;codecs="vp9"', audio ? 'video/webm;codecs="vp8,opus"' : 'video/webm;codecs="vp8"', "video/webm");
  return c.find(t => { try { return MediaRecorder.isTypeSupported(t); } catch (e) { return false; } }) || "";
}
function describeType(t){
  const m = /codecs="?([^"]*)"?/.exec(t || ""), codecs = m ? m[1].split(",").map(s => s.trim().toLowerCase()) : [];
  const name = c => /^avc1|^h264/.test(c) ? "H.264" : /^vp9|^vp09/.test(c) ? "VP9" : /^vp8/.test(c) ? "VP8" : /^av01/.test(c) ? "AV1" : /^mp4a/.test(c) ? "AAC" : /^opus/.test(c) ? "Opus" : c;
  return (/mp4/.test(t) ? "MP4" : "WebM") + (codecs.length ? " (" + codecs.map(name).join(" + ") + ")" : "");
}

// ---- a WebM from the browser has no length in its header, so players cannot seek it: write the length in ----
async function fixWebmDuration(blob, ms){
  try {
    const headLen = Math.min(blob.size, 1 << 20), buf = new Uint8Array(await blob.slice(0, headLen).arrayBuffer());
    const vint = (p, keep) => {
      const b = buf[p]; let len = 1, mask = 0x80;
      while (len <= 8 && !(b & mask)){ len++; mask >>= 1; }
      if (len > 8 || p + len > buf.length) return null;
      let v = keep ? b : b & (mask - 1), allOnes = (b & (mask - 1)) === mask - 1;
      for (let i = 1; i < len; i++){ v = v * 256 + buf[p + i]; if (buf[p + i] !== 255) allOnes = false; }
      return {v, len, unknown: !keep && allOnes};
    };
    const el = p => {
      const id = vint(p, true); if (!id) return null;
      const sz = vint(p + id.len, false); if (!sz) return null;
      const dataStart = p + id.len + sz.len;
      return {id: id.v, start: p, idLen: id.len, sizeLen: sz.len, dataStart, size: sz.unknown ? Infinity : sz.v, end: sz.unknown ? Infinity : dataStart + sz.v};
    };
    const ebml = el(0); if (!ebml || ebml.id !== 0x1A45DFA3) return blob;
    const seg = el(ebml.end); if (!seg || seg.id !== 0x18538067 || seg.size !== Infinity) return blob;
    let info = null;
    for (let q = seg.dataStart; q < buf.length;){
      const c = el(q); if (!c || c.end === Infinity) break;
      if (c.id === 0x1549A966){ info = c; break; }
      if (c.id === 0x1F43B675) break;               // a cluster: the header part is over
      q = c.end;
    }
    if (!info || info.end > buf.length) return blob;
    let scale = 1e6, dur = null;
    for (let q = info.dataStart; q < info.end;){
      const c = el(q); if (!c || c.end > info.end) break;
      if (c.id === 0x2AD7B1){ scale = 0; for (let i = c.dataStart; i < c.end; i++) scale = scale * 256 + buf[i]; }
      if (c.id === 0x4489) dur = c;
      q = c.end;
    }
    const value = ms * 1e6 / (scale || 1e6);
    if (dur && (dur.size === 8 || dur.size === 4)){
      const dv = new DataView(buf.buffer, buf.byteOffset + dur.dataStart, dur.size);
      if (dur.size === 8) dv.setFloat64(0, value); else dv.setFloat32(0, value);
      return new Blob([buf, blob.slice(headLen)], {type: blob.type});
    }
    if (dur) return blob;
    const add = new Uint8Array(11); add.set([0x44, 0x89, 0x88]); new DataView(add.buffer).setFloat64(3, value);
    const newSize = info.size + add.length;
    if (newSize >= Math.pow(2, 7 * info.sizeLen) - 1) return blob;
    const sz = new Uint8Array(info.sizeLen);
    let v = newSize;
    for (let i = info.sizeLen - 1; i >= 0; i--){ sz[i] = v % 256; v = Math.floor(v / 256); }
    sz[0] |= 0x80 >> (info.sizeLen - 1);
    return new Blob([buf.subarray(0, info.start + info.idLen), sz, buf.subarray(info.dataStart, info.end), add, buf.subarray(info.end), blob.slice(headLen)], {type: blob.type});
  } catch (e) { return blob; }
}

// ---- MP4 writer: the export's video and sound in one file, at the exact times the editor gave them ----
// One sample per chunk, the samples of both tracks in time order, and the index (moov) ahead of the data, so a player
// starts at once. Video: H.264 (avc1, its avcC from the encoder) or VP9 (vp09, vpcC). Sound: AAC (mp4a, esds; the
// encoder's priming skipped by an edit list) or Opus (Opus, dOps with its pre-skip). Times come in microseconds.
function mp4File(tracks){
  const te = new TextEncoder();
  const cat = parts => { let n = 0; for (const q of parts) n += q.length; const o = new Uint8Array(n); let k = 0; for (const q of parts){ o.set(q, k); k += q.length; } return o; };
  const be = (n, bytes) => { const o = new Uint8Array(bytes); for (let i = bytes - 1; i >= 0; i--){ o[i] = n % 256; n = Math.floor(n / 256); } return o; };
  const u8 = n => be(n, 1), u16 = n => be(n, 2), u24 = n => be(n, 3), u32 = n => be(n >>> 0, 4), u64 = n => be(n, 8);
  const i16 = n => u16(n < 0 ? 65536 + n : n), i32 = n => be(n < 0 ? 4294967296 + n : n, 4);
  const u32s = a => { const o = new Uint8Array(a.length * 4), dv = new DataView(o.buffer); a.forEach((v, i) => dv.setUint32(i * 4, v >>> 0)); return o; };
  const u64s = a => { const o = new Uint8Array(a.length * 8), dv = new DataView(o.buffer); a.forEach((v, i) => { dv.setUint32(i * 8, Math.floor(v / 4294967296)); dv.setUint32(i * 8 + 4, v >>> 0); }); return o; };
  const cc = t => te.encode(t);
  const box = (type, ...parts) => { const body = cat(parts.filter(Boolean)); return cat([u32(body.length + 8), cc(type), body]); };
  const full = (type, v, flags, ...parts) => box(type, u8(v), u24(flags), ...parts);
  const zeros = n => new Uint8Array(n);
  const MATRIX = u32s([0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000]);
  const MOVIE = 1000;
  const prep = tracks.map((t, n) => {
    const ts = t.kind === "video" ? 90000 : t.sampleRate, toU = us => Math.round(us * ts / 1e6);
    const pts = t.samples.map(x => toU(x.pts)), sorted = [...pts].sort((a, b) => a - b);
    // decode times are the presentation times in order; frames an encoder sends out of order get composition offsets
    let shift = 0;
    for (let i = 0; i < pts.length; i++) shift = Math.max(shift, sorted[i] - pts[i]);
    const start = sorted.length ? sorted[0] : 0;
    const D = sorted.map(x => x - start), C = pts.map((x, i) => x + shift - sorted[i]);
    const dur = D.map((x, i) => i + 1 < D.length ? D[i + 1] - x : Math.max(1, toU(t.samples[i].dur) || (i ? x - D[i - 1] : 1)));
    const total = D.length ? D[D.length - 1] + dur[dur.length - 1] : 0;
    // media skipped at the start: a reordering encoder's lead (the video still shows all of its frames), or the sound's priming
    const skip = t.kind === "video" ? shift : (t.delay || 0);
    let shown = t.kind === "video" ? total : Math.max(0, total - skip);
    if (t.length) shown = Math.min(shown, Math.round(t.length * ts));          // the sound ends with the video
    return {t, id: n + 1, ts, D, C, dur, total, skip, shown, start, sizes: t.samples.map(x => x.data.length)};
  });
  const movieDur = Math.max(1, ...prep.map(q => Math.round((q.start + q.shown) / q.ts * MOVIE)));
  const entry = q => {
    const t = q.t;
    if (t.kind === "video"){
      let child;
      if (t.codec.startsWith("avc1")) child = box("avcC", t.description);
      else {
        const [, prof, lvl, depth] = t.codec.split(".").map(x => parseInt(x, 10));
        child = full("vpcC", 1, 0, u8(prof || 0), u8(lvl || 10), u8(((depth || 8) << 4) | (1 << 1)), u8(1), u8(1), u8(1), u16(0));
      }
      return box(t.codec.startsWith("avc1") ? "avc1" : "vp09", zeros(6), u16(1), u16(0), u16(0), zeros(12), u16(t.width), u16(t.height),
        u32(0x00480000), u32(0x00480000), u32(0), u16(1), zeros(32), u16(0x0018), i16(-1), child);
    }
    const audioBox = (fmt, child) => box(fmt, zeros(6), u16(1), zeros(8), u16(t.channels), u16(16), u16(0), u16(0), u32(t.sampleRate * 65536), child);
    if (t.codec === "opus") return audioBox("Opus", box("dOps", u8(0), u8(t.channels), u16(t.preSkip || 0), u32(t.sampleRate), i16(0), u8(0)));
    // esds: the MPEG-4 descriptors around the AudioSpecificConfig, each size written in four bytes
    const desc = (tag, body) => cat([u8(tag), u8(0x80 | (body.length >> 21 & 0x7f)), u8(0x80 | (body.length >> 14 & 0x7f)), u8(0x80 | (body.length >> 7 & 0x7f)), u8(body.length & 0x7f), body]);
    const asc = t.description || new Uint8Array([0x11, 0x90]);                  // AAC-LC, 48 kHz, stereo
    const dcd = desc(4, cat([u8(0x40), u8(0x15), u24(0), u32(t.bitrate || 192000), u32(t.bitrate || 192000), desc(5, asc)]));
    return audioBox("mp4a", full("esds", 0, 0, desc(3, cat([u16(q.id), u8(0), dcd, desc(6, u8(2))]))));
  };
  const runs = a => { const r = []; for (const x of a){ const l = r[r.length - 1]; if (l && l[1] === x) l[0]++; else r.push([1, x]); } return r; };
  const trak = (q, offs, big) => {
    const t = q.t, vid = t.kind === "video";
    const startMv = Math.round(q.start / q.ts * MOVIE), shownMv = Math.round(q.shown / q.ts * MOVIE);
    const edits = [];
    if (startMv > 0) edits.push([startMv, -1]);
    if (q.skip > 0 || startMv > 0) edits.push([shownMv, q.skip]);
    const edts = edits.length ? box("edts", full("elst", 0, 0, u32(edits.length), ...edits.map(([dd, mt]) => cat([u32(dd), i32(mt), u16(1), u16(0)])))) : null;
    const tts = runs(q.dur), cts = runs(q.C);
    const keys = vid ? t.samples.map((x, i) => x.key ? i + 1 : 0).filter(Boolean) : [];
    const stbl = box("stbl",
      full("stsd", 0, 0, u32(1), entry(q)),
      full("stts", 0, 0, u32(tts.length), u32s(tts.flat())),
      cts.length > 1 || (cts[0] && cts[0][1]) ? full("ctts", 0, 0, u32(cts.length), u32s(cts.flat())) : null,
      vid && keys.length < t.samples.length ? full("stss", 0, 0, u32(keys.length), u32s(keys)) : null,
      full("stsc", 0, 0, u32(1), u32(1), u32(1), u32(1)),
      full("stsz", 0, 0, u32(0), u32(q.sizes.length), u32s(q.sizes)),
      big ? full("co64", 0, 0, u32(offs.length), u64s(offs)) : full("stco", 0, 0, u32(offs.length), u32s(offs)));
    const minf = box("minf", vid ? full("vmhd", 0, 1, u16(0), u16(0), u16(0), u16(0)) : full("smhd", 0, 0, u16(0), u16(0)),
      box("dinf", full("dref", 0, 0, u32(1), full("url ", 0, 1))), stbl);
    const mdia = box("mdia", full("mdhd", 0, 0, u32(0), u32(0), u32(q.ts), u32(q.total), u16(0x55c4), u16(0)),
      full("hdlr", 0, 0, u32(0), cc(vid ? "vide" : "soun"), zeros(12), cc(vid ? "VideoHandler\0" : "SoundHandler\0")), minf);
    const tkhd = full("tkhd", 0, 3, u32(0), u32(0), u32(q.id), u32(0), u32(startMv + shownMv), zeros(8), u16(0), u16(0), u16(vid ? 0 : 0x0100), u16(0),
      MATRIX, u32(vid ? t.width * 65536 : 0), u32(vid ? t.height * 65536 : 0));
    return box("trak", tkhd, edts, mdia);
  };
  const hasAvc = tracks.some(t => t.codec.startsWith("avc1"));
  const ftyp = box("ftyp", cc("isom"), u32(512), cc("isom"), cc("iso2"), cc(hasAvc ? "avc1" : "iso6"), cc("mp41"));
  const moovWith = (offs, big) => box("moov", full("mvhd", 0, 0, u32(0), u32(0), u32(MOVIE), u32(movieDur), u32(0x00010000), u16(0x0100), zeros(10), MATRIX, zeros(24), u32(prep.length + 1)),
    ...prep.map((q, k) => trak(q, offs[k], big)));
  // the samples of both tracks in the order they play
  const order = [];
  prep.forEach((q, k) => q.D.forEach((x, i) => order.push({k, i, at: (x + q.start) / q.ts})));
  order.sort((a, b) => a.at - b.at || a.k - b.k);
  const dataLen = prep.reduce((a, q) => a + q.sizes.reduce((x, y) => x + y, 0), 0);
  const zeroOffs = prep.map(q => q.sizes.map(() => 0));
  const big = ftyp.length + moovWith(zeroOffs, false).length + 16 + dataLen > 0xFFFFFFFF;
  const moovLen = moovWith(zeroOffs, big).length;
  const mdatHead = big ? cat([u32(1), cc("mdat"), u64(dataLen + 16)]) : cat([u32(dataLen + 8), cc("mdat")]);
  let pos = ftyp.length + moovLen + mdatHead.length;
  const offs = prep.map(q => new Array(q.sizes.length));
  for (const o of order){ offs[o.k][o.i] = pos; pos += prep[o.k].sizes[o.i]; }
  const moov = moovWith(offs, big);
  return new Blob([ftyp, moov, mdatHead, ...order.map(o => prep[o.k].t.samples[o.i].data)], {type: "video/mp4"});
}

// ---- pictures: kept at most 1600 px on the long side (sharp at 1080 x 1920, light to store) ----
function shrinkImage(file){
  return new Promise((res, rej) => {
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      const s = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement("canvas"); c.width = Math.max(1, Math.round(img.naturalWidth * s)); c.height = Math.max(1, Math.round(img.naturalHeight * s));
      const g = c.getContext("2d", {willReadFrequently: true});
      g.drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
      // a picture with any see-through pixel stays a PNG whatever its type says (a JPEG has no transparency: its
      // clear parts came out solid); only a fully solid photo becomes a JPEG
      let clear = /png|gif|webp|svg/.test(file.type);
      if (!clear){ const d = g.getImageData(0, 0, c.width, c.height).data; for (let i = 3; i < d.length; i += 4) if (d[i] < 255){ clear = true; break; } }
      c.toBlob(b => b ? res({blob: b, w: c.width, h: c.height}) : rej(new Error("not an image")), clear ? "image/png" : "image/jpeg", 0.92);
    };
    img.onerror = () => { URL.revokeObjectURL(url); rej(new Error("not an image")); };
    img.src = url;
  });
}
// Transparent background: the color around a picture's edges (a logo on white, a screenshot on a plain color) is
// made see-through. px: RGBA bytes (changed in place). The background is the commonest color on the border, and a
// second one when it covers a fifth of the border too (the gray and white squares baked into a "transparent" logo
// saved from an image search); a pixel within tol (0 to 1 of the largest color difference) of one goes clear where it
// touches the border through other such pixels, or anywhere with inside (the holes in an "O"). The pixels just past
// tol next to a cleared one fade, so the edge has no halo. Returns {bg: [r, g, b] or null when the border is already
// clear, bgs: every background color, cleared: pixels made clear}.
function clearBackground(px, w, h, tol, inside){
  const n = w * h, buckets = new Map();
  const border = [];
  for (let x = 0; x < w; x++){ border.push(x, (h - 1) * w + x); }
  for (let y = 1; y < h - 1; y++){ border.push(y * w, y * w + w - 1); }
  let clear = 0;
  for (const i of border){
    const o = i * 4;
    if (px[o + 3] < 16){ clear++; continue; }
    const k = (px[o] >> 4) << 8 | (px[o + 1] >> 4) << 4 | px[o + 2] >> 4;
    const b = buckets.get(k) || [0, 0, 0, 0]; b[0]++; b[1] += px[o]; b[2] += px[o + 1]; b[3] += px[o + 2]; buckets.set(k, b);
  }
  const ranked = [...buckets.values()].sort((a, b) => b[0] - a[0]);
  if (!ranked.length || clear > border.length / 2) return {bg: null, bgs: [], cleared: 0};
  const bgs = ranked.slice(0, 2).filter((b, k) => k === 0 || b[0] >= border.length / 5).map(b => [b[1] / b[0], b[2] / b[0], b[3] / b[0]]);
  const bg = bgs[0];
  const far = 441.7, lim = tol * far, soft = Math.max(8, lim * 0.6);
  const dist = i => { const o = i * 4; let d = Infinity; for (const c of bgs) d = Math.min(d, Math.hypot(px[o] - c[0], px[o + 1] - c[1], px[o + 2] - c[2])); return d; };
  const gone = new Uint8Array(n);
  const near = i => px[i * 4 + 3] < 16 || dist(i) <= lim;
  if (inside){ for (let i = 0; i < n; i++) if (near(i)) gone[i] = 1; }
  else {
    const q = new Int32Array(n); let head = 0, tail = 0;
    for (const i of border) if (!gone[i] && near(i)){ gone[i] = 1; q[tail++] = i; }
    while (head < tail){
      const i = q[head++], x = i % w;
      for (const j of [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, i - w, i + w]) if (j >= 0 && j < n && !gone[j] && near(j)){ gone[j] = 1; q[tail++] = j; }
    }
  }
  let cleared = 0;
  for (let i = 0; i < n; i++){
    const o = i * 4;
    if (gone[i]){ if (px[o + 3]) cleared++; px[o + 3] = 0; continue; }
    const x = i % w;
    if ((x > 0 && gone[i - 1]) || (x < w - 1 && gone[i + 1]) || (i >= w && gone[i - w]) || (i + w < n && gone[i + w])){
      const a = Math.min(1, Math.max(0, (dist(i) - lim) / soft));
      px[o + 3] = Math.round(px[o + 3] * (0.25 + 0.75 * a));
    }
  }
  return {bg: bg.map(Math.round), bgs: bgs.map(c => c.map(Math.round)), cleared};
}
function loadImg(url){
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error("picture")); i.src = url; });
}
// AIFF and uncompressed AIFF-C, as some SoundCloud downloads are: Chrome cannot decode them, so their samples are read
// here. {rate, data: a Float32Array per channel}, or null for any other file (and for compressed AIFF-C).
function aiffDecode(ab){
  const dv = new DataView(ab), n = ab.byteLength;
  const id = o => String.fromCharCode(dv.getUint8(o), dv.getUint8(o + 1), dv.getUint8(o + 2), dv.getUint8(o + 3));
  if (n < 12 || id(0) !== "FORM" || (id(8) !== "AIFF" && id(8) !== "AIFC")) return null;
  let comm = -1, commSize = 0, ssnd = -1;
  for (let o = 12; o + 8 <= n;){
    const k = id(o), size = dv.getUint32(o + 4);
    if (k === "COMM"){ comm = o + 8; commSize = size; } else if (k === "SSND") ssnd = o + 8;
    o += 8 + size + (size & 1);
  }
  if (comm < 0 || ssnd < 0 || commSize < 18 || comm + 18 > n || ssnd + 8 > n) return null;
  const chans = dv.getInt16(comm), frames = dv.getUint32(comm + 2), bits = dv.getInt16(comm + 6);
  const ex = dv.getUint16(comm + 8) & 0x7fff, hi = dv.getUint32(comm + 10), lo = dv.getUint32(comm + 14);
  const rate = Math.round(hi * Math.pow(2, ex - 16383 - 31) + lo * Math.pow(2, ex - 16383 - 63));    // 80-bit extended
  const kind = id(8) === "AIFC" && commSize >= 22 ? id(comm + 18) : "NONE";
  const float = /^(fl|FL)(32|64)$/.test(kind), little = kind === "sowt";
  if (!float && !little && kind !== "NONE" && kind !== "twos") return null;
  const bytes = float ? (/64$/.test(kind) ? 8 : 4) : Math.ceil(bits / 8);
  if (!(chans > 0 && chans <= 32) || !(rate >= 3000 && rate <= 768000) || !(float || (bytes >= 1 && bytes <= 4))) return null;
  const start = ssnd + 8 + dv.getUint32(ssnd), count = Math.min(frames, Math.floor((n - start) / (bytes * chans)));
  if (!(count > 0)) return null;
  const data = Array.from({length: chans}, () => new Float32Array(count)), scale = Math.pow(2, 8 * bytes - 1);
  for (let f = 0, o = start; f < count; f++){
    for (let c = 0; c < chans; c++, o += bytes){
      let v;
      if (float) v = bytes === 8 ? dv.getFloat64(o) : dv.getFloat32(o);
      else if (bytes === 1) v = dv.getInt8(o) / scale;
      else if (bytes === 2) v = dv.getInt16(o, little) / scale;
      else if (bytes === 4) v = dv.getInt32(o, little) / scale;
      else {
        const a = dv.getUint8(o), b = dv.getUint8(o + 1), d = dv.getUint8(o + 2);
        let x = little ? d << 16 | b << 8 | a : a << 16 | b << 8 | d;
        if (x & 0x800000) x -= 0x1000000;
        v = x / scale;
      }
      data[c][f] = v;
    }
  }
  return {rate, data};
}
function decodeAudio(blob){
  return blob.arrayBuffer().then(ab => {
    const a = aiffDecode(ab);
    if (!a) return new OfflineAudioContext(2, 1, 48000).decodeAudioData(ab);
    const buf = new AudioBuffer({numberOfChannels: a.data.length, length: a.data[0].length, sampleRate: a.rate});
    a.data.forEach((d, c) => buf.copyToChannel(d, c));
    return buf;
  });
}
function peaks(buf, n){
  const a = buf.getChannelData(0), b = buf.numberOfChannels > 1 ? buf.getChannelData(1) : null, step = a.length / n, out = new Float32Array(n);
  for (let i = 0; i < n; i++){
    let m = 0;
    for (let j = Math.floor(i * step), e = Math.min(a.length, Math.floor((i + 1) * step)); j < e; j += 8){
      const v = b ? (Math.abs(a[j]) + Math.abs(b[j])) / 2 : Math.abs(a[j]);
      if (v > m) m = v;
    }
    out[i] = m;
  }
  let top = 0; for (const v of out) top = Math.max(top, v);
  if (top > 0) for (let i = 0; i < n; i++) out[i] /= top;
  return out;
}

// what a link to a song can and cannot do here
const LINK_ADVICE = {
  soundcloud: "SoundCloud: open the track and look for Download file under More (the three dots next to Share). That is there only when the artist allows downloads; save the file, then drop it here. Saving a track any other way is against SoundCloud's terms. A song you bought (Bandcamp, iTunes) or made yourself works the same way.",
  youtube: "YouTube does not allow saving songs from its videos. Free songs made for videos: YouTube Studio's Audio Library (Download as MP3), then drop the file here.",
  tiktok: "For a popular song, the reliable way is TikTok's own Add sound when you post: it is licensed there and keeps the sound linked. A TikTok uploaded with a song it has no license for can be muted. Export without a song (or with a quiet one) and add the sound in TikTok."
};
function linkKind(url){
  let u; try { u = new URL(String(url).trim()); } catch (e) { return "bad"; }
  const h = u.hostname.toLowerCase().replace(/^(www|m)\./, "");
  if (/(^|\.)soundcloud\.com$|^snd\.sc$/.test(h)) return "soundcloud";
  if (/(^|\.)youtube\.com$|^youtu\.be$/.test(h)) return "youtube";
  if (/\.(mp3|m4a|aac|wav|aif|aiff|aifc|ogg|oga|opus|flac|weba|webm)$/i.test(u.pathname)) return "file";
  return "other";
}

let cssDone = false;
function injectCSS(){
  if (cssDone) return; cssDone = true;
  const s = document.createElement("style"); s.id = "studio-css";
  s.textContent = `
.av-body.wide{max-width:1480px}
.st{--g:#0f7d47;--line:#e3e3dc;--mute:#6b6f76;outline:none;font:14px/1.45 'IBM Plex Sans',system-ui,sans-serif;color:#141414}
.st button{font-family:inherit}
.st-head{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin:0 0 10px}.st-head .st-tabs{margin:0}
.st-saved{font-size:12px;color:var(--mute);margin-left:auto}.st-sep{width:1px;height:22px;background:#d9ddd8;margin:0 4px}
.st button,.st-tlbar,.st-transport{user-select:none;-webkit-user-select:none}
.st-ib{height:32px;padding:0 11px;border:1px solid #c9cfca;border-radius:8px;background:#fff;color:#141414;font:600 12.5px 'IBM Plex Sans',sans-serif;cursor:pointer;white-space:nowrap}
.st-ib:hover:not(:disabled){border-color:var(--g);color:var(--g)}.st-ib:disabled{opacity:.45;cursor:default}
.st-ib.on{background:#141414;border-color:#141414;color:#fff}
.st-main{display:grid;grid-template-columns:auto minmax(300px,1fr);gap:16px;align-items:stretch}
.st-left{display:flex;flex-direction:column;align-items:center;gap:8px;min-width:0}
.st-stage{position:relative;height:clamp(380px,calc(100vh - 196px),820px);aspect-ratio:9/16;background:#000;border-radius:14px;overflow:hidden;box-shadow:0 10px 30px rgba(0,0,0,.2);user-select:none;-webkit-user-select:none;touch-action:none;container-type:size}
.st-stage.drop{outline:3px dashed #0f7d47;outline-offset:3px}
.st-video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;background:#000}.st-video.fit{object-fit:contain}.st-video[hidden]{display:none}
.st-mock{position:absolute;inset:0;background:linear-gradient(#05070c,#0b1018 60%,#05070c 60%);pointer-events:none}
.st-mock::before{content:"";position:absolute;left:9%;right:9%;top:8%;height:46%;background:repeating-linear-gradient(#05070c 0 9px,#1b2430 9px 11px);opacity:.75}
.st-mock::after{content:"";position:absolute;left:0;right:0;bottom:0;height:40%;background:#fff;border-radius:6% 6% 0 0/3.5% 3.5% 0 0;box-shadow:0 -10px 24px rgba(0,0,0,.3)}
.st-mock span{position:absolute;left:8%;right:8%;top:24%;z-index:1;text-align:center;color:#9fb4c8;font:600 clamp(11px,3.6cqw,15px)/1.35 'IBM Plex Sans',sans-serif}
.st-mock[hidden]{display:none}
.st-items{position:absolute;inset:0}
.so{position:absolute;transform:translate(-50%,-50%) rotate(var(--r,0deg));cursor:grab;touch-action:none}
.so:active{cursor:grabbing}.so img,.so canvas{display:block;width:100%;height:auto;pointer-events:none;-webkit-user-drag:none}
.so.hide{display:none}.so.sel{outline:2px solid #fff;outline-offset:3px;box-shadow:0 0 0 1px rgba(0,0,0,.55),0 0 0 5px rgba(0,0,0,.25)}
.so.off{opacity:.42;outline-style:dashed}
.so i[data-h]{position:absolute;width:24px;height:24px;margin:-12px 0 0 -12px;border-radius:50%;background:#fff;border:2px solid #141414;box-sizing:border-box;display:none;font:700 15px/20px sans-serif;text-align:center;color:#141414;font-style:normal;z-index:2}
.so.sel i[data-h]{display:block}.so i[data-h=del]{left:0;top:0;cursor:pointer;background:#e5484d;border-color:#fff;color:#fff}
.so i[data-h=rot]{left:50%;top:-30px;cursor:grab}.so i[data-h=rot]::after{content:"";position:absolute;left:9px;top:20px;width:2px;height:16px;background:#fff}
.so i[data-h=size]{left:100%;top:100%;cursor:nwse-resize}
.st-guides i{position:absolute;background:#ff3da6;display:none;pointer-events:none;z-index:3}.st-guides i.on{display:block}
.st-guides .gv{left:50%;top:0;bottom:0;width:1px}.st-guides .gh{top:50%;left:0;right:0;height:1px}
.st-safe{position:absolute;left:5%;right:13%;top:7%;bottom:21%;border:1.5px dashed rgba(255,210,63,.9);border-radius:6px;pointer-events:none;z-index:3}
.st-safe span{position:absolute;right:4px;bottom:3px;font:600 10px 'IBM Plex Mono',monospace;color:#ffd23f;background:rgba(0,0,0,.6);padding:1px 4px;border-radius:3px}.st-safe[hidden]{display:none}
/* the bin is under the video, over the play controls, while a picture or text is dragged: on the video it covered
   its top, where nothing could be put */
.st-left{position:relative}
.st-bin{position:absolute;bottom:0;left:50%;transform:translateX(-50%);z-index:6;box-sizing:border-box;width:min(100%,420px);height:38px;display:flex;align-items:center;justify-content:center;border-radius:12px;background:rgba(0,0,0,.78);color:#fff;font:600 13px 'IBM Plex Sans',sans-serif;border:2px dashed rgba(255,255,255,.55);pointer-events:none}
.st-bin.over{background:#e5484d;border-color:#fff}.st-bin[hidden]{display:none}
.st-flash{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);z-index:4;max-width:88%;padding:7px 12px;border-radius:10px;background:rgba(0,0,0,.78);color:#fff;font:600 12.5px/1.35 'IBM Plex Sans',sans-serif;text-align:center;pointer-events:none;opacity:0;transition:opacity .2s}.st-flash.on{opacity:1}
.st-big{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:2}
.st-big b{width:64px;height:64px;border-radius:50%;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .25s}
.st-big b::after{content:"";margin-left:6px;border-left:22px solid #fff;border-top:14px solid transparent;border-bottom:14px solid transparent}
.st-stage.paused .st-big b{opacity:1}
.st-transport{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:6px;width:100%}
.st-play{min-width:78px;background:#141414!important;color:#fff!important;border-color:#141414!important}
.st-time{font:600 13px 'IBM Plex Mono',monospace;min-width:120px;text-align:center}
.st-transport label{font-size:12.5px;color:#3a3a3a;display:inline-flex;gap:5px;align-items:center;cursor:pointer}
.st-right{min-width:0;min-height:0;display:flex;flex-direction:column}
.st-tools{flex:1 1 0;min-height:230px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px 14px;overflow:auto}
.st-tabs{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 10px}
.st-tools > .st-head{position:sticky;top:-12px;z-index:3;background:#fff;margin:-12px -14px 10px;padding:12px 14px 8px;border-bottom:1px solid #f0f0ec}   /* the tabs stay in view while a panel scrolls */
.st-tlbar .st-go{margin-left:4px}
.st-tabs button{height:32px;padding:0 13px;border-radius:16px;border:1px solid #c9cfca;background:#fff;font:600 12.5px 'IBM Plex Sans',sans-serif;cursor:pointer;color:#3a3a3a}
.st-tabs button[aria-selected=true]{background:#141414;border-color:#141414;color:#fff}
.st-tabs button i{display:inline-block;width:7px;height:7px;border-radius:50%;background:#ff3da6;margin-left:5px;vertical-align:1px}
.st-panel[hidden]{display:none}.st-panel h4{margin:12px 0 6px;font:700 13px 'IBM Plex Sans',sans-serif}
.st-note{font-size:12.5px;color:var(--mute);margin:6px 0}.st-msg{font-size:13px;margin:6px 0;color:var(--g);min-height:1em}.st-msg.err{color:#b42318}
.st-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.st-btn{height:34px;padding:0 13px;border-radius:8px;border:1px solid var(--g);background:var(--g);color:#fff;font:600 13px 'IBM Plex Sans',sans-serif;cursor:pointer}
.st-btn:disabled{opacity:.55;cursor:default}.st-btn.ghost{background:#fff;color:var(--g)}
.st-drop{display:block;border:2px dashed #c9cfca;border-radius:12px;padding:14px;text-align:center;cursor:pointer;background:#fafaf7;font-size:13px;color:#3a3a3a}
.st-drop b{display:block;font-size:13.5px;color:#141414}.st-drop.over{border-color:var(--g);background:#eef8f2}
.st-list{list-style:none;margin:8px 0 0;padding:0}
.st-list li{display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid #f0f0ec}
.st-list li.on{background:#f3faf6}.st-list .th{width:34px;height:60px;border-radius:5px;background:#1b2430;flex:none;object-fit:cover;overflow:hidden}
.st-list .th.pic{width:48px;height:48px;object-fit:contain;background:repeating-conic-gradient(#e3e5e0 0 25%,#fff 0 50%) 0 0/10px 10px}
.st-quick{margin:0 0 12px}.st-qh{margin:0 0 6px;font-size:13.5px;color:#141414}.st-qh small{font-size:12.5px;color:#6b6f76}
.st-qrow{display:flex;flex-wrap:wrap;gap:8px}.st-q{position:relative}
.st-qpic,.st-qnew{display:grid;place-items:center;width:64px;height:64px;box-sizing:border-box;border:1px solid #c9cfca;border-radius:10px;background:repeating-conic-gradient(#e3e5e0 0 25%,#fff 0 50%) 0 0/10px 10px;padding:4px;cursor:pointer}
.st-qpic img{max-width:100%;max-height:100%;object-fit:contain;display:block}.st-qpic:hover,.st-qnew:hover{border-color:var(--g);box-shadow:0 0 0 2px var(--g)}
.st-qnew{background:#fafaf7;border-style:dashed;font-size:12px;color:#3a3a3a;align-content:center;gap:0}.st-qnew b{font-size:20px;line-height:1;color:#141414}
.st-qx{position:absolute;top:-7px;right:-7px;width:22px;height:22px;border-radius:50%;border:1px solid #c9cfca;background:#fff;color:#3a3a3a;font-size:14px;line-height:1;cursor:pointer;padding:0}
.st-qx:hover{border-color:#b42318;color:#b42318}
.st-list .st-ib[data-keep][aria-pressed=true]{border-color:var(--g);color:var(--g)}
.st-rep{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:8px 0}
.st-repv{display:flex;flex-direction:column;align-items:center;gap:6px;padding:8px;border:1px solid #c9cfca;border-radius:10px;background:repeating-conic-gradient(#e3e5e0 0 25%,#fff 0 50%) 0 0/10px 10px;cursor:pointer;font:600 12px 'IBM Plex Sans',sans-serif;color:#141414}
.st-repv:hover{border-color:var(--g);box-shadow:0 0 0 2px var(--g)}.st-repv img{width:100%;height:150px;object-fit:contain;display:block}
.st-repv span{background:#fff;border-radius:6px;padding:2px 6px}
.st-bgx{margin:12px 0 0;padding:12px;border:1px solid #e3e3dc;border-radius:12px;background:#fafaf7}
.st-bgx-row{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}
.st-bgx-pv{display:grid;place-items:center;width:180px;height:180px;flex:none;border-radius:10px;overflow:hidden;
  background:repeating-conic-gradient(#d9dcd6 0 25%,#fff 0 50%) 0 0/16px 16px}
.st-bgx-pv img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.st-bgx-set{display:flex;flex-direction:column;gap:8px;flex:1;min-width:220px;font-size:13px;color:#3a3a3a}
.st-bgx-set label{display:flex;flex-wrap:wrap;align-items:center;gap:6px}.st-bgx-set input[type=range]{flex:1;min-width:120px}
.st-list .nm{flex:1;min-width:0}.st-list .nm b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st-list .nm small{display:block;font-size:12px;color:var(--mute)}
.st-list .ac{display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end}
.st-sw{display:flex;flex-wrap:wrap;gap:6px}.st-sw button{width:26px;height:26px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px #c9cfca;cursor:pointer}
.st-sw button[aria-pressed=true]{box-shadow:0 0 0 2px #141414}
.st-seg{display:inline-flex;border:1px solid #c9cfca;border-radius:8px;overflow:hidden}
.st-seg button{height:30px;border:0;background:#fff;padding:0 10px;font:600 12px 'IBM Plex Sans',sans-serif;cursor:pointer;color:#141414}
.st-seg button+button{border-left:1px solid #e3e3dc}.st-seg button[aria-pressed=true]{background:#141414;color:#fff}
.st textarea,.st input[type=url],.st input[type=text]{width:100%;box-sizing:border-box;border:1px solid #c9cfca;border-radius:8px;padding:7px 9px;font:14px/1.4 'IBM Plex Sans',sans-serif;resize:vertical}
.st textarea:focus,.st input:focus{outline:none;border-color:var(--g);box-shadow:0 0 0 3px rgba(15,125,71,.12)}
.st label.rng{display:grid;grid-template-columns:110px 1fr 46px;align-items:center;gap:8px;font-size:12.5px;margin:4px 0}
.st label.rng output{font:600 12px 'IBM Plex Mono',monospace;text-align:right}.st input[type=range]{accent-color:#0f7d47}
.st-wave{position:relative;height:64px;border-radius:8px;background:#f1f3ef;overflow:hidden;cursor:grab;touch-action:none;margin:6px 0}
.st-wave canvas{position:absolute;inset:0;width:100%;height:100%}
.st-wave .win{position:absolute;top:0;bottom:0;border:2px solid #0f9d8a;background:rgba(15,157,138,.16);border-radius:6px;box-sizing:border-box;pointer-events:none}
.st-prog{height:8px;border-radius:4px;background:#eceee9;overflow:hidden;margin:8px 0}.st-prog i{display:block;height:100%;width:0;background:var(--g);transition:width .2s}
.st-result video{width:100%;max-height:360px;background:#000;border-radius:10px;margin:6px 0}
.st-result a.st-btn{display:inline-flex;align-items:center;text-decoration:none}
.st-tlbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:12px 0 6px}
.st-tlbar .st-sel{font-size:12.5px;color:var(--mute);margin-left:4px}
.st-zoom{margin-left:auto;display:inline-flex;gap:4px}
.st-tl{display:grid;grid-template-columns:104px minmax(0,1fr);border:1px solid var(--line);border-radius:12px;background:#fafaf7;overflow:hidden;user-select:none;-webkit-user-select:none}
.st-tl-labels{border-right:1px solid var(--line);background:#fff}
.st-tl-labels div{display:flex;align-items:center;gap:6px;padding:0 8px;font:600 11.5px 'IBM Plex Sans',sans-serif;color:#3a3a3a;white-space:nowrap;overflow:hidden;border-bottom:1px solid #eceee9;box-sizing:border-box}
.st-tl-labels div.sel{background:#fff7d6}
.st-tl-scroll{overflow-x:auto;overflow-y:hidden;position:relative;scrollbar-width:thin}
.st-tl-inner{position:relative;min-width:100%}
.st-ruler{height:26px;position:relative;border-bottom:1px solid var(--line);cursor:pointer;box-sizing:border-box}
.st-ruler i{position:absolute;bottom:0;width:1px;height:6px;background:#b9c0bb}.st-ruler i.maj{height:11px;background:#8a9099}
.st-ruler b{position:absolute;top:3px;font:500 10.5px 'IBM Plex Mono',monospace;color:#6b6f76;transform:translateX(-50%);pointer-events:none}
.st-trk{position:relative;border-bottom:1px solid #eceee9;box-sizing:border-box}
.st-trk.v{height:64px}.st-trk.i{height:34px}.st-trk.m{height:48px}
.st-clip{position:absolute;top:4px;bottom:4px;border-radius:8px;background:#20262e;overflow:hidden;cursor:pointer;box-sizing:border-box}
.st-clip canvas{position:absolute;left:0;top:0;height:100%;pointer-events:none}
.st-clip.sel{box-shadow:inset 0 0 0 3px #ffd23f}.st-clip.none{background:repeating-linear-gradient(45deg,#e9ebe7 0 8px,#f3f4f1 8px 16px);cursor:default}
.st-clip.none span{position:absolute;left:10px;top:50%;transform:translateY(-50%);font-size:12px;color:#6b6f76;white-space:nowrap}
.st-hd{position:absolute;top:0;bottom:0;width:12px;background:rgba(255,255,255,.9);cursor:ew-resize;z-index:2;box-sizing:border-box}
.st-hd::after{content:"";position:absolute;left:5px;top:35%;bottom:35%;width:2px;border-radius:1px;background:#141414}
.st-hd.l{left:0;border-radius:8px 0 0 8px}.st-hd.r{right:0;border-radius:0 8px 8px 0}
.st-bar{position:absolute;top:4px;bottom:4px;border-radius:7px;display:flex;align-items:center;gap:6px;padding:0 15px;font:600 11.5px 'IBM Plex Sans',sans-serif;color:#fff;white-space:nowrap;overflow:hidden;cursor:grab;box-sizing:border-box}
.st-bar.pic{background:#7a5cf0}.st-bar.text{background:#d9820b}.st-bar.mus{background:#0f9d8a;padding:0 12px}
.st-bar.sel{box-shadow:0 0 0 2px #141414,inset 0 0 0 1px #fff}
.st-bar .st-hd{width:10px;background:rgba(255,255,255,.4)}.st-bar .st-hd::after{left:4px;background:#fff}
.st-bar img{height:22px;width:auto;border-radius:3px;pointer-events:none}
.st-bar canvas{position:absolute;left:0;top:0;height:100%;opacity:.55;pointer-events:none}
.st-bar .lbl{position:relative;pointer-events:none;overflow:hidden;text-overflow:ellipsis}
.st-ph{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:#ff3da6;pointer-events:none;z-index:6}
.st-ph i{position:absolute;top:0;left:-7px;width:16px;height:16px;background:#ff3da6;border-radius:3px 3px 50% 50%;pointer-events:auto;cursor:ew-resize}
.st-tip{position:fixed;z-index:4000;padding:3px 7px;border-radius:6px;background:#141414;color:#fff;font:600 11.5px 'IBM Plex Mono',monospace;pointer-events:none;transform:translate(-50%,-130%)}
.st-keys{font-size:12px;color:var(--mute);margin:8px 0 0}
@media (max-width:980px){.st-main{grid-template-columns:1fr}.st-stage{height:min(72vh,640px)}.st-tools{flex:none;max-height:none}}
`;
  document.head.appendChild(s);
}

const TEMPLATE = `
<div class="st" tabindex="-1">
  <div class="st-main">
    <div class="st-left">
      <div class="st-stage paused" aria-label="Preview">
        <div class="st-mock"><span>Your recording plays here. Pictures, text and a song can go on it now or after you record.</span></div>
        <video class="st-video" playsinline preload="auto" hidden></video>
        <div class="st-items"></div>
        <div class="st-guides"><i class="gv"></i><i class="gh"></i></div>
        <div class="st-safe" hidden><span>TikTok buttons and caption sit outside this line</span></div>
        <div class="st-big"><b></b></div>
        <div class="st-flash"></div>
      </div>
      <div class="st-bin" hidden>Drop here to remove</div>
      <div class="st-transport">
        <button type="button" class="st-ib" data-a="back" title="Back 1 second (left arrow; Shift: 5 seconds)">&minus;1s</button>
        <button type="button" class="st-ib st-play" data-a="play" title="Play or pause (Space)">Play</button>
        <button type="button" class="st-ib" data-a="fwd" title="Forward 1 second (right arrow; Shift: 5 seconds)">+1s</button>
        <span class="st-time">0:00.0 / 0:00.0</span>
        <label><input type="checkbox" data-a="safe"> TikTok safe area</label>
      </div>
    </div>
    <div class="st-right">
      <div class="st-tools">
      <div class="st-head"><div class="st-tabs" role="tablist" aria-label="Studio tools">
        <button type="button" role="tab" data-tab="takes" aria-selected="true">Takes</button>
        <button type="button" role="tab" data-tab="pics" aria-selected="false">Pictures</button>
        <button type="button" role="tab" data-tab="report" aria-selected="false">Report</button>
        <button type="button" role="tab" data-tab="text" aria-selected="false">Text</button>
        <button type="button" role="tab" data-tab="music" aria-selected="false">Music</button>
        <button type="button" role="tab" data-tab="export" aria-selected="false">Export</button>
      </div><span class="st-saved" aria-live="polite"></span><button type="button" class="st-ib" data-a="new" title="Start a new edit on the same take; the takes stay">New edit</button></div>
      <section class="st-panel" data-p="takes" role="tabpanel">
        <div class="st-row"><button type="button" class="st-btn" data-a="record">Record a take</button>
          <label class="st-ib" style="display:inline-flex;align-items:center">Open a video file<input type="file" accept="video/*,.mp4,.mov,.webm,.m4v" data-in="video" hidden></label></div>
        <p class="st-note">Record mode opens in its own window. When the take ends it comes here by itself, with the button Edit in Studio. Takes are kept in this browser (the newest 10; your downloaded files are not touched).</p>
        <p class="st-msg" data-m="takes" role="status"></p>
        <ul class="st-list st-takes"></ul>
      </section>
      <section class="st-panel" data-p="pics" role="tabpanel" hidden>
        <div class="st-quick" role="group" aria-label="Quick add"><p class="st-qh"><b>Quick add</b> <small>Pictures you use a lot, kept in this browser for every video: click one to put it on the video.</small></p><div class="st-qrow"></div></div>
        <label class="st-drop" data-drop="pics"><input type="file" accept="image/*" multiple data-in="pics" hidden><b>Drop pictures here or click to choose</b>You can also paste one (Cmd+V).</label>
        <p class="st-note">Drag a picture on the preview to move it, the corner to resize it, the round handle to turn it, and drop it on the bin under the video to remove it. Its purple bar under the video sets when it shows: drag the bar, or its ends.</p>
        <p class="st-msg" data-m="pics" role="status"></p>
        <ul class="st-list st-pics"></ul>
        <div class="st-bgx" hidden>
          <p class="st-qh"><b>Transparent background</b> <small class="st-bgx-nm"></small></p>
          <div class="st-bgx-row"><span class="st-bgx-pv"><img alt="The picture with its background removed"></span>
            <span class="st-bgx-set"><label>How close a color counts as background <input type="range" min="1" max="45" value="12" data-in="bgtol"><output>12%</output></label>
              <label><input type="checkbox" data-in="bginside"> Also inside the picture (the holes in letters like O and A)</label>
              <span class="st-note st-bgx-st" role="status"></span>
              <span class="st-row"><button type="button" class="st-btn" data-a="bgok">Use it</button><button type="button" class="st-ib" data-a="bgno">Cancel</button></span></span></div>
          <p class="st-note">It clears the color around the picture's edges: best for a logo or a picture on a plain background (a photo's background is not one color). The original is kept: Undo brings it back.</p>
        </div>
      </section>
      <section class="st-panel" data-p="report" role="tabpanel" hidden>
        <div class="st-row">Style <div class="st-seg" data-set="rstyle"><button type="button" data-v="dark">Dark</button><button type="button" data-v="light">Light</button></div></div>
        <p class="st-msg" data-m="report" role="status"></p>
        <div class="st-rep"></div>
        <p class="st-note">Each version is drawn from the report the Crawler made of this filing: every figure as the filing prints it, with its page. Click one to put it on the video: it shows for 5 seconds from the playhead (the last 5 seconds when the playhead is at the start). Then move, resize and time it like a picture.</p>
      </section>
      <section class="st-panel" data-p="text" role="tabpanel" hidden>
        <textarea rows="3" data-in="text" placeholder="Type your text"></textarea>
        <div class="st-row"><button type="button" class="st-btn" data-a="addtext">Add text</button><span class="st-note" data-m="text"></span></div>
        <h4>Style</h4><div class="st-seg" data-set="style">${Object.entries(FONTS).map(([k, f]) => `<button type="button" data-v="${k}">${f.label}</button>`).join("")}</div>
        <h4>Color</h4><div class="st-sw" data-set="color">${COLORS.map(c => `<button type="button" data-v="${c}" style="background:${c}" aria-label="Color ${c}"></button>`).join("")}</div>
        <h4>Background</h4><div class="st-seg" data-set="bg"><button type="button" data-v="none">None</button><button type="button" data-v="box">Box</button><button type="button" data-v="outline">Outline</button></div>
        <h4>Align</h4><div class="st-seg" data-set="align"><button type="button" data-v="left">Left</button><button type="button" data-v="center">Center</button><button type="button" data-v="right">Right</button></div>
        <label class="rng">Size <input type="range" min="2" max="20" step="0.25" data-in="tsize"><output></output></label>
        <p class="st-note">Text goes on top of everything, where you drag it. Double-click text on the preview to edit it here.</p>
      </section>
      <section class="st-panel" data-p="music" role="tabpanel" hidden>
        <label class="st-drop" data-drop="music"><input type="file" accept="audio/*,.mp3,.m4a,.aac,.wav,.aif,.aiff,.aifc,.ogg,.oga,.opus,.flac,.weba" data-in="music" hidden><b>Drop a song here or click to choose</b>MP3, M4A, WAV, AIFF, AAC, OGG or FLAC.</label>
        <div class="st-row"><input type="url" data-in="link" placeholder="Or paste a link (SoundCloud, YouTube or a link to an audio file)"><button type="button" class="st-ib" data-a="link">Use link</button></div>
        <p class="st-note" data-m="link"></p>
        <div class="st-mus" hidden>
          <div class="st-row"><b class="st-mus-name"></b><span class="st-note st-mus-len"></span><button type="button" class="st-ib" data-a="listen">Listen</button><button type="button" class="st-ib" data-a="rmmusic">Remove song</button></div>
          <p class="st-note">The light box is the part of the song that plays under the video: drag it to choose the part. Its teal bar on the timeline sets where in the video the song starts.</p>
          <div class="st-wave"><canvas></canvas><div class="win"></div></div>
          <label class="rng">Song volume <input type="range" min="0" max="100" data-in="mvol"><output></output></label>
          <label class="rng">Fade in <input type="range" min="0" max="5" step="0.5" data-in="fadein"><output></output></label>
          <label class="rng">Fade out <input type="range" min="0" max="8" step="0.5" data-in="fadeout"><output></output></label>
        </div>
        <label class="rng">Original sound <input type="range" min="0" max="100" data-in="ovol"><output></output></label>
        <p class="st-note">Record mode takes have no sound of their own; a video file you open may.</p>
      </section>
      <section class="st-panel" data-p="export" role="tabpanel" hidden>
        <p class="st-note">The video is made at 1080 &times; 1920, the TikTok and Reels size, with the pictures, text and song as you see them in the preview.</p>
        <div class="st-row">Frame rate <div class="st-seg" data-set="fps"><button type="button" data-v="30">30 fps</button><button type="button" data-v="60">60 fps</button></div></div>
        <div class="st-row">Video in the frame <div class="st-seg" data-set="fit"><button type="button" data-v="auto">Auto</button><button type="button" data-v="fill">Fill (crop edges)</button><button type="button" data-v="fit">Whole (bars)</button></div></div>
        <label class="st-row st-disk" hidden><input type="checkbox" data-in="disk" checked> Save to disk while exporting (choose where; long videos use no extra memory)</label>
        <div class="st-row"><button type="button" class="st-btn" data-a="export">Export video</button><button type="button" class="st-ib" data-a="cancel" hidden>Cancel</button></div>
        <div class="st-prog" hidden><i></i></div>
        <p class="st-msg" data-m="export" role="status"></p>
        <div class="st-result"></div>
        <p class="st-note">Export plays the edit once in real time: a 45-second video takes about 45 seconds. Keep this tab in front while it runs; it pauses if you switch away and goes on when you come back.</p>
      </section>
      </div>
  <div class="st-tlbar">
    <button type="button" class="st-ib" data-a="undo" title="Undo (Cmd+Z)" disabled>Undo</button>
    <button type="button" class="st-ib" data-a="redo" title="Redo (Shift+Cmd+Z)" disabled>Redo</button>
    <i class="st-sep"></i>
    <button type="button" class="st-ib" data-a="split" title="Split the video at the playhead (S)">Split</button>
    <button type="button" class="st-ib" data-a="del" title="Remove what is selected (Delete)">Delete</button>
    <button type="button" class="st-ib" data-a="from" title="Selected picture or text starts at the playhead">Start here</button>
    <button type="button" class="st-ib" data-a="to" title="Selected picture or text ends at the playhead">End here</button>
    <button type="button" class="st-ib" data-a="whole" title="Selected picture or text shows the whole video">Whole video</button>
    <span class="st-sel"></span>
    <span class="st-zoom"><button type="button" class="st-ib" data-a="zout" title="Zoom out">&minus;</button><button type="button" class="st-ib" data-a="zfit" title="Fit the timeline">Fit</button><button type="button" class="st-ib" data-a="zin" title="Zoom in">+</button></span>
    <button type="button" class="st-btn st-go" data-a="goexport" title="Make the 1080 &times; 1920 video and download it">Export &amp; download</button>
  </div>
  <div class="st-tl">
    <div class="st-tl-labels"></div>
    <div class="st-tl-scroll"><div class="st-tl-inner"><div class="st-ruler"></div><div class="st-tracks"></div><div class="st-ph"><i></i></div></div></div>
  </div>
  <p class="st-keys">Space play or pause &middot; &larr; &rarr; 1 second (Shift: 5) &middot; S split &middot; Delete removes the selection &middot; Cmd+Z undo &middot; press or drag on the timeline to scrub &middot; pinch or Ctrl+scroll on it to zoom</p>
    </div>
  </div>
</div>`;

function newProject(){
  return {v: 1, take: null, segs: [], fit: "auto", fps: 30, items: [], music: null, origVol: 1, placeholder: 20};
}

function mount(root, opts){
  opts = opts || {};
  const db = opts.db;
  injectCSS();
  root.innerHTML = TEMPLATE;
  const st = root.querySelector(".st");
  const $ = s => st.querySelector(s), $$ = s => [...st.querySelectorAll(s)];
  const stage = $(".st-stage"), video = $(".st-video"), itemsEl = $(".st-items"), mock = $(".st-mock");
  const tlScroll = $(".st-tl-scroll"), inner = $(".st-tl-inner"), ruler = $(".st-ruler"), tracks = $(".st-tracks"), labels = $(".st-tl-labels"), ph = $(".st-ph");
  let P = newProject();
  const media = new Map();            // media id -> {url, blob, name, type, img?, ratio?, buf?, peaks?}
  const take = {id: null, url: null};   // the take on the preview
  let cur = 0, playing = false, raf = 0, segIdx = 0, clock0 = 0;
  let selected = null;                // an item id, "seg:<id>" or "music"
  let shown = false, exportJob = null, pps = 0, fitPps = 0, thumbs = [], stripFor = null;
  let ac = null, songNode = null, songGain = null;
  let tab = "takes";
  try { tab = localStorage.getItem("bmg-studio-tab") || "takes"; } catch (e) {}

  const editDur = () => P.take ? P.segs.reduce((a, s) => a + (s.out - s.in), 0) : P.placeholder;
  const segStart = i => { let a = 0; for (let k = 0; k < i; k++) a += P.segs[k].out - P.segs[k].in; return a; };
  function locate(e){
    let acc = 0;
    for (let i = 0; i < P.segs.length; i++){
      const s = P.segs[i], d = s.out - s.in;
      if (e < acc + d - 1e-6 || i === P.segs.length - 1) return {i, src: clamp(s.in + (e - acc), s.in, s.out), start: acc};
      acc += d;
    }
    return {i: -1, src: 0, start: 0};
  }
  const itemById = id => P.items.find(x => x.id === id);
  // pictures and text that span the whole video keep doing so when its length changes
  function normalize(){
    const d = editDur();
    for (const it of P.items){
      if (it.whole){ it.t0 = 0; it.t1 = d; continue; }
      it.t1 = clamp(it.t1, MIN_LEN, d); it.t0 = clamp(it.t0, 0, Math.max(0, it.t1 - MIN_LEN));
    }
    if (P.music) P.music.at = clamp(P.music.at, 0, Math.max(0, d - 0.5));
    cur = clamp(cur, 0, d);
  }

  // ---------------- saving, and undo ----------------
  const savedEl = $(".st-saved");
  let saveTimer = 0, lastSnap = null, saving = false;
  const past = [], future = [];
  function save(){
    clearTimeout(saveTimer);
    savedEl.textContent = "Saving..."; saving = true;
    saveTimer = setTimeout(async () => {
      try { await db.put("projects", {id: "current", p: P, saved: Date.now()}); savedEl.textContent = "Saved in this browser"; }
      catch (e) { savedEl.textContent = "Not saved: " + (e.message || e); }
      saving = false;
    }, 350);
  }
  function commit(){
    const s = JSON.stringify(P);
    if (s === lastSnap) return;
    if (lastSnap != null){ past.push(lastSnap); if (past.length > 120) past.shift(); }
    future.length = 0; lastSnap = s;
    save(); undoButtons();
  }
  function undoButtons(){ $('[data-a="undo"]').disabled = !past.length; $('[data-a="redo"]').disabled = !future.length; }
  async function restore(snap){
    const before = P.take && P.take.id;
    P = JSON.parse(snap); lastSnap = snap;
    if (selected && !(selected === "music" ? P.music : selected.startsWith("seg:") ? P.segs.some(s => "seg:" + s.id === selected) : itemById(selected))) selected = null;
    if ((P.take && P.take.id) !== before) await showTake(P.take && P.take.id, false);
    normalize(); renderItems(); drawTimeline(); renderAt(); panels(); save(); undoButtons();
  }
  function undo(){ if (!past.length) return; pause(); future.push(lastSnap); restore(past.pop()); }
  function redo(){ if (!future.length) return; pause(); past.push(lastSnap); restore(future.pop()); }

  // ---------------- messages ----------------
  const flashEl = $(".st-flash");
  let flashTimer = 0;
  function flash(text){ flashEl.textContent = text; flashEl.classList.add("on"); clearTimeout(flashTimer); flashTimer = setTimeout(() => flashEl.classList.remove("on"), 2600); }
  function note(key, text, err){ const m = st.querySelector(`[data-m="${key}"]`); if (!m) return; m.textContent = text || ""; m.classList.toggle("err", !!err); }

  // ---------------- media ----------------
  async function loadMedia(id){
    if (media.has(id)) return media.get(id);
    const rec = await db.get("media", id);
    if (!rec || !rec.blob) return null;
    const m = {id, blob: rec.blob, name: rec.name, type: rec.blob.type, url: URL.createObjectURL(rec.blob)};
    if (/^image\//.test(m.type)){ try { m.img = await loadImg(m.url); m.ratio = m.img.naturalHeight / m.img.naturalWidth; } catch (e) { return null; } }
    if (/^audio\/|^video\/webm/.test(m.type) || rec.kind === "song"){ try { m.buf = await decodeAudio(rec.blob); m.peaks = peaks(m.buf, 1600); } catch (e) { return null; } }
    media.set(id, m);
    return m;
  }
  async function addMedia(blob, name, kind, extra){
    const id = "m-" + uid();
    await db.put("media", Object.assign({id, blob, name, kind, added: Date.now()}, extra));
    return loadMedia(id);
  }

  // ---------------- Quick add: pictures kept for every video (a logo, a headshot) ----------------
  // a picture's own media record carries quick: the time it was kept; taking it off the shelf keeps the record
  // (a video may still use it)
  let quick = [];
  async function loadQuick(){
    let all = [];
    try { all = (await db.all("media")).filter(r => r.quick && r.blob && /^image\//.test(r.blob.type)).sort((a, b) => a.quick - b.quick); } catch (e) {}
    quick = [];
    for (const r of all){ const m = await loadMedia(r.id).catch(() => null); if (m) quick.push(m); }
    drawQuick();
  }
  function drawQuick(){
    $(".st-qrow").innerHTML = quick.map(m => `<span class="st-q"><button type="button" class="st-qpic" data-qadd="${esc(m.id)}" title="Put ${esc(m.name)} on the video"><img src="${esc(m.url)}" alt="${esc(m.name)}"></button><button type="button" class="st-qx" data-qrm="${esc(m.id)}" title="Take ${esc(m.name)} off Quick add" aria-label="Take ${esc(m.name)} off Quick add">&times;</button></span>`).join("")
      + `<label class="st-qnew" title="Add pictures to Quick add"><input type="file" accept="image/*" multiple data-in="quick" hidden><b>+</b>Add</label>`;
  }
  async function keepQuick(id, on){
    try { const rec = await db.get("media", id); if (!rec) return; rec.quick = on ? Date.now() : 0; await db.put("media", rec); } catch (e) { flash("This browser could not keep it (storage full?)."); return; }
    await loadQuick(); panels();
    if (on) flash("Kept in Quick add, for every video.");
  }
  // ---------------- Report: versions of the take's report, drawn as pictures for the video ----------------
  // The record window keeps the report with each take (meta.report: the figures and findings its ending shows, as
  // printed, with their pages). Nothing here computes a figure: it lays out what the report says.
  let repStyle = "dark", repShown = "";
  try { repStyle = localStorage.getItem("bmg-studio-repstyle") === "light" ? "light" : "dark"; } catch (e) {}
  const REP_SEV = {flag: "#e5484d", watch: "#e8920c", observation: "#3e7bfa"};
  const repTheme = st => st === "light"
    ? {bg: "#ffffff", line: "#e3e3dc", text: "#141414", mute: "#6b6f76", up: "#0f7d47", down: "#b42318", brand: "#0f7d47", tile: "#f5f6f3"}
    : {bg: "#0b0f14", line: "rgba(255,255,255,.12)", text: "#f2f5f8", mute: "#9aa6b1", up: "#2fdc74", down: "#ff5c6b", brand: "#2fdc74", tile: "rgba(255,255,255,.06)"};
  const takeRep = () => { const r = P.take && P.take.meta && P.take.meta.report; return r && ((r.figures || []).length || (r.findings || []).length) ? r : null; };
  // the four tiles, as the ending's summary picks them (revenue, net income, diluted EPS, free cash flow), then the rest
  const repTiles = (r, n) => { const pref = ["revenue", "net_income", "eps_diluted", "fcf"], f = r.figures || [];
    return [...pref.map(k => f.find(x => x.k === k)).filter(Boolean), ...f.filter(x => !pref.includes(x.k))].slice(0, n); };
  function repVersions(r){
    const v = [];
    if ((r.figures || []).length) v.push({id: "one", name: "One-page summary"});
    if ((r.findings || []).length) v.push({id: "keys", name: "Key findings"});
    if ((r.figures || []).length) v.push({id: "nums", name: "Headline numbers"});
    for (const f of r.figures || []) v.push({id: "fig:" + f.k, name: f.label + " card"});
    return v;
  }
  function repWrap(g, text, maxW){
    const lines = []; let line = "";
    for (const w of String(text || "").split(/\s+/).filter(Boolean)){ const t = line ? line + " " + w : w; if (line && g.measureText(t).width > maxW){ lines.push(line); line = w; } else line = t; }
    if (line) lines.push(line);
    return lines;
  }
  // one version, drawn on a canvas: a rounded card with a soft shadow on a clear background
  async function repCard(r, ver, st){
    try { await Promise.all(["700 50px 'IBM Plex Sans'", "500 24px 'IBM Plex Sans'", "600 22px 'IBM Plex Mono'"].map(f => document.fonts.load(f))); } catch (e) {}
    const T = repTheme(st), fig = ver.id.startsWith("fig:"), W = fig ? 760 : 1000, M = 36, Pd = fig ? 56 : 52, inner = W - 2 * Pd;
    const c = document.createElement("canvas"); c.width = W + 2 * M; c.height = 3000;
    const g = c.getContext("2d"), ops = [];
    const sans = (w, px) => `${w} ${px}px 'IBM Plex Sans', Helvetica, Arial, sans-serif`, mono = (w, px) => `${w} ${px}px 'IBM Plex Mono', ui-monospace, monospace`;
    const text = (t, font, color, x, y, align) => ops.push(() => { g.font = font; g.fillStyle = color; g.textAlign = align || "left"; g.fillText(t, M + x, M + y); });
    const measure = (t, font) => { g.font = font; return g.measureText(t).width; };
    const brand = (y, right) => {
      ops.push(() => { g.strokeStyle = T.brand; g.lineWidth = 4; g.lineCap = "round"; g.lineJoin = "round"; g.beginPath();
        g.moveTo(M + Pd, M + y + 2); g.lineTo(M + Pd + 12, M + y - 10); g.lineTo(M + Pd + 22, M + y - 3); g.lineTo(M + Pd + 36, M + y - 18); g.stroke(); });
      text("BMG Capital", sans(700, 26), T.text, Pd + 48, y);
      if (right) text(right, sans(500, 22), T.mute, Pd + inner, y, "right");
    };
    const tile = (f, x, y, w, h) => {
      ops.push(() => { g.fillStyle = T.tile; g.beginPath(); g.roundRect(M + x, M + y, w, h, 18); g.fill(); });
      text(f.label, sans(500, 24), T.mute, x + 22, y + 42);
      let vf = sans(700, 52); if (measure(f.value, vf) > w - 44) vf = sans(700, 40);
      text(f.value, vf, T.text, x + 22, y + 100);
      if (f.change) text(f.change, sans(600, 26), f.up == null ? T.mute : f.up ? T.up : T.down, x + 22, y + 138);
      text([f.period, f.page ? "p." + f.page : ""].filter(Boolean).join(" · "), mono(500, 19), T.mute, x + 22, y + 168);
    };
    const tiles = (list, y) => { const gap = 18, w = (inner - gap) / 2, h = 186; list.forEach((f, i) => tile(f, Pd + (i % 2) * (w + gap), y + Math.floor(i / 2) * (h + gap), w, h)); return y + Math.ceil(list.length / 2) * (h + gap) - gap; };
    const finding = (x, y, big) => {
      const fs = big ? 28 : 24, lh = big ? 38 : 32;
      const y0 = y;
      ops.push(() => { g.fillStyle = REP_SEV[x.severity] || T.mute; g.beginPath(); g.arc(M + Pd + 8, M + y0 - fs * .35, 7, 0, 7); g.fill(); });
      g.font = sans(600, fs); const lines = repWrap(g, x.headline, inner - 30);
      lines.forEach((l, i) => text(l, sans(600, fs), T.text, Pd + 30, y + i * lh));
      y += (lines.length - 1) * lh + (big ? 32 : 28);
      text([x.tag, x.pages].filter(Boolean).join(" · "), mono(500, big ? 20 : 18), T.mute, Pd + 30, y);
      return y + (big ? 30 : 22);
    };
    const who = [r.ticker, r.form].filter(Boolean).join(" · ");
    let y = Pd + 26;
    if (fig){
      const f = (r.figures || []).find(x => "fig:" + x.k === ver.id);
      text(f.label.toUpperCase(), mono(600, 30), T.brand, Pd, y + 6); y += 120;
      let vf = sans(700, 120); if (measure(f.value, vf) > inner) vf = sans(700, 88);
      text(f.value, vf, T.text, Pd, y); y += 62;
      if (f.change){ text(f.change + " vs a year earlier", sans(600, 34), f.up == null ? T.mute : f.up ? T.up : T.down, Pd, y); y += 50; }
      text([who, f.period, f.page ? "p." + f.page : ""].filter(Boolean).join(" · "), mono(500, 24), T.mute, Pd, y); y += 56;
      brand(y, ""); y += 14;
    } else if (ver.id === "keys"){
      brand(y, who); y += 74;
      g.font = sans(700, 40); repWrap(g, `Key findings · ${r.company}`, inner).forEach(l => { text(l, sans(700, 40), T.text, Pd, y); y += 50; });
      y += 18;
      for (const x of (r.findings || []).slice(0, 4)) y = finding(x, y + 12, true) + 8;
      y -= 10;
    } else {
      brand(y, ver.id === "one" ? "Company report" : who); y += 78;
      g.font = sans(700, 50); repWrap(g, r.company, inner).slice(0, 2).forEach(l => { text(l, sans(700, 50), T.text, Pd, y); y += 58; });
      text([r.ticker, r.form, r.period].filter(Boolean).join(" · "), sans(500, 26), T.mute, Pd, y - 8); y += 34;
      y = tiles(repTiles(r, ver.id === "one" ? 4 : 6), y) + 26;
      if (ver.id === "one" && (r.findings || []).length){ y += 16; for (const x of r.findings.slice(0, 3)) y = finding(x, y + 8, false) + 4; }
      if (ver.id === "one"){ y += 10; text([r.coverage, "every figure cites its page in the filing"].filter(Boolean).join(" · "), sans(500, 19), T.mute, Pd, y); y += 6; }
    }
    const H = y + Pd;
    const out = document.createElement("canvas"); out.width = W + 2 * M; out.height = H + 2 * M;
    const o = out.getContext("2d");
    o.save(); o.shadowColor = "rgba(0,0,0,.35)"; o.shadowBlur = 28; o.shadowOffsetY = 8; o.fillStyle = T.bg; o.beginPath(); o.roundRect(M, M, W, H, 28); o.fill(); o.restore();
    o.strokeStyle = T.line; o.lineWidth = 2; o.beginPath(); o.roundRect(M + 1, M + 1, W - 2, H - 2, 27); o.stroke();
    c.height = H + 2 * M;                           // (clears it) then the card's content is drawn on the card
    for (const op of ops) op();
    o.drawImage(c, 0, 0);
    return out;
  }
  const canvasBlob = cv => new Promise((res, rej) => cv.toBlob(b => b ? res(b) : rej(new Error("picture")), "image/png"));
  async function drawReport(){
    setPressed("rstyle", repStyle);
    const box = $(".st-rep"), r = takeRep();
    const key = (P.take ? P.take.id : "") + repStyle;
    if (!r){
      box.innerHTML = ""; repShown = "";
      note("report", P.take ? "This take has no report kept with it (a take from a video file, or recorded before the Report tab). Takes recorded from now on keep their report." : "Open a take first (Takes): its report shows here.");
      return;
    }
    note("report", `${r.company}${r.form ? " · " + r.form : ""}: ${repVersions(r).length} versions.`);
    if (repShown === key) return;
    repShown = key; box.innerHTML = "";
    for (const ver of repVersions(r)){
      const cv = await repCard(r, ver, repStyle);
      if (repShown !== key) return;
      const url = URL.createObjectURL(await canvasBlob(cv));
      box.insertAdjacentHTML("beforeend", `<button type="button" class="st-repv" data-rep="${esc(ver.id)}" title="Put the ${esc(ver.name.toLowerCase())} on the video"><img src="${esc(url)}" alt=""><span>${esc(ver.name)}</span></button>`);
    }
  }
  async function addReport(id){
    const r = takeRep(), ver = r && repVersions(r).find(v => v.id === id); if (!ver) return;
    let m;
    try { const cv = await repCard(r, ver, repStyle); m = await addMedia(await canvasBlob(cv), `${r.ticker || "Report"} ${ver.name}.png`, "picture"); } catch (e) { m = null; }
    if (!m){ flash("This browser could not keep it (storage full?)."); return; }
    const d = editDur(), it = picItem(m, m.ratio || 1, null, 0);
    it.w = id.startsWith("fig:") ? 0.72 : 0.9; it.x = 0.5; it.y = 0.45;
    if (cur < 0.5){ it.t0 = Math.max(0, d - 5); it.t1 = d; } else { it.t0 = Math.min(cur, Math.max(0, d - 0.5)); it.t1 = Math.min(d, it.t0 + 5); }
    it.whole = false;
    P.items.push(it); selected = it.id; renderItems(); drawTimeline(); panels(); commit();
    flash(`On the video from ${fmt(it.t0, true)} to ${fmt(it.t1, true)}. Drag it to move; its purple bar sets when it shows.`);
  }

  // ---------------- Transparent background (the Pictures tab) ----------------
  let bgx = null;                         // {id: the picture on the video, src: its pixels, blob: the result}
  async function bgOpen(id){
    const it = itemById(id), m = it && media.get(it.media); if (!m || !m.img) return;
    const c = document.createElement("canvas"); c.width = m.img.naturalWidth; c.height = m.img.naturalHeight;
    const g = c.getContext("2d", {willReadFrequently: true}); g.drawImage(m.img, 0, 0);
    bgx = {id, media: m.id, w: c.width, h: c.height, src: g.getImageData(0, 0, c.width, c.height), blob: null, url: null};
    $(".st-bgx-nm").textContent = m.name;
    $(".st-bgx").hidden = false;
    select(id, true);
    bgDraw();
    $(".st-bgx").scrollIntoView({block: "nearest", behavior: "smooth"});
  }
  function bgDraw(){
    if (!bgx) return;
    const tol = +$('[data-in="bgtol"]').value / 100, inside = $('[data-in="bginside"]').checked;
    $('[data-in="bgtol"]').nextElementSibling.textContent = Math.round(tol * 100) + "%";
    const out = new ImageData(new Uint8ClampedArray(bgx.src.data), bgx.w, bgx.h);
    const r = clearBackground(out.data, bgx.w, bgx.h, tol, inside);
    const st = $(".st-bgx-st"), ok = $('[data-a="bgok"]');
    if (!r.bg){ st.textContent = "Its edges are already see-through: there is no background color to clear."; ok.disabled = true; return; }
    const pct = Math.round(r.cleared / (bgx.w * bgx.h) * 100);
    st.textContent = (r.bgs.length > 1 ? `Background: two colors, rgb(${r.bgs[0].join(", ")}) and rgb(${r.bgs[1].join(", ")}) (squares saved into the picture). ` : `Background rgb(${r.bg.join(", ")}). `)
      + `${pct}% of the picture cleared.` + (pct > 85 ? " That is nearly all of it: lower the slider." : "");
    ok.disabled = !r.cleared;
    const c = document.createElement("canvas"); c.width = bgx.w; c.height = bgx.h; c.getContext("2d").putImageData(out, 0, 0);
    const mine = bgx;
    c.toBlob(b => {
      if (bgx !== mine || !b) return;
      if (mine.url) URL.revokeObjectURL(mine.url);
      mine.blob = b; mine.url = URL.createObjectURL(b);
      $(".st-bgx-pv img").src = mine.url;
    }, "image/png");
  }
  function bgClose(){
    if (bgx && bgx.url) URL.revokeObjectURL(bgx.url);
    bgx = null; $(".st-bgx").hidden = true; $(".st-bgx-pv img").removeAttribute("src");
  }
  async function bgUse(){
    const x = bgx, it = x && itemById(x.id); if (!x || !x.blob || !it) return;
    const old = media.get(x.media), name = (old ? old.name : "Picture").replace(/\.[^.]+$/, "") + " (transparent).png";
    const kept = quick.some(q => q.id === x.media);
    let m;
    try { m = await addMedia(x.blob, name, "picture", kept ? {quick: Date.now()} : undefined); } catch (e) { m = null; }
    if (!m){ flash("This browser could not keep it (storage full?)."); return; }
    it.media = m.id;
    bgClose();
    if (kept){ try { const rec = await db.get("media", x.media); if (rec){ rec.quick = 0; await db.put("media", rec); } } catch (e) {} await loadQuick(); }
    renderItems(); drawTimeline(); panels(); commit();
    flash(kept ? "Background cleared. Quick add has the clear one now." : "Background cleared. Keep puts it in Quick add.");
  }
  // a picture whose edges are one or two solid colors that clear well: its background is not see-through
  function plainBackground(img){
    if (!img) return false;
    const s = Math.min(1, 160 / Math.max(img.naturalWidth, img.naturalHeight)), w = Math.max(2, Math.round(img.naturalWidth * s)), h = Math.max(2, Math.round(img.naturalHeight * s));
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const g = c.getContext("2d", {willReadFrequently: true}); g.drawImage(img, 0, 0, w, h);
    const r = clearBackground(g.getImageData(0, 0, w, h).data, w, h, 0.12, false), share = r.cleared / (w * h);
    return !!r.bg && share > 0.08 && share < 0.9;
  }
  async function addQuick(files){
    let n = 0;
    for (const f of files){
      if (!/^image\//.test(f.type)) continue;
      try { const im = await shrinkImage(f); if (await addMedia(im.blob, f.name || "Picture", "picture", {quick: Date.now() + n})) n++; }
      catch (e) { flash(`${f.name || "That file"} is not a picture the browser can read.`); }
    }
    if (n){ await loadQuick(); flash(n === 1 ? "Added to Quick add. Click it to put it on the video." : `${n} pictures added to Quick add.`); }
  }

  // ---------------- takes ----------------
  const takesEl = $(".st-takes");
  async function listTakes(){
    let all = [];
    try { all = (await db.all("takes")).sort((a, b) => b.created - a.created); } catch (e) { note("takes", "This browser's storage could not be read: " + (e.message || e), true); return; }
    takesEl.innerHTML = all.length ? all.map(t => `<li data-id="${esc(t.id)}" class="${P.take && P.take.id === t.id ? "on" : ""}">
        ${t.thumb ? `<img class="th" src="${esc(t.thumb)}" alt="">` : `<span class="th"></span>`}
        <span class="nm"><b>${esc(t.name || "Take")}</b><small>${[t.dur ? fmt(t.dur) : "", t.size ? mb(t.size) : "", t.handle ? "saved on disk" : "", new Date(t.created).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})].filter(Boolean).join(" · ")}${t.meta && t.meta.hook ? `<br>${esc(t.meta.hook)}` : ""}</small></span>
        <span class="ac">${take.id === t.id ? `<span class="st-note">On the preview</span>` : `<button type="button" class="st-ib" data-open="${esc(t.id)}">Open</button>`}<button type="button" class="st-ib" data-rm="${esc(t.id)}">Remove</button></span></li>`).join("")
      : `<li><span class="nm"><small>No takes yet. Record one, or open a video file.</small></span></li>`;
    return all;
  }
  // The file behind a take: kept in this browser, handed over by the record window, or the file Record mode saved on
  // disk. Reading that file again needs Chrome's OK, which it can ask for only on a click: until then the take waits
  // for its Open button.
  const passed = opts.files || new Map();            // take id -> its File, from the record window
  async function takeFile(rec){
    if (rec.blob) return rec.blob;
    if (passed.has(rec.id)) return passed.get(rec.id);
    if (rec.handle){
      let ok = (await rec.handle.queryPermission({mode: "read"}).catch(() => "prompt")) === "granted";
      if (!ok && (!navigator.userActivation || navigator.userActivation.isActive)) ok = (await rec.handle.requestPermission({mode: "read"}).catch(() => "denied")) === "granted";
      if (!ok) throw new Error("This take is saved on your disk: press Open next to it and let Chrome read the file.");
      let file;
      try { file = await rec.handle.getFile(); } catch (e) { throw new Error("The take's file is not where Record mode saved it (moved or deleted?)."); }
      if (!file.size) throw new Error(`The take's file on disk (${file.name}) is empty: was something saved over it? Record the clip again.`);
      // a copy kept in this browser: the edit and its export read it, so the file on disk can be moved, renamed or
      // even saved over (an export that picked it in the save dialog emptied it) without losing the take
      // (a real copy, streamed: a File from the disk stored as it is would still be the file on disk)
      // (kept on this record too: the thumbnail saved a moment later writes this same record, and would drop it)
      try { const copy = await new Response(file.stream()).blob(); rec.blob = new Blob([copy], {type: file.type || rec.type || "video/mp4"}); await db.put("takes", rec); return (await db.get("takes", rec.id)).blob || file; }
      catch (e) { delete rec.blob; return file; }  // no room for the copy: the file on disk is read
    }
    throw new Error("This take's video is no longer here.");
  }
  async function showTake(id, fresh){
    pause();
    if (!id){
      if (take.url) URL.revokeObjectURL(take.url);
      take.id = take.url = take.file = null; video.removeAttribute("src"); video.load(); video.hidden = true; mock.hidden = false;
      thumbs = []; stripFor = null;
      return true;
    }
    const rec = await db.get("takes", id);
    if (!rec){ flash("The take for this edit is no longer in this browser."); P.take = null; P.segs = []; return showTake(null); }
    let file;
    try { file = await takeFile(rec); } catch (e) { note("takes", e.message || String(e), true); return false; }
    if (take.url) URL.revokeObjectURL(take.url);
    take.id = id; take.url = URL.createObjectURL(file); take.file = file;
    video.src = take.url; video.hidden = false; mock.hidden = true;
    try { await loadedMeta(video); await fixDuration(video); }
    catch (e){ note("takes", e.message || String(e), true); return false; }
    note("takes", "");
    const dur = video.duration;
    if (fresh || !P.take || P.take.id !== id){
      P.take = {id, name: rec.name || "Take", dur, w: video.videoWidth, h: video.videoHeight, type: file.type, size: file.size, meta: rec.meta || {}, hasAudio: null};
      P.segs = [{id: uid(), in: 0, out: dur}];
    } else P.take.dur = dur;
    video.volume = clamp(P.origVol, 0, 1); video.muted = P.origVol <= 0;
    makeStrip(file, rec);
    return true;
  }
  async function openTake(id, msg){
    const ok = await showTake(id, !(P.take && P.take.id === id));
    if (!ok) return;
    cur = 0; selected = null; normalize();
    renderItems(); zoomFit(); renderAt(); panels(); commit(); listTakes();
    flash(msg ? "Your clip is on the preview." : "The take is on the preview. Press Play, or add pictures, text and a song.");
    note("pics", msg || "");             // a Make a clip take's note (what to do next, what was not found); gone with another take
  }
  // a clip made in Posting (Make a clip) starts a clean edit: its pictures, text and song are its own. The export
  // settings stay, and Undo brings the last edit back.
  function cleanEdit(){
    if (!P.items.length && !P.music) return;
    pause();
    P = Object.assign(newProject(), {fit: P.fit, fps: P.fps, origVol: P.origVol, take: P.take, segs: P.segs});
    selected = null; textCache.clear();
  }
  async function addVideoFile(file){
    if (!file || !/^video\//.test(file.type) && !/\.(mp4|mov|m4v|webm)$/i.test(file.name)){ note("takes", "That is not a video file.", true); return; }
    const id = "take-" + uid();
    try { await db.put("takes", {id, name: file.name.replace(/\.[^.]+$/, ""), created: Date.now(), size: file.size, type: file.type, blob: file, meta: {source: "file"}}); }
    catch (e){ note("takes", "This browser could not keep the file (storage full?): " + (e.message || e), true); return; }
    await openTake(id);
  }
  // a filmstrip of the take for the timeline, and the take's own thumbnail and length for the list
  async function makeStrip(file, rec){
    const id = take.id; stripFor = id; thumbs = [];
    const v = document.createElement("video"); v.muted = true; v.preload = "auto"; v.playsInline = true;
    const url = URL.createObjectURL(file); v.src = url;
    try {
      await loadedMeta(v); await fixDuration(v);
      try { const s = v.captureStream && v.captureStream(); if (P.take && P.take.id === id && s) P.take.hasAudio = s.getAudioTracks().length > 0; } catch (e) {}
      const D = v.duration, n = clamp(Math.ceil(D / 1.2), 6, 90), th = 120, tw = Math.max(20, Math.round(th * v.videoWidth / Math.max(1, v.videoHeight)));
      for (let k = 0; k < n; k++){
        if (stripFor !== id) break;
        const t = Math.min(D - 0.05, (k + 0.5) * D / n);
        v.currentTime = t; await once(v, "seeked", 4000); await settled(v);
        const c = document.createElement("canvas"); c.width = tw; c.height = th; c.getContext("2d").drawImage(v, 0, 0, tw, th);
        thumbs.push({t, c});
        if (k === 0 && rec && !rec.thumb){ rec.thumb = c.toDataURL("image/jpeg", 0.7); rec.dur = D; try { await db.put("takes", rec); listTakes(); } catch (e) {} }
        if (k % 6 === 5) paintStrips();
      }
    } catch (e) { /* the timeline shows plain blocks */ }
    finally { URL.revokeObjectURL(url); v.removeAttribute("src"); v.load(); }
    if (stripFor === id) paintStrips();
  }

  // ---------------- the preview: pictures and text where they are dragged ----------------
  const textCache = new Map();          // item id -> {cv, L, key} at preview size
  function stageScale(){ return stage.clientWidth / W; }
  function renderItems(){
    const keep = new Set();
    for (const it of P.items){
      keep.add(it.id);
      let el = itemsEl.querySelector(`.so[data-id="${it.id}"]`);
      if (!el){
        el = document.createElement("div"); el.className = "so"; el.dataset.id = it.id;
        el.innerHTML = `<i data-h="del" title="Remove">&times;</i><i data-h="rot" title="Turn"></i><i data-h="size" title="Resize"></i>`;
        itemsEl.appendChild(el);
      }
      placeItem(it, el);
    }
    for (const el of [...itemsEl.children]) if (!keep.has(el.dataset.id)) el.remove();
    // the order on screen is the list's order: the last one is in front
    for (const it of P.items){ const el = itemsEl.querySelector(`.so[data-id="${it.id}"]`); if (el) itemsEl.appendChild(el); }
    renderAt();
  }
  function placeItem(it, el){
    el = el || itemsEl.querySelector(`.so[data-id="${it.id}"]`); if (!el) return;
    el.style.left = it.x * 100 + "%"; el.style.top = it.y * 100 + "%"; el.style.setProperty("--r", (it.rot || 0) + "deg");
    if (it.kind === "pic"){
      const m = media.get(it.media);
      let img = el.querySelector("img");
      if (!img){ img = document.createElement("img"); img.alt = ""; img.draggable = false; el.prepend(img); }
      if (m && img.getAttribute("src") !== m.url) img.src = m.url;
      el.style.width = it.w * 100 + "%";
    } else {
      const sc = Math.max(0.2, stageScale()) * (window.devicePixelRatio || 1);
      const key = JSON.stringify([it.text, it.style, it.color, it.bg, it.align, it.size, it.wrap, sc.toFixed(3)]);
      let t = textCache.get(it.id);
      if (!t || t.key !== key){ t = Object.assign(textCanvas(it, sc), {key}); textCache.set(it.id, t); }
      let cv = el.querySelector("canvas");
      if (cv !== t.cv){ if (cv) cv.remove(); el.prepend(t.cv); }
      el.style.width = t.L.w / W * 100 + "%";
    }
  }
  function renderAt(){
    const d = editDur();
    for (const el of itemsEl.children){
      const it = itemById(el.dataset.id); if (!it) continue;
      const on = cur >= it.t0 && cur < it.t1, sel = selected === it.id;
      el.classList.toggle("hide", !on && !sel);
      el.classList.toggle("off", !on && sel);
      el.classList.toggle("sel", sel);
    }
    $(".st-time").textContent = `${fmt(cur, true)} / ${fmt(d, true)}`;
    ph.style.left = (cur * pps + 0) + "px";
    stage.classList.toggle("paused", !playing);
    $('[data-a="play"]').textContent = playing ? "Pause" : "Play";
  }
  new ResizeObserver(() => { if (shown){ renderItems(); } }).observe(stage);

  // drag to move, the corner to resize, the round handle to turn; onto the bin to remove
  const bin = $(".st-bin"), gv = $(".gv"), gh = $(".gh");
  stage.addEventListener("pointerdown", e => {
    if (e.button > 0) return;
    const el = e.target.closest(".so");
    if (!el){
      if (selected && !String(selected).startsWith("seg:") && selected !== "music"){ select(null); return; }
      togglePlay(); return;
    }
    const it = itemById(el.dataset.id); if (!it) return;
    e.preventDefault(); st.focus({preventScroll: true});
    const h = e.target.closest("[data-h]"), mode = h ? h.dataset.h : "move";
    if (mode === "del"){ removeItem(it.id); return; }
    if (selected !== it.id) select(it.id, true);
    pause();
    el.setPointerCapture(e.pointerId);
    const R = stage.getBoundingClientRect(), cx = R.left + it.x * R.width, cy = R.top + it.y * R.height;
    const start = {x: it.x, y: it.y, w: it.w, size: it.size, wrap: it.wrap, rot: it.rot || 0, px: e.clientX, py: e.clientY,
      d: Math.hypot(e.clientX - cx, e.clientY - cy) || 1, a: Math.atan2(e.clientY - cy, e.clientX - cx)};
    let moved = false, overBin = false;
    if (mode === "move") bin.hidden = false;
    const move = ev => {
      moved = true;
      if (mode === "move"){
        let x = start.x + (ev.clientX - start.px) / R.width, y = start.y + (ev.clientY - start.py) / R.height;
        const sx = Math.abs(x - 0.5) < 0.012, sy = Math.abs(y - 0.5) < 0.012;
        if (sx) x = 0.5; if (sy) y = 0.5;
        gv.classList.toggle("on", sx); gh.classList.toggle("on", sy);
        it.x = clamp(x, -0.4, 1.4); it.y = clamp(y, -0.4, 1.4);
        const b = bin.getBoundingClientRect();
        // on the bin itself (a little to the sides and below; not above it, where the video's bottom edge is)
        overBin = ev.clientX > b.left - 10 && ev.clientX < b.right + 10 && ev.clientY > Math.max(b.top, stage.getBoundingClientRect().bottom + 2) && ev.clientY < b.bottom + 10;
        bin.classList.toggle("over", overBin);
      } else if (mode === "size"){
        const k = clamp(Math.hypot(ev.clientX - cx, ev.clientY - cy) / start.d, 0.1, 12);
        if (it.kind === "pic") it.w = clamp(start.w * k, 0.04, 3);
        else { it.size = clamp(start.size * k, 0.012, 0.45); it.wrap = clamp(start.wrap * k, 0.08, 3); }
      } else if (mode === "rot"){
        let r = start.rot + (Math.atan2(ev.clientY - cy, ev.clientX - cx) - start.a) * 180 / Math.PI;
        r = ((r % 360) + 540) % 360 - 180;
        for (const s of [-180, -90, 0, 90, 180]) if (Math.abs(r - s) < 4) r = s;
        it.rot = r;
      }
      placeItem(it, el);
    };
    const up = () => {
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", up);
      bin.hidden = true; bin.classList.remove("over"); gv.classList.remove("on"); gh.classList.remove("on");
      if (overBin){ removeItem(it.id); return; }
      if (moved){ commit(); panels(); }
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  });
  stage.addEventListener("dblclick", e => {
    const el = e.target.closest(".so"); const it = el && itemById(el.dataset.id);
    if (it && it.kind === "text"){ showTab("text"); const ta = $('[data-in="text"]'); ta.focus(); ta.select(); }
  });
  // a trackpad pinch (or Ctrl+scroll) over the preview resizes the selected picture or text
  stage.addEventListener("wheel", e => {
    const it = itemById(selected);
    if (!e.ctrlKey || !it) return;
    e.preventDefault();
    const k = Math.exp(-e.deltaY / 200);
    if (it.kind === "pic") it.w = clamp(it.w * k, 0.04, 3); else { it.size = clamp(it.size * k, 0.012, 0.45); it.wrap = clamp(it.wrap * k, 0.08, 3); }
    placeItem(it); clearTimeout(stage._wt); stage._wt = setTimeout(() => { commit(); panels(); }, 300);
  }, {passive: false});

  function select(id, keepTime){
    selected = id;
    const it = itemById(id);
    if (it && !keepTime && (cur < it.t0 || cur >= it.t1)) seek(it.t0 + 0.01);
    renderAt(); drawTimeline(); panels();
    if (it) showTab(it.kind === "text" ? "text" : "pics", true);
  }
  function removeItem(id){
    const i = P.items.findIndex(x => x.id === id); if (i < 0) return;
    P.items.splice(i, 1); textCache.delete(id);
    if (selected === id) selected = null;
    renderItems(); drawTimeline(); panels(); commit();
    flash("Removed. Cmd+Z brings it back.");
  }

  // ---------------- playback: the video through its parts, the song under it ----------------
  function ensureAudio(){
    if (!ac){ try { ac = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { ac = null; } }
    if (ac && ac.state === "suspended") ac.resume().catch(() => {});
  }
  function stopSong(){
    if (songNode){ try { songNode.stop(); } catch (e) {} songNode.disconnect(); songNode = null; }
    if (songGain){ songGain.disconnect(); songGain = null; }
  }
  function songEnd(m, buf){ return Math.min(editDur(), m.at + (buf.duration - m.offset)); }
  function startSong(at){
    stopSong();
    const m = P.music, mm = m && media.get(m.media), buf = mm && mm.buf;
    if (!buf || !ac) return;
    const end = songEnd(m, buf);
    if (at >= end) return;
    const now = ac.currentTime + 0.04, base = now - at;
    const src = ac.createBufferSource(); src.buffer = buf;
    const g = ac.createGain(); src.connect(g).connect(ac.destination);
    applyCurve(g.gain, gainCurve(m, end), base, at, now);
    src.start(base + Math.max(at, m.at), m.offset + Math.max(0, at - m.at));
    src.stop(base + end);
    songNode = src; songGain = g;
  }
  function play(){
    if (playing) return;
    const d = editDur();
    if (cur >= d - 0.04) cur = 0;
    playing = true; ensureAudio();
    if (P.take && take.url){
      const L = locate(cur); segIdx = L.i;
      if (Math.abs(video.currentTime - L.src) > 0.03) video.currentTime = L.src;
      video.play().catch(() => {});
    } else clock0 = performance.now() - cur * 1000;
    startSong(cur);
    cancelAnimationFrame(raf); raf = requestAnimationFrame(tick);
    renderAt();
  }
  function pause(){
    if (!playing) return;
    playing = false; video.pause(); stopSong(); cancelAnimationFrame(raf);
    renderAt();
  }
  function togglePlay(){ playing ? pause() : play(); }
  function tick(){
    if (!playing) return;
    raf = requestAnimationFrame(tick);
    const d = editDur();
    if (P.take && take.url){
      const s = P.segs[segIdx];
      if (!s){ pause(); return; }
      if (video.seeking) return;
      const vt = video.currentTime;
      if (vt >= s.out - 0.012 || video.ended){
        if (segIdx + 1 < P.segs.length){
          // the next part: the sound waits while the video finds its first frame
          segIdx++; cur = segStart(segIdx);
          if (ac && songNode) ac.suspend().catch(() => {});
          video.currentTime = P.segs[segIdx].in;
          once(video, "seeked", 3000).then(() => { if (playing){ video.play().catch(() => {}); if (ac) ac.resume().catch(() => {}); } });
        } else { cur = d; pause(); renderAt(); return; }
      } else cur = segStart(segIdx) + (vt - s.in);
    } else {
      cur = (performance.now() - clock0) / 1000;
      if (cur >= d){ cur = d; pause(); }
    }
    renderAt(); follow();
  }
  // a still frame for a moment on the timeline (scrubbing): one seek at a time, the newest wins
  let seekBusy = false, seekWant = null;
  function showFrame(src){
    if (!P.take || !take.url) return;
    if (seekBusy){ seekWant = src; return; }
    seekBusy = true; video.currentTime = src;
    once(video, "seeked", 2500).then(() => { seekBusy = false; if (seekWant != null){ const s = seekWant; seekWant = null; showFrame(s); } });
  }
  function seek(e){
    const wasPlaying = playing;
    if (wasPlaying){ playing = false; video.pause(); stopSong(); cancelAnimationFrame(raf); }
    cur = clamp(e, 0, editDur());
    if (P.take) showFrame(locate(cur).src);
    renderAt();
    if (wasPlaying) once(video, "seeked", 1500).then(() => play());
  }
  // the playhead stays in view while it plays
  function follow(){
    const x = cur * pps, l = tlScroll.scrollLeft, w = tlScroll.clientWidth;
    if (x < l || x > l + w - 30) tlScroll.scrollLeft = Math.max(0, x - w * 0.25);
  }

  // ---------------- the timeline ----------------
  const ROW = {ruler: 26, v: 64, i: 34, m: 48};
  function zoomFit(){
    const w = Math.max(200, tlScroll.clientWidth - 24);
    fitPps = w / Math.max(1, editDur());
    pps = fitPps; drawTimeline();
  }
  function setZoom(np, anchorX){
    const lo = Math.min(fitPps, 4), hi = 400;
    np = clamp(np, lo, hi);
    const ax = anchorX != null ? anchorX : tlScroll.clientWidth / 2, t = (tlScroll.scrollLeft + ax) / pps;
    pps = np; drawTimeline();
    tlScroll.scrollLeft = Math.max(0, t * pps - ax);
  }
  function ticksStep(){
    for (const s of [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120]) if (s * pps >= 64) return s;
    return 300;
  }
  function drawTimeline(){
    if (!pps) { fitPps = Math.max(200, tlScroll.clientWidth - 24) / Math.max(1, editDur()); pps = fitPps; }
    const d = editDur(), width = Math.max(tlScroll.clientWidth, Math.ceil(d * pps) + 40);
    inner.style.width = width + "px";
    const step = ticksStep(), minor = step / 5;
    let rh = "";
    const inc = d / minor < 2500 ? minor : step;
    for (let k = 0; k * inc <= d + 1e-6; k++){
      const t = k * inc, maj = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      rh += `<i class="${maj ? "maj" : ""}" style="left:${(t * pps).toFixed(1)}px"></i>`;
      if (maj){ const r = Math.round(t / step) * step; rh += `<b style="left:${(t * pps).toFixed(1)}px${k ? "" : ";transform:none"}">${step < 1 ? fmt(r + 1e-6, true) : fmt(Math.round(r))}</b>`; }
    }
    ruler.innerHTML = rh;
    let th = "", lh = `<div style="height:${ROW.ruler}px"></div>`;
    // the video
    if (P.take){
      th += `<div class="st-trk v">${P.segs.map((s, i) => {
        const x = segStart(i) * pps, w = (s.out - s.in) * pps;
        return `<div class="st-clip${selected === "seg:" + s.id ? " sel" : ""}" data-drag="seg" data-i="${i}" style="left:${x.toFixed(1)}px;width:${Math.max(4, w).toFixed(1)}px"><canvas></canvas><span class="st-hd l" data-drag="segl" data-i="${i}"></span><span class="st-hd r" data-drag="segr" data-i="${i}"></span></div>`;
      }).join("")}</div>`;
    } else th += `<div class="st-trk v"><div class="st-clip none" style="left:0;width:${(d * pps).toFixed(1)}px"><span>No video yet: record a take or open a video file (${fmt(d)} to place things on meanwhile)</span></div></div>`;
    lh += `<div style="height:${ROW.v}px" class="${String(selected).startsWith("seg:") ? "sel" : ""}">Video</div>`;
    // pictures and text, one row each
    for (const it of P.items){
      const m = it.kind === "pic" && media.get(it.media);
      const x = it.t0 * pps, w = Math.max(6, (it.t1 - it.t0) * pps);
      const name = it.kind === "pic" ? (m ? m.name : "Picture") : String(it.text || "").replace(/\s+/g, " ").slice(0, 40);
      th += `<div class="st-trk i"><div class="st-bar ${it.kind}${selected === it.id ? " sel" : ""}" data-drag="item" data-id="${it.id}" style="left:${x.toFixed(1)}px;width:${w.toFixed(1)}px" title="${esc(name)}: ${fmt(it.t0, true)} to ${fmt(it.t1, true)}">
        ${m ? `<img src="${esc(m.url)}" alt="">` : ""}<span class="lbl">${esc(name)}</span><span class="st-hd l" data-drag="iteml" data-id="${it.id}"></span><span class="st-hd r" data-drag="itemr" data-id="${it.id}"></span></div></div>`;
      lh += `<div style="height:${ROW.i}px" class="${selected === it.id ? "sel" : ""}">${it.kind === "pic" ? "Picture" : "Text"}</div>`;
    }
    // the song
    const mm = P.music && media.get(P.music.media);
    if (P.music && mm && mm.buf){
      const end = songEnd(P.music, mm.buf), x = P.music.at * pps, w = Math.max(6, (end - P.music.at) * pps);
      th += `<div class="st-trk m"><div class="st-bar mus${selected === "music" ? " sel" : ""}" data-drag="music" style="left:${x.toFixed(1)}px;width:${w.toFixed(1)}px" title="${esc(mm.name)}"><canvas></canvas><span class="lbl">&#9835; ${esc(mm.name)}</span></div></div>`;
      lh += `<div style="height:${ROW.m}px" class="${selected === "music" ? "sel" : ""}">Song</div>`;
    } else {
      th += `<div class="st-trk m"></div>`;
      lh += `<div style="height:${ROW.m}px;color:#8a9099">Song (none)</div>`;
    }
    tracks.innerHTML = th; labels.innerHTML = lh;
    ph.style.left = cur * pps + "px";
    paintStrips(); paintSongBar();
    const it = itemById(selected), sel = $(".st-sel");
    sel.textContent = it ? `${it.kind === "pic" ? "Picture" : "Text"}: ${fmt(it.t0, true)} to ${fmt(it.t1, true)}` : selected === "music" ? "Song selected" : String(selected).startsWith("seg:") ? "Part of the video selected" : "";
    for (const k of ["from", "to", "whole"]) $(`[data-a="${k}"]`).disabled = !it;
    $('[data-a="del"]').disabled = !selected;
    $('[data-a="split"]').disabled = !P.take;
  }
  function paintStrips(){
    if (!P.take) return;
    const dpr = window.devicePixelRatio || 1;
    tracks.querySelectorAll(".st-clip[data-i]").forEach(el => {
      const s = P.segs[+el.dataset.i]; if (!s) return;
      const cv = el.querySelector("canvas"), w = el.clientWidth, h = el.clientHeight;
      if (!w || !h) return;
      cv.width = Math.ceil(w * dpr); cv.height = Math.ceil(h * dpr); cv.style.width = w + "px";
      const g = cv.getContext("2d"); g.scale(dpr, dpr);
      if (!thumbs.length){ g.fillStyle = "#2b333d"; g.fillRect(0, 0, w, h); return; }
      const tw = h * thumbs[0].c.width / thumbs[0].c.height;
      for (let x = 0; x < w; x += tw){
        const t = s.in + (x + tw / 2) / pps;
        let best = thumbs[0];
        for (const tb of thumbs) if (Math.abs(tb.t - t) < Math.abs(best.t - t)) best = tb;
        g.drawImage(best.c, x, 0, tw, h);
      }
    });
  }
  function paintSongBar(){
    const el = tracks.querySelector(".st-bar.mus canvas"), mm = P.music && media.get(P.music.media);
    if (!el || !mm || !mm.peaks) return;
    const bar = el.parentElement, w = bar.clientWidth, h = bar.clientHeight, dpr = window.devicePixelRatio || 1;
    el.width = Math.ceil(w * dpr); el.height = Math.ceil(h * dpr); el.style.width = w + "px";
    const g = el.getContext("2d"); g.scale(dpr, dpr); g.fillStyle = "#fff";
    const pk = mm.peaks, D = mm.buf.duration;
    for (let x = 0; x < w; x += 2){
      const t = P.music.offset + x / pps, v = pk[Math.min(pk.length - 1, Math.floor(t / D * pk.length))] || 0;
      g.fillRect(x, h / 2 - v * h * 0.42, 1.2, Math.max(1, v * h * 0.84));
    }
  }
  const timeAt = clientX => { const r = inner.getBoundingClientRect(); return clamp((clientX - r.left) / pps, 0, editDur()); };
  const tip = document.createElement("div"); tip.className = "st-tip"; tip.hidden = true; document.body.appendChild(tip);
  function showTip(x, y, text){ tip.hidden = false; tip.style.left = x + "px"; tip.style.top = y + "px"; tip.textContent = text; }
  // press or drag on the ruler or an empty stretch to scrub; drag bars, their ends and the trim handles
  inner.addEventListener("pointerdown", e => {
    if (e.button > 0) return;
    const h = e.target.closest("[data-drag]");
    e.preventDefault(); st.focus({preventScroll: true});
    inner.setPointerCapture(e.pointerId);
    const x0 = e.clientX, t0 = timeAt(e.clientX);
    if (!h || e.target.closest(".st-ph")){
      pause(); seek(t0);
      const move = ev => seek(timeAt(ev.clientX));
      const up = () => { inner.removeEventListener("pointermove", move); inner.removeEventListener("pointerup", up); inner.removeEventListener("pointercancel", up); };
      inner.addEventListener("pointermove", move); inner.addEventListener("pointerup", up); inner.addEventListener("pointercancel", up);
      return;
    }
    pause();
    const kind = h.dataset.drag, i = +h.dataset.i, it = itemById(h.dataset.id), s = P.segs[i];
    const snapPts = () => [0, editDur(), cur, ...P.items.filter(x => x !== it).flatMap(x => [x.t0, x.t1])];
    const snap = t => { for (const p of snapPts()) if (Math.abs(p - t) * pps < 7) return p; return t; };
    const start = s ? {in: s.in, out: s.out} : it ? {t0: it.t0, t1: it.t1} : P.music ? {at: P.music.at} : {};
    let moved = false;
    if (kind === "seg"){ selected = "seg:" + s.id; seek(t0); drawTimeline(); panels(); }
    else if (it && selected !== it.id){ selected = it.id; renderAt(); drawTimeline(); panels(); }
    else if (kind === "music" && selected !== "music"){ selected = "music"; drawTimeline(); panels(); }
    const move = ev => {
      const dt = (ev.clientX - x0) / pps;
      if (Math.abs(ev.clientX - x0) < 3 && !moved) return;
      moved = true;
      if (kind === "segl"){
        s.in = clamp(start.in + dt, 0, start.out - MIN_LEN); showFrame(s.in);
        showTip(ev.clientX, inner.getBoundingClientRect().top, "Starts at " + fmt(s.in, true) + " of the take");
      } else if (kind === "segr"){
        s.out = clamp(start.out + dt, start.in + MIN_LEN, P.take.dur); showFrame(Math.max(s.in, s.out - 0.04));
        showTip(ev.clientX, inner.getBoundingClientRect().top, "Ends at " + fmt(s.out, true) + " of the take");
      } else if (kind === "item" && it){
        const len = start.t1 - start.t0, d = editDur();
        let a = snap(start.t0 + dt); if (Math.abs(snap(start.t1 + dt) - (start.t1 + dt)) > 1e-9) a = snap(start.t1 + dt) - len;
        a = clamp(a, 0, Math.max(0, d - len)); it.t0 = a; it.t1 = a + len; it.whole = false;
      } else if (kind === "iteml" && it){ it.t0 = clamp(snap(start.t0 + dt), 0, it.t1 - MIN_LEN); it.whole = false; }
      else if (kind === "itemr" && it){ it.t1 = clamp(snap(start.t1 + dt), it.t0 + MIN_LEN, editDur()); it.whole = false; }
      else if (kind === "music" && P.music){ P.music.at = clamp(snap(start.at + dt), 0, Math.max(0, editDur() - 0.5)); }
      if (kind === "segl" || kind === "segr") normalize();
      drawTimeline(); renderAt();
    };
    const up = () => {
      inner.removeEventListener("pointermove", move); inner.removeEventListener("pointerup", up); inner.removeEventListener("pointercancel", up);
      tip.hidden = true;
      if (moved){
        if (kind === "segl" || kind === "segr"){ normalize(); seek(kind === "segl" ? segStart(i) : Math.max(0, segStart(i) + s.out - s.in - 0.05)); }
        drawTimeline(); renderAt(); panels(); commit();
      }
    };
    inner.addEventListener("pointermove", move); inner.addEventListener("pointerup", up); inner.addEventListener("pointercancel", up);
  });
  tlScroll.addEventListener("wheel", e => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const r = tlScroll.getBoundingClientRect();
    setZoom(pps * Math.exp(-e.deltaY / 220), e.clientX - r.left);
  }, {passive: false});

  // ---------------- editing commands ----------------
  function split(){
    if (!P.take) return;
    const L = locate(cur), s = P.segs[L.i];
    if (!s || L.src - s.in < MIN_LEN || s.out - L.src < MIN_LEN){ flash("Move the playhead inside a part of the video, away from its ends, to split it."); return; }
    P.segs.splice(L.i, 1, {id: s.id, in: s.in, out: L.src}, {id: uid(), in: L.src, out: s.out});
    selected = "seg:" + P.segs[L.i + 1].id;
    drawTimeline(); panels(); commit();
    flash("Split. Select a part and press Delete to cut it out.");
  }
  function del(){
    if (!selected) return;
    if (selected === "music"){ removeSong(); return; }
    if (String(selected).startsWith("seg:")){
      const i = P.segs.findIndex(s => "seg:" + s.id === selected);
      if (i < 0) return;
      if (P.segs.length < 2){ flash("This is the whole video: drag its ends to trim it, or split it first."); return; }
      P.segs.splice(i, 1); selected = null; normalize();
      drawTimeline(); renderAt(); panels(); commit(); seek(Math.min(cur, editDur())); return;
    }
    removeItem(selected);
  }
  function setTimes(k){
    const it = itemById(selected); if (!it) return;
    const d = editDur();
    if (k === "from"){ it.t0 = clamp(cur, 0, d - MIN_LEN); if (it.t1 < it.t0 + MIN_LEN) it.t1 = Math.min(d, it.t0 + 3); }
    else if (k === "to"){ it.t1 = clamp(cur, MIN_LEN, d); if (it.t0 > it.t1 - MIN_LEN) it.t0 = Math.max(0, it.t1 - 3); }
    else { it.t0 = 0; it.t1 = d; }
    it.whole = k === "whole";
    drawTimeline(); renderAt(); commit();
  }

  // ---------------- panels ----------------
  function showTab(k, quiet){
    tab = k;
    $$(".st-tabs [data-tab]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === k)));
    $$(".st-panel").forEach(p => { p.hidden = p.dataset.p !== k; });
    try { localStorage.setItem("bmg-studio-tab", k); } catch (e) {}
    if (k === "music") drawWave();
    if (k === "report") drawReport();
    if (!quiet && k === "takes") listTakes();
  }
  const setPressed = (set, v) => $$(`[data-set="${set}"] button`).forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === String(v))));
  let textDefaults = {style: "classic", color: "#ffffff", bg: "none", align: "center", size: 0.065};
  function panels(){
    // pictures
    const pics = P.items.filter(x => x.kind === "pic");
    $(".st-pics").innerHTML = pics.length ? pics.map(it => { const m = media.get(it.media);
      return `<li class="${selected === it.id ? "on" : ""}"><img class="th pic" src="${m ? esc(m.url) : ""}" alt=""><span class="nm"><b>${esc(m ? m.name : "Picture")}</b><small>${it.whole ? "Whole video" : `${fmt(it.t0, true)} to ${fmt(it.t1, true)}`}</small></span>
        <span class="ac"><button type="button" class="st-ib" data-bgx="${it.id}" title="Make this picture's background see-through">Transparent</button><button type="button" class="st-ib" data-keep="${esc(it.media)}" aria-pressed="${quick.some(q => q.id === it.media)}" title="Keep this picture in Quick add, for every video">${quick.some(q => q.id === it.media) ? "Kept" : "Keep"}</button><button type="button" class="st-ib" data-pick="${it.id}">Select</button><button type="button" class="st-ib" data-front="${it.id}">To front</button><button type="button" class="st-ib" data-rmi="${it.id}">Remove</button></span></li>`; }).join("")
      : `<li><span class="nm"><small>No pictures on this video yet.</small></span></li>`;
    // text: the selected text, else what the next one will look like
    const t = itemById(selected), tx = t && t.kind === "text" ? t : null, v = tx || textDefaults;
    const ta = $('[data-in="text"]');
    if (document.activeElement !== ta) ta.value = tx ? tx.text : "";
    $('[data-a="addtext"]').textContent = tx ? "Add another text" : "Add text";
    note("text", tx ? "Editing the selected text." : "");
    setPressed("style", v.style); setPressed("color", v.color); setPressed("bg", v.bg); setPressed("align", v.align);
    const sz = $('[data-in="tsize"]'); sz.value = (v.size * 100).toFixed(2); sz.nextElementSibling.textContent = Math.round(v.size * W) + " px";
    // song
    const mm = P.music && media.get(P.music.media);
    $(".st-mus").hidden = !(P.music && mm);
    if (P.music && mm){
      $(".st-mus-name").textContent = mm.name; $(".st-mus-len").textContent = fmt(mm.buf.duration);
      const mv = $('[data-in="mvol"]'); mv.value = Math.round(P.music.vol * 100); mv.nextElementSibling.textContent = mv.value + "%";
      const fi = $('[data-in="fadein"]'); fi.value = P.music.fadeIn; fi.nextElementSibling.textContent = P.music.fadeIn + " s";
      const fo = $('[data-in="fadeout"]'); fo.value = P.music.fadeOut; fo.nextElementSibling.textContent = P.music.fadeOut + " s";
      drawWave();
    }
    const ov = $('[data-in="ovol"]'); ov.value = Math.round(P.origVol * 100); ov.nextElementSibling.textContent = ov.value + "%";
    // export
    setPressed("fps", P.fps); setPressed("fit", P.fit);
    video.classList.toggle("fit", !!(P.take && P.take.w && !videoFill(P.take.w, P.take.h, P.fit)));
    $(".st-disk").hidden = !window.showSaveFilePicker;
  }
  st.addEventListener("click", async e => {
    const b = e.target.closest("button"); if (!b || !st.contains(b)) return;
    const a = b.dataset.a;
    if (b.dataset.tab){ showTab(b.dataset.tab); return; }
    if (a === "play"){ togglePlay(); return; }
    if (a === "back" || a === "fwd"){ seek(cur + (a === "back" ? -1 : 1) * (e.shiftKey ? 5 : 1)); return; }
    if (a === "undo"){ undo(); return; }
    if (a === "redo"){ redo(); return; }
    if (a === "new"){ if (confirm("Start a new edit? This one's pictures, text and song are cleared (the takes stay). Undo brings it back.")) newEdit(); return; }
    if (a === "record"){ if (opts.openRecorder && !opts.openRecorder()) note("takes", "The Record mode window was blocked: allow pop-ups for this page.", true); else note("takes", "Record mode is open in its own window. The take comes here when it ends."); return; }
    if (a === "split"){ split(); return; }
    if (a === "del"){ del(); return; }
    if (a === "from" || a === "to" || a === "whole"){ setTimes(a); return; }
    if (a === "zin"){ setZoom(pps * 1.6); return; }
    if (a === "zout"){ setZoom(pps / 1.6); return; }
    if (a === "zfit"){ zoomFit(); return; }
    if (a === "addtext"){ addText(); return; }
    if (a === "rmmusic"){ removeSong(); return; }
    if (a === "listen"){ listen(b); return; }
    if (a === "link"){ useLink(); return; }
    if (a === "export"){ runExport(); return; }
    if (a === "goexport"){ showTab("export"); $(".st-tools").scrollTop = 0; runExport(true); return; }
    if (a === "cancel"){ if (exportJob) exportJob.cancel = true; return; }
    if (b.dataset.open){ openTake(b.dataset.open); return; }
    if (b.dataset.rm){
      const id = b.dataset.rm;
      if (!confirm("Remove this take from the browser's list? A file you downloaded or saved stays where it is.")) return;
      try { await db.del("takes", id); } catch (err) {}
      if (P.take && P.take.id === id){ P.take = null; P.segs = []; await showTake(null); normalize(); renderItems(); zoomFit(); renderAt(); commit(); }
      listTakes(); return;
    }
    if (b.dataset.pick){ select(b.dataset.pick); return; }
    if (b.dataset.qadd){
      const m = media.get(b.dataset.qadd); if (!m) return;
      const it = picItem(m, m.ratio || 1, null, P.items.filter(x => x.kind === "pic").length);
      P.items.push(it); selected = it.id; renderItems(); drawTimeline(); panels(); commit();
      flash("Drag it where you want it. The corner resizes, the round handle turns it."); return;
    }
    if (b.dataset.qrm){ keepQuick(b.dataset.qrm, false); return; }
    if (b.dataset.bgx){ bgOpen(b.dataset.bgx); return; }
    if (b.dataset.rep){ addReport(b.dataset.rep); return; }
    if (a === "bgok"){ bgUse(); return; }
    if (a === "bgno"){ bgClose(); return; }
    if (b.dataset.keep){ keepQuick(b.dataset.keep, !quick.some(m => m.id === b.dataset.keep)); return; }
    if (b.dataset.rmi){ removeItem(b.dataset.rmi); return; }
    if (b.dataset.front){ const i = P.items.findIndex(x => x.id === b.dataset.front); if (i >= 0){ P.items.push(P.items.splice(i, 1)[0]); renderItems(); drawTimeline(); commit(); } return; }
    const set = b.parentElement && b.parentElement.dataset.set;
    if (set){
      const v = b.dataset.v;
      if (set === "rstyle"){ repStyle = v === "light" ? "light" : "dark"; try { localStorage.setItem("bmg-studio-repstyle", repStyle); } catch (e) {} drawReport(); return; }
      if (set === "fps"){ P.fps = +v; panels(); commit(); return; }
      if (set === "fit"){ P.fit = v; panels(); commit(); return; }
      const it = itemById(selected);
      if (it && it.kind === "text"){ it[set] = v; textDefaults[set] = v; placeItem(it); drawTimeline(); commit(); }
      else textDefaults[set] = v;
      panels();
    }
  });
  st.addEventListener("input", e => {
    const k = e.target.dataset.in; if (!k) return;
    if (k === "bgtol"){ clearTimeout(st._bt); st._bt = setTimeout(bgDraw, 60); return; }
    const it = itemById(selected);
    if (k === "text" && it && it.kind === "text"){ it.text = e.target.value || " "; placeItem(it); drawTimeline(); clearTimeout(st._tt); st._tt = setTimeout(commit, 500); return; }
    if (k === "tsize"){
      const v = +e.target.value / 100; e.target.nextElementSibling.textContent = Math.round(v * W) + " px";
      if (it && it.kind === "text"){ const r = v / it.size; it.size = v; it.wrap = clamp(it.wrap * r, 0.08, 3); placeItem(it); clearTimeout(st._tt); st._tt = setTimeout(commit, 400); }
      else textDefaults.size = v;
      return;
    }
    if (k === "mvol" || k === "fadein" || k === "fadeout"){
      if (!P.music) return;
      const v = +e.target.value;
      if (k === "mvol") P.music.vol = v / 100; else if (k === "fadein") P.music.fadeIn = v; else P.music.fadeOut = v;
      e.target.nextElementSibling.textContent = k === "mvol" ? v + "%" : v + " s";
      if (playing) startSong(cur);
      clearTimeout(st._mt); st._mt = setTimeout(commit, 400); return;
    }
    if (k === "ovol"){
      P.origVol = +e.target.value / 100; e.target.nextElementSibling.textContent = e.target.value + "%";
      video.volume = clamp(P.origVol, 0, 1); video.muted = P.origVol <= 0;
      clearTimeout(st._mt); st._mt = setTimeout(commit, 400);
    }
  });
  st.addEventListener("change", e => {
    const k = e.target.dataset.in; if (!k) return;
    if (k === "video"){ const f = e.target.files[0]; e.target.value = ""; if (f) addVideoFile(f); }
    else if (k === "pics"){ addPictures([...e.target.files]); e.target.value = ""; }
    else if (k === "quick"){ addQuick([...e.target.files]); e.target.value = ""; }
    else if (k === "bginside") bgDraw();
    else if (k === "music"){ const f = e.target.files[0]; e.target.value = ""; if (f) addSong(f); }
  });
  // files dropped on the drop boxes, or straight on the preview
  for (const zone of [...$$(".st-drop"), stage]){
    zone.addEventListener("dragover", e => { if (![...e.dataTransfer.types].includes("Files")) return; e.preventDefault(); zone.classList.add(zone === stage ? "drop" : "over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drop", "over"));
    zone.addEventListener("drop", e => {
      if (!e.dataTransfer.files.length) return;
      e.preventDefault(); zone.classList.remove("drop", "over");
      const files = [...e.dataTransfer.files];
      const R = stage.getBoundingClientRect(), at = zone === stage ? {x: (e.clientX - R.left) / R.width, y: (e.clientY - R.top) / R.height} : null;
      const imgs = files.filter(f => /^image\//.test(f.type)), aud = files.find(f => /^audio\//.test(f.type) || /\.(mp3|m4a|aac|wav|aif|aiff|aifc|ogg|oga|opus|flac)$/i.test(f.name)), vid = files.find(f => /^video\//.test(f.type));
      if (imgs.length) addPictures(imgs, at);
      if (aud) addSong(aud);
      if (vid && zone === stage) addVideoFile(vid);
    });
  }
  document.addEventListener("paste", e => {
    if (!shown || !e.clipboardData) return;
    if (e.target.closest && e.target.closest("textarea,input")) return;
    const imgs = [...e.clipboardData.files].filter(f => /^image\//.test(f.type));
    if (imgs.length){ e.preventDefault(); addPictures(imgs); }
  });

  const picItem = (m, ratio, at, n) => {
    const w = ratio > 1.4 ? 0.42 : ratio > 0.8 ? 0.5 : 0.72;
    return {id: "i-" + uid(), kind: "pic", media: m.id, ratio, x: at ? at.x : 0.5 + (n % 3 - 1) * 0.06, y: at ? at.y : 0.4 + (n % 3) * 0.05, w, rot: 0, t0: 0, t1: editDur(), whole: true};
  };
  async function addPictures(files, at){
    let n = P.items.filter(x => x.kind === "pic").length, added = null;
    for (const f of files){
      if (!/^image\//.test(f.type)) continue;
      try {
        const im = await shrinkImage(f);
        const m = await addMedia(im.blob, f.name || "Picture", "picture");
        if (!m) continue;
        const it = picItem(m, im.h / im.w, at, n);
        P.items.push(it); added = it; n++;
      } catch (e) { flash(`${f.name || "That file"} is not a picture the browser can read.`); }
    }
    if (!added) return;
    selected = added.id; renderItems(); drawTimeline(); panels(); commit(); showTab("pics", true);
    const m = media.get(added.media);
    if (m && plainBackground(m.img)) note("pics", `${m.name} has a solid background (it is not see-through). Transparent, next to it in the list, clears it.`);
    flash("Drag it where you want it. The corner resizes, the round handle turns it.");
  }
  function addText(){
    const ta = $('[data-in="text"]'), sel = itemById(selected);
    const text = (sel && sel.kind === "text" ? "" : ta.value.trim()) || "Your text";
    const it = Object.assign({id: "i-" + uid(), kind: "text", text, wrap: 0.82, x: 0.5, y: 0.3, rot: 0, t0: 0, t1: editDur(), whole: true}, textDefaults);
    P.items.push(it); selected = it.id;
    renderItems(); drawTimeline(); panels(); commit();
    ta.value = it.text; ta.focus();
    if (it.text === "Your text") ta.select(); else ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  async function addSong(file){
    note("link", "Reading the song...");
    let m;
    try { m = await addMedia(file, (file.name || "Song").replace(/\.[^.]+$/, ""), "song"); }
    catch (e){ m = null; }
    if (!m || !m.buf){ note("link", "The browser could not read that song. MP3, M4A, WAV, AIFF, AAC, OGG and FLAC work. Another kind: on the Mac, open it in QuickTime Player and use File, Export As, Audio Only, then drop the new .m4a file here. A protected iTunes file (M4P) cannot be used.", true); return; }
    P.music = {media: m.id, offset: 0, at: 0, vol: 0.8, fadeIn: 0, fadeOut: 1.5};
    selected = "music"; normalize(); note("link", "");
    drawTimeline(); panels(); commit(); showTab("music", true);
    flash("Song added. Drag the light box under Music to choose the part that plays.");
  }
  function removeSong(){
    if (!P.music) return;
    stopSong(); P.music = null; if (selected === "music") selected = null;
    drawTimeline(); panels(); commit(); flash("Song removed. Cmd+Z brings it back.");
  }
  async function useLink(){
    const url = $('[data-in="link"]').value.trim(), k = linkKind(url);
    if (!url) return;
    if (k === "soundcloud" || k === "youtube"){ note("link", LINK_ADVICE[k] + " " + LINK_ADVICE.tiktok); return; }
    if (k !== "file"){ note("link", "Only a link straight to an audio file (ending in .mp3, .m4a, .wav and so on) can be loaded. For a song on a music site, save the file first, then drop it here. " + LINK_ADVICE.tiktok, true); return; }
    note("link", "Fetching the file...");
    try {
      const r = await fetch(url); if (!r.ok) throw new Error("HTTP " + r.status);
      const b = await r.blob(), name = decodeURIComponent(new URL(url).pathname.split("/").pop() || "Song");
      await addSong(new File([b], name, {type: b.type || "audio/mpeg"}));
    } catch (e){ note("link", "That site does not let other pages fetch its files (" + (e.message || e) + "). Download the file, then drop it here.", true); }
  }
  // the whole song, with the part that plays under the video in the light box; drag the box to choose the part
  const wave = $(".st-wave"), waveWin = wave.querySelector(".win");
  function drawWave(){
    const mm = P.music && media.get(P.music.media); if (!mm || !mm.peaks || wave.offsetParent === null) return;
    const cv = wave.querySelector("canvas"), w = wave.clientWidth, h = wave.clientHeight, dpr = window.devicePixelRatio || 1;
    cv.width = Math.ceil(w * dpr); cv.height = Math.ceil(h * dpr);
    const g = cv.getContext("2d"); g.scale(dpr, dpr); g.fillStyle = "#7d8a83";
    for (let x = 0; x < w; x += 2){ const v = mm.peaks[Math.floor(x / w * mm.peaks.length)] || 0; g.fillRect(x, h / 2 - v * h * 0.45, 1.3, Math.max(1, v * h * 0.9)); }
    const D = mm.buf.duration, used = Math.min(D - P.music.offset, editDur() - P.music.at);
    waveWin.style.left = P.music.offset / D * 100 + "%"; waveWin.style.width = Math.max(0.5, used / D * 100) + "%";
  }
  wave.addEventListener("pointerdown", e => {
    const mm = P.music && media.get(P.music.media); if (!mm) return;
    e.preventDefault(); wave.setPointerCapture(e.pointerId); pause();
    const D = mm.buf.duration, w = wave.clientWidth, x0 = e.clientX, o0 = P.music.offset;
    const used = Math.min(D, editDur() - P.music.at);
    // a press outside the box moves the box there first
    const R = wave.getBoundingClientRect(), at = (e.clientX - R.left) / w * D;
    let base = o0;
    if (at < o0 || at > o0 + used) base = clamp(at - used / 2, 0, Math.max(0, D - 1));
    P.music.offset = base; drawWave();
    const move = ev => { P.music.offset = clamp(base + (ev.clientX - x0) / w * D, 0, Math.max(0, D - 1)); drawWave(); paintSongBar(); };
    const up = () => { wave.removeEventListener("pointermove", move); wave.removeEventListener("pointerup", up); wave.removeEventListener("pointercancel", up); drawTimeline(); commit(); listen(null, true); };
    wave.addEventListener("pointermove", move); wave.addEventListener("pointerup", up); wave.addEventListener("pointercancel", up);
  });
  // hear the chosen part of the song on its own for 8 seconds
  let listenNode = null, listenTimer = 0;
  function listen(btn, quiet){
    const lb = $('[data-a="listen"]');
    if (listenNode){ try { listenNode.stop(); } catch (e) {} listenNode = null; clearTimeout(listenTimer); lb.textContent = "Listen"; if (btn) return; }
    const mm = P.music && media.get(P.music.media); if (!mm || !mm.buf) return;
    if (quiet && !btn) { if (!ac) return; }
    ensureAudio(); if (!ac) return;
    pause();
    const src = ac.createBufferSource(), g = ac.createGain(); src.buffer = mm.buf; g.gain.value = P.music.vol;
    src.connect(g).connect(ac.destination); src.start(ac.currentTime + 0.02, P.music.offset, 8);
    listenNode = src; lb.textContent = "Stop";
    listenTimer = setTimeout(() => { listenNode = null; lb.textContent = "Listen"; }, 8100);
  }

  // ---------------- export: the edit played once into a 1080 x 1920 video ----------------
  // With WebCodecs (Chrome), every frame and every stretch of sound is encoded with its exact time in the edit and
  // written into an MP4 here (mp4File): the song sits exactly where it plays in the preview, and no frame is dropped,
  // since the video waits while the encoder is busy. Without WebCodecs, the browser's recorder captures the edit as it
  // plays (MediaRecorder), which can place the sound up to a tenth of a second early.
  // from the click to the end (the save dialog included, before the export itself starts) the page counts as busy
  async function runExport(download){
    preparing = true;
    try { await exportNow(download); } finally { preparing = !!exportJob; }
  }
  async function exportNow(download){
    if (exportJob) return;
    autoDownload = !!download;              // from Export & download: the file downloads by itself when it is made
    const msg = (t, err) => note("export", t, err);
    const d = editDur();
    if (!P.take && !P.items.length){ msg("Nothing to export yet: open a take, or add pictures or text.", true); return; }
    pause();
    const fps = P.fps || 30;
    const mm = P.music && media.get(P.music.media), song = mm && mm.buf ? P.music : null;
    const orig = !!(P.take && P.take.hasAudio !== false && P.origVol > 0);
    const audio = !!song || orig;
    msg("Getting ready...");
    const plan = await wcPlan(audio, fps);
    const type = plan ? "video/mp4" : pickType(audio, fps);
    if (!plan && (!type || !window.MediaRecorder || !HTMLCanvasElement.prototype.captureStream)){ msg("This browser cannot make video files. Use Chrome.", true); return; }
    const ext = /mp4/.test(type) ? "mp4" : "webm";
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    const tk = ((P.take && P.take.meta && P.take.meta.ticker) || "").replace(/[^A-Za-z0-9]/g, "");
    const fname = `BMG-${tk ? tk + "-" : ""}studio-${stamp}.${ext}`;
    let handle = null, writable = null;
    if (window.showSaveFilePicker && $('[data-in="disk"]').checked){
      try {
        handle = await window.showSaveFilePicker({suggestedName: fname, types: [{description: "Video", accept: {[ext === "mp4" ? "video/mp4" : "video/webm"]: ["." + ext]}}]});
        // Saving over a take's own file (Record mode saved it to disk) empties it before a frame is read: the take is
        // gone and the export has nothing to read. Any file a take lives in is refused.
        for (const t of await db.all("takes").catch(() => [])){
          if (!t.handle || !await handle.isSameEntry(t.handle).catch(() => false)) continue;
          // the save dialog may already have emptied it (Replace): put the take back from the browser's copy
          let back = false;
          try { const f = await handle.getFile(); if (!f.size && t.blob && t.blob.size){ const w = await handle.createWritable(); await w.write(t.blob); await w.close(); back = true; } } catch (e) {}
          msg(`That file is a take${P.take && t.id === P.take.id ? " (the one you are exporting)" : ""}${back ? "; the save dialog had emptied it, and it is back as it was" : ""}. Export again and choose another name.`, true);
          return;
        }
        writable = await handle.createWritable();
      } catch (e){ if (e && e.name === "AbortError"){ msg("Export cancelled."); return; } handle = writable = null; }
    }
    const job = exportJob = {cancel: false, away: false, onAway: null};
    const ui = on => { $$('[data-a="export"],[data-a="goexport"]').forEach(b => { b.disabled = on; }); $('[data-a="cancel"]').hidden = !on; $(".st-prog").hidden = !on; };
    const bar = $(".st-prog i"), res = $(".st-result");
    ui(true); bar.style.width = "0%"; res.innerHTML = ""; unsavedResult = false;    // the last result (if any) is gone from the page
    let doing = "Exporting";
    const progress = e => { bar.style.width = (clamp(e / d, 0, 1) * 100).toFixed(1) + "%"; msg(`${doing}: ${fmt(e)} of ${fmt(d)}${job.away ? " (paused while this tab is in the background)" : ""}`); };
    // in the background the browser stops drawing frames: the export waits, and goes on when the tab is back
    const onVis = () => {
      if (exportJob !== job) return;
      const away = document.hidden;
      if (away === job.away) return;
      job.away = away;
      if (job.onAway) job.onAway(away);
      if (away) msg("Paused while this tab is in the background. Come back to it to go on.");
    };
    document.addEventListener("visibilitychange", onVis);
    const ctx = {d, fps, song, mm, orig, audio, job, progress, msg};
    try {
      let out;
      try { out = plan ? await exportWC(plan, ctx) : await exportMR(type, ctx, writable); }
      catch (e){
        // the computer's own encoder stopped answering: once more with the browser's own H.264 encoder
        const soft = e && e.stall && !job.cancel && plan && !plan.video.hardwareAcceleration ? await wcPlan(audio, fps, true) : null;
        if (!soft){ if (e && e.stall) e.message += " Export again."; throw e; }
        doing = "Exporting again with the browser's own encoder (the computer's stopped answering)";
        out = await exportWC(soft, ctx);
      }
      if (job.cancel || out.cancelled){
        if (writable){ try { await writable.abort(); } catch (x) {} }
        msg("Export cancelled.");
        return;
      }
      bar.style.width = "100%";
      if (out.onDisk){ const f = await handle.getFile(); showResult(f, f.name, out.kind, true, out.warn); }
      else if (writable){
        msg("Saving the video...");
        await writable.write(out.blob); await writable.close();
        const f = await handle.getFile(); showResult(f, f.name, out.kind, true, out.warn);
      } else showResult(out.blob, fname, out.kind, false, out.warn);
    } catch (e){
      if (writable){ try { await writable.abort(); } catch (x) {} }
      msg("The export stopped: " + ((e && e.message) || e), true);
    } finally {
      document.removeEventListener("visibilitychange", onVis);
      exportJob = null; ui(false);
    }
  }
  // the encoders this browser has, best first: H.264 and AAC (an MP4 that QuickTime, TikTok and Instagram all take),
  // else VP9 and Opus in MP4
  // soft: the browser's own H.264 encoder, not the computer's (a second try when the computer's stopped answering)
  async function wcPlan(audio, fps, soft){
    if (!window.VideoEncoder || !window.VideoFrame || (audio && (!window.AudioEncoder || !window.AudioData || !window.OfflineAudioContext))) return null;
    let vids = fps > 30 ? ["avc1.64002a", "avc1.640028", "avc1.4d002a", "vp09.00.41.08"] : ["avc1.640028", "avc1.4d0028", "avc1.42e028", "vp09.00.40.08"];
    if (soft) vids = vids.filter(x => x.startsWith("avc1"));
    let video = null;
    for (const codec of vids){
      // "quality": an export is not live, every frame is kept ("realtime" lets an encoder drop frames to keep up)
      const c = {codec, width: W, height: H, bitrate: fps > 30 ? 20e6 : 14e6, framerate: fps, latencyMode: "quality"};
      if (soft) c.hardwareAcceleration = "prefer-software";
      if (codec.startsWith("avc1")) c.avc = {format: "avc"};
      try { if ((await VideoEncoder.isConfigSupported(c)).supported){ video = c; break; } } catch (e) { /* the next one */ }
    }
    if (!video) return null;
    let sound = null;
    if (audio){
      for (const codec of video.codec.startsWith("avc1") ? ["mp4a.40.2", "opus"] : ["opus", "mp4a.40.2"]){
        const c = {codec, sampleRate: 48000, numberOfChannels: 2, bitrate: 192000};
        try { if ((await AudioEncoder.isConfigSupported(c)).supported){ sound = c; break; } } catch (e) { /* the next one */ }
      }
      if (!sound) return null;
    }
    return {video, audio: sound};
  }
  const kindOf = plan => `MP4 (${plan.video.codec.startsWith("avc1") ? "H.264" : "VP9"}${plan.audio ? " + " + (plan.audio.codec.startsWith("mp4a") ? "AAC" : "Opus") : ""})`;
  // the canvas the export is drawn on: the video where it sits in the frame, then the pictures and text in order
  function exportCanvas(){
    const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
    const g = cv.getContext("2d", {alpha: false});
    const texts = new Map(P.items.filter(x => x.kind === "text").map(x => [x.id, textCanvas(x, 1)]));
    let R = null;
    return {cv, fit(v){ R = videoRect(v.videoWidth || W, v.videoHeight || H, P.fit); }, draw(frame, e){
      g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
      if (frame && R) g.drawImage(frame, R.x, R.y, R.w, R.h);
      for (const it of P.items){
        if (!(e >= it.t0 && e < it.t1)) continue;
        g.save(); g.translate(it.x * W, it.y * H); g.rotate((it.rot || 0) * Math.PI / 180);
        if (it.kind === "pic"){ const m = media.get(it.media); if (m && m.img){ const w = it.w * W, h = w * it.ratio; g.drawImage(m.img, -w / 2, -h / 2, w, h); } }
        else { const t = texts.get(it.id); if (t) g.drawImage(t.cv, -t.L.w / 2, -t.L.h / 2, t.L.w, t.L.h); }
        g.restore();
      }
    }};
  }
  async function exportVideo(withSound){
    const open = async url => { const v = document.createElement("video"); v.preload = "auto"; v.playsInline = true; v.muted = !withSound; v.src = url; await loadedMeta(v); await fixDuration(v); return v; };
    try { return await open(take.url); }
    catch (e){
      // a take saved on disk is read through its file: read it again (it may have been replaced since it opened)
      const rec = P.take && await db.get("takes", P.take.id).catch(() => null);
      let file = null; try { file = rec && rec.handle ? await rec.handle.getFile() : null; } catch (x) {}
      if (file && !file.size) throw new Error(`The take's file on disk (${file.name}) is empty now: was something saved over it? Record the clip again.`);
      if (!file) throw e;
      take.file = file; take.url = URL.createObjectURL(file);       // (the preview keeps the address it plays from)
      return open(take.url);
    }
  }
  // the next frame the video shows (its own time), or after a second without one, where it is
  const frameOf = v => new Promise(r => {
    const t = setTimeout(() => r(v.currentTime), 1000);
    const got = mt => { clearTimeout(t); r(mt); };
    if (v.requestVideoFrameCallback) v.requestVideoFrameCallback((now, md) => got(md.mediaTime)); else requestAnimationFrame(() => got(v.currentTime));
  });
  // the whole sound of the edit, mixed ahead: the song from its chosen part with its fades, and the take's own sound
  // through each kept part
  async function mixSound(c){
    const sr = 48000, oc = new OfflineAudioContext(2, Math.max(1, Math.ceil(c.d * sr)), sr);
    if (c.song){
      const end = Math.min(c.d, c.song.at + (c.mm.buf.duration - c.song.offset));
      const src = oc.createBufferSource(), gn = oc.createGain(); src.buffer = c.mm.buf;
      applyCurve(gn.gain, gainCurve(c.song, end), 0, 0, 0);
      src.connect(gn).connect(oc.destination); src.start(c.song.at, c.song.offset); src.stop(end);
    }
    if (c.orig && take.file){
      const buf = await decodeAudio(take.file).catch(() => null);
      if (buf){
        let e0 = 0;
        for (const s of P.segs){
          const src = oc.createBufferSource(), gn = oc.createGain(); src.buffer = buf; gn.gain.value = P.origVol;
          src.connect(gn).connect(oc.destination);
          if (s.in < buf.duration) src.start(e0, s.in, Math.max(0, Math.min(s.out, buf.duration) - s.in));
          e0 += s.out - s.in;
        }
      }
    }
    return oc.startRendering();
  }
  const bytesOf = d => (d instanceof ArrayBuffer ? new Uint8Array(d) : new Uint8Array(d.buffer, d.byteOffset, d.byteLength)).slice();
  async function encodeSound(buf, config){
    const chunks = []; let description = null, err = null;
    const enc = new AudioEncoder({output: (ch, meta) => {
      const data = new Uint8Array(ch.byteLength); ch.copyTo(data);
      chunks.push({data, pts: ch.timestamp, dur: ch.duration || 0, key: true});
      if (meta && meta.decoderConfig && meta.decoderConfig.description) description = bytesOf(meta.decoderConfig.description);
    }, error: e => { err = e; }});
    enc.configure(config);
    const n = buf.length, step = 4800, L = buf.getChannelData(0), Rt = buf.numberOfChannels > 1 ? buf.getChannelData(1) : L;
    for (let i = 0; i < n && !err; i += step){
      const m = Math.min(step, n - i), data = new Float32Array(m * 2);
      data.set(L.subarray(i, i + m), 0); data.set(Rt.subarray(i, i + m), m);
      const ad = new AudioData({format: "f32-planar", sampleRate: 48000, numberOfFrames: m, numberOfChannels: 2, timestamp: Math.round(i * 1e6 / 48000), data});
      enc.encode(ad); ad.close();
      if (enc.encodeQueueSize > 16) await new Promise(r => { const t = setTimeout(r, 100); enc.addEventListener("dequeue", () => { clearTimeout(t); r(); }, {once: true}); });
    }
    await enc.flush(); enc.close();
    if (err) throw err;
    return {chunks, description};
  }
  // How many samples this encoder puts ahead of the sound (AAC's priming): a click 0.1 s in, encoded and decoded again,
  // comes out that much later. The file's edit list skips them, so the sound starts with the picture.
  async function soundDelay(config, description){
    if (!window.AudioDecoder) return 0;
    try {
      const sr = 48000, n = sr / 2, at = 4800, data = new Float32Array(n * 2);
      data[at] = data[n + at] = 0.9;
      const out = [];
      const enc = new AudioEncoder({output: ch => { const b = new Uint8Array(ch.byteLength); ch.copyTo(b); out.push(new EncodedAudioChunk({type: "key", timestamp: ch.timestamp, duration: ch.duration || 0, data: b})); }, error: () => {}});
      enc.configure(config);
      const ad = new AudioData({format: "f32-planar", sampleRate: sr, numberOfFrames: n, numberOfChannels: 2, timestamp: 0, data});
      enc.encode(ad); ad.close(); await enc.flush(); enc.close();
      const pcm = [];
      const dec = new AudioDecoder({output: a => { const f = new Float32Array(a.numberOfFrames); a.copyTo(f, {planeIndex: 0, format: "f32-planar"}); pcm.push(f); a.close(); }, error: () => {}});
      dec.configure(Object.assign({codec: config.codec, sampleRate: sr, numberOfChannels: 2}, description ? {description} : {}));
      for (const ch of out) dec.decode(ch);
      await dec.flush(); dec.close();
      let best = 0, pos = -1, k = 0;
      for (const f of pcm){ for (let i = 0; i < f.length; i++){ const a = Math.abs(f[i]); if (a > best){ best = a; pos = k + i; } } k += f.length; }
      return pos >= 0 && best > 0.15 ? Math.max(0, pos - at) : 0;
    } catch (e) { return 0; }
  }
  const STALL_MS = 8000;                   // an encoder that takes in and gives out nothing this long has stopped answering
  async function exportWC(plan, c){
    const {d, fps, job} = c;
    let sound = null;
    if (plan.audio){
      c.msg("Mixing the sound...");
      const mix = await mixSound(c);
      const a = await encodeSound(mix, plan.audio);
      const delay = await soundDelay(plan.audio, a.description);
      const opus = plan.audio.codec === "opus";
      const head = opus && a.description && a.description.length >= 12 && String.fromCharCode(...a.description.slice(0, 8)) === "OpusHead" ? a.description : null;
      sound = {kind: "audio", codec: plan.audio.codec, sampleRate: 48000, channels: 2, bitrate: plan.audio.bitrate, description: a.description, samples: a.chunks,
        delay: opus ? 0 : delay, preSkip: opus ? (head ? head[10] | head[11] << 8 : delay) : 0, length: d};
    }
    const ec = exportCanvas();
    // beat: the last time the encoder took a frame in or gave one out. Nothing for STALL_MS: it has stopped answering.
    const frames = [], short = []; let vdesc = null, verr = null, beat = performance.now();
    const venc = new VideoEncoder({output: (ch, meta) => {
      beat = performance.now();
      const data = new Uint8Array(ch.byteLength); ch.copyTo(data);
      frames.push({data, pts: ch.timestamp, dur: ch.duration || Math.round(1e6 / fps), key: ch.type === "key"});
      if (meta && meta.decoderConfig && meta.decoderConfig.description) vdesc = bytesOf(meta.decoderConfig.description);
    }, error: e => { verr = e; }});
    venc.configure(plan.video);
    venc.addEventListener("dequeue", () => { beat = performance.now(); });
    const stopped = () => Object.assign(new Error(`The video encoder stopped answering at ${fmt(frames.length / fps)} of ${fmt(d)}.`), {stall: true});
    let k = 0;
    const total = Math.max(1, Math.round(d * fps));
    // the export's frames before edit time `limit`, each drawn at its own time from the source frame given
    // A long run of frames (a part's last frames) goes to the encoder a few at a time and the page stays live: given
    // at once, 2,900 frames (2:01 cut at 0:24) held about 8 MB each in the encoder's queue and froze the page.
    const emit = async (src, limit) => {
      while (k < total && k / fps < limit - 1e-9 && !verr && !job.cancel){
        const t = k / fps;
        ec.draw(src, t);
        const vf = new VideoFrame(ec.cv, {timestamp: Math.round(t * 1e6), duration: Math.round(1e6 / fps)});
        if (!venc.encodeQueueSize) beat = performance.now();    // an idle encoder is not late: its clock starts now
        venc.encode(vf, {keyFrame: k % (fps * 2) === 0}); vf.close(); k++;
        if (venc.encodeQueueSize > 3) await drain();
        if (k % 30 === 0){ c.progress(Math.min(d, k / fps)); await new Promise(r => setTimeout(r, 0)); }
      }
      c.progress(Math.min(d, k / fps));
    };
    const drain = async () => {
      while (venc.encodeQueueSize > 1 && !verr && !job.cancel){
        await new Promise(r => { const t = setTimeout(r, 200); venc.addEventListener("dequeue", () => { clearTimeout(t); r(); }, {once: true}); });
        // stopped answering: as an encoder error, the video is let go and the export stops (to try again)
        if (!job.away && performance.now() - beat > STALL_MS){ verr = stopped(); try { venc.close(); } catch (e) {} }
      }
    };
    if (P.take){
      c.msg("Exporting...");
      const v = await exportVideo(false);                // the sound is mixed separately: the video plays muted
      ec.fit(v);
      // Each frame the video shows is copied in its frame callback (a VideoFrame), so a busy page never draws a later
      // frame in its place. When the copies pile up, the video slows to a crawl for the encoder; a frame the page
      // missed, one the video dropped, or a copy that came out a frame late is played again: the video steps back to
      // just after the last frame it copied and goes on slower (back up to speed once the page keeps up).
      const q = [];
      let wake = null, partDone = false, last = null;
      const notify = () => { if (wake){ const w = wake; wake = null; w(); } };
      const next = () => new Promise(r => { if (q.length || partDone) return r(); wake = r; setTimeout(() => { if (wake === r){ wake = null; r(); } }, 2000); });
      const dropped = () => { try { return v.getVideoPlaybackQuality().droppedVideoFrames; } catch (e) { return 0; } };
      let e0 = 0, pace = 1;
      for (let si = 0; si < P.segs.length && !job.cancel && !verr; si++){
        const s = P.segs[si], end = e0 + (s.out - s.in);
        // the part's first frame: the seeked frame, taken only when its own time says it is the one sought
        v.currentTime = s.in; await once(v, "seeked", 5000); await settled(v);
        try { const f = new VideoFrame(v); if (Math.abs(f.timestamp / 1e6 - s.in) < 0.05) last = f; else f.close(); } catch (e) { /* the first frame that plays */ }
        partDone = false;
        let quiet = 0, shownBefore = null, droppedBefore = dropped(), steady = 0, lastMt = last ? s.in : -1, retries = 0, held = false, restarts = 0;
        // play on from just after the last frame copied (after a pause the video may have skipped ahead)
        let onward = null;
        const playOn = () => onward || (onward = (async () => {
          if (partDone || job.away || job.cancel) return;
          held = false; shownBefore = null; droppedBefore = dropped();
          if (lastMt >= s.in){ v.currentTime = Math.max(s.in, lastMt + 0.001); await once(v, "seeked", 5000); }
          if (!partDone && !job.away && !job.cancel){ v.playbackRate = pace; await v.play().catch(() => {}); }
        })().finally(() => { onward = null; }));
        job.onAway = away => { if (away){ v.pause(); held = true; } else if (held) playOn(); };
        const onFrame = (now, md) => {
          if (partDone || job.cancel) return;
          if (md.mediaTime >= s.out - 0.5 / fps || v.ended){ partDone = true; v.pause(); notify(); return; }
          // The copy holds the frame the video shows now: on a page that stalled, a later one than this callback's (its
          // own time says which). A browser whose copies keep another clock: the callback's time.
          let vf = null;
          if (md.mediaTime > lastMt + 1e-4){ try { vf = new VideoFrame(v); } catch (e) { vf = null; } }
          const own = vf ? vf.timestamp / 1e6 : md.mediaTime;
          const copied = Math.abs(own - md.mediaTime) < 1 ? own : md.mediaTime;
          const d0 = dropped();
          const missed = md.mediaTime > lastMt + 1e-4 && (Math.abs(copied - md.mediaTime) > 1e-3 || d0 > droppedBefore || (shownBefore != null && md.presentedFrames - shownBefore > 1));
          shownBefore = md.presentedFrames; droppedBefore = d0;
          if (missed){
            pace = Math.max(0.25, pace * 0.7); steady = 0;
            if (retries++ < 60){
              if (vf) vf.close();
              shownBefore = null; v.playbackRate = pace;
              v.currentTime = Math.max(s.in, lastMt + 0.001);
              v.requestVideoFrameCallback(onFrame);
              return;
            }
          } else if (++steady > 45 && pace < 1){ pace = Math.min(1, pace * 1.25); steady = 0; }
          if (vf && copied > lastMt + 1e-4){ q.push({vf, mt: copied}); lastMt = copied; }
          else if (vf) vf.close();
          // the encoder is behind: the video crawls (it never jumps ahead), and stops if the copies keep piling up
          v.playbackRate = q.length > 2 ? 0.0625 : pace;
          if (q.length > 8){ v.pause(); held = true; }
          notify();
          v.requestVideoFrameCallback(onFrame);
        };
        v.requestVideoFrameCallback(onFrame);
        while (job.away && !job.cancel) await new Promise(r => setTimeout(r, 200));
        v.playbackRate = pace;
        await v.play().catch(() => {});
        for (;;){
          await next();
          if (job.cancel || verr) break;
          const it = q.shift();
          if (!it){
            if (partDone) break;
            // no frame for a while: the video ended or stopped short of the part's end. More than a second short and
            // not ended, it is played again from the last frame copied (up to 3 times); after that, the part's last
            // frames are that frame, and the export says so
            if (v.ended || (!v.paused && ++quiet > 5)){
              if (!v.ended && lastMt < s.out - 1 && restarts++ < 3){ quiet = 0; held = true; await playOn(); continue; }
              if (lastMt < s.out - 1) short.push(e0 + Math.max(0, lastMt - s.in));
              partDone = true; break;
            }
            if (held && q.length <= 1) await playOn();
            continue;
          }
          quiet = 0;
          // As a player shows it: each frame of the export shows the source frame that started last before its time.
          // A new source frame starts at its own time (within 2 ms: a WebM's times are whole milliseconds), so the
          // frames before it are drawn from the one before it (the part's first frames, from it, if there is none).
          await emit(last || it.vf, Math.min(e0 + (it.mt - s.in) - 0.002, end));
          if (last) last.close();
          last = it.vf;
          if (venc.encodeQueueSize > 3) await drain();
          if (!v.paused && q.length <= 1) v.playbackRate = pace;
          if (held && q.length <= 1) await playOn();
        }
        v.pause();
        if (!job.cancel && !verr && last) await emit(last, end);        // the part's last frames, up to its end
        for (const it of q.splice(0)) it.vf.close();
        if (last){ last.close(); last = null; }
        e0 = end;
      }
      job.onAway = null; v.removeAttribute("src"); v.load();
    } else {
      // no video: the pictures and text over black
      while (k < total && !job.cancel && !verr){ await emit(null, (k + 0.5) / fps); if (venc.encodeQueueSize > 3) await drain(); if (k % 10 === 0) await nextFrame(); }
    }
    if (job.cancel){ try { venc.close(); } catch (e) {} return {cancelled: true}; }
    if (verr) throw verr;
    c.msg("Finishing the file...");
    // The encoder gives its last frames. A Mac's can stop answering here, its flush never ending (Brock's 2:01 export
    // sat on this message): after STALL_MS with nothing from it, the file is made from the frames it gave when they
    // reach the end (the last one held to the end); else it is exported again with the browser's own encoder.
    beat = performance.now();
    let flushed = false;
    venc.flush().then(() => { flushed = true; }, () => { flushed = true; });
    while (!flushed && !verr && !job.cancel && performance.now() - beat < STALL_MS) await new Promise(r => setTimeout(r, 100));
    try { venc.close(); } catch (e) {}
    if (job.cancel) return {cancelled: true};
    if (verr) throw verr;
    if (!flushed){
      const endUs = Math.round(d * 1e6), lastPts = frames.reduce((a, f) => Math.max(a, f.pts), -Infinity);
      if (!frames.length || endUs - (lastPts + 1e6 / fps) > 5e5) throw stopped();
      const tail = frames[frames.length - 1];            // the file's last sample: its length ends the video
      tail.dur = Math.max(tail.dur, endUs - lastPts);
    }
    if (plan.video.codec.startsWith("avc1") && !vdesc) throw new Error("The video encoder gave no H.264 header.");
    const blob = mp4File([{kind: "video", codec: plan.video.codec, width: W, height: H, description: vdesc, samples: frames}, sound].filter(Boolean));
    const warn = short.length ? `The take stopped playing at ${short.map(x => fmt(x)).join(", ")}: from there to its end the video holds that frame. Play the take in Takes; if it stops there too, record it again.` : "";
    return {blob, kind: kindOf(plan), warn};
  }
  // Without WebCodecs: the browser's recorder captures the canvas and the sound as the edit plays.
  async function exportMR(type, c, writable){
    const {d, fps, job} = c;
    const ec = exportCanvas();
    let v = null, acx = null, rec = null, stream = null;
    try {
      if (P.take){ v = await exportVideo(c.orig); ec.fit(v); }
      // the sound runs from the start (a silent source), so its track does not start late in the file; each time the
      // recording starts or goes on, the song is placed on the sound clock at that frame's moment
      acx = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 48000});
      if (acx.state === "suspended") await acx.resume().catch(() => {});
      const dest = acx.createMediaStreamDestination();
      const quiet = acx.createConstantSource(); quiet.offset.value = 0; quiet.connect(dest); quiet.start();
      if (c.orig && v){ const s = acx.createMediaElementSource(v), gn = acx.createGain(); gn.gain.value = P.origVol; s.connect(gn).connect(dest); }
      const songEnd = c.song ? Math.min(d, c.song.at + (c.mm.buf.duration - c.song.offset)) : 0;
      let songSrc = null, songGn = null;
      const songFrom = e => {
        if (songSrc){ try { songSrc.stop(); } catch (x) {} songSrc.disconnect(); songSrc = null; }
        if (songGn){ songGn.disconnect(); songGn = null; }
        if (!c.song || !(e < songEnd)) return;
        const now = acx.currentTime, base = now - e;
        songSrc = acx.createBufferSource(); songSrc.buffer = c.mm.buf; songGn = acx.createGain();
        applyCurve(songGn.gain, gainCurve(c.song, songEnd), base, e, now);
        songSrc.connect(songGn).connect(dest);
        songSrc.start(base + Math.max(e, c.song.at), c.song.offset + Math.max(0, e - c.song.at)); songSrc.stop(base + songEnd);
      };
      stream = ec.cv.captureStream(fps);
      if (c.audio) stream.addTrack(dest.stream.getAudioTracks()[0]);
      rec = new MediaRecorder(stream, {mimeType: type, videoBitsPerSecond: fps > 30 ? 24e6 : 16e6, audioBitsPerSecond: 192000});
      const chunks = []; let disk = Promise.resolve(), diskErr = null;
      rec.ondataavailable = e => {
        if (!e.data || !e.data.size) return;
        if (writable){ const part = e.data; disk = disk.then(() => writable.write(part)).catch(err => { diskErr = diskErr || err; }); }
        else chunks.push(e.data);
      };
      const stopped = new Promise(r => { rec.onstop = r; });
      let started = false;
      const record = e => { if (!started){ rec.start(500); started = true; } else if (rec.state === "paused") rec.resume(); songFrom(e); };
      const hold = () => { if (v) v.pause(); if (rec.state === "recording") rec.pause(); songFrom(Infinity); };
      const go = async eAt => {
        await v.play();
        const mt = await frameOf(v);
        if (job.cancel || job.away) return;
        record(eAt(mt)); ec.draw(v, eAt(mt));
      };
      job.onAway = away => { if (away) hold(); else if (v) go(job.eAt); else record(job.eNow || 0); };
      if (P.take){
        let e0 = 0;
        const half = 0.5 / fps;
        for (let k = 0; k < P.segs.length && !job.cancel; k++){
          const s = P.segs[k], base0 = e0, eAt = mt => base0 + (mt - s.in);
          job.eAt = eAt;
          v.currentTime = s.in; await once(v, "seeked", 5000); await settled(v);
          ec.draw(v, e0);
          while (job.away && !job.cancel) await new Promise(r => setTimeout(r, 200));
          await go(eAt);
          for (;;){
            const mt = await frameOf(v);
            if (job.cancel) break;
            if (mt >= s.out - half || v.ended) break;
            if (job.away) continue;
            ec.draw(v, eAt(mt)); c.progress(eAt(mt));
          }
          hold();
          e0 += s.out - s.in;
        }
      } else {
        ec.draw(null, 0); record(0);
        let e = 0, last = performance.now();
        while (e < d && !job.cancel){
          await new Promise(r => setTimeout(r, 1000 / fps));
          const now = performance.now();
          if (!job.away) e += (now - last) / 1000;
          last = now; job.eNow = e; ec.draw(null, e); c.progress(e);
        }
        hold();
      }
      job.onAway = null;
      if (job.cancel) return {cancelled: true};
      rec.stop(); await stopped;
      const kind = describeType(rec.mimeType || type), outType = (rec.mimeType || type).split(";")[0];
      if (writable){
        await disk;
        try { await writable.close(); } catch (e){ diskErr = diskErr || e; }
        if (diskErr) throw new Error("The video could not be written: " + (diskErr.message || diskErr));
        return {onDisk: true, kind};
      }
      let blob = new Blob(chunks, {type: outType});
      if (/webm/.test(outType)) blob = await fixWebmDuration(blob, d * 1000);
      return {blob, kind};
    } finally {
      job.onAway = null;
      try { if (rec && rec.state !== "inactive") rec.stop(); } catch (e) {}
      if (stream) stream.getTracks().forEach(t => t.stop());
      if (acx) acx.close().catch(() => {});
      if (v){ v.pause(); v.removeAttribute("src"); v.load(); }
    }
  }
  let resultUrl = null, unsavedResult = false, holdUntil = 0, preparing = false;
  let autoDownload = false;
  function showResult(file, name, kind, onDisk, warn){
    if (resultUrl) URL.revokeObjectURL(resultUrl);
    resultUrl = URL.createObjectURL(file);
    const res = $(".st-result");
    res.innerHTML = `<video controls playsinline src="${resultUrl}"></video>
      <div class="st-row">${onDisk ? `<span class="st-note">Saved to ${esc(name)}</span>` : `<a class="st-btn" href="${resultUrl}" download="${esc(name)}">Download the video</a>`}<span class="st-note">${esc(kind)} · ${mb(file.size)}<span class="st-len"></span></span></div>
      <p class="st-note">Upload it in TikTok Studio or Instagram. Drafts of a caption, hashtags and the holdings line: <a href="./#posts" target="_blank" rel="noopener">Today's posts</a>.</p>`;
    note("export", (onDisk ? "Done: the video is saved." : autoDownload ? "Done: the video is downloading (and its link is below)." : "Done: download the video below.") + (warn ? " " + warn : ""), !!warn);
    // a video only in this page until it is downloaded: the page does not load a new version over it, nor for 30 s
    // after a download starts (a large file is still being written)
    unsavedResult = !onDisk;
    const dl = res.querySelector("a[download]"); if (dl) dl.addEventListener("click", () => { unsavedResult = false; holdUntil = Date.now() + 30000; });
    if (autoDownload && dl) dl.click();
    autoDownload = false;
    res.scrollIntoView({block: "nearest", behavior: "smooth"});
    const pv = res.querySelector("video");
    pv.addEventListener("loadedmetadata", async () => { await fixDuration(pv); const l = res.querySelector(".st-len"); if (l && Number.isFinite(pv.duration)) l.textContent = " · " + fmt(pv.duration, true); }, {once: true});
  }

  // ---------------- keys (while the Studio is on screen) ----------------
  document.addEventListener("keydown", e => {
    if (!shown || !st.isConnected) return;
    const typing = e.target.closest && e.target.closest("input,textarea,select,[contenteditable]");
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === "z" || e.key === "Z")){ if (typing) return; e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
    if (mod && (e.key === "y" || e.key === "Y")){ if (typing) return; e.preventDefault(); redo(); return; }
    if (typing || mod || e.altKey) return;
    if (e.key === " "){ e.preventDefault(); togglePlay(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight"){ e.preventDefault(); seek(cur + (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 5 : 1)); }
    else if (e.key === "s" || e.key === "S"){ split(); }
    else if (e.key === "Delete" || e.key === "Backspace"){ if (selected){ e.preventDefault(); del(); } }
    else if (e.key === "Escape"){ if (selected){ selected = null; renderAt(); drawTimeline(); panels(); } }
    else if (e.key === "Home"){ e.preventDefault(); seek(0); }
    else if (e.key === "End"){ e.preventDefault(); seek(editDur()); }
  });
  addEventListener("resize", () => { if (shown){ const fit = Math.abs(pps - fitPps) < 1e-6; if (fit) zoomFit(); else drawTimeline(); drawWave(); } });

  // ---------------- start ----------------
  async function newEdit(){
    pause();
    const keepTake = P.take ? {take: P.take, segs: [{id: uid(), in: 0, out: P.take.dur}]} : {};
    P = Object.assign(newProject(), keepTake);
    selected = null; cur = 0; textCache.clear();
    renderItems(); zoomFit(); renderAt(); panels(); commit();
    flash("A new edit, on the same take.");
  }
  async function start(){
    let saved = null;
    try { saved = await db.get("projects", "current"); } catch (e) {}
    if (saved && saved.p && saved.p.v === 1) P = Object.assign(newProject(), saved.p);
    const ids = [...P.items.filter(x => x.kind === "pic").map(x => x.media), ...(P.music ? [P.music.media] : [])];
    for (const id of ids) await loadMedia(id).catch(() => null);
    P.items = P.items.filter(x => x.kind !== "pic" || media.has(x.media));
    if (P.music && !media.has(P.music.media)) P.music = null;
    if (P.take) await showTake(P.take.id, false);
    await loadQuick();
    let wanted = null;
    try { wanted = localStorage.getItem("bmg-studio-open"); localStorage.removeItem("bmg-studio-open"); } catch (e) {}
    const all = await listTakes();
    if (wanted && (!P.take || P.take.id !== wanted) && all && all.some(t => t.id === wanted)) await openTake(wanted);
    else if (!P.take && all && all.length && Date.now() - all[0].created < 15 * 60 * 1000) await openTake(all[0].id);
    normalize(); lastSnap = JSON.stringify(P);
    renderItems(); zoomFit(); renderAt(); panels(); undoButtons();
    showTab(P.take && take.id === P.take.id ? (tab === "takes" ? "pics" : tab) : "takes", true);
    savedEl.textContent = saved ? "Saved in this browser" : "";
  }
  const ready = start();
  return {
    show(){ shown = true; ready.then(() => { zoomFit(); renderItems(); drawWave(); listTakes(); }); st.focus({preventScroll: true}); },
    hide(){ shown = false; pause(); if (listenNode) listen($('[data-a="listen"]')); },
    async openTake(id, o = {}){ await ready; if (o.clean) cleanEdit(); showTab(o.tab || "takes", true); await openTake(id, o.note); },
    async takeArrived(id){ await ready; const all = await listTakes(); if (!P.take && all && all.some(t => t.id === id)) await openTake(id); else flash("A new take is in Takes."); },
    // the page waits before loading a new version: an export (from its click), an edit not yet saved, a video not
    // downloaded or still downloading, the edit or a song playing
    busy: () => !!exportJob || preparing || saving || unsavedResult || Date.now() < holdUntil || playing || !!listenNode,
    state: () => ({P, cur, playing, selected, pps, thumbs: thumbs.map(t => +t.t.toFixed(2)), shown})        // for checks: what the editor holds right now
  };
}

// parts: the pure pieces, for the tests (site/server/tests/test_studio.py)
window.BMGStudio = {mount, version: 1, parts: {gainCurve, curveAt, videoFill, videoRect, linkKind, mp4File, fixWebmDuration, pickType, describeType, aiffDecode}};
})();
