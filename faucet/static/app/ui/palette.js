// ⌘K search: your library, TMDb (add / request), and — admins — releases
// across every indexer. Library results are instant (filtered client-side);
// TMDb is debounced; releases only run on Enter, since each one fans out to
// every indexer.
import { api, qs } from "../api.js";
import { $, $$, bg, html, mount, tint, bytes } from "../dom.js";
import { icons } from "../icons.js";
import { links } from "../links.js";
import { toast } from "./toast.js";

let state = null;

const norm = (s) => String(s || "").toLowerCase().normalize("NFKD")
  .replace(/[\u0300-\u036f]/g, "").replace(/&/g, "and").replace(/[^a-z0-9]+/g, " ").trim();

export function initPalette(me) {
  document.addEventListener("keydown", (e) => {
    const k = e.key.toLowerCase();
    if ((e.metaKey || e.ctrlKey) && k === "k") {
      e.preventDefault();
      state ? close() : open(me);
    } else if (k === "/" && !state && !/^(input|textarea|select)$/i.test(e.target.tagName)
               && !e.target.isContentEditable) {
      e.preventDefault();
      open(me);
    }
  });
}

export async function open(me, tab = "all") {
  if (state) return;
  state = { me, tab, q: "", library: null, tmdb: [], releases: null, busy: {},
            sel: 0, timer: null, returnFocus: document.activeElement };
  const root = $("#palette-root");
  mount(root, html`<div class="palette-scrim" data-close>
    <div class="palette" role="dialog" aria-modal="true" aria-label="Search">
      <label class="q">
        <span class="sr">Search</span>${icons.search}
        <input id="pq" type="text" autocomplete="off" spellcheck="false"
               placeholder="Search your library, TMDb${me.role === "admin" ? " and releases" : ""}">
        <kbd>Esc</kbd>
      </label>
      <div class="tabs" role="tablist" aria-label="Where to search"></div>
      <div class="results" id="pres" role="listbox" aria-label="Results"></div>
      <div class="foot"><span>↑ ↓ move</span><span>↵ open</span><span>⌘K anywhere</span></div>
    </div></div>`);
  const input = $("#pq");
  input.addEventListener("input", () => { state.q = input.value; state.sel = 0;
    state.releases = null; render(); scheduleTmdb(); });
  input.addEventListener("keydown", onKey);
  root.querySelector("[data-close]").addEventListener("mousedown", (e) => {
    if (e.target.hasAttribute("data-close")) close();
  });
  input.focus();
  render();
  try {
    const [s, m] = await Promise.all([api.get("/api/series"), api.get("/api/movies")]);
    if (!state) return;
    state.library = [
      ...s.series.map((x) => ({ kind: "tv", id: x.id, title: x.title, year: x.year,
        poster: x.poster, tmdb_id: x.tmdb_id, sub: ["TV", x.year, `${x.have}/${x.total} on disk`].filter(Boolean).join(" · ") })),
      ...m.movies.map((x) => ({ kind: "movie", id: x.id, title: x.title, year: x.year,
        poster: x.poster, tmdb_id: x.tmdb_id, sub: ["Movie", x.year, x.status === "have" ? "on disk" : "wanted"].filter(Boolean).join(" · ") })),
    ];
    render();
  } catch (e) {
    toast(e.message, "err");
  }
}

export function close() {
  if (!state) return;
  clearTimeout(state.timer);
  const back = state.returnFocus;
  state = null;
  mount($("#palette-root"), "");
  if (back && back.focus) back.focus();
}

function tabs() {
  const t = [["all", "All"], ["library", "Library"], ["tmdb", "Add from TMDb"]];
  if (state.me.role === "admin") t.push(["releases", "Releases"]);
  return t;
}

