// Requests: members see theirs and can ask for more; admins approve or
// decline (pending first).
import { api, qs } from "../api.js";
import { ago, bg, html, mount, tint } from "../dom.js";
import { links } from "../links.js";
import { confirmDialog } from "../ui/modal.js";
import { open as openPalette } from "../ui/palette.js";
import { toast } from "../ui/toast.js";

export const title = "Requests";

const view = { filter: "pending" };
const LABEL = { pending: "Waiting", approved: "Approved", auto_approved: "Approved", declined: "Declined", fulfilled: "Available" };
const CLS = { pending: "upgrade", approved: "downloading", auto_approved: "downloading", declined: "skipped", fulfilled: "have" };

export async function render(el, ctx) {
  const admin = ctx.me.role === "admin";
  const d = await api.get(`/api/requests?${qs({ scope: admin ? "auto" : "mine" })}`);
  const all = d.requests;
  if (!admin) view.filter = "all";
  const draw = () => {
    const list = view.filter === "pending" ? all.filter((r) => r.status === "pending") : all;
    mount(el, html`<div class="page">
      <div class="stack"><div class="page-h"><h1>${admin ? "Requests" : "My requests"}</h1>
        <button class="btn" type="button" data-new>Request something</button></div>
        ${admin ? html`<div class="seg" role="group" aria-label="Filter">
          <button type="button" data-f="pending" aria-pressed="${view.filter === "pending"}">Waiting<span class="n">${all.filter((r) => r.status === "pending").length}</span></button>
          <button type="button" data-f="all" aria-pressed="${view.filter === "all"}">All<span class="n">${all.length}</span></button></div>` : ""}</div>
      ${list.length ? html`<div class="ftable">${list.map((r) => {
        const lib = r.series_id ? links.show(r.series_id) : r.movie_id ? links.movie(r.movie_id) : null;
        return html`<div class="frow req">
          <span class="cal-thumb" style="${r.poster ? bg(r.poster) : tint(r.title)}" aria-hidden="true"></span>
          <span class="fname"><b>${lib ? html`<a href="${lib}">${r.title}</a>` : r.title}${r.year ? ` (${r.year})` : ""}</b>
            <span class="muted">${r.media_type === "movie" ? "Movie" : "TV"}${admin && r.user ? ` · ${r.user}` : ""} · ${ago(r.ts)}</span></span>
          <span class="status ${CLS[r.status] || "skipped"}">${LABEL[r.status] || r.status}</span>
          ${admin ? html`<span class="acts">${r.status === "pending" ? html`<button class="btn" type="button" data-ok="${r.id}">Approve</button>
              <button class="btn line" type="button" data-no="${r.id}">Decline</button>` : ""}
            <button class="btn line" type="button" data-del="${r.id}" aria-label="Delete request">Delete</button></span>` : ""}
        </div>`;
      })}</div>` : html`<div class="empty">${view.filter === "pending" ? "Nothing waiting for approval." : "No requests yet."}</div>`}
    </div>`);
    el.querySelector("[data-new]").addEventListener("click", () => openPalette(ctx.me, "tmdb"));
    el.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { view.filter = b.dataset.f; draw(); }));
    const act = async (fn, msg) => { try { await fn(); toast(msg); render(el, ctx); } catch (e) { toast(e.message, "err"); } };
    el.querySelectorAll("[data-ok]").forEach((b) => b.addEventListener("click", () =>
      act(() => api.post(`/api/requests/${b.dataset.ok}/approve`), "Approved — it's monitored now.")));
    el.querySelectorAll("[data-no]").forEach((b) => b.addEventListener("click", () =>
      act(() => api.post(`/api/requests/${b.dataset.no}/decline`), "Declined.")));
    el.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (await confirmDialog("Delete request?", "It disappears from the list. Anything already monitored stays.", "Delete", true))
        act(() => api.del(`/api/requests/${b.dataset.del}`), "Deleted.");
    }));
  };
  draw();
}
