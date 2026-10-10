// Settings → Libraries: library cards (add / edit / remove / scan) and the
// show-folder links, where folders matching couldn't place get linked by hand.
import { api, qs } from "../../api.js";
import { ago, bg, html, mount, raw, tint } from "../../dom.js";
import { folderPicker } from "../../ui/folderpicker.js";
import { confirmDialog, openModal } from "../../ui/modal.js";
import { toast } from "../../ui/toast.js";

const KIND = { tv: "TV", movie: "Movies" };

export async function render(el) {
  let libs = [], folders = [], series = [], onlyUnlinked = true;

  async function load() {
    const [l, f, s] = await Promise.all([api.get("/api/libraries"),
      api.get("/api/libraries/folders"), api.get("/api/series")]);
    libs = l.libraries;
    folders = f.folders;
    series = s.series;
    draw();
  }

  function libCard(lib) {
    const warn = !lib.exists ? "Folder not reachable right now — the NAS may be unmounted. Its files are kept."
      : !lib.writable ? "Read-only: Faucet can scan it but can't file downloads into it." : "";
    return html`<article class="lib">
      <span class="lib-ico ${lib.kind}" aria-hidden="true">${lib.kind === "tv" ? "TV" : "MOV"}</span>
      <div class="lib-main">
        <div class="lib-h"><h3>${lib.name}</h3><span class="tagpill">${KIND[lib.kind]}</span>
          ${lib.is_default ? html`<span class="tagpill good">New downloads filed here</span>` : ""}
          ${lib.enabled ? "" : html`<span class="tagpill">Paused</span>`}</div>
        <span class="mono lib-path">${lib.path}</span>
        <span class="muted lib-stats">${lib.files} file${lib.files === 1 ? "" : "s"}${lib.last_scan ? ` · scanned ${ago(lib.last_scan)}` : " · not scanned yet"}</span>
        ${warn ? html`<span class="lib-warn">${warn}</span>` : ""}
      </div>
      <div class="acts"><button class="btn line" type="button" data-edit="${lib.id}">Edit</button>
        <button class="btn line" type="button" data-rm="${lib.id}">Remove</button></div>
    </article>`;
  }

  function folderRows() {
    const list = onlyUnlinked ? folders.filter((f) => !f.series_id) : folders;
    if (!list.length) {
      return html`<div class="empty">${onlyUnlinked ? "Every show folder is linked to a show." : "No show folders scanned yet."}</div>`;
    }
    return html`<div class="ftable">${list.map((f, i) => html`<div class="frow">
      <span class="fname"><b>${f.folder}</b><span class="muted">${f.library}</span></span>
      <span class="flink">${f.series_id ? html`→ ${f.series} <span class="tagpill">${f.manual ? "by hand" : "automatic"}</span>`
        : html`<span class="chip warn">Not linked</span>`}</span>
      <span class="acts"><button class="btn line" type="button" data-link="${i}">${f.series_id ? "Change" : "Link…"}</button>
        ${f.series_id ? html`<button class="btn line" type="button" data-unlink="${i}">Unlink</button>` : ""}</span>
    </div>`)}</div>`;
  }

  function draw() {
    const unlinked = folders.filter((f) => !f.series_id).length;
    mount(el, html`<div class="stack">
      <div class="sec-intro">
        <div><h2>Libraries</h2>
          <p class="muted">Folders Faucet scans for what you own and files new downloads into. Anything the container can see can be a library; libraries can't sit inside each other.</p></div>
        <div class="acts"><button class="btn line" type="button" data-scan>Scan all</button>
          <button class="btn" type="button" data-add>Add library</button></div>
      </div>
      ${libs.map(libCard)}
      <div class="sec-intro" style="margin-top:18px">
        <div><h2>Show folders</h2>
          <p class="muted">Each show folder is linked to a show once — automatically when the names agree, or by hand here. A link survives renames on TMDb and wins over name matching.</p></div>
        <div class="seg" role="group" aria-label="Which folders">
          <button type="button" data-only="1" aria-pressed="${onlyUnlinked}">Not linked<span class="n">${unlinked}</span></button>
          <button type="button" data-only="0" aria-pressed="${!onlyUnlinked}">All<span class="n">${folders.length}</span></button></div>
      </div>
      <div data-folders>${folderRows()}</div>
    </div>`);
    wire();
  }

  function wire() {
    el.querySelector("[data-add]").addEventListener("click", () => editLibrary(null));
    el.querySelector("[data-scan]").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        const r = await api.post("/api/libraries/scan");
        toast(r.started ? "Scanning every library in the background." : "A scan is already running.");
        setTimeout(load, 4000);
      } catch (err) { toast(err.message, "err"); }
      e.target.disabled = false;
    });
    el.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () =>
      editLibrary(libs.find((l) => l.id === Number(b.dataset.edit)))));
    el.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", async () => {
      const lib = libs.find((l) => l.id === Number(b.dataset.rm));
      if (!await confirmDialog(`Remove ${lib.name}?`, `Faucet stops scanning ${lib.path}. Files are never touched; what it recorded from that folder is forgotten, so shows there stop counting as owned.`, "Remove library", true)) return;
      try { await api.del(`/api/libraries/${lib.id}`); toast(`Removed ${lib.name}.`); load(); }
      catch (err) { toast(err.message, "err"); }
    }));
    el.querySelectorAll("[data-only]").forEach((b) => b.addEventListener("click", () => {
      onlyUnlinked = b.dataset.only === "1";
      draw();
    }));
    const list = () => (onlyUnlinked ? folders.filter((f) => !f.series_id) : folders);
    el.querySelectorAll("[data-link]").forEach((b) => b.addEventListener("click", () => linkFolder(list()[Number(b.dataset.link)])));
    el.querySelectorAll("[data-unlink]").forEach((b) => b.addEventListener("click", async () => {
      const f = list()[Number(b.dataset.unlink)];
      try {
        await api.put(`/api/libraries/${f.library_id}/links`, { folder: f.folder, series_id: null });
        toast(`Unlinked ${f.folder}.`);
        load();
      } catch (err) { toast(err.message, "err"); }
    }));
  }

  function editLibrary(lib) {
    let kind = lib ? lib.kind : "tv";
    let path = lib ? lib.path : "";
    const m = openModal({
      title: lib ? `Edit ${lib.name}` : "Add library",
      wide: true,
      body: html`<div class="stack">
        ${lib ? "" : html`<div class="seg" role="radiogroup" aria-label="Library type">
          <button type="button" role="radio" data-kind="tv" aria-pressed="true">TV shows</button>
          <button type="button" role="radio" data-kind="movie" aria-pressed="false">Movies</button></div>`}
        <label class="fld">Name<input class="field" data-name value="${lib ? lib.name : ""}" placeholder="${lib ? "" : "TV Shows"}" autocomplete="off"></label>
        <div class="fld">Folder<div data-picker></div>
          <span class="mono picked" data-picked>${path || "No folder chosen yet"}</span></div>
        <label class="check"><input type="checkbox" data-default${lib && lib.is_default ? raw(" checked") : ""}>
          <span>File new <span data-kindnoun>${kind === "tv" ? "TV" : "movie"}</span> downloads into this library</span></label>
        ${lib ? html`<label class="check"><input type="checkbox" data-enabled${lib.enabled ? raw(" checked") : ""}><span>Scan this library</span></label>` : ""}
        <div class="acts" style="justify-content:flex-end"><button class="btn line" type="button" data-cancel>Cancel</button>
          <button class="btn" type="button" data-save>${lib ? "Save" : "Add & scan"}</button></div>
      </div>`,
    });
    const b = m.body;
    folderPicker(b.querySelector("[data-picker]"), {
      start: path,
      onPick: (p) => { path = p; b.querySelector("[data-picked]").textContent = p; },
    });
    b.querySelectorAll("[data-kind]").forEach((x) => x.addEventListener("click", () => {
      kind = x.dataset.kind;
      b.querySelectorAll("[data-kind]").forEach((y) => y.setAttribute("aria-pressed", String(y === x)));
      b.querySelector("[data-kindnoun]").textContent = kind === "tv" ? "TV" : "movie";
    }));
    b.querySelector("[data-cancel]").addEventListener("click", () => m.close());
    b.querySelector("[data-save]").addEventListener("click", async (e) => {
      if (!path) { toast("Choose a folder first.", "err"); return; }
      const name = b.querySelector("[data-name]").value.trim();
      const isDefault = b.querySelector("[data-default]").checked;
      e.target.disabled = true;
      try {
        const r = lib
          ? await api.patch(`/api/libraries/${lib.id}`, { name, path, is_default: isDefault || null,
              enabled: b.querySelector("[data-enabled]").checked })
          : await api.post("/api/libraries", { name, kind, path, is_default: isDefault });
        (r.warnings || []).forEach((w) => toast(w));
        if (!lib) await api.post("/api/libraries/scan");
        toast(lib ? "Library saved." : "Library added — scanning it now.");
        m.close();
        load();
      } catch (err) {
        toast(err.message, "err");
        e.target.disabled = false;
      }
    });
  }

  function linkFolder(f) {
    const m = openModal({
      title: `Link “${f.folder}”`,
      sub: `${f.library} library`,
      body: html`<div class="stack">
        <label class="sr" for="lk-q">Find a show</label>
        <input id="lk-q" class="field" placeholder="Filter your shows, or search TMDb" value="${f.folder.replace(/\s*[[(].*$/, "")}" autocomplete="off">
        <div data-mine></div><div data-tmdb></div></div>`,
    });
    const input = m.body.querySelector("#lk-q");
    let timer = null, seq = 0;
    const link = async (sid, label) => {
      try {
        await api.put(`/api/libraries/${f.library_id}/links`, { folder: f.folder, series_id: sid });
        toast(`Linked ${f.folder} → ${label}.`);
        m.close();
        load();
      } catch (err) { toast(err.message, "err"); }
    };
    const drawMine = () => {
      const q = input.value.trim().toLowerCase();
      const words = q.split(/\s+/).filter(Boolean);
      const mine = series.filter((s) => words.every((w) => s.title.toLowerCase().includes(w))).slice(0, 8);
      mount(m.body.querySelector("[data-mine]"), html`<div class="grp-label">Your shows</div>
        ${mine.length ? mine.map((s) => html`<button class="pick" type="button" data-sid="${s.id}">
          <span class="thumb" style="${s.poster ? bg(s.poster) : tint(s.title)}"></span>
          <span class="main"><b>${s.title}${s.year ? ` (${s.year})` : ""}</b><span>${s.have}/${s.total} on disk</span></span></button>`)
          : html`<div class="empty">None of your shows match.</div>`}`);
      m.body.querySelectorAll("[data-sid]").forEach((b) => b.addEventListener("click", () => {
        const s = series.find((x) => x.id === Number(b.dataset.sid));
        link(s.id, s.title);
      }));
    };
    const searchTmdb = async () => {
      const q = input.value.trim();
      const box = m.body.querySelector("[data-tmdb]");
      if (q.length < 2) { mount(box, ""); return; }
      const my = ++seq;
      let items = [];
      try { items = (await api.get(`/api/admin/match-search?${qs({ q, kind: "show" })}`)).results || []; }
      catch { /* TMDb optional */ }
      if (my !== seq) return;
      const owned = new Set(series.map((s) => s.tmdb_id));
      items = items.filter((x) => !owned.has(x.tmdb_id)).slice(0, 5);
      mount(box, items.length ? html`<div class="grp-label">Not in Faucet yet — monitor and link</div>
        ${items.map((x, i) => html`<button class="pick" type="button" data-t="${i}">
          <span class="thumb" style="${x.poster ? bg(x.poster) : tint(x.title)}"></span>
          <span class="main"><b>${x.title}${x.year ? ` (${x.year})` : ""}</b><span>${x.overview || ""}</span></span></button>`)}` : "");
      box.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", async () => {
        const x = items[Number(b.dataset.t)];
        b.disabled = true;
        try {
          const r = await api.post("/api/series", { tmdb_id: x.tmdb_id, title: x.title, year: x.year, poster: x.poster });
          await link(r.id, x.title);
        } catch (err) { toast(err.message, "err"); b.disabled = false; }
      }));
    };
    input.addEventListener("input", () => { drawMine(); clearTimeout(timer); timer = setTimeout(searchTmdb, 350); });
    drawMine();
    searchTmdb();
  }

  await load();
}
