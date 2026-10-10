// Settings: a section list and the section's page. Sections still in the
// classic UI say so and link there.
import { html, mount } from "../dom.js";
import { icons } from "../icons.js";

export const title = "Settings";

const SECTIONS = [
  ["libraries", "Libraries", () => import("./settings/libraries.js")],
  ["integrations", "Integrations", () => import("./settings/integrations.js")],
  ["paths", "Path mappings", () => import("./settings/paths.js")],
  ["general", "General", null, "/classic#settings"],
  ["profiles", "Quality profiles", null, "/classic#page/profiles"],
  ["users", "Users & requests", null, "/classic#users"],
  ["hunting", "Hunting", null, null],
  ["maintenance", "Maintenance & backup", null, "/classic#settings"],
];

export async function render(el, ctx) {
  const want = ctx.params[0] || "libraries";
  const sec = SECTIONS.find((s) => s[0] === want) || SECTIONS[0];
  mount(el, html`<div class="page settings">
    <h1>Settings</h1>
    <div class="settings-wrap">
      <nav class="settings-nav" aria-label="Settings sections">${SECTIONS.map(([id, label, load, classic]) => html`
        <a href="#/settings/${id}"${id === sec[0] ? html` aria-current="page"` : ""}>${label}
          ${!load && classic ? html`<span class="ext" title="Still in the classic UI">${icons.external}</span>` : ""}</a>`)}</nav>
      <section class="settings-body" data-body aria-label="${sec[1]}"></section>
    </div></div>`);
  const body = el.querySelector("[data-body]");
  if (sec[2]) {
    const mod = await sec[2]();
    await mod.render(body, ctx);
  } else {
    mount(body, html`<div class="classic-card"><h2>${sec[1]}</h2>
      ${sec[3] ? html`<p>This section moves to the new UI in the next update. Until then it lives in the classic UI.</p>
        <div><a class="btn" href="${sec[3]}">Open in classic UI</a></div>`
        : html`<p>Hunting settings (download cap, searches per tick, backoff, stall handling, season packs) are environment variables today. They get a page here in the next update.</p>`}
    </div>`);
  }
}
