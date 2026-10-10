// Calendar: what aired lately and what's coming, by day.
import { api, qs } from "../api.js";
import { bg, html, mount, sxe, tint } from "../dom.js";
import { links } from "../links.js";
import { confirmDialog, openModal } from "../ui/modal.js";
import { toast } from "../ui/toast.js";

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
      <div class="stack"><div class="page-h"><h1>Calendar</h1><span class="count">last 7 days · next 3 weeks</span>
        <button class="btn line" type="button" data-sub>Subscribe on your phone</button></div>
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
    el.querySelector("[data-sub]").addEventListener("click", subscribe);
  };
  draw();
}

// Private calendar link: shown once when created; regenerate to get a new one.
async function subscribe() {
  const m = openModal({ title: "Subscribe to your calendar", body: html`<div class="empty">Loading…</div>` });
  const draw = (feed, url) => {
    mount(m.body, html`<div class="stack">
      <p class="muted" style="margin:0">A private link your phone or computer's calendar can subscribe to: every episode of your shows as an all-day event, refreshed every few hours. It only includes the libraries you can see.</p>
      ${url ? html`<div class="nadd"><input class="field mono" readonly value="${url}" data-url data-autofocus>
          <button class="btn" type="button" data-copy>Copy</button></div>
        <p class="lib-warn" style="margin:0">Copy it now — for your security it isn't shown again. Anyone with the link can see your schedule.</p>
        <div class="panel" style="font-size:13.5px;line-height:1.6"><b>iPhone:</b> Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar → paste.<br>
          <b>Google Calendar</b> (web): Other calendars → + → From URL → paste. It then shows on Android too.<br>
          <b>Outlook:</b> Add calendar → Subscribe from web → paste.</div>`
        : feed ? html`<p style="margin:0">You have a calendar link, created ${new Date(feed.created_ts).toLocaleString()}${feed.last_used ? html` · last fetched ${new Date(feed.last_used).toLocaleString()}` : " · not fetched yet"}.</p>
          <p class="muted" style="margin:0">Lost it, or shared it by mistake? Make a new one — the old link stops working.</p>`
        : html`<p style="margin:0">You don't have a calendar link yet.</p>`}
      <div class="acts" style="justify-content:flex-end">
        ${feed && !url ? html`<button class="btn line" type="button" data-off>Turn off</button>` : ""}
        ${url ? "" : html`<button class="btn" type="button" data-make>${feed ? "Make a new link" : "Create my link"}</button>`}
      </div></div>`);
    const copy = m.body.querySelector("[data-copy]");
    if (copy) copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(url); toast("Copied."); }
      catch { m.body.querySelector("[data-url]").select(); }
    });
    const make = m.body.querySelector("[data-make]");
    if (make) make.addEventListener("click", async () => {
      try { const r = await api.post("/api/me/calendar"); draw(r.feed, r.url); }
      catch (err) { toast(err.message, "err"); }
    });
    const off = m.body.querySelector("[data-off]");
    if (off) off.addEventListener("click", async () => {
      if (!await confirmDialog("Turn off the calendar link?", "Calendars subscribed to it stop updating.", "Turn off", true)) return;
      try { await api.del("/api/me/calendar"); toast("Calendar link turned off."); } catch (err) { toast(err.message, "err"); }
    });
  };
  try { draw((await api.get("/api/me/calendar")).feed, null); }
  catch (err) { mount(m.body, html`<div class="empty">${err.message}</div>`); }
}
