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
  [/^\/shows\/(\d+)(?:\/(\d+))?\/?$/, "shows", () => import("./views/show.js")],
  [/^\/movies\/?$/, "movies", () => import("./views/movies.js")],
  [/^\/movies\/(\d+)\/?$/, "movies", () => import("./views/movie.js")],
];

let me = null;
let current = { stop() {} };

function match(path) {
  for (const [re, nav, load] of ROUTES) {
    const m = path.match(re);
    if (m) return { nav, load, params: m.slice(1) };
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
  const ctx = { me, params: r.params, status: updateStatus };
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

async function boot() {
  try {
    const d = await api.get("/api/auth/me");
    if (!d.authenticated) { location.href = "/login"; return; }
    me = d.user;
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  renderShell(me, "home");
  initPalette(me);
  window.addEventListener("hashchange", route);
  route();
}

boot();
