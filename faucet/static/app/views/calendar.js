// Calendar: what aired lately and what's coming, by day.
import { api, qs } from "../api.js";
import { bg, html, mount, sxe, tint } from "../dom.js";
import { links } from "../links.js";

export const title = "Calendar";

const view = { filter: "all" };
const LABEL = { have: "On disk", grabbed: "Downloading", missing: "Missing", upcoming: "Upcoming" };
const CLS = { have: "have", grabbed: "downloading", missing: "missing", upcoming: "unaired" };

function dayLabel(iso, today) {
  const d = new Date(`${iso}T12:00:00`);
  const t = new Date(`${today}T12:00:00`);
  const diff = Math.round((d - t) / 86400000);
  const name = d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  return diff === 0 ? `Today · ${name}` : diff === 1 ? `Tomorrow · ${name}` : diff === -1 ? `Yesterday · ${name}` : name;
}

export async function render(el) {
  const d = await api.get(`/api/calendar?${qs({ past: 7, days: 21 })}`);
  const draw = () => {
    const eps = d.episodes.filter((e) => view.filter === "all" || e.status === view.filter);
    const days = [];
    for (const e of eps) {
      if (!days.length || days[days.length - 1].date !== e.date) days.push({ date: e.date, eps: [] });
      days[days.length - 1].eps.push(e);
    }
    const count = (s) => d.episodes.filter((e) => e.status === s).length;
    mount(el, html`<div class="page">
      <div class="stack"><div class="page-h"><h1>Calendar</h1><span class="count">last 7 days · next 3 weeks</span></div>
        <div class="seg" role="group" aria-label="Filter">${[["all", "All", d.episodes.length], ["upcoming", "Upcoming", count("upcoming")],
          ["missing", "Missing", count("missing")], ["grabbed", "Downloading", count("grabbed")], ["have", "On disk", count("have")]]
          .map(([id, l, n]) => html`<button type="button" data-f="${id}" aria-pressed="${view.filter === id}">${l}<span class="n">${n}</span></button>`)}</div></div>
      ${days.length ? days.map((day) => html`<section class="cal-day${day.date === d.today ? " today" : ""}" aria-label="${day.date}">
          <h2>${dayLabel(day.date, d.today)}</h2>
          <div class="eps">${day.eps.map((e) => html`<a class="ep-row cal" href="${links.show(e.series_id, e.season)}">
            <span class="cal-thumb" style="${e.poster ? bg(e.poster) : tint(e.title)}" aria-hidden="true"></span>
            <span class="txt"><b>${e.title}</b><span class="k">${sxe(e.season, e.episode)}${e.ep_title ? ` · ${e.ep_title}` : ""}</span></span>
            <span class="status ${CLS[e.status]}">${LABEL[e.status]}</span></a>`)}</div></section>`)
        : html`<div class="empty">Nothing ${view.filter === "all" ? "airs" : `${LABEL[view.filter].toLowerCase()}`} in this window.</div>`}
    </div>`);
    el.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { view.filter = b.dataset.f; draw(); }));
  };
  draw();
}
