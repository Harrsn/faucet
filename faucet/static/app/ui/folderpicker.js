// Folder picker for libraries: browses the container's mounts (directories
// only), marks folders that are or sit inside a library, and reports the
// chosen path through onPick.
import { api, qs } from "../api.js";
import { html, mount } from "../dom.js";
import { icons } from "../icons.js";

export function folderPicker(box, { start = "", onPick }) {
  let cur = null;
  let selected = start || "";

  async function go(path) {
    mount(box, html`<div class="fp"><div class="fp-bar mono">Loading…</div></div>`);
    let d;
    try {
      d = await api.get(`/api/libraries/browse?${qs({ path })}`);
    } catch (e) {
      if (path) return go("");                   // a stale start path: back to the roots
      mount(box, html`<div class="empty">${e.message}</div>`);
      return;
    }
    cur = d;
    const crumbs = [];
    if (d.path) {
      const root = d.roots.find((r) => d.path === r || d.path.startsWith(`${r}/`)) || "/";
      crumbs.push([root, root]);
      let acc = root;
      for (const part of d.path.slice(root.length).split("/").filter(Boolean)) {
        acc = `${acc}/${part}`;
        crumbs.push([part, acc]);
      }
    }
    mount(box, html`<div class="fp">
      <div class="fp-bar">
        <button type="button" class="fp-crumb" data-go="">Mounts</button>
        ${crumbs.map(([label, p]) => html`<span class="sep" aria-hidden="true">›</span>
          <button type="button" class="fp-crumb" data-go="${p}">${label}</button>`)}
      </div>
      <div class="fp-list" role="listbox" aria-label="Folders">
        ${d.parent ? html`<button type="button" class="fp-row" data-go="${d.parent}">${icons.left}<span>Up</span></button>` : ""}
        ${d.dirs.length ? d.dirs.map((x) => {
          const taken = x.library || x.in_library;
          return html`<div class="fp-row${selected === x.path ? " on" : ""}">
            <button type="button" class="fp-open" data-go="${x.path}">${icons.folder}<span>${x.name}</span>
              ${taken ? html`<span class="fp-note">${x.library ? `${x.library} library` : `inside ${x.in_library}`}</span>` : ""}</button>
            ${taken ? "" : html`<button type="button" class="btn line fp-pick" data-pick="${x.path}">Choose</button>`}
          </div>`;
        }) : html`<div class="empty">No folders here.</div>`}
      </div>
      ${d.path ? html`<div class="fp-foot"><span class="mono">${d.path}</span>
        <button type="button" class="btn line" data-pick="${d.path}">Use this folder</button></div>` : ""}
    </div>`);
    box.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));
    box.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => {
      selected = b.dataset.pick;
      onPick(selected);
      go(cur.path || "");
    }));
  }

  const startDir = start ? start.replace(/\/[^/]*$/, "") || start : "";
  go(startDir);
}
