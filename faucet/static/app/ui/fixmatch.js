// Fix Match: re-point a show or movie at a different TMDb entry.
import { api, qs } from "../api.js";
import { bg, html, mount, tint } from "../dom.js";
import { openModal } from "./modal.js";
import { toast } from "./toast.js";

export function fixMatch({ kind, id, title, onDone }) {
  const m = openModal({
    title: "Fix match",
    sub: `${title} is matched to the wrong ${kind === "show" ? "show" : "movie"}? Pick the right one.`,
    body: html`<label class="sr" for="fm-q">Search TMDb</label>
      <input id="fm-q" class="field" style="width:100%" value="${title}" autocomplete="off">
      <div data-res style="margin-top:14px"></div>`,
  });
  const input = m.body.querySelector("#fm-q");
  const res = m.body.querySelector("[data-res]");
  let timer = null;
  const search = async () => {
    const q = input.value.trim();
    if (q.length < 2) { mount(res, ""); return; }
    mount(res, html`<div class="empty">Searching TMDb…</div>`);
    try {
      const d = await api.get(`/api/admin/match-search?${qs({ q, kind })}`);
      const items = d.results || [];
      mount(res, items.length ? html`${items.map((x, i) => html`
        <button class="pick" type="button" data-i="${i}">
          <span class="thumb" style="${x.poster ? bg(x.poster) : tint(x.title)}"></span>
          <span class="main"><b>${x.title}${x.year ? ` (${x.year})` : ""}</b><span>${x.overview || ""}</span></span>
        </button>`)}` : html`<div class="empty">No matches on TMDb.</div>`);
      res.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", async () => {
        const x = items[+b.dataset.i];
        b.disabled = true;
        try {
          const r = await api.post(`/api/admin/title/${kind}/${id}/fixmatch`, { tmdb_id: x.tmdb_id });
          if (r.error) throw new Error(r.error);
          toast(`Now matched to ${x.title}.`);
          m.close();
          if (onDone) onDone();
        } catch (e) {
          toast(e.message, "err");
          b.disabled = false;
        }
      }));
    } catch (e) {
      mount(res, html`<div class="empty">${e.message}</div>`);
    }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 300); });
  search();
}
