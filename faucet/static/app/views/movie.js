// Movie detail: backdrop header, what's on disk, what the hunter is doing.
import { api, qs } from "../api.js";
import { bg, bytes, day, html, mount, raw, stamp, tint, until } from "../dom.js";
import { links } from "../links.js";
import { pickVideo } from "../ui/filepicker.js";
import { fixMatch } from "../ui/fixmatch.js";
import { wireMenu } from "../ui/menu.js";
import { confirmDialog } from "../ui/modal.js";
import { releaseSearch } from "../ui/releases.js";
import { toast } from "../ui/toast.js";

export const title = "Movie";

function state(d) {
  const w = d.want;
  if (w && w.status === "grabbed") return ["downloading", "Downloading", w.reason === "upgrade" ? "a better copy is downloading" : "the release is downloading"];
  if (d.file && w && w.reason === "upgrade") {
    return ["upgrade", d.file.source === "CAM" ? "On disk · CAM" : "Upgrade wanted",
      d.file.source === "CAM" ? "a cam copy — Faucet keeps hunting for a real release" : "below the quality target — hunting for better"];
  }
  if (d.file) return ["have", "On disk", ""];
  if (w) return ["wanted", "Wanted", w.next_search ? `next search ${until(w.next_search)}` : "searching soon"];
  return ["skipped", "Not monitored", ""];
}

