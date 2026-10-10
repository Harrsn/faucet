// Settings → General: defaults for new titles plus everyday behaviour.
import { api } from "../../api.js";
import { html, mount, raw } from "../../dom.js";
import { tunablesForm } from "../../ui/tunables.js";
import { toast } from "../../ui/toast.js";

const LANGS = [["en", "English"], ["es", "Spanish"], ["fr", "French"], ["de", "German"], ["it", "Italian"],
  ["pt", "Portuguese"], ["ja", "Japanese"], ["ko", "Korean"], ["any", "Any language"]];

export async function render(el) {
  const [s, p] = await Promise.all([api.get("/api/settings"), api.get("/api/profiles")]);
  const lang = (s.db && s.db.default_language) || "en";
  const prof = String((s.db && s.db.default_profile_id) || "");
  mount(el, html`<div class="stack">
    <div class="sec-intro"><div><h2>General</h2>
      <p class="muted">Defaults for shows and movies you add, and everyday behaviour.</p></div></div>
    <div class="panel tgroup"><h3>New titles</h3>
      <div class="trow"><label class="tlabel" for="g-lang">Preferred audio language
        <span class="thelp">Releases detected as another language are skipped; unknown ones still pass.</span></label>
        <div class="tctl"><select class="field" id="g-lang">${LANGS.map(([v, l]) =>
          html`<option value="${v}"${v === lang ? raw(" selected") : ""}>${l}</option>`)}
          ${LANGS.some(([v]) => v === lang) ? "" : html`<option value="${lang}" selected>${lang}</option>`}</select></div></div>
      <div class="trow"><label class="tlabel" for="g-prof">Default quality profile
        <span class="thelp">Used by every show and movie that doesn't pick its own.</span></label>
        <div class="tctl"><select class="field" id="g-prof"><option value="">First profile (${p.profiles[0] ? p.profiles[0].name : "none"})</option>
          ${p.profiles.map((x) => html`<option value="${x.id}"${String(x.id) === prof ? raw(" selected") : ""}>${x.name}</option>`)}</select></div></div>
      <div class="acts"><button class="btn" type="button" data-save-defaults>Save defaults</button></div>
    </div>
    <div class="stack" data-form></div></div>`);
  el.querySelector("[data-save-defaults]").addEventListener("click", async () => {
    try {
      const r = await api.patch("/api/settings", { values: {
        default_language: el.querySelector("#g-lang").value,
        default_profile_id: el.querySelector("#g-prof").value } });
      (r.warnings || []).forEach((w) => toast(w));
      toast("Defaults saved.");
    } catch (err) { toast(err.message, "err"); }
  });
  await tunablesForm(el.querySelector("[data-form]"), "general");
}
