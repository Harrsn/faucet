// First-run setup: a guided pass over what Faucet needs — libraries, the
// download client (and path mapping), the indexer, TMDb — then import what's
// already on disk. Every step is the same API the Settings pages use, so
// anything set here can be changed there later.
import { api } from "../api.js";
import { html, mount, raw } from "../dom.js";
import { toast } from "../ui/toast.js";

export const title = "Setup";

const STEPS = [
  ["libraries", "Libraries"], ["client", "Download client"], ["paths", "Path mapping"],
  ["indexer", "Indexer"], ["tmdb", "TMDb"], ["import", "Import"], ["done", "Done"],
];
const CLIENTS = [["transmission", "Transmission"], ["qbittorrent", "qBittorrent"], ["deluge", "Deluge"]];

export async function render(el, ctx) {
  const step = STEPS.some((s) => s[0] === ctx.params[0]) ? ctx.params[0] : "libraries";
  const idx = STEPS.findIndex((s) => s[0] === step);
  const state = await api.get("/api/setup/state");
  const done = (id) => ({ libraries: state.steps.libraries, client: state.steps.client, indexer: state.steps.indexer,
                          tmdb: state.steps.tmdb, import: state.shows + state.movies > 0 }[id]);
  const go = (n) => { location.hash = `#/setup/${STEPS[n][0]}`; };

  mount(el, html`<div class="page wizard">
    <div class="stack"><h1>Set up Faucet</h1>
      <ol class="wiz-steps" aria-label="Setup steps">${STEPS.map(([id, label], i) => html`
        <li class="${done(id) ? "done" : ""}"${id === step ? raw(' aria-current="step"') : ""}><span class="n">${i + 1}</span>${label}</li>`)}</ol></div>
    <div class="stack" data-body></div>
    <div class="wiz-nav">
      ${idx > 0 ? html`<button class="btn line" type="button" data-prev>Back</button>` : ""}
      <span class="spacer"></span>
      ${step !== "done" ? html`<button class="btn line" type="button" data-later>Finish later</button>
        <button class="btn" type="button" data-next>${["paths", "tmdb", "import"].includes(step) ? "Next (or skip)" : "Next"}</button>` : ""}
    </div></div>`);
  const body = el.querySelector("[data-body]");
  if (el.querySelector("[data-prev]")) el.querySelector("[data-prev]").addEventListener("click", () => go(idx - 1));
  if (el.querySelector("[data-next]")) el.querySelector("[data-next]").addEventListener("click", () => go(idx + 1));
  if (el.querySelector("[data-later]")) el.querySelector("[data-later]").addEventListener("click", () => {
    sessionStorage.setItem("faucet-setup-later", "1");
    location.hash = "#/";
  });

  const intro = (h, p) => html`<div class="sec-intro"><div><h2>${h}</h2><p class="muted">${p}</p></div></div>`;

  if (step === "libraries") {
    const mod = await import("./settings/libraries.js");
    await mod.render(body, ctx);
    body.insertBefore(Object.assign(document.createElement("p"), {
      className: "muted",
      textContent: "Faucet starts with a TV and a Movies library under its default media folder. Point them at your real folders (Edit), or add more — anything the container can see.",
    }), body.firstChild);
  } else if (step === "paths") {
    const mod = await import("./settings/paths.js");
    await mod.render(body, ctx);
  } else if (step === "client") {
    await connForm(body, intro("Download client", "Where Faucet sends what it grabs. Test before saving."), [
      ["DOWNLOAD_CLIENT", "Client", "select", CLIENTS], ["CLIENT_URL", "RPC / Web UI URL", "text", "http://10.0.0.5:9091/transmission/rpc"],
      ["CLIENT_USER", "Username", "text", ""], ["CLIENT_PASS", "Password", "password", ""]], "client");
  } else if (step === "indexer") {
    await connForm(body, intro("Indexer", "Faucet searches every indexer configured in Jackett, in parallel. Paste Jackett's URL and API key."), [
      ["JACKETT_URL", "Jackett URL", "text", "http://10.0.0.5:9117"], ["JACKETT_API_KEY", "API key", "password", ""]], "indexer");
  } else if (step === "tmdb") {
    await connForm(body, intro("TMDb", "Posters, episode lists and matching come from TMDb. A free v3 API key is at themoviedb.org → Settings → API. Optional, but without it Faucet can't import your library or track episodes."), [
      ["tmdb_key", "API key (v3)", "password", ""]], "tmdb");
  } else if (step === "import") {
    importStep(body, intro);
  } else {
    mount(body, html`<div class="panel wiz-done"><h2 style="margin:0">You're set.</h2>
      <ul>
        <li>${state.libraries.length} librar${state.libraries.length === 1 ? "y" : "ies"}${state.steps.libraries ? "" : " (none reachable yet)"}</li>
        <li>Download client ${state.steps.client ? "connected" : "not set — Settings → Integrations"}</li>
        <li>Indexer ${state.steps.indexer ? "connected" : "not set — Settings → Integrations"}</li>
        <li>TMDb ${state.steps.tmdb ? "connected" : "not set"}</li>
        <li>${state.shows} shows and ${state.movies} movies monitored</li>
      </ul>
      <p class="muted" style="margin:0">The hunter runs every 30 minutes; anything missing gets searched on its own. Add titles with ⌘K or from Discover.</p>
      <div><button class="btn" type="button" data-finish>Go to Faucet</button></div></div>`);
    body.querySelector("[data-finish]").addEventListener("click", async () => {
      await api.post("/api/setup/complete");
      location.hash = "#/";
    });
  }
}