export async function render(el, ctx) {
  const mid = Number(ctx.params[0]);
  const admin = ctx.me.role === "admin";
  let d = await api.get(`/api/movies/${mid}/detail`);
  document.title = `${d.movie.title} · Faucet`;
  const reload = async () => { d = await api.get(`/api/movies/${mid}/detail`); draw(); };

  function draw() {
    const m = d.movie, meta = d.meta || {};
    const [cls, label, note] = state(d);
    const genres = (meta.genres || []).slice(0, 3);
    mount(el, html`<div class="page" style="gap:0">
      <section class="detail-hero" style="${meta.backdrop ? bg(meta.backdrop) : tint(m.title)}">
        <div class="scrim">
          <span class="poster" style="${m.poster || meta.poster ? bg(m.poster || meta.poster) : tint(m.title)}" role="img" aria-label="${m.title} poster"></span>
          <div class="info">
            <a class="crumb" href="${links.movies}">Movies</a>
            <h1>${m.title}</h1>
            <div class="metarow">
              ${m.year ? html`<span>${m.year}</span>` : ""}
              ${meta.runtime ? html`<span>${Math.floor(meta.runtime / 60)}h ${meta.runtime % 60}m</span>` : ""}
              ${meta.rating ? html`<span>★ ${meta.rating}</span>` : ""}
              ${genres.map((g) => html`<span class="tagpill">${g}</span>`)}
              <span class="status ${cls}">${label}</span>
            </div>
            ${meta.overview ? html`<p>${meta.overview}</p>` : ""}
            ${admin ? html`<div class="acts">
              <button class="btn" type="button" data-search>Search releases</button>
              <label class="selbox">Quality
                <select data-profile aria-label="Quality profile">
                  <option value="0">Use the default</option>
                  ${d.profiles.map((pr) => html`<option value="${pr.id}"${m.profile_id === pr.id ? raw(" selected") : ""}>${pr.name}</option>`)}
                </select></label>
              <details class="menu"><summary aria-label="More actions">${raw('<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>')}</summary>
                <div class="items">
                  <button type="button" data-act="fix">Fix match…</button>
                  <button type="button" data-act="locate">Fix location…</button>
                  <button type="button" class="danger" data-act="remove">Remove movie…</button>
                </div></details>
            </div>` : ""}
          </div>
        </div></section>
      <div class="stack" style="padding-top:26px;max-width:980px">
        <div class="panel"><h3>${label}</h3>
          ${note ? html`<p class="muted" style="margin:0 0 14px">${note}</p>` : ""}
          ${d.file ? html`<dl class="kv">
              <dt>Quality</dt><dd>${d.file.quality || "unknown"}${d.file.source ? ` · ${d.file.source}` : ""}</dd>
              ${d.file.size ? html`<dt>Size</dt><dd>${bytes(d.file.size)}</dd>` : ""}
              ${d.file.path ? html`<dt>File</dt><dd class="mono">${d.file.path}</dd>` : ""}
            </dl>` : html`<p class="muted" style="margin:0">Not on disk yet.</p>`}
          ${d.want && admin ? html`<dl class="kv" style="margin-top:14px">
              <dt>Searches</dt><dd>${d.want.search_count || 0}${d.want.last_search ? ` · last ${stamp(d.want.last_search)}` : ""}</dd>
              ${d.want.next_search ? html`<dt>Next search</dt><dd>${stamp(d.want.next_search)}</dd>` : ""}
            </dl>` : ""}
        </div>
        ${admin ? html`<div class="panel"><h3>Hunt log</h3><div data-log><div class="empty">Loading…</div></div></div>` : ""}
      </div></div>`);
    if (admin) wire();
  }

  async function loadLog() {
    const box = el.querySelector("[data-log]");
    try {
      const r = await api.get(`/api/wanted/log?${qs({ kind: "movie", series_id: mid, limit: 50 })}`);
      mount(box, r.entries.length ? html`${r.entries.map((x) => html`<div class="log-row">
          <span class="when">${stamp(x.ts)}</span><span class="tag">${x.results} results</span>
          <span><span class="status ${x.outcome === "grabbed" ? "have" : x.outcome === "error" ? "wanted" : "skipped"}">${x.outcome === "miss" ? "nothing usable" : x.outcome}</span></span>
          <span class="main"><span>${x.outcome === "grabbed" ? x.release : x.outcome === "error" ? x.detail : x.query}</span>
            ${x.outcome === "grabbed" ? html`<span>${x.tracker || ""} · ${x.seeders} seeders</span>`
              : x.next_search ? html`<span>next search ${day(x.next_search)}</span>` : ""}</span></div>`)}`
        : html`<div class="empty">No searches logged in the last 14 days.</div>`);
    } catch (e) {
      mount(box, html`<div class="empty">${e.message}</div>`);
    }
  }

  function wire() {
    el.querySelector("[data-search]").addEventListener("click", () => releaseSearch({
      title: d.movie.title, sub: d.movie.year ? String(d.movie.year) : "",
      url: `/api/movies/${mid}/releases`, grabUrl: `/api/movies/${mid}/grab`, onGrabbed: reload,
    }));
    el.querySelector("[data-profile]").addEventListener("change", async (e) => {
      try {
        await api.post(`/api/movies/${mid}/profile?${qs({ profile_id: e.target.value })}`);
        toast("Quality profile updated.");
      } catch (err) { toast(err.message, "err"); }
    });
    const menu = el.querySelector(".menu");
    wireMenu(menu);
    menu.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
      menu.open = false;
      if (b.dataset.act === "fix") fixMatch({ kind: "movie", id: mid, title: d.movie.title, onDone: reload });
      else if (b.dataset.act === "locate") pickVideo({
        title: `Which file is ${d.movie.title}?`, sub: "Point the movie at a file Faucet didn't recognise. Nothing is moved.",
        onPick: async (rel) => {
          try {
            const r = await api.post(`/api/admin/movie/${mid}/link-file`, { rel });
            if (r.error) throw new Error(r.error);
            toast("Linked — the movie counts as on disk.");
            reload();
            return true;
          } catch (e) { toast(e.message, "err"); return false; }
        } });
      else if (await confirmDialog("Remove movie?", `${d.movie.title} stops being monitored. The file on disk is not touched.`, "Remove", true)) {
        try {
          await api.del(`/api/movies/${mid}`);
          toast(`Removed ${d.movie.title}.`);
          location.hash = links.movies;
        } catch (e) { toast(e.message, "err"); }
      }
    }));
    loadLog();
  }

  draw();
}
