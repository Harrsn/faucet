// Show detail: backdrop header, then Episodes / Hunt log / Files.
import { api, qs } from "../api.js";
import { bg, day, html, mount, raw, stamp, sxe, tint, until } from "../dom.js";
import { icons } from "../icons.js";
import { links } from "../links.js";
import { fixMatch } from "../ui/fixmatch.js";
import { wireMenu } from "../ui/menu.js";
import { confirmDialog } from "../ui/modal.js";
import { enhanceRails, rail } from "../ui/rail.js";
import { releaseSearch } from "../ui/releases.js";
import { toast } from "../ui/toast.js";

export const title = "Show";

const REASONS = {
  blocklisted: "blocklisted", wrong_title: "wrong show", pack: "packs", not_upgrade: "not better",
  seeders: "too few seeders", size: "size", resolution: "resolution", cam: "cam",
  language: "language", profile: "profile",
};

const pctOf = (have, total) => (total ? Math.floor((100 * have) / total) : 0);

function defaultSeason(seasons) {
  const real = seasons.filter((s) => s.total > 0);
  const pick = real.length ? real[real.length - 1] : seasons[seasons.length - 1];
  return pick ? pick.season : null;
}

export async function render(el, ctx) {
  const sid = Number(ctx.params[0]);
  const admin = ctx.me.role === "admin";
  let d = await api.get(`/api/series/${sid}/detail`);
  let season = ctx.params[1] != null ? Number(ctx.params[1]) : defaultSeason(d.seasons);
  if (!d.seasons.some((s) => s.season === season)) season = defaultSeason(d.seasons);
  let tab = "episodes";
  document.title = `${d.series.title} · Faucet`;

  const reload = async () => {
    d = await api.get(`/api/series/${sid}/detail`);
    draw();
  };

  function hero() {
    const s = d.series, m = d.meta || {};
    const p = pctOf(d.have, d.total);
    const genres = (m.genres || []).slice(0, 3);
    const returning = m.status && !["Ended", "Canceled"].includes(m.status);
    return html`<section class="detail-hero" style="${m.backdrop ? bg(m.backdrop) : tint(s.title)}">
      <div class="scrim">
        <span class="poster" style="${s.poster || m.poster ? bg(s.poster || m.poster) : tint(s.title)}" role="img" aria-label="${s.title} poster"></span>
        <div class="info">
          <a class="crumb" href="${links.shows}">TV Shows</a>
          <h1>${s.title}</h1>
          <div class="metarow">
            ${m.year || s.year ? html`<span>${m.year || s.year}</span>` : ""}
            <span>${d.seasons.length} season${d.seasons.length === 1 ? "" : "s"}</span>
            <span>${d.total} episodes</span>
            ${m.rating ? html`<span>★ ${m.rating}</span>` : ""}
            ${genres.map((g) => html`<span class="tagpill">${g}</span>`)}
            ${m.status ? html`<span class="tagpill${returning ? " good" : ""}">${returning ? "Returning series" : m.status}</span>` : ""}
          </div>
          ${m.overview ? html`<p>${m.overview}</p>` : ""}
          ${admin ? html`<div class="acts">
            <button class="btn" type="button" data-hunt${d.wanted ? "" : raw(" disabled")}>${d.wanted ? `Hunt missing (${d.wanted})` : "Nothing missing"}</button>
            <label class="selbox">Monitor
              <select data-monitor aria-label="Monitor">
                ${[["all", "All episodes"], ["future", "Future only"], ["none", "Not monitored"]].map(([v, l]) =>
                  html`<option value="${v}"${(s.monitor_mode || "all") === v ? raw(" selected") : ""}>${l}</option>`)}
              </select></label>
            <label class="selbox">Quality
              <select data-profile aria-label="Quality profile">
                <option value="0">Use the default</option>
                ${d.profiles.map((pr) => html`<option value="${pr.id}"${s.profile_id === pr.id ? raw(" selected") : ""}>${pr.name}</option>`)}
              </select></label>
            ${d.save_to && d.save_to.options.length > 1 ? html`<label class="selbox">Save to
              <select data-saveto aria-label="Library new episodes are filed into">
                <option value="">${d.save_to.options.find((o) => o.default)?.name || "Default"} (default)</option>
                ${d.save_to.options.filter((o) => !o.default).map((o) => html`<option value="${o.id}"${s.library_id === o.id ? raw(" selected") : ""}>${o.name}</option>`)}
              </select></label>` : ""}
            <details class="menu"><summary aria-label="More actions">${raw('<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>')}</summary>
              <div class="items">
                <button type="button" data-act="log">Hunt log</button>
                <button type="button" data-act="fix">Fix match…</button>
                <button type="button" class="danger" data-act="remove">Remove show…</button>
              </div></details>
          </div>` : ""}
        </div>
        <div class="completion">
          <b>${p}%<small>complete</small></b>
          <span class="progress"><i style="width:${d.wanted ? Math.min(p, 99) : p}%"></i></span>
          <span class="mono">${d.have} on disk · ${d.wanted} missing${d.downloading ? ` · ${d.downloading} downloading` : ""}</span>
        </div>
      </div></section>`;
  }

  function tabs() {
    const t = [["episodes", "Episodes"]];
    if (admin) t.push(["log", "Hunt log"], ["files", "Files"]);
    return html`<div class="ptabs" role="tablist" aria-label="Show sections">${t.map(([id, l]) =>
      html`<button type="button" role="tab" data-tab="${id}" aria-selected="${tab === id}">${l}</button>`)}</div>`;
  }

  function seasonCards() {
    const cards = [...d.seasons].reverse().map((sn) => {
      const done = sn.total > 0 && sn.have >= sn.total;
      const p = pctOf(sn.have, sn.total);
      return html`<button class="season-card" type="button" role="listitem" data-season="${sn.season}" aria-pressed="${sn.season === season}">
        <span class="art" style="${d.series.poster ? bg(d.series.poster) : tint(d.series.title + sn.season)}"><span>Season ${sn.season}</span></span>
        <span class="meta"><span class="${done ? "done" : ""}">${sn.have} / ${sn.total}</span>
          <span class="progress${done ? " done" : ""}"><i style="width:${sn.wanted ? Math.min(p, 99) : p}%"></i></span></span>
      </button>`;
    });
    return rail("Seasons", cards);
  }

  function epDetail(e) {
    if (e.status === "have") return e.upgrade ? `${e.quality || "on disk"} · upgrade wanted` : (e.quality || "on disk");
    if (e.status === "downloading") return "downloading";
    if (e.status === "wanted") {
      const parts = [e.upgrade ? "upgrade wanted" : "missing"];
      if (e.next_search) parts.push(`next search ${until(e.next_search)}`);
      if (e.searches) parts.push(`${e.searches} search${e.searches === 1 ? "" : "es"} so far`);
      return parts.join(" · ");
    }
    if (e.status === "unaired") return e.air_date ? `airs ${day(e.air_date)}` : "air date not announced";
    if (e.status === "skipped") return "not monitored";
    return "missing";
  }

  const LABEL = { have: "On disk", downloading: "Downloading", wanted: "Missing", missing: "Missing",
                  unaired: "Unaired", skipped: "Skipped" };

  function episodes() {
    const sn = d.seasons.find((x) => x.season === season);
    if (!sn) return html`<div class="empty">No episode list yet — TMDb hasn't been read for this show.</div>`;
    return html`<div class="season-h"><h2>Season ${sn.season}</h2>
        <span class="mono">${sn.have} of ${sn.total} on disk${sn.wanted ? ` · ${sn.wanted} missing` : ""}${sn.downloading ? ` · ${sn.downloading} downloading` : ""}</span>
        ${admin ? html`<button class="btn line" type="button" data-pack="${sn.season}">Search season pack</button>` : ""}</div>
      <div class="eps">${sn.episodes.map((e) => html`
        <div class="ep-row">
          <span class="still" style="${e.still ? bg(e.still) : tint(d.series.title + e.episode)}" aria-hidden="true"></span>
          <span class="txt"><span class="k">E${String(e.episode).padStart(2, "0")}${e.air_date ? ` · ${day(e.air_date)}` : ""}</span>
            <b>${e.title || `Episode ${e.episode}`}</b><span class="d">${epDetail(e)}</span></span>
          <span class="status ${e.upgrade && e.status !== "downloading" ? "upgrade" : e.status}">${e.upgrade && e.status === "have" ? "Upgrade" : LABEL[e.status] || e.status}</span>
          ${admin && e.aired ? html`<button class="icon-btn" type="button" data-ep="${e.episode}" aria-label="Search releases for ${sxe(sn.season, e.episode)}">${icons.search}</button>` : ""}
        </div>`)}</div>`;
  }

  async function huntLog(box) {
    mount(box, html`<div class="empty">Loading…</div>`);
    const r = await api.get(`/api/wanted/log?${qs({ kind: "episode", series_id: sid, limit: 150 })}`);
    if (!r.entries.length) {
      mount(box, html`<div class="empty">No searches logged in the last 14 days.</div>`);
      return;
    }
    mount(box, html`<p class="muted" style="margin:0 0 14px">Every search the hunter ran for this show in the last 14 days, newest first.</p>
      ${r.entries.map((x) => {
        const tag = x.kind === "pack" ? `S${String(x.season).padStart(2, "0")} pack` : x.season != null ? sxe(x.season, x.episode) : "—";
        const cls = x.outcome === "grabbed" ? "have" : x.outcome === "error" ? "wanted" : "skipped";
        const why = Object.entries(x.reasons || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${REASONS[k] || k}`).join(", ");
        const main = x.outcome === "grabbed" ? x.release : x.outcome === "error" ? (x.detail || "search failed") : `${x.results} result${x.results === 1 ? "" : "s"}`;
        const sub = x.outcome === "grabbed" ? [x.tracker, x.seeders != null ? `${x.seeders} seeders` : "", why && `rejected ${why}`].filter(Boolean).join(" · ")
          : [why && `rejected ${why}`, x.next_search && `next search ${stamp(x.next_search)}`].filter(Boolean).join(" · ");
        return html`<div class="log-row"><span class="when">${stamp(x.ts)}</span><span class="tag">${tag}</span>
          <span><span class="status ${cls}">${x.outcome === "miss" ? "nothing usable" : x.outcome}</span></span>
          <span class="main"><span>${main}</span>${sub ? html`<span>${sub}</span>` : ""}</span></div>`;
      })}`);
  }

  function files() {
    const f = d.folders || [];
    return html`<div class="stack" style="max-width:900px">
      <div class="panel"><h3>Folders</h3>
        ${f.length ? html`<dl class="kv">${f.map((x) => html`<dt>${x.library}</dt>
          <dd><span class="mono">${x.path}</span> <span class="tagpill">${x.manual ? "linked by hand" : "matched automatically"}</span></dd>`)}</dl>`
          : html`<p class="muted" style="margin:0">No library folder is linked to this show yet, so only name matching counts its files. Linking folders by hand arrives with Settings → Libraries.</p>`}
      </div>
      <div class="panel"><h3>On disk</h3>
        <dl class="kv"><dt>Episodes</dt><dd>${d.have} of ${d.total} aired</dd>
          <dt>Missing</dt><dd>${d.wanted}</dd><dt>Downloading</dt><dd>${d.downloading}</dd></dl>
      </div></div>`;
  }

  function draw() {
    mount(el, html`<div class="page" style="gap:0">${hero()}${tabs()}<div data-tabbody style="padding-top:26px"></div></div>`);
    wireHero();
    el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.tab; draw(); }));
    const body = el.querySelector("[data-tabbody]");
    if (tab === "episodes") drawEpisodes(body);
    else if (tab === "log") huntLog(body).catch((e) => mount(body, html`<div class="empty">${e.message}</div>`));
    else mount(body, files());
  }

  function drawEpisodes(body) {
    mount(body, html`${d.seasons.length > 1 ? seasonCards() : ""}<div data-eps>${episodes()}</div>`);
    enhanceRails(body);
    const sel = body.querySelector(`[data-season="${season}"]`);
    if (sel) sel.scrollIntoView({ block: "nearest", inline: "nearest" });
    body.querySelectorAll("[data-season]").forEach((b) => b.addEventListener("click", () => {
      season = Number(b.dataset.season);
      history.replaceState(null, "", links.show(sid, season));
      body.querySelectorAll("[data-season]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      const eps = body.querySelector("[data-eps]");
      mount(eps, episodes());
      wireEpisodes(eps);
    }));
    wireEpisodes(body);
  }

  function wireEpisodes(box) {
    box.querySelectorAll("[data-ep]").forEach((b) => b.addEventListener("click", () => {
      const ep = Number(b.dataset.ep);
      releaseSearch({
        title: `${d.series.title} ${sxe(season, ep)}`,
        sub: (d.seasons.find((x) => x.season === season).episodes.find((x) => x.episode === ep) || {}).title || "",
        url: `/api/series/${sid}/episodes/${season}/${ep}/releases`,
        grabUrl: `/api/series/${sid}/episodes/${season}/${ep}/grab`,
        onGrabbed: reload,
      });
    }));
    const pk = box.querySelector("[data-pack]");
    if (pk) pk.addEventListener("click", () => releaseSearch({
      title: `${d.series.title} · Season ${season} pack`,
      sub: "Whole-season releases only",
      url: `/api/series/${sid}/seasons/${season}/releases`,
      grabUrl: `/api/series/${sid}/seasons/${season}/grab`,
      onGrabbed: reload,
    }));
  }

  function wireHero() {
    if (!admin) return;
    const hunt = el.querySelector("[data-hunt]");
    hunt.addEventListener("click", async () => {
      hunt.disabled = true;
      hunt.textContent = "Hunting…";
      try {
        const r = await api.post(`/api/series/${sid}/hunt`);
        toast(r.skipped_reason ? `Hunt skipped: ${r.skipped_reason}`
          : r.grabbed ? `Grabbed ${r.grabbed} release${r.grabbed === 1 ? "" : "s"}.`
          : `Searched ${r.searches || 0} time${r.searches === 1 ? "" : "s"} — nothing usable yet.`);
      } catch (e) {
        toast(e.message, "err");
      }
      reload();
    });
    el.querySelector("[data-monitor]").addEventListener("change", async (e) => {
      try {
        await api.post(`/api/series/${sid}/monitor?${qs({ mode: e.target.value })}`);
        toast("Monitoring updated.");
        reload();
      } catch (err) { toast(err.message, "err"); }
    });
    el.querySelector("[data-profile]").addEventListener("change", async (e) => {
      try {
        await api.post(`/api/series/${sid}/profile?${qs({ profile_id: e.target.value })}`);
        toast("Quality profile updated.");
      } catch (err) { toast(err.message, "err"); }
    });
    const st = el.querySelector("[data-saveto]");
    if (st) st.addEventListener("change", async (e) => {
      try {
        const r = await api.post(`/api/series/${sid}/library`, { library_id: e.target.value ? Number(e.target.value) : null });
        toast(`New episodes go to ${r.current}.`);
      } catch (err) { toast(err.message, "err"); }
    });
    const menu = el.querySelector(".menu");
    wireMenu(menu);
    menu.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
      menu.open = false;
      const act = b.dataset.act;
      if (act === "log") { tab = "log"; draw(); }
      else if (act === "fix") fixMatch({ kind: "show", id: sid, title: d.series.title, onDone: reload });
      else if (act === "remove") {
        if (await confirmDialog("Remove show?", `${d.series.title} stops being monitored and its wanted episodes are dropped. Files on disk are not touched.`, "Remove", true)) {
          try {
            await api.del(`/api/series/${sid}`);
            toast(`Removed ${d.series.title}.`);
            location.hash = links.shows;
          } catch (e) { toast(e.message, "err"); }
        }
      }
    }));
  }

  draw();
}
