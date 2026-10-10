// DOM helpers. Every view renders through `html`, which escapes each
// interpolated value unless it is already a Raw (another `html` result, or
// `raw(...)` for trusted markup like icons). There is no other path from
// data to innerHTML.

export class Raw {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}
export const raw = (s) => new Raw(String(s));

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"'`]/g, (c) => ESC[c]);

function fmt(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join("");
  return esc(v);
}

export function html(strings, ...vals) {
  let out = "";
  strings.forEach((s, i) => {
    out += s;
    if (i < vals.length) out += fmt(vals[i]);
  });
  return new Raw(out);
}

export function mount(el, content) {
  el.innerHTML = content instanceof Raw ? content.s : esc(content);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// Only TMDb image URLs ever reach a style attribute or <img>.
const TMDB_IMG = /^https:\/\/image\.tmdb\.org\/t\/p\/[a-z0-9]+\/[A-Za-z0-9_\-]+\.(?:jpg|jpeg|png|webp)$/;
export const safeImg = (u) => (typeof u === "string" && TMDB_IMG.test(u) ? u : "");
export const bg = (u) => {
  const s = safeImg(u);
  return s ? raw(`background-image:url('${s}')`) : "";
};

// deterministic muted tint for art-less cards
export function tint(seed) {
  let h = 0;
  for (const ch of String(seed || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const hue = h % 360;
  return raw(`background-color:hsl(${hue} 28% 22%)`);
}

export function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

export function rate(bps) { return `${bytes(bps)}/s`; }

export function eta(s) {
  if (!s || s < 0) return "";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  return `${Math.round(s / 86400)}d`;
}

export function ago(iso) {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 90) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}

export const sxe = (s, e) => `S${String(s).padStart(2, "0")}E${String(e).padStart(2, "0")}`;

export function initials(title) {
  return String(title || "?").replace(/^the\s+/i, "").split(/\s+/).slice(0, 2)
    .map((w) => w[0] || "").join("").toUpperCase();
}