async function connForm(body, intro, fields, which) {
  const s = await api.get("/api/settings");
  const e = s.env;
  const saved = { CLIENT_PASS: e.CLIENT_PASS_SET, JACKETT_API_KEY: e.JACKETT_API_KEY_SET, tmdb_key: e.TMDB_KEY_SET };
  mount(body, html`${intro}<div class="panel stack">
    <div class="form2">${fields.map(([k, label, type, extra]) => html`<label class="fld">${label}
      ${type === "select" ? html`<select class="field" data-k="${k}">${extra.map(([v, l]) => html`<option value="${v}"${e[k] === v ? raw(" selected") : ""}>${l}</option>`)}</select>`
        : html`<input class="field" data-k="${k}" type="${type}" value="${type === "password" ? "" : e[k] || ""}"
            placeholder="${type === "password" ? (saved[k] ? "•••••• saved — type to replace" : "") : extra}" autocomplete="${type === "password" ? "new-password" : "off"}">`}</label>`)}</div>
    <div class="acts"><button class="btn line" type="button" data-test>Test</button><button class="btn" type="button" data-save>Save</button>
      <span class="istatus" data-st><span class="dot"></span>${which === "tmdb" && saved.tmdb_key || which !== "tmdb" && (which === "client" ? e.CLIENT_PASS_SET : e.JACKETT_API_KEY_SET) ? "saved" : "not set"}</span></div></div>`);
  const val = (k) => (body.querySelector(`[data-k="${k}"]`) || { value: "" }).value.trim();
  const st = (ok, m) => {
    const x = body.querySelector("[data-st]");
    x.className = `istatus ${ok === null ? "" : ok ? "ok" : "bad"}`;
    x.lastChild.textContent = m;
  };
  body.querySelector("[data-test]").addEventListener("click", async () => {
    st(null, "testing…");
    try {
      if (which === "tmdb") {
        const r = await api.post("/api/settings/test-tmdb", { key: val("tmdb_key") || null });
        st(r.ok, r.msg);
      } else if (which === "client") {
        const r = await api.post("/api/settings/test", { client_kind: val("DOWNLOAD_CLIENT"), client_url: val("CLIENT_URL"),
          client_user: val("CLIENT_USER"), client_pass: val("CLIENT_PASS") || null });
        st(r.client.ok, r.client.msg);
      } else {
        const r = await api.post("/api/settings/test", { jackett_url: val("JACKETT_URL"), jackett_api_key: val("JACKETT_API_KEY") || null });
        st(r.indexer.ok, r.indexer.msg);
      }
    } catch (err) { st(false, err.message); }
  });
  body.querySelector("[data-save]").addEventListener("click", async () => {
    const values = {};
    for (const [k] of fields) {
      const v = val(k);
      if (["CLIENT_PASS", "JACKETT_API_KEY", "tmdb_key"].includes(k) && !v) continue;
      values[k] = v;
    }
    try {
      const r = await api.patch("/api/settings", { values });
      (r.warnings || []).forEach((w) => toast(w));
      toast("Saved.");
      st(true, "saved");
    } catch (err) { toast(err.message, "err"); }
  });
}

function importStep(body, intro) {
  const draw = (s) => {
    const job = s.import;
    const r = job.result;
    mount(body, html`${intro("Import your library", "Faucet scans every library and matches each show and movie folder against TMDb, then monitors them: what's on disk counts as owned, what's missing gets hunted. Big libraries take a few minutes.")}
      <div class="panel stack">
        ${!s.steps.tmdb ? html`<p class="lib-warn" style="margin:0">Add a TMDb key first (previous step) — matching needs it.</p>` : ""}
        <p style="margin:0">${s.shows} shows and ${s.movies} movies monitored right now.</p>
        ${job.running ? html`<p class="muted" style="margin:0">Importing… started ${job.started ? job.started.slice(11) : ""}. You can leave this page; it keeps going.</p>`
          : r && r.error ? html`<p class="lib-warn" style="margin:0">${r.error}</p>`
          : r ? html`<p class="istatus ok" style="margin:0"><span class="dot"></span>Imported ${r.shows_imported} shows and ${r.movies_imported} movies${r.unmatched && r.unmatched.length ? ` · ${r.unmatched.length} folders couldn't be matched (link them in Settings → Libraries)` : ""}.</p>` : ""}
        <div><button class="btn" type="button" data-import${job.running || !s.steps.tmdb ? raw(" disabled") : ""}>${r ? "Import again" : "Import what's on disk"}</button></div>
      </div>`);
    const b = body.querySelector("[data-import]");
    b.addEventListener("click", async () => {
      try { await api.post("/api/setup/import"); poll(); } catch (err) { toast(err.message, "err"); }
    });
  };
  const poll = async () => {
    const s = await api.get("/api/setup/state");
    if (!body.isConnected) return;
    draw(s);
    if (s.import.running) setTimeout(poll, 3000);
  };
  poll();
}
