// Settings → Integrations: everything Faucet talks to, each with its live
// status, a Test button and a Save button. Secrets are write-only: the page
// only learns whether one is set.
import { api } from "../../api.js";
import { html, mount, raw, stamp } from "../../dom.js";
import { toast } from "../../ui/toast.js";

const CLIENTS = [["transmission", "Transmission"], ["qbittorrent", "qBittorrent"], ["deluge", "Deluge"]];

export async function render(el) {
  let s, idx, health, notif;

  async function load() {
    [s, idx, health, notif] = await Promise.all([
      api.get("/api/settings"), api.get("/api/indexers").catch(() => ({ indexers: [] })),
      api.get("/api/indexers/health").catch(() => null), api.get("/api/settings/notifications")]);
    draw();
  }

  const status = (ok, msg) => html`<span class="istatus ${ok === null ? "" : ok ? "ok" : "bad"}" data-status>
    <span class="dot"></span>${msg}</span>`;

  function healthTable() {
    if (!health) return "";
    const per = (health.search && health.search.indexers) || [];
    const probe = health.probe && health.probe.indexers ? Object.fromEntries(health.probe.indexers.map((p) => [p.id, p])) : {};
    if (!per.length && !Object.keys(probe).length) return html`<p class="muted">No searches in the last 24 hours yet.</p>`;
    const names = [...new Set([...per.map((x) => x.indexer), ...Object.keys(probe)])];
    return html`<div class="htable">
      <div class="hrow h"><span>Indexer</span><span>Probe</span><span>Searches</span><span>Avg</span><span>Results</span></div>
      ${names.map((n) => {
        const x = per.find((p) => p.indexer === n) || {};
        const p = probe[n];
        const dead = x.searches >= 5 && !x.results;
        return html`<div class="hrow"><span class="mono">${p && p.name ? p.name : n}</span>
          <span>${p ? html`<span class="istatus ${p.ok ? "ok" : "bad"}" title="${p.error || ""}"><span class="dot"></span>${p.ok ? `${p.results} in ${(p.ms / 1000).toFixed(1)}s` : (p.error || "failed").slice(0, 60)}</span>` : html`<span class="muted">—</span>`}</span>
          <span class="mono">${x.searches || 0}${x.failed ? html` <span style="color:var(--bad-text)">(${x.failed} failed)</span>` : ""}</span>
          <span class="mono">${x.avg_ms != null ? (x.avg_ms >= 1000 ? `${(x.avg_ms / 1000).toFixed(1)}s` : `${x.avg_ms}ms`) : "—"}</span>
          <span>${dead ? html`<span class="chip warn">never returns anything</span>` : html`<span class="mono">${x.results || 0}</span>`}</span></div>`;
      })}</div>
      <p class="muted" style="font-size:12.5px;margin:8px 0 0">Searches, average time and results are the last 24 hours.${health.probe ? ` Last probe ${stamp(health.probe.ts)}.` : ""}</p>`;
  }

  function draw() {
    const e = s.env;
    mount(el, html`<div class="stack">
      <div class="sec-intro"><div><h2>Integrations</h2>
        <p class="muted">Everything Faucet talks to. Keys and passwords are never shown again once saved — leave a field blank to keep what's there.</p></div></div>

      <article class="panel integ" data-card="jackett">
        <div class="integ-h"><h3>Indexers · Jackett</h3><span data-st>${status(null, "not tested")}</span></div>
        <div class="form2">
          <label class="fld">Jackett URL<input class="field" data-k="JACKETT_URL" value="${e.JACKETT_URL || ""}" placeholder="http://10.0.0.17:9117"></label>
          <label class="fld">API key<input class="field" type="password" data-k="JACKETT_API_KEY" autocomplete="new-password"
            placeholder="${e.JACKETT_API_KEY_SET ? "•••••• saved — type to replace" : "Paste from Jackett's dashboard"}"></label>
          <label class="fld">Indexers to search<select class="field" data-k="JACKETT_INDEXER">
            <option value="all"${(e.JACKETT_INDEXER || "all") === "all" ? raw(" selected") : ""}>Every configured indexer (in parallel)</option>
            ${(idx.indexers || []).map((x) => html`<option value="${x.id}"${e.JACKETT_INDEXER === x.id ? raw(" selected") : ""}>Only ${x.name}</option>`)}
          </select></label>
        </div>
        <div class="acts"><button class="btn line" type="button" data-test="jackett">Test</button>
          <button class="btn" type="button" data-save="jackett">Save</button></div>
        <div class="integ-sub"><div class="integ-h"><h4>Indexer health</h4>
          <button class="btn line" type="button" data-probe${health && health.probing ? raw(" disabled") : ""}>${health && health.probing ? "Probing…" : "Probe now"}</button></div>
          ${healthTable()}</div>
      </article>

      <article class="panel integ" data-card="client">
        <div class="integ-h"><h3>Download client</h3><span data-st>${status(null, "not tested")}</span></div>
        <div class="form2">
          <label class="fld">Client<select class="field" data-k="DOWNLOAD_CLIENT">
            ${CLIENTS.map(([v, l]) => html`<option value="${v}"${e.DOWNLOAD_CLIENT === v ? raw(" selected") : ""}>${l}</option>`)}</select></label>
          <label class="fld">RPC / Web UI URL<input class="field" data-k="CLIENT_URL" value="${e.CLIENT_URL || ""}" placeholder="http://10.0.0.17:9091/transmission/rpc"></label>
          <label class="fld">Username<input class="field" data-k="CLIENT_USER" value="${e.CLIENT_USER || ""}" autocomplete="off"></label>
          <label class="fld">Password<input class="field" type="password" data-k="CLIENT_PASS" autocomplete="new-password"
            placeholder="${e.CLIENT_PASS_SET ? "•••••• saved — type to replace" : "Client password"}"></label>
        </div>
        <div class="acts"><button class="btn line" type="button" data-test="client">Test</button>
          <button class="btn" type="button" data-save="client">Save</button></div>
      </article>

      <article class="panel integ" data-card="tmdb">
        <div class="integ-h"><h3>Metadata · TMDb</h3><span data-st>${status(e.TMDB_KEY_SET ? null : false, e.TMDB_KEY_SET ? "key saved" : "no key — posters and episode lists are off")}</span></div>
        <div class="form2">
          <label class="fld">API key (v3)<input class="field" type="password" data-k="tmdb_key" autocomplete="new-password"
            placeholder="${e.TMDB_KEY_SET ? "•••••• saved — type to replace" : "From themoviedb.org → Settings → API"}"></label>
        </div>
        <div class="acts"><button class="btn line" type="button" data-test="tmdb">Test</button>
          <button class="btn" type="button" data-save="tmdb">Save</button></div>
      </article>

      <article class="panel integ" data-card="notify">
        <div class="integ-h"><h3>Notifications</h3><span class="muted">${notif.targets.length} target${notif.targets.length === 1 ? "" : "s"}</span></div>
        <div class="ntargets">${notif.targets.length ? notif.targets.map((t) => html`<div class="ntarget" data-i="${t.i}">
            <span class="tagpill">${t.kind}</span><span class="mono">${t.display}</span>
            <span class="acts"><button class="btn line" type="button" data-ntest="${t.i}">Send test</button>
              <button class="btn line" type="button" data-nrm="${t.i}">Remove</button></span></div>`)
          : html`<p class="muted" style="margin:0">No targets yet.</p>`}</div>
        <div class="nadd"><label class="sr" for="n-url">New target URL</label>
          <input id="n-url" class="field" placeholder="https://discord.com/api/webhooks/…  ·  ntfy://ntfy.sh/topic  ·  telegram://token@chat" autocomplete="off">
          <button class="btn line" type="button" data-nadd>Add</button></div>
        <fieldset class="nevents"><legend class="muted">Notify when a release is…</legend>
          ${notif.events.map((ev) => html`<label class="check"><input type="checkbox" data-ev="${ev}"${notif.notify_on.includes(ev) ? raw(" checked") : ""}><span>${ev}</span></label>`)}
        </fieldset>
        <div class="acts"><button class="btn" type="button" data-nsave>Save events</button></div>
      </article>

      <article class="panel integ dim">
        <div class="integ-h"><h3>Media server · Plex</h3><span class="muted">coming later</span></div>
        <p class="muted" style="margin:0">Library refreshes after a download lands and “recently added” from Plex are planned for a later phase.</p>
      </article>
    </div>`);
    wire();
  }

  const val = (card, k) => {
    const x = el.querySelector(`[data-card="${card}"] [data-k="${k}"]`);
    return x ? x.value.trim() : "";
  };
  const setStatus = (card, ok, msg) => mount(el.querySelector(`[data-card="${card}"] [data-st]`), status(ok, msg));

  function wire() {
    el.querySelector('[data-test="jackett"]').addEventListener("click", async () => {
      setStatus("jackett", null, "testing…");
      const r = await api.post("/api/settings/test", { jackett_url: val("jackett", "JACKETT_URL"),
        jackett_api_key: val("jackett", "JACKETT_API_KEY") || null, jackett_indexer: val("jackett", "JACKETT_INDEXER") });
      setStatus("jackett", r.indexer.ok, r.indexer.msg);
    });
    el.querySelector('[data-test="client"]').addEventListener("click", async () => {
      setStatus("client", null, "testing…");
      const r = await api.post("/api/settings/test", { client_kind: val("client", "DOWNLOAD_CLIENT"),
        client_url: val("client", "CLIENT_URL"), client_user: val("client", "CLIENT_USER"),
        client_pass: val("client", "CLIENT_PASS") || null });
      setStatus("client", r.client.ok, r.client.msg);
    });
    el.querySelector('[data-test="tmdb"]').addEventListener("click", async () => {
      setStatus("tmdb", null, "testing…");
      const r = await api.post("/api/settings/test-tmdb", { key: val("tmdb", "tmdb_key") || null });
      setStatus("tmdb", r.ok, r.msg);
    });
    const save = async (card, keys) => {
      const values = {};
      for (const k of keys) {
        const v = val(card, k);
        const secret = ["JACKETT_API_KEY", "CLIENT_PASS", "tmdb_key"].includes(k);
        if (secret && !v) continue;                   // blank secret = keep the saved one
        values[k] = v;
      }
      try {
        const r = await api.patch("/api/settings", { values });
        (r.warnings || []).forEach((w) => toast(w));
        toast("Saved.");
        load();
      } catch (err) { toast(err.message, "err"); }
    };
    el.querySelector('[data-save="jackett"]').addEventListener("click", () => save("jackett", ["JACKETT_URL", "JACKETT_API_KEY", "JACKETT_INDEXER"]));
    el.querySelector('[data-save="client"]').addEventListener("click", () => save("client", ["DOWNLOAD_CLIENT", "CLIENT_URL", "CLIENT_USER", "CLIENT_PASS"]));
    el.querySelector('[data-save="tmdb"]').addEventListener("click", () => save("tmdb", ["tmdb_key"]));

    const probe = el.querySelector("[data-probe]");
    probe.addEventListener("click", async () => {
      probe.disabled = true;
      probe.textContent = "Probing…";
      await api.post("/api/indexers/probe");
      const poll = async () => {
        health = await api.get("/api/indexers/health");
        if (health.probing && el.isConnected) setTimeout(poll, 3000);
        else if (el.isConnected) draw();
      };
      setTimeout(poll, 3000);
    });

    const putTargets = async (keep, add, notifyOn) => {
      try {
        notif = await api.put("/api/settings/notifications", { keep, add, notify_on: notifyOn });
        draw();
        return true;
      } catch (err) { toast(err.message, "err"); return false; }
    };
    const keepAll = () => notif.targets.map((t) => t.i);
    el.querySelectorAll("[data-ntest]").forEach((b) => b.addEventListener("click", async () => {
      const r = await api.post("/api/settings/notifications/test", { i: Number(b.dataset.ntest) });
      toast(r.msg, r.ok ? "" : "err");
    }));
    el.querySelectorAll("[data-nrm]").forEach((b) => b.addEventListener("click", async () => {
      if (await putTargets(keepAll().filter((i) => i !== Number(b.dataset.nrm)), [], null)) toast("Target removed.");
    }));
    el.querySelector("[data-nadd]").addEventListener("click", async () => {
      const url = el.querySelector("#n-url").value.trim();
      if (!url) return;
      const t = await api.post("/api/settings/notifications/test", { url }).catch((err) => ({ ok: false, msg: err.message }));
      if (!t.ok) { toast(`Test failed — not added: ${t.msg}`, "err"); return; }
      if (await putTargets(keepAll(), [url], null)) toast("Target added and a test message sent.");
    });
    el.querySelector("[data-nsave]").addEventListener("click", async () => {
      const on = [...el.querySelectorAll("[data-ev]")].filter((c) => c.checked).map((c) => c.dataset.ev);
      if (await putTargets(keepAll(), [], on)) toast("Notification events saved.");
    });
  }

  await load();
}
