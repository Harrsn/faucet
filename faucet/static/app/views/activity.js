// Activity (admins): live downloads with pause / resume / remove; every grab
// with why it was picked and what became of it; the blocklist (releases the
// hunter won't grab again); the history of what Faucet did; the event log.
import { api, qs } from "../api.js";
import { bytes, html, initials, mount, stamp } from "../dom.js";
import { links } from "../links.js";
import { confirmDialog } from "../ui/modal.js";
import { toast } from "../ui/toast.js";

export const title = "Activity";

const view = { tab: "downloads", blFilter: "problems", q: "" };
const REASONS = {
  blocklisted: "already grabbed", wrong_title: "wrong show", pack: "packs", not_upgrade: "not better",
  seeders: "too few seeders", size: "size", resolution: "resolution", cam: "cam",
  language: "language", profile: "profile",
};
const STATE = { done: ["have", "done"], problem: ["missing", "problem"], removed: ["skipped", "removed"],
                pending: ["downloading", "downloading"] };
const EVENT_TEXT = { completed: "finished", sorted: "filed in the library", stalled: "stalled", failed: "failed",
  suspicious: "flagged as suspicious", grab_failed: "couldn't be added", removed: "removed" };
const EV = { added: "downloading", completed: "have", sorted: "have", removed: "skipped", failed: "wanted",
             grab_failed: "wanted", suspicious: "upgrade", stalled: "upgrade" };

export async function render(el, ctx) {
  let data = {};

  async function load() {
    if (view.tab === "downloads") data = await api.get("/api/transfers").catch((e) => ({ error: e.message, transfers: [] }));
    else if (view.tab === "grabs") data = await api.get(`/api/grabs?${qs({ days: 30, q: view.q })}`);
    else if (view.tab === "blocklist") data = await api.get(`/api/blocklist?${qs({ filter: view.blFilter, q: view.q })}`);
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

  const search = (ph) => html`<input class="field" type="search" placeholder="${ph}" value="${view.q}" data-q style="max-width:320px">`;

  function grabs() {
    const g = data.grabs || [];
    return html`<div class="stack">
      <div class="acts">${search("Filter by release or show")}<span class="muted" style="font-size:13px">last 30 days · ${g.length} grab${g.length === 1 ? "" : "s"}</span></div>
      ${g.length ? html`<div class="ftable">${g.map((x) => {
        const [cls, word] = STATE[x.state] || STATE.pending;
        const href = x.link ? (x.link.kind === "movie" ? links.movie(x.link.id) : links.show(x.link.id, x.link.season)) : null;
        const rej = x.why && Object.entries(x.why.rejected || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${REASONS[k] || k}`).join(", ");
        return html`<div class="frow grab">
          <span class="when mono muted">${stamp(x.ts)}</span>
          <span class="fname">
            <b>${href ? html`<a href="${href}">${x.label}</a>` : x.label || "—"} ${x.how === "manual" ? html`<span class="tagpill">manual</span>` : x.how === "pack" ? html`<span class="tagpill">season pack</span>` : ""}</b>
            <span class="mono relname" title="${x.release}">${x.release}</span>
            <span class="muted">${[x.tracker, x.seeders != null ? `${x.seeders} seeders` : "", x.size ? bytes(x.size) : ""].filter(Boolean).join(" · ")}
              ${x.why ? html` — picked from ${x.why.kept} of ${x.why.found} found${rej ? html`; skipped ${rej}` : ""}` : ""}</span>
            ${x.event && x.state !== "done" ? html`<span class="muted">${EVENT_TEXT[x.event] || x.event} ${stamp(x.event_ts)}${x.detail ? ` — ${x.detail}` : ""}</span>` : ""}
          </span>
          <span class="status ${cls}">${word}</span></div>`;
      })}</div>` : html`<div class="empty">No grabs in the last 30 days${view.q ? " match that" : ""}.</div>`}</div>`;
  }

  function blocklist() {
    const c = data.counts || { all: 0, problems: 0 };
    const items = data.items || [];
    return html`<div class="stack">
      <p class="muted" style="margin:0;max-width:820px">Every release Faucet has grabbed stays here, so it's never grabbed twice — that's what keeps a stalled or fake release from coming back. <b>Allow again</b> if one was blocked for the wrong reason (a tracker hiccup, say); the hunter may then pick it on its next search.</p>
      <div class="acts"><div class="seg" role="group" aria-label="Show">
        <button type="button" data-bf="problems" aria-pressed="${view.blFilter === "problems"}">Went wrong<span class="n">${c.problems}</span></button>
        <button type="button" data-bf="all" aria-pressed="${view.blFilter === "all"}">Everything<span class="n">${c.all}</span></button></div>
        ${search("Filter by release")}</div>
      ${items.length ? html`<div class="ftable">${items.map((x) => {
        const [cls, word] = STATE[x.state] || STATE.pending;
        return html`<div class="frow grab">
          <span class="when mono muted">${stamp(x.ts)}</span>
          <span class="fname"><span class="mono relname" title="${x.release}">${x.release}</span>
            ${x.event ? html`<span class="muted">${EVENT_TEXT[x.event] || x.event}${x.detail ? ` — ${x.detail}` : ""}</span>` : ""}</span>
          <span class="status ${cls}">${word}</span>
          <button class="btn line small" type="button" data-allow="${x.id}">Allow again</button></div>`;
      })}</div>${data.truncated ? html`<p class="muted">Showing the newest 500 — filter to find older ones.</p>` : ""}`
        : html`<div class="empty">${view.blFilter === "problems" ? "Nothing has gone wrong — no stalled, failed or flagged releases." : "Nothing grabbed yet."}</div>`}</div>`;
  }

  function draw() {
    mount(el, html`<div class="page" style="gap:22px">
      <h1>Activity</h1>
      <div class="ptabs" role="tablist" aria-label="Activity">${[["downloads", "Downloads"], ["grabs", "Grabs"], ["blocklist", "Blocklist"], ["history", "History"], ["events", "Event log"]].map(([id, l]) =>
        html`<button type="button" role="tab" data-tab="${id}" aria-selected="${view.tab === id}">${l}</button>`)}</div>
      <div data-body>${view.tab === "downloads" ? downloads() : view.tab === "grabs" ? grabs()
        : view.tab === "blocklist" ? blocklist() : view.tab === "history" ? history() : events()}</div></div>`);
    const qbox = el.querySelector("[data-q]");
    if (qbox) {
      let t = null;
      qbox.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(async () => { view.q = qbox.value.trim(); await load(); draw(); el.querySelector("[data-q]").focus(); }, 300);
      });
    }
    el.querySelectorAll("[data-bf]").forEach((b) => b.addEventListener("click", async () => {
      view.blFilter = b.dataset.bf; await load(); draw();
    }));
    el.querySelectorAll("[data-allow]").forEach((b) => b.addEventListener("click", async () => {
      const x = data.items.find((i) => i.id === Number(b.dataset.allow));
      if (!await confirmDialog("Allow this release again?", x.release, "Allow again")) return;
      try { await api.del(`/api/blocklist/${x.id}`); toast("Allowed — the hunter may pick it again."); await load(); draw(); }
      catch (err) { toast(err.message, "err"); }
    }));
    el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", async () => { view.tab = b.dataset.tab; view.q = ""; await load(); draw(); }));
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
