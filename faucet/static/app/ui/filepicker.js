// Video-file picker over the admin file browser (scoped to BROWSE_ROOT).
import { api, qs } from "../api.js";
import { bytes, html, mount } from "../dom.js";
import { icons } from "../icons.js";
import { openModal } from "./modal.js";

export function pickVideo({ title, sub = "", onPick }) {
  const m = openModal({ title, sub, wide: true, body: html`<div data-fp></div>` });
  const box = m.body.querySelector("[data-fp]");
  async function go(rel) {
    const d = await api.get(`/api/admin/files/list?${qs({ path: rel })}`).catch((e) => ({ error: e.message }));
    if (d.error) { mount(box, html`<div class="empty">${d.error}</div>`); return; }
    const dirs = d.entries.filter((e) => e.kind === "dir");
    const vids = d.entries.filter((e) => e.kind === "video");
    mount(box, html`<div class="fp">
      <div class="fp-bar mono"><span>${d.root}${d.cwd ? `/${d.cwd}` : ""}</span></div>
      <div class="fp-list">
        ${d.at_root ? "" : html`<button type="button" class="fp-row" data-go="${d.parent || ""}">${icons.left}<span>Up</span></button>`}
        ${dirs.map((x) => html`<div class="fp-row"><button type="button" class="fp-open" data-go="${x.rel}">${icons.folder}<span>${x.name}</span></button></div>`)}
        ${vids.map((x) => html`<div class="fp-row"><button type="button" class="fp-open" data-pick="${x.rel}">${icons.film}<span>${x.name}</span>
          <span class="fp-note">${bytes(x.size)}</span></button></div>`)}
        ${dirs.length || vids.length ? "" : html`<div class="empty">No folders or videos here.</div>`}
      </div></div>`);
    box.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));
    box.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      if (await onPick(b.dataset.pick)) m.close();
      else b.disabled = false;
    }));
  }
  go("");
}
