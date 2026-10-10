// Discover: TMDb's lists as poster rails. Titles you have link to their
// page; the rest get Monitor (admins) or Request (members) right on the card.
import { api, qs } from "../api.js";
import { bg, html, mount, tint } from "../dom.js";
import { links } from "../links.js";
import { enhanceRails, rail } from "../ui/rail.js";
import { toast } from "../ui/toast.js";

export const title = "Discover";

const LISTS = {
  tv: [["trending", "Trending this week"], ["popular", "Popular"], ["top_rated", "Top rated"]],
  movie: [["trending", "Trending this week"], ["now_playing", "In theaters"], ["upcoming", "Coming soon"],
          ["popular", "Popular"], ["top_rated", "Top rated"]],
};
const view = { kind: "tv" };

export async function render(el, ctx) {
  const admin = ctx.me.role === "admin";
  mount(el, html`<div class="page">
    <div class="page-h"><h1>Discover</h1>
      <div class="seg" role="group" aria-label="TV or movies">
        <button type="button" data-k="tv" aria-pressed="${view.kind === "tv"}">TV shows</button>
        <button type="button" data-k="movie" aria-pressed="${view.kind === "movie"}">Movies</button></div></div>
    <div data-lists class="stack" style="gap:34px"><div class="empty">Loading…</div></div></div>`);
  el.querySelectorAll("[data-k]").forEach((b) => b.addEventListener("click", () => { view.kind = b.dataset.k; render(el, ctx); }));

  const kind = view.kind;
  const data = await Promise.all(LISTS[kind].map(([id]) =>
    api.get(`/api/meta/discover?${qs({ kind, list: id })}`).catch(() => ({ results: [], enabled: true }))));
  if (data.length && data[0].enabled === false) {
    mount(el.querySelector("[data-lists]"), html`<div class="empty">Discover needs a TMDb key — add one in Settings → Integrations.</div>`);
    return;
  }
  const all = [];
  const card = (x) => {
    const i = all.push(x) - 1;
    const href = x.in_library ? (x.media_type === "tv" ? links.show(x.library_id) : links.movie(x.library_id)) : null;
    const art = html`<span class="art" style="${x.poster ? bg(x.poster) : tint(x.title)}">
      ${x.in_library ? html`<span class="chip good">In library</span>` : html`<span></span>`}
      ${x.poster ? "" : html`<span class="placeholder-title">${x.title}</span>`}</span>`;
    return href
      ? html`<a class="card poster" role="listitem" href="${href}">${art}<span class="t">${x.title}</span><span class="s">${x.year || ""}${x.rating ? ` · ★ ${x.rating}` : ""}</span></a>`
      : html`<div class="card poster" role="listitem">${art}<span class="t">${x.title}</span>
          <span class="s">${x.year || ""}${x.rating ? ` · ★ ${x.rating}` : ""}</span>
          <button class="btn line small" type="button" data-add="${i}">${admin ? "Monitor" : "Request"}</button></div>`;
  };
  mount(el.querySelector("[data-lists]"), html`${LISTS[kind].map(([, label], n) => {
    const items = data[n].results || [];
    return items.length ? html`<section aria-label="${label}"><div class="sec-h"><h2>${label}</h2></div>${rail(label, items.map(card))}</section>` : "";
  })}`);
  enhanceRails(el);
  el.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", async () => {
    const x = all[+b.dataset.add];
    b.disabled = true;
    const body = { tmdb_id: x.tmdb_id, title: x.title, year: x.year, poster: x.poster };
    try {
      if (admin) {
        const r = await api.post(x.media_type === "tv" ? "/api/series" : "/api/movies", body);
        toast(`Monitoring ${x.title}.`);
        const a = document.createElement("a");
        a.className = "btn line small";
        a.href = x.media_type === "tv" ? links.show(r.id) : links.movie(r.id);
        a.textContent = "Open";
        b.replaceWith(a);
      } else {
        const r = await api.post("/api/requests", { media_type: x.media_type, ...body });
        toast(r.status === "auto_approved" ? `${x.title} requested and approved.` : r.status === "already_requested"
          ? `${x.title} was already requested.` : r.status === "already_elsewhere" ? r.error : `Requested ${x.title}.`);
        b.textContent = "Requested";
      }
    } catch (e) { toast(e.message, "err"); b.disabled = false; }
  }));
}
