// TV Shows: poster grid with filters, sort and a quick text filter.
import { api } from "../api.js";
import { bg, html, mount, tint } from "../dom.js";
import { links } from "../links.js";
import { open as openPalette } from "../ui/palette.js";

export const title = "TV Shows";

// remembered for the session, so coming back from a show keeps your view
// per section, remembered for the session
const views = {};

const FILTERS = [
  ["all", "All", () => true],
  ["missing", "Missing episodes", (s) => s.wanted > 0],
  ["complete", "Complete", (s) => s.total > 0 && s.have >= s.total],
  ["continuing", "Continuing", (s) => s.tv_status && !s.ended],
  ["ended", "Ended", (s) => s.ended],
  ["paused", "Not monitored", (s) => !s.monitored || s.monitor_mode === "none"],
];

const SORTS = {
  title: (a, b) => sortKey(a.title).localeCompare(sortKey(b.title)),
  added: (a, b) => String(b.added_ts || "").localeCompare(String(a.added_ts || "")),
  missing: (a, b) => b.wanted - a.wanted || SORTS.title(a, b),
  completion: (a, b) => pct(a) - pct(b) || SORTS.title(a, b),
};

const sortKey = (t) => String(t || "").toLowerCase().replace(/^(the|a|an)\s+/, "");
const pct = (s) => (s.total ? Math.min(100, Math.floor((100 * s.have) / s.total)) : 0);

function card(s) {
  const p = pct(s);
  const line = !s.total ? html`<span class="s">No episode list yet</span>`
    : s.wanted ? html`<span class="s warn mono">${s.have}/${s.total} · ${s.wanted} missing</span>`
    : s.have >= s.total ? html`<span class="s good mono">${s.have}/${s.total} · complete</span>`
    : html`<span class="s mono">${s.have}/${s.total}</span>`;
  return html`<a class="card poster" href="${links.show(s.id)}">
    <span class="art" style="${s.poster ? bg(s.poster) : tint(s.title)}">
      ${!s.monitored || s.monitor_mode === "none" ? html`<span class="chip">Not monitored</span>`
        : s.ended ? html`<span class="chip">Ended</span>` : html`<span></span>`}
      ${s.poster ? "" : html`<span class="placeholder-title">${s.title}</span>`}
      <span class="bar"><i style="width:${s.wanted ? Math.min(p, 99) : p}%${s.total && s.have >= s.total ? ";background:var(--good)" : ""}"></i></span>
    </span>
    <span class="t">${s.title}</span>${line}</a>`;
}

export async function render(el, ctx) {
  const view = (views[ctx.params[0] ?? "all"] ||= { filter: "all", sort: "title", q: "" });
  const d = await api.get("/api/series");
  // a library section (#/shows/in/<id>) shows only that library's titles
  const libId = ctx.params[0] != null ? Number(ctx.params[0]) : null;
  const section = libId != null ? (ctx.sections || []).find((x) => x.id === libId) : null;
  const all = libId != null ? d.series.filter((x) => x.lib === libId) : d.series;
  const heading = section ? section.name : "TV Shows";
  document.title = `${heading} · Faucet`;
  const draw = () => {
    const f = FILTERS.find((x) => x[0] === view.filter) || FILTERS[0];
    const q = view.q.trim().toLowerCase();
    const list = all.filter(f[2]).filter((s) => !q || s.title.toLowerCase().includes(q))
      .sort(SORTS[view.sort] || SORTS.title);
    mount(el.querySelector("[data-grid]"), list.length ? html`${list.map(card)}`
      : html`<div class="empty" style="grid-column:1/-1">No shows match.</div>`);
    el.querySelectorAll("[data-f]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === view.filter)));
    el.querySelector("[data-count]").textContent = `${list.length} of ${all.length}`;
  };
  mount(el, html`<div class="page">
    <div class="stack">
      <div class="page-h"><h1>${heading}</h1><span class="count" data-count></span>
        <button class="btn" type="button" data-add>${ctx.me.role === "admin" ? "Add a show" : "Request a show"}</button></div>
      <div class="toolbar">
        <div class="seg" role="group" aria-label="Filter">${FILTERS.map(([id, label, fn]) => html`
          <button type="button" data-f="${id}" aria-pressed="false">${label}<span class="n">${all.filter(fn).length}</span></button>`)}</div>
        <label class="sr" for="sh-q">Filter by title</label>
        <input id="sh-q" class="field filter-input" placeholder="Filter by title" value="${view.q}">
        <label class="sr" for="sh-sort">Sort</label>
        <select id="sh-sort" class="field">
          ${[["title", "Title"], ["added", "Recently added"], ["missing", "Most missing"], ["completion", "Least complete"]]
            .map(([v, l]) => html`<option value="${v}"${view.sort === v ? html` selected` : ""}>${l}</option>`)}
        </select>
      </div>
    </div>
    <div class="grid" data-grid></div></div>`);
  el.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { view.filter = b.dataset.f; draw(); }));
  el.querySelector("#sh-q").addEventListener("input", (e) => { view.q = e.target.value; draw(); });
  el.querySelector("#sh-sort").addEventListener("change", (e) => { view.sort = e.target.value; draw(); });
  el.querySelector("[data-add]").addEventListener("click", () => openPalette(ctx.me, "tmdb"));
  draw();
}
