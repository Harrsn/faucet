// Entry point: who am I, shell, hash router, ⌘K.
import { api } from "./api.js";
import { $, html, mount } from "./dom.js";
import { renderShell, setActive, updateStatus } from "./shell.js";
import { initPalette } from "./ui/palette.js";
import { toast } from "./ui/toast.js";

// route -> [nav id, () => import(view module)]
const ROUTES = [
  [/^\/?$/, "home", () => import("./views/home.js")],
  [/^\/shows\/?$/, "shows", () => import("./views/shows.js")],
  [/^\/shows\/in\/(\d+)\/?$/, (p) => `lib-${p[0]}`, () => import("./views/shows.js")],
  [/^\/shows\/(\d+)(?:\/(\d+))?\/?$/, null, () => import("./views/show.js")],
  [/^\/movies\/?$/, "movies", () => import("./views/movies.js")],
  [/^\/movies\/in\/(\d+)\/?$/, (p) => `lib-${p[0]}`, () => import("./views/movies.js")],
  [/^\/movies\/(\d+)\/?$/, null, () => import("./views/movie.js")],
  [/^\/settings(?:\/([a-z]+))?\/?$/, "settings", () => import("./views/settings.js")],
  [/^\/discover\/?$/, "discover", () => import("./views/discover.js")],
  [/^\/calendar\/?$/, "calendar", () => import("./views/calendar.js")],
  [/^\/requests\/?$/, "requests", () => import("./views/requests.js")],
  [/^\/activity\/?$/, "activity", () => import("./views/activity.js")],
  [/^\/wanted\/?$/, "wanted", () => import("./views/wanted.js")],
  [/^\/setup(?:\/([a-z]+))?\/?$/, "settings", () => import("./views/setup.js")],
];

let me = null;
let sections = [];
let current = { stop() {} };

function match(path) {
  for (const [re, nav, load] of ROUTES) {
    const m = path.match(re);
    if (m) {
      const params = m.slice(1);
      return { nav: typeof nav === "function" ? nav(params) : nav, load, params };
    }
  }
  return null;
}

async function route() {
  current.stop();
  const path = decodeURIComponent((location.hash || "#/").slice(1));
  const r = match(path) || match("/");
  setActive(r.nav);
  const view = $("#view");
  mount(view, html`<div class="boot">Loading…</div>`);
  const ctx = { me, params: r.params, status: updateStatus, sections, setNav: setActive, refreshNav };
  let timer = null;
  let alive = true;
  current = { stop() { alive = false; clearInterval(timer); } };
  try {
    const mod = await r.load();
    if (!alive) return;
    document.title = `${mod.title || "Faucet"} · Faucet`;
    const live = (await mod.render(view, ctx)) || {};
    if (alive && live.every && live.refresh) {
      timer = setInterval(() => {
        if (document.visibilityState === "visible") live.refresh().catch(() => {});
      }, live.every);
    }
  } catch (e) {
    if (!alive) return;
    mount(view, html`<div class="page"><div class="empty">Couldn't load this page: ${e.message}</div></div>`);
  }
  view.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

// library sections changed (a library added, a title moved): redraw the sidebar
async function refreshNav() {
  try {
    sections = (await api.get("/api/nav")).sections || [];
  } catch { return; }
  const active = document.querySelector("#side .nav-a[aria-current]");
  renderShell(me, active ? active.dataset.nav : null, sections);
}

async function boot() {
  try {
    const d = await api.get("/api/auth/me");
    if (!d.authenticated) { location.href = "/login"; return; }
    me = d.user;
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  try {
    sections = (await api.get("/api/nav")).sections || [];
  } catch { sections = []; }
  renderShell(me, "home", sections);
  initPalette(me);
  window.addEventListener("hashchange", route);
  if (me.role === "admin" && !location.hash.startsWith("#/setup")
      && !sessionStorage.getItem("faucet-setup-later")) {
    try {
      const s = await api.get("/api/setup/state");
      if (!s.complete) { location.hash = "#/setup"; return; }   // hashchange routes it
    } catch { /* never block the app on this */ }
  }
  route();
}

boot();