function scheduleTmdb() {
  clearTimeout(state.timer);
  const q = state.q.trim();
  if (q.length < 2 || !["all", "tmdb"].includes(state.tab)) { state.tmdb = []; return; }
  state.timer = setTimeout(async () => {
    state.busy.tmdb = true; render();
    try {
      const r = await api.get(`/api/meta/search?${qs({ q, kind: "multi" })}`);
      if (state && state.q.trim() === q) state.tmdb = r.results || [];
    } catch { /* TMDb is optional */ }
    if (state) { state.busy.tmdb = false; render(); }
  }, 300);
}

async function searchReleases() {
  const q = state.q.trim();
  if (!q) return;
  state.busy.releases = true; state.releases = null; render();
  try {
    const r = await api.get(`/api/search?${qs({ q, cat: "all", limit: 30 })}`);
    if (state && state.q.trim() === q) state.releases = r;
  } catch (e) {
    toast(e.message, "err");
  }
  if (state) { state.busy.releases = false; render(); }
}

function libraryMatches() {
  const q = norm(state.q);
  if (!state.library) return [];
  if (!q) return state.library.slice(0, state.tab === "library" ? 30 : 6);
  const words = q.split(" ");
  return state.library
    .map((x) => ({ x, n: norm(x.title) }))
    .filter(({ n }) => words.every((w) => n.includes(w)))
    .sort((a, b) => (a.n.startsWith(q) ? 0 : 1) - (b.n.startsWith(q) ? 0 : 1) || a.n.length - b.n.length)
    .slice(0, state.tab === "library" ? 30 : 6)
    .map(({ x }) => x);
}

