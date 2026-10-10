// Activity (admins): live downloads with pause / resume / remove, the
// history of what Faucet did, and the raw event log.
import { api } from "../api.js";
import { bytes, html, initials, mount, stamp } from "../dom.js";
import { confirmDialog } from "../ui/modal.js";
import { toast } from "../ui/toast.js";

export const title = "Activity";

const view = { tab: "downloads" };
const EV = { added: "downloading", completed: "have", sorted: "have", removed: "skipped", failed: "wanted",
             grab_failed: "wanted", suspicious: "upgrade", stalled: "upgrade" };

export async function render(el, ctx) {
  let data = {};

  async function load() {
    if (view.tab === "downloads") data = await api.get("/api/transfers").catch((e) => ({ error: e.message, transfers: [] }));
    else if (view.tab === "history") data = await api.get("/api/history?limit=200");
    else data = await api.get("/api/events?limit=200");
  }

  function downloads() {
    if (data.error) return html`<div class="empty">The download client isn't reachable: ${data.error}</div>`;
    const t = data.transfers;
    const slow = data.client && data.client.ok === false
      ? html`<p class="lib-warn" style="margin:0 0 10px">The client is slow to respond right now — showing the list from ${data.client.as_of ? data.client.as_of.slice(11, 19) : "earlier"}.</p>` : "";
    if (!t.length) return html`${slow}<div class="empty">Nothing in the download client.</div>`;
    return html`${slow}<div class="ftable">${t.map((x) => {
      const paused = /stop|pause/i.test(x.status);
      return html`<div class="frow dlrow">
        <span class="ini" aria-hidden="true">${initials(x.name)}</span>
        <span class="fname"><b title="${x.name}">${x.name}</b>
          <span class="progress ${x.done ? "done" : ""}"><i style="width:${Math.min(100, x.percent)}%"></i></span>
          <span class="muted mono">${x.percent.toFixed(1)}% of ${x.size_h} · ${x.status}${!x.done && x.down_h && !paused ? ` · ${x.down_h}` : ""}${x.eta_h && !x.done ? ` · ${x.eta_h}` : ""}${x.done ? ` · ratio ${(x.ratio || 0).toFixed(2)}` : ""}</span>
          ${x.flag ? html`<span class="lib-warn">⚑ ${x.flag}</span>` : ""}${x.error ? html`<span class="lib-warn">${x.error}</span>` : ""}</span>
        <span class="acts"><button class="btn line" type="button" data-act="${paused ? "resume" : "pause"}" data-id="${x.id}">${paused ? "Resume" : "Pause"}</button>
          <button class="btn line" type="button" data-act="remove" data-id="${x.id}">Remove</button></span></div>`;
    })}</div>`;
  }

  const history = () => (data.history.length ? html`<div class="ftable">${data.history.map((h) => html`<div class="frow">
      <span class="when mono muted" style="min-width:120px">${stamp(h.ts)}</span>
      <span class="status ${EV[h.event] || "skipped"}">${h.event.replace("_", " ")}</span>
      <span class="fname"><b>${h.title || "—"}</b>${h.detail ? html`<span class="muted">${h.detail}</span>` : ""}</span>
      ${h.size ? html`<span class="mono muted">${bytes(h.size)}</span>` : ""}</div>`)}</div>`
    : html`<div class="empty">No history yet.</div>`);

  const events = () => (data.events.length ? html`<div class="ftable">${data.events.map((e) => html`<div class="frow">
      <span class="when mono muted" style="min-width:120px">${stamp(e.ts)}</span>
      <span class="status ${EV[e.event] || "skipped"}">${e.event || "event"}</span>
      <span class="fname"><b>${e.name || "—"}</b>${e.msg ? html`<span class="muted">${e.msg}</span>` : ""}</span></div>`)}</div>`
    : html`<div class="empty">No events yet.</div>`);

  function draw() {
    mount(el, html`<div class="page" style="gap:22px">
      <h1>Activity</h1>
      <div class="ptabs" role="tablist" aria-label="Activity">${[["downloads", "Downloads"], ["history", "History"], ["events", "Event log"]].map(([id, l]) =>
        html`<button type="button" role="tab" data-tab="${id}" aria-selected="${view.tab === id}">${l}</button>`)}</div>
      <div data-body>${view.tab === "downloads" ? downloads() : view.tab === "history" ? history() : events()}</div></div>`);
    el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", async () => { view.tab = b.dataset.tab; await load(); draw(); }));
    el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
      const a = b.dataset.act;
      let action = a;
      if (a === "remove") {
        const t = data.transfers.find((x) => String(x.id) === b.dataset.id);
        if (!await confirmDialog("Remove download?", `${t.name} is removed from the client. Downloaded data is kept unless you choose otherwise.`, "Remove from client")) return;
      }
      try {
        await api.post(`/api/torrent/${encodeURIComponent(b.dataset.id)}`, { action });
        toast(a === "remove" ? "Removed from the client." : a === "pause" ? "Paused." : "Resumed.");
        await load(); draw();
      } catch (e) { toast(e.message, "err"); }
    }));
  }

  await load();
  draw();
  return {
    every: 5000,
    async refresh() {
      if (view.tab !== "downloads" || document.querySelector(".modal-scrim")) return;
      await load();
      draw();
    },
  };
}
