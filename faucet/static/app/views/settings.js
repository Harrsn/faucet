// Settings: a section list and the section's page.
import { html, mount } from "../dom.js";

export const title = "Settings";

const SECTIONS = [
  ["libraries", "Libraries", () => import("./settings/libraries.js")],
  ["integrations", "Integrations", () => import("./settings/integrations.js")],
  ["paths", "Path mappings", () => import("./settings/paths.js")],
  ["general", "General", () => import("./settings/general.js")],
  ["profiles", "Quality profiles", () => import("./settings/profiles.js")],
  ["users", "Users & requests", () => import("./settings/users.js")],
  ["hunting", "Hunting", () => import("./settings/hunting.js")],
  ["maintenance", "Maintenance & backup", () => import("./settings/maintenance.js")],
];

export async function render(el, ctx) {
  const want = ctx.params[0] || "libraries";
  const sec = SECTIONS.find((s) => s[0] === want) || SECTIONS[0];
  mount(el, html`<div class="page settings">
    <h1>Settings</h1>
    <div class="settings-wrap">
      <nav class="settings-nav" aria-label="Settings sections">${SECTIONS.map(([id, label]) => html`
        <a href="#/settings/${id}"${id === sec[0] ? html` aria-current="page"` : ""}>${label}</a>`)}</nav>
      <section class="settings-body" data-body aria-label="${sec[1]}"></section>
    </div></div>`);
  const body = el.querySelector("[data-body]");
  const mod = await sec[2]();
  await mod.render(body, ctx);
}
