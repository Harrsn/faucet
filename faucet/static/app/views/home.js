// Home: hero (newest arrival) + rails. Admins also see downloads and what's
// still being hunted.
import { api } from "../api.js";
import { ago, bg, eta, html, initials, mount, rate, sxe, tint } from "../dom.js";
import { links } from "../links.js";
import { enhanceRails, rail } from "../ui/rail.js";

export const title = "Home";

function hero(h) {
  if (!h) return "";
  const n = h.new.length;
  const eps = h.new.slice(0, 3).map((e) => `${sxe(e.season, e.episode)}${e.ep_title ? ` ${e.ep_title}` : ""}`).join(" · ");
  return html`<section class="hero" aria-label="Featured" style="${h.backdrop ? bg(h.backdrop) : tint(h.title)}">
    <div class="scrim">
      <div class="txt">
        <span class="kicker">Just added${n > 1 ? ` · ${n} new episodes` : ""}</span>
        <h1>${h.title}</h1>
        <div class="eps">${eps}${n > 3 ? ` · +${n - 3} more` : ""}</div>
        ${h.overview ? html`<p>${h.overview}</p>` : ""}
      </div>
      <div class="acts"><a class="btn" href="${links.show(h.series_id, h.new.length ? h.new[0].season : undefined)}">Open show</a></div>
    </div></section>`;
}

// A season pack lands as a run of episodes from one season: one card for
// the run instead of seven identical ones.
function group(eps) {
  const out = [];
  for (const e of eps) {
    const last = out[out.length - 1];
    if (last && last.series_id === e.series_id && last.season === e.season) last.items.push(e);
    else out.push({ ...e, items: [e] });
  }
  return out;
}

function epCard(g) {
  const n = g.items.length;
  const still = (g.items.find((x) => x.still) || {}).still;
  const nums = g.items.map((x) => x.episode).sort((a, b) => a - b);
  const tag = n > 1 ? `S${String(g.season).padStart(2, "0")} · ${n} episodes` : `${sxe(g.season, g.episode)}${g.quality ? ` · ${g.quality}` : ""}`;
  const sub = n > 1 ? `E${String(nums[0]).padStart(2, "0")}–E${String(nums[n - 1]).padStart(2, "0")}` : (g.ep_title || "—");
  return html`<a class="card ep" role="listitem" href="${links.show(g.series_id, g.season)}">
    <span class="art" style="${still ? bg(still) : g.poster ? bg(g.poster) : tint(g.title)}">
      <span class="tag">${tag}</span></span>
    <span class="t">${g.title}</span>
    <span class="s">${sub} · ${ago(g.added)}</span></a>`;
}

const movieCard = (m) => html`<a class="card poster" role="listitem" href="${links.movie(m.id)}">
  <span class="art" style="${m.poster ? bg(m.poster) : tint(m.title)}">
    ${m.cam ? html`<span class="chip bad">CAM · upgrading</span>`
      : m.want === "upgrade" ? html`<span class="chip warn">Upgrading</span>`
      : m.quality ? html`<span class="chip">${m.quality}</span>` : html`<span></span>`}
    ${m.poster ? "" : html`<span class="placeholder-title">${m.title}</span>`}
  </span>
  <span class="t">${m.title}</span><span class="s">${m.year || ""}</span></a>`;

const showCard = (s) => html`<a class="card poster" role="listitem" href="${links.show(s.id)}">
  <span class="art" style="${s.poster ? bg(s.poster) : tint(s.title)}">
    <span></span>${s.poster ? "" : html`<span class="placeholder-title">${s.title}</span>`}
    <span class="bar"><i style="width:${s.pct}%"></i></span></span>
  <span class="t">${s.title}</span><span class="s mono">${s.wanted} wanted · ${s.pct}%</span></a>`;

function dlCard(d) {
  const stuck = d.status === "downloading" && !d.down_rate && d.percent < 100;
  const done = d.percent >= 100;
  const note = d.error ? d.error
    : done ? "finishing · sorting next"
    : stuck ? "no peers right now · the stall handler retries another release"
    : `${d.percent}% · ${rate(d.down_rate)}${d.eta > 0 ? ` · ${eta(d.eta)}` : ""}`;
  return html`<div class="dl">
    <span class="ini" aria-hidden="true">${initials(d.name)}</span>
    <div class="body">
      <span class="name" title="${d.name}">${d.name}</span>
      <span class="progress ${done ? "done" : stuck ? "stuck" : ""}"><i style="width:${Math.min(100, d.percent)}%"></i></span>
      <span class="note ${stuck || d.error ? "warn" : ""}">${note}</span>
    </div></div>`;
}

function section(name, meta, more, body) {
  return html`<section aria-label="${name}">
    <div class="sec-h"><h2>${name}</h2>${meta ? html`<span class="meta">${meta}</span>` : ""}
      ${more ? html`<a href="${more[1]}">${more[0]}</a>` : ""}</div>${body}</section>`;
}

export async function render(view, ctx) {
  const d = await api.get("/api/home");
  ctx.status(d.stats);
  const parts = [hero(d.hero)];
  parts.push(section("Recently added episodes", "", ["TV Shows", links.shows],
    d.episodes.length ? rail("Recently added episodes", group(d.episodes).slice(0, 24).map(epCard))
      : html`<div class="empty">Nothing added yet. New episodes show up here as they land.</div>`));
  if (d.admin) parts.push(html`<div id="dl-sec">${downloads(d)}</div>`);
  parts.push(section("Movies", "", ["All movies", links.movies],
    d.movies.length ? rail("Recently added movies", d.movies.map(movieCard))
      : html`<div class="empty">No movies on disk yet.</div>`));
  if (d.admin && d.hunting && d.hunting.length) {
    parts.push(section("Still hunting", `${d.stats.wanted} wanted`, ["Wanted", links.wanted],
      rail("Shows still being hunted", d.hunting.map(showCard))));
  }
  mount(view, html`<div class="page">${parts}</div>`);
  enhanceRails(view);
  if (!d.admin) return {};
  // live part only: re-rendering the whole page would reset the rails
  return {
    every: 15000,
    async refresh() {
      const n = await api.get("/api/home");
      ctx.status(n.stats);
      const el = view.querySelector("#dl-sec");
      if (el) mount(el, downloads(n));
    },
  };
}

function downloads(d) {
  const act = d.downloads || [];
  return section("Downloading",
    !d.stats.client_ok ? "client offline"
      : `${act.length} active${d.stats.down_rate ? ` · ${rate(d.stats.down_rate)}` : ""}`,
    ["Activity", links.activity],
    !d.stats.client_ok ? html`<div class="empty">The download client isn't reachable. Check Settings → Integrations.</div>`
      : html`${d.stats.client_slow ? html`<p class="lib-warn" style="margin:0 0 10px">The client is slow to respond right now (often while a big download is being moved onto the NAS) — showing the list from ${d.stats.client_as_of ? d.stats.client_as_of.slice(11, 16) : "earlier"}.</p>` : ""}
        ${act.length ? html`<div class="dl-grid">${act.slice(0, 8).map(dlCard)}</div>`
          : html`<div class="empty">Nothing downloading right now.</div>`}`);
}
