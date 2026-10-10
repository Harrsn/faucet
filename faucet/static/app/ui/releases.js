// Interactive release search (the Sonarr hourglass), shared by episodes,
// season packs and movies. Releases the profile rejects are still listed,
// greyed with the reason, so an override is deliberate.
import { api } from "../api.js";
import { bytes, html, mount } from "../dom.js";
import { openModal } from "./modal.js";
import { toast } from "./toast.js";

export function releaseSearch({ title, sub, url, grabUrl, onGrabbed }) {
  const m = openModal({ title, sub, wide: true,
    body: html`<div class="empty">Searching every indexer…</div>` });
  load();

  async function load() {
    let d;
    try {
      d = await api.get(url);
    } catch (e) {
      mount(m.body, html`<div class="empty">Search failed: ${e.message}</div>`);
      return;
    }
    const failed = (d.indexer_errors || []).map((x) => x.indexer);
    const head = html`<p class="muted" style="margin:0 0 10px;font-size:13px">
      ${d.releases.length} matching of ${d.considered} results${d.indexers > 1 ? ` · ${d.indexers} indexers` : ""}${failed.length ? ` · ${failed.length} failed (${failed.join(", ")})` : ""}${d.profile ? ` · ranked by “${d.profile}”` : ""}${d.hidden ? ` · ${d.hidden} unsafe hidden` : ""}</p>`;
    if (!d.releases.length) {
      mount(m.body, html`${head}<div class="empty">Nothing matched. The hunter keeps trying on its schedule.</div>`);
      return;
    }
    mount(m.body, html`${head}${d.releases.map((r, i) => html`
      <div class="rel${r._passes ? "" : " fail"}">
        <span class="rt">${r.title}</span>
        <span class="rm">
          ${r.badges && r.badges.res ? html`<span class="chip">${r.badges.res}</span>` : ""}
          <span>${r.tracker || "?"}</span><span>${r.seeders} seeders</span><span>${bytes(r.size)}</span>
          ${r._passes ? "" : html`<span class="why">${r._why || "fails the profile"}</span>`}
        </span>
        <button class="btn ${r._passes && i === 0 ? "" : "line"}" type="button" data-i="${i}">Grab</button>
      </div>`)}`);
    m.body.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", async () => {
      const r = d.releases[+b.dataset.i];
      b.disabled = true;
      b.textContent = "Adding…";
      try {
        await api.post(grabUrl, { magnet: r.href, title: r.title });
        toast(`Grabbed ${r.title.slice(0, 70)}`);
        m.close();
        if (onGrabbed) onGrabbed();
      } catch (e) {
        toast(e.message, "err");
        b.disabled = false;
        b.textContent = "Grab";
      }
    }));
  }
}
