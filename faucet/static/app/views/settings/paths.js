// Settings → Path mappings: the download client's paths → Faucet's.
import { api, qs } from "../../api.js";
import { html, mount } from "../../dom.js";
import { icons } from "../../icons.js";
import { toast } from "../../ui/toast.js";

export async function render(el) {
  let rows = (await api.get("/api/path-mappings")).mappings.map((m) => ({ remote: m.remote, local: m.local }));
  if (!rows.length) rows = [{ remote: "", local: "" }];

  function draw() {
    mount(el, html`<div class="stack" style="max-width:900px">
      <div class="sec-intro"><div><h2>Path mappings</h2>
        <p class="muted">When the download client sees a folder under a different path than Faucet does — Transmission on the host writes to <span class="mono">/mnt/nas/torrents</span>, Faucet's container sees <span class="mono">/nas/torrents</span> — map one to the other. The completion hook uses these for paths it can't see; paths it can already see are left alone.</p></div></div>
      <div class="panel">
        <div class="pm-row h"><span>Client path</span><span></span><span>Faucet path</span><span></span></div>
        ${rows.map((r, i) => html`<div class="pm-row">
          <label class="sr" for="pr${i}">Client path</label><input id="pr${i}" class="field mono" data-r="${i}" value="${r.remote}" placeholder="/mnt/nas/torrents">
          <span class="muted" aria-hidden="true">→</span>
          <label class="sr" for="pl${i}">Faucet path</label><input id="pl${i}" class="field mono" data-l="${i}" value="${r.local}" placeholder="/nas/torrents">
          <button class="icon-btn" type="button" data-del="${i}" aria-label="Remove mapping">${icons.close}</button></div>`)}
        <div class="acts" style="margin-top:12px"><button class="btn line" type="button" data-addrow>Add mapping</button>
          <button class="btn" type="button" data-save>Save</button></div>
      </div>
      <div class="panel"><h3>Test a path</h3>
        <div class="nadd"><label class="sr" for="pm-t">A path as the client reports it</label>
          <input id="pm-t" class="field mono" placeholder="/mnt/nas/torrents/complete/Some.Show.S01E01">
          <button class="btn line" type="button" data-test>Translate</button></div>
        <div data-out class="muted" style="margin-top:10px"></div></div>
    </div>`);
    el.querySelectorAll("[data-r]").forEach((x) => x.addEventListener("input", () => { rows[+x.dataset.r].remote = x.value; }));
    el.querySelectorAll("[data-l]").forEach((x) => x.addEventListener("input", () => { rows[+x.dataset.l].local = x.value; }));
    el.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => { rows.splice(+b.dataset.del, 1); draw(); }));
    el.querySelector("[data-addrow]").addEventListener("click", () => { rows.push({ remote: "", local: "" }); draw(); });
    el.querySelector("[data-save]").addEventListener("click", async () => {
      const keep = rows.filter((r) => r.remote.trim() || r.local.trim());
      try {
        const r = await api.put("/api/path-mappings", { mappings: keep });
        rows = r.mappings.map((m) => ({ remote: m.remote, local: m.local }));
        if (!rows.length) rows = [{ remote: "", local: "" }];
        toast("Path mappings saved.");
        draw();
      } catch (err) { toast(err.message, "err"); }
    });
    el.querySelector("[data-test]").addEventListener("click", async () => {
      const p = el.querySelector("#pm-t").value.trim();
      if (!p) return;
      try {
        const r = await api.get(`/api/path-mappings/test?${qs({ path: p })}`);
        mount(el.querySelector("[data-out]"), html`<span class="mono">${r.local_path}</span> —
          ${r.local_path === r.client_path ? "no mapping applies" : "mapped"}${r.exists ? ", and Faucet can see it." : ", but Faucet can't see that path."}`);
      } catch (err) { toast(err.message, "err"); }
    });
  }

  draw();
}
