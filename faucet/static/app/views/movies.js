// Movies: poster grid with filters, sort and a quick text filter.
import { api } from "../api.js";
import { bg, html, mount, tint } from "../dom.js";
import { links } from "../links.js";
import { open as openPalette } from "../ui/palette.js";

export const title = "Movies";

// per section, remembered for the session
const views = {};

const onDisk = (m) => m.status === "have";
const FILTERS = [
  ["all", "All", () => true],
  ["have", "On disk", (m) => onDisk(m) && m.want_reason !== "upgrade"],
  ["wanted", "Wanted", (m) => !onDisk(m)],
  ["upgrade", "Upgrading", (m) => m.want_reason === "upgrade"],
  ["ignored", "Not monitored", (m) => m.lib_status && m.lib_status !== "monitored"],
];

const sortKey = (t) => String(t || "").toLowerCase().replace(/^(the|a|an)\s+/, "");
const SORTS = {
  title: (a, b) => sortKey(a.title).localeCompare(sortKey(b.title)),
  year: (a, b) => (b.year || 0) - (a.year || 0) || SORTS.title(a, b),
  added: (a, b) => String(b.added_ts || "").localeCompare(String(a.added_ts || "")),
};

function card(m) {
  const chip = m.want_reason === "upgrade" ? html`<span class="chip warn">Upgrading</span>`
    : !onDisk(m) ? html`<span class="chip bad">Wanted</span>` : html`<span></span>`;
  return html`<a class="card poster" href="${links.movie(m.id)}">
    <span class="art" style="${m.poster ? bg(m.poster) : tint(m.title)}">${chip}
      ${m.poster ? "" : html`<span class="placeholder-title">${m.title}</span>`}</span>
    <span class="t">${m.title}</span>
    <span class="s">${m.year || ""}${!onDisk(m) ? " · wanted" : ""}</span></a>`;
}

export async function render(el, ctx) {
  const view = (views[ctx.params[0] ?? "all"] ||= { filter: "all", sort: "title", q: "" });
  const d = await api.get("/api/movies");
  // a library section (#/movies/in/<id>) shows only that library's titles
  const libId = ctx.params[0] != null ? Number(ctx.params[0]) : null;
  const section = libId != null ? (ctx.sections || []).find((x) => x.id === libId) : null;
  const all = libId != null ? d.movies.filter((x) => x.lib === libId) : d.movies;
  const heading = section ? section.name : "Movies";
  document.title = `${heading} · Faucet`;
  const draw = () => {
    const f = FILTERS.find((x) => x[0] === view.filter) || FILTERS[0];
    const q = view.q.trim().toLowerCase();
    const list = all.filter(f[2]).filter((m) => !q || m.title.toLowerCase().includes(q))
      .sort(SORTS[view.sort] || SORTS.title);
    mount(el.querySelector("[data-grid]"), list.length ? html`${list.map(card)}`
      : html`<div class="empty" style="grid-column:1/-1">No movies match.</div>`);
    el.querySelectorAll("[data-f]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === view.filter)));
    el.querySelector("[data-count]").textContent = `${list.length} of ${all.length}`;
  };
  mount(el, html`<div class="page">
    <div class="stack">
      <div class="page-h"><h1>${heading}</h1><span class="count" data-count></span>
        <button class="btn" type="button" data-add>${ctx.me.role === "admin" ? "Add a movie" : "Request a movie"}</button></div>
      <div class="toolbar">
        <div class="seg" role="group" aria-label="Filter">${FILTERS.map(([id, label, fn]) => html`
          <button type="button" data-f="${id}" aria-pressed="false">${label}<span class="n">${all.filter(fn).length}</span></button>`)}</div>
        <label class="sr" for="mv-q">Filter by title</label>
        <input id="mv-q" class="field filter-input" placeholder="Filter by title" value="${view.q}">
        <label class="sr" for="mv-sort">Sort</label>
        <select id="mv-sort" class="field">
          ${[["title", "Title"], ["year", "Newest release"], ["added", "Recently added"]]
            .map(([v, l]) => html`<option value="${v}"${view.sort === v ? html` selected` : ""}>${l}</option>`)}
        </select>
      </div>
    </div>
    <div class="grid" data-grid></div></div>`);
  el.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { view.filter = b.dataset.f; draw(); }));
  el.querySelector("#mv-q").addEventListener("input", (e) => { view.q = e.target.value; draw(); });
  el.querySelector("#mv-sort").addEventListener("change", (e) => { view.sort = e.target.value; draw(); });
  el.querySelector("[data-add]").addEventListener("click", () => openPalette(ctx.me, "tmdb"));
  draw();
}
