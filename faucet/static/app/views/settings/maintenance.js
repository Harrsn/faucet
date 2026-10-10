// Settings → Maintenance & backup: database, duplicate files, retention.
import { api } from "../../api.js";
import { bytes, html, mount } from "../../dom.js";
import { confirmDialog } from "../../ui/modal.js";
import { tunablesForm } from "../../ui/tunables.js";
import { toast } from "../../ui/toast.js";

export async function render(el) {
  const [st, dup] = await Promise.all([api.get("/api/settings/storage"), api.get("/api/library/duplicates")]);
  const largest = (st.tables || []).slice(0, 3).map((t) => `${t.name} ${bytes(t.bytes)}`).join(" · ");
  mount(el, html`<div class="stack">
    <div class="sec-intro"><div><h2>Maintenance & backup</h2>
      <p class="muted">Faucet prunes old history, rotates its event log and compacts the database on its own; these are the knobs and the manual jobs.</p></div></div>
    <div class="panel"><div class="integ-h"><h3>Database</h3><a class="btn line" href="/api/settings/backup" download>Download backup</a></div>
      <dl class="kv" style="margin-top:12px"><dt>Size</dt><dd>${bytes(st.bytes)}${st.free_pct != null ? ` · ${st.free_pct}% free pages` : ""}</dd>
        ${largest ? html`<dt>Largest</dt><dd class="mono">${largest}</dd>` : ""}</dl>
      <p class="muted" style="margin:12px 0 0;font-size:13px">The backup is a consistent copy without sessions or reset tokens. To restore: stop Faucet, replace <span class="mono">cascade.db</span> in the config volume, start it. It contains your saved keys and passwords — keep it private.</p></div>
    <div class="panel"><div class="integ-h"><h3>Duplicate files</h3>
      ${dup.count ? html`<button class="btn line" type="button" data-retire>Retire all ${dup.count} (${bytes(dup.bytes)})</button>` : ""}</div>
      ${dup.count ? html`<p class="muted" style="margin:10px 0 12px;font-size:13px">Extra copies of episodes and movies you already hold at least as well. Retiring moves them to <span class="mono">_superseded/</span> beside their library — nothing is deleted.</p>
        <div class="ftable">${dup.items.slice(0, 50).map((x) => html`<div class="frow"><span class="fname"><b>${x.label}</b><span class="muted mono">${x.name}</span></span>
          <span class="flink muted">keeps ${x.winner_name}</span><span class="mono muted">${bytes(x.size)}</span></div>`)}</div>
        ${dup.count > 50 ? html`<p class="muted">…and ${dup.count - 50} more.</p>` : ""}`
        : html`<p class="muted" style="margin:10px 0 0">No duplicate copies.</p>`}</div>
    <div class="stack" data-form></div></div>`);
  const rb = el.querySelector("[data-retire]");
  if (rb) rb.addEventListener("click", async () => {
    if (!await confirmDialog("Retire duplicates?", `${dup.count} files (${bytes(dup.bytes)}) move to _superseded/. You can move them back by hand.`, "Retire")) return;
    try {
      const r = await api.post("/api/library/duplicates/retire", {});
      toast(`Moved ${r.moved} file${r.moved === 1 ? "" : "s"}, ${bytes(r.bytes)} freed from the library.`);
      render(el);
    } catch (err) { toast(err.message, "err"); }
  });
  await tunablesForm(el.querySelector("[data-form]"), "maintenance");
}
