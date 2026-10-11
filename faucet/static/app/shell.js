// Sidebar + top bar. Members see Library and Browse; admins also Manage.
import { api } from "./api.js";
import { $, html, mount, rate } from "./dom.js";
import { icons } from "./icons.js";
import { links } from "./links.js";
import { open as openPalette } from "./ui/palette.js";

const NAV = [
  ["Library", [
    ["home", "Home", icons.home, links.home],
    ["shows", "TV Shows", icons.tv, links.shows],
    ["movies", "Movies", icons.film, links.movies],
  ]],
  ["Browse", [
    ["discover", "Discover", icons.compass, links.discover],
    ["calendar", "Calendar", icons.calendar, links.calendar],
    ["requests", "Requests", icons.inbox, links.requests],
  ]],
  ["Manage", [
    ["activity", "Activity", icons.download, links.activity],
    ["wanted", "Wanted", icons.search, links.wanted],
    ["indexers", "Indexers", icons.server, links.indexers],
    ["users", "Users", icons.users, links.users],
    ["settings", "Settings", icons.gear, links.settings],
  ], "admin"],
];

function libraryGroup(sections) {
  // one entry per library (Plex-style); without sections, the two classics
  if (!sections || !sections.length) return NAV[0];
  const items = [["home", "Home", icons.home, links.home]];
  for (const s of sections) {
    items.push([`lib-${s.id}`, s.name, s.kind === "tv" ? icons.tv : icons.film, links.section(s.kind, s.id), s.count]);
  }
  return ["Library", items];
}

export function renderShell(me, active, sections = []) {
  const admin = me.role === "admin";
  const groups = [libraryGroup(sections), ...NAV.slice(1)].filter(([, , who]) => !who || admin);
  mount($("#side"), html`
    <a class="brand" href="#/">${icons.drop}<b>Faucet</b></a>
    ${groups.map(([label, items]) => html`
      <h2>${label}</h2>
      <nav aria-label="${label}">${items.map(([id, name, icon, href, count]) => html`
        <a class="nav-a" href="${href}" data-nav="${id}"${id === active ? html` aria-current="page"` : ""}>
          ${icon}<span class="nav-name">${name}</span><span class="slot" data-slot="${id}">${count != null ? html`<span class="count">${count}</span>` : ""}</span>
        </a>`)}</nav>`)}
    <div class="me">
      <a class="me-link" href="#/account" title="Account & security">
        <span class="avatar" aria-hidden="true">${(me.username || "?").slice(0, 1).toUpperCase()}</span>
        <span class="who"><span>${me.username}</span><span>${admin ? "Admin" : "Member"}</span></span></a>
      <a href="#" data-logout>Sign out</a>
    </div>`);
  $("#side [data-logout]").addEventListener("click", async (e) => {
    e.preventDefault();
    try { await api.post("/api/auth/logout"); } finally { location.href = "/login"; }
  });

  mount($("#topbar"), html`
    <button class="menu-btn" type="button" aria-label="Menu" aria-expanded="false">${icons.menu}</button>
    <button class="search-trigger" type="button">${icons.search}
      <span>Search your library, TMDb${admin ? " and releases" : ""}</span><kbd>⌘K</kbd></button>
    ${admin ? html`<a class="activity-pill" href="${links.activity}" id="act-pill">
      <span class="dot idle"></span><span class="lbl">Activity</span></a>` : ""}`);
  $("#topbar .search-trigger").addEventListener("click", () => openPalette(me));
  const app = $(".app");
  const menu = $("#topbar .menu-btn");
  menu.addEventListener("click", () => {
    const openNow = !app.classList.contains("menu-open");
    app.classList.toggle("menu-open", openNow);
    menu.setAttribute("aria-expanded", String(openNow));
  });
  $("#side").addEventListener("click", (e) => {
    if (e.target.closest("a")) app.classList.remove("menu-open");
  });
}

export function setActive(id) {
  document.querySelectorAll("#side .nav-a").forEach((a) => {
    if (a.dataset.nav === id) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

// live bits: download pill, sidebar counters
export function updateStatus(stats) {
  const pill = $("#act-pill");
  if (pill && stats) {
    const dot = pill.querySelector(".dot");
    dot.className = `dot ${!stats.client_ok ? "bad" : stats.downloading ? "" : "idle"}`;
    pill.querySelector(".lbl").textContent = !stats.client_ok ? "Client offline"
      : stats.downloading ? `${stats.downloading} downloading · ${rate(stats.down_rate)}` : "Nothing downloading";
  }
  const slot = (id, markup) => { const el = $(`#side [data-slot="${id}"]`); if (el) mount(el, markup); };
  if (stats) {
    slot("activity", stats.downloading ? html`<span class="pill">${stats.downloading}</span>` : "");
    slot("wanted", html`<span class="count">${stats.wanted}</span>`);
  }
}
