// Wanted (admins): what the hunter is after, per title, and when it looks next.
import { api } from "../api.js";
import { bg, html, mount, tint, until } from "../dom.js";
import { links } from "../links.js";

export const title = "Wanted";

export async function render(el) {
  const d = await api.get("/api/wanted/summary");
  const t = d.totals;
  mount(el, html`<div class="page">
    <div class="stack"><div class="page-h"><h1>Wanted</h1>
      <span class="count">${t.wanted} wanted · ${t.upgrades} upgrades · ${t.downloading} downloading · ${t.due} due now</span></div>
      <p class="muted" style="margin:0;max-width:760px">Every missing episode and movie the hunter is after. Searches that find nothing back off on their own; titles with something due are searched on the next tick, up to the per-tick limit in Settings → Hunting.</p></div>
    ${d.items.length ? html`<div class="ftable">${d.items.map((g) => html`<a class="frow req" href="${g.kind === "movie" ? links.movie(g.id) : links.show(g.id)}">
        <span class="cal-thumb" style="${g.poster ? bg(g.poster) : tint(g.title)}" aria-hidden="true"></span>
        <span class="fname"><b>${g.title}${g.year ? ` (${g.year})` : ""}</b>
          <span class="muted">${g.kind === "movie" ? "Movie" : `${g.wanted} episode${g.wanted === 1 ? "" : "s"}`}${g.upgrades ? ` · ${g.upgrades} upgrade${g.upgrades === 1 ? "" : "s"}` : ""}${g.downloading ? ` · ${g.downloading} downloading` : ""}${g.searches ? ` · ${g.searches} searches so far` : ""}</span></span>
        <span class="status ${g.due ? "missing" : "unaired"}">${g.due ? `${g.due} due now` : g.next_search ? `next ${until(g.next_search)}` : "waiting"}</span></a>`)}</div>`
      : html`<div class="empty">Nothing wanted — the library is complete.</div>`}
  </div>`);
}