function render() {
  if (!state) return;
  mount($(".palette .tabs"), html`${tabs().map(([id, label]) => html`
    <button class="tab" type="button" role="tab" data-tab="${id}" aria-selected="${state.tab === id}">${label}</button>`)}`);
  $$(".palette .tab").forEach((b) => b.addEventListener("click", () => {
    state.tab = b.dataset.tab; state.sel = 0; render(); scheduleTmdb();
    $("#pq").focus();
  }));

  const groups = [];
  const owned = new Set((state.library || []).map((x) => `${x.kind}:${x.tmdb_id}`));
  if (["all", "library"].includes(state.tab)) {
    const lib = libraryMatches();
    groups.push(html`<div class="grp">In your library</div>`);
    groups.push(state.library === null ? html`<div class="note">Loading…</div>`
      : lib.length ? lib.map((x) => html`<a class="res" role="option" href="${x.kind === "tv" ? links.show(x.id) : links.movie(x.id)}">
          <span class="thumb" style="${x.poster ? bg(x.poster) : tint(x.title)}"></span>
          <span class="main"><b>${x.title}</b><span>${x.sub}</span></span></a>`)
        : html`<div class="note">Nothing in your library matches.</div>`);
  }
  if (["all", "tmdb"].includes(state.tab) && state.q.trim().length >= 2) {
    const add = state.tmdb.filter((x) => !owned.has(`${x.media_type}:${x.tmdb_id}`)).slice(0, state.tab === "tmdb" ? 20 : 5);
    groups.push(html`<div class="grp">Add from TMDb</div>`);
    groups.push(state.busy.tmdb && !add.length ? html`<div class="note">Searching TMDb…</div>`
      : add.length ? add.map((x, i) => html`<div class="res" role="option" data-tmdb="${i}">
          <span class="thumb" style="${x.poster ? bg(x.poster) : tint(x.title)}"></span>
          <span class="main"><b>${x.title}</b><span>${x.media_type === "tv" ? "TV" : "Movie"} · ${x.year || "—"} · not in library</span></span>
          <span class="act"><button class="btn" type="button" data-add="${i}">${state.me.role === "admin" ? "Monitor" : "Request"}</button></span></div>`)
        : html`<div class="note">No TMDb results.</div>`);
    state.tmdbShown = add;
  }
  if (state.tab === "releases" || (state.tab === "all" && state.me.role === "admin" && state.q.trim())) {
    const r = state.releases;
    groups.push(html`<div class="grp">Releases${r && r.indexers > 1 ? ` · ${r.indexers} indexers` : ""}</div>`);
    if (state.busy.releases) groups.push(html`<div class="note">Searching every indexer…</div>`);
    else if (!r) groups.push(html`<div class="note">Releases aren't searched until you ask — it queries every indexer.<button class="btn line" type="button" data-releases>Search releases for “${state.q.trim()}”</button></div>`);
    else if (!r.results.length) groups.push(html`<div class="note">No releases.</div>`);
    else groups.push(r.results.slice(0, 30).map((x, i) => html`<div class="res" role="option" data-rel="${i}">
        <span class="chip">${(x.badges && x.badges.res) || "—"}</span>
        <span class="main"><b>${x.title}</b><span>${x.tracker || "?"} · ${x.seeders} seeders · ${bytes(x.size)}</span></span>
        <span class="act"><button class="btn line" type="button" data-grab="${i}">Grab</button></span></div>`));
  }
  mount($("#pres"), html`${groups}`);

  const opts = $$("#pres .res");
  state.sel = Math.min(state.sel, Math.max(0, opts.length - 1));
  opts.forEach((o, i) => o.setAttribute("aria-selected", String(i === state.sel)));
  $$("#pres [data-add]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); addTmdb(+b.dataset.add); }));
  $$("#pres [data-grab]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); grab(+b.dataset.grab, b); }));
  const rb = $("#pres [data-releases]");
  if (rb) rb.addEventListener("click", () => searchReleases());
}

function onKey(e) {
  const opts = $$("#pres .res");
  if (e.key === "Escape") { e.preventDefault(); close(); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!opts.length) return;
    state.sel = (state.sel + (e.key === "ArrowDown" ? 1 : -1) + opts.length) % opts.length;
    opts.forEach((o, i) => o.setAttribute("aria-selected", String(i === state.sel)));
    opts[state.sel].scrollIntoView({ block: "nearest" });
    return;
  }
  if (e.key === "Enter") {
    e.preventDefault();
    const o = opts[state.sel];
    if (state.tab === "releases" && !state.releases) { searchReleases(); return; }
    if (!o) { if (state.me.role === "admin" && state.tab === "all") searchReleases(); return; }
    if (o.tagName === "A") { location.href = o.getAttribute("href"); close(); return; }
    if (o.dataset.tmdb !== undefined) { addTmdb(+o.dataset.tmdb); return; }
    if (o.dataset.rel !== undefined) { grab(+o.dataset.rel, o.querySelector("[data-grab]")); return; }
    searchReleases();
  }
}

async function addTmdb(i) {
  const x = (state.tmdbShown || [])[i];
  if (!x) return;
  const body = { tmdb_id: x.tmdb_id, title: x.title, year: x.year, poster: x.poster };
  try {
    if (state.me.role === "admin") {
      const r = await api.post(x.media_type === "tv" ? "/api/series" : "/api/movies", body);
      toast(`Monitoring ${x.title}.`);
      location.href = x.media_type === "tv" ? links.show(r.id) : links.movie(r.id);
      close();
    } else {
      const r = await api.post("/api/requests", { media_type: x.media_type, ...body });
      toast(r.status === "auto_approved" ? `Requested ${x.title} — approved.`
        : r.status === "already_requested" ? `${x.title} was already requested.`
        : `Requested ${x.title}. An admin will review it.`);
      close();
    }
  } catch (e) {
    toast(e.message, "err");
  }
}

async function grab(i, btn) {
  const x = state.releases && state.releases.results[i];
  if (!x) return;
  if (btn) { btn.disabled = true; btn.textContent = "Adding…"; }
  try {
    await api.post("/api/add", { magnet: x.href, title: x.title });
    toast(`Added: ${x.title.slice(0, 60)}`);
    if (btn) btn.textContent = "Added";
  } catch (e) {
    toast(e.message, "err");
    if (btn) { btn.disabled = false; btn.textContent = "Grab"; }
  }
}
