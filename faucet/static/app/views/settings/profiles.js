// Settings → Quality profiles: what a release must be to be grabbed, and
// what's preferred when several qualify.
import { api } from "../../api.js";
import { html, mount, raw } from "../../dom.js";
import { confirmDialog, openModal } from "../../ui/modal.js";
import { toast } from "../../ui/toast.js";

const SRC_NOTE = { CAM: "cam / telesync — only if you really want it" };
const CODEC_OPTS = [["", "No preference"], ["prefer-hevc", "Prefer HEVC / x265 (smaller files)"],
  ["prefer-h264", "Prefer H.264 / x264 (plays everywhere)"], ["only-hevc", "HEVC only"], ["only-h264", "H.264 only"]];
const HDR_OPTS = [["", "No preference"], ["prefer", "Prefer HDR"], ["avoid", "Avoid HDR"], ["never", "Never HDR"]];
const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
function formatSummary(f) {
  const out = [];
  const c = { "prefer-hevc": "prefers HEVC", "prefer-h264": "prefers H.264", "only-hevc": "HEVC only", "only-h264": "H.264 only" }[f.codec];
  if (c) out.push(c);
  const h = { prefer: "prefers HDR", avoid: "avoids HDR", never: "no HDR" }[f.hdr];
  if (h) out.push(h);
  if ((f.prefer_groups || []).length) out.push(`prefers ${f.prefer_groups.join(", ")}`);
  if ((f.never_groups || []).length) out.push(`never ${f.never_groups.join(", ")}`);
  if ((f.never_words || []).length) out.push(`skips "${f.never_words.join('", "')}"`);
  return out;
}

export async function render(el) {
  const [p, opts, s] = await Promise.all([api.get("/api/profiles"), api.get("/api/profiles/options"), api.get("/api/settings")]);
  const def = String((s.db && s.db.default_profile_id) || (p.profiles[0] && p.profiles[0].id) || "");
  const summary = (x) => [
    x.resolutions.length ? x.resolutions.join(" / ") : "any resolution",
    x.sources.length ? `prefers ${x.sources.filter((v) => v !== "CAM").join(" > ") || "—"}` : "any source",
    `≥ ${x.min_seeders} seeder${x.min_seeders === 1 ? "" : "s"}`,
    x.max_size_gb ? `≤ ${x.max_size_gb} GB` : "", x.min_size_gb ? `≥ ${x.min_size_gb} GB` : "",
    x.language && x.language !== "any" ? `${x.language} audio` : "any language",
    ...formatSummary(x.formats || {}),
  ].filter(Boolean).join(" · ");
  mount(el, html`<div class="stack">
    <div class="sec-intro"><div><h2>Quality profiles</h2>
      <p class="muted">A profile decides which releases may be grabbed and which wins when several qualify. Shows and movies use the default unless they pick one.</p></div>
      <div class="acts"><button class="btn" type="button" data-new>New profile</button></div></div>
    ${p.profiles.map((x) => html`<article class="lib">
      <div class="lib-main"><div class="lib-h"><h3>${x.name}</h3>${String(x.id) === def ? html`<span class="tagpill good">Default</span>` : ""}</div>
        <span class="muted lib-stats">${summary(x)}</span></div>
      <div class="acts"><button class="btn line" type="button" data-edit="${x.id}">Edit</button>
        ${p.profiles.length > 1 ? html`<button class="btn line" type="button" data-del="${x.id}">Delete</button>` : ""}</div></article>`)}
  </div>`);
  el.querySelector("[data-new]").addEventListener("click", () => edit(null));
  el.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => edit(p.profiles.find((x) => x.id === +b.dataset.edit))));
  el.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    const x = p.profiles.find((y) => y.id === +b.dataset.del);
    if (!await confirmDialog(`Delete ${x.name}?`, "Shows and movies using it switch to the default profile.", "Delete", true)) return;
    try { await api.del(`/api/profiles/${x.id}`); toast(`Deleted ${x.name}.`); render(el); }
    catch (err) { toast(err.message, "err"); }
  }));

  function edit(x) {
    const v = x || { name: "", min_seeders: 5, resolutions: ["1080p", "720p"], sources: ["WEB-DL", "BluRay", "WEBRip", "HDTV"],
      max_size_gb: 0, min_size_gb: 0, language: "en", formats: {} };
    const fm = v.formats || {};
    const m = openModal({ title: x ? `Edit ${x.name}` : "New profile", wide: true, body: html`<div class="stack">
      <label class="fld">Name<input class="field" data-f="name" value="${v.name}" maxlength="60" autocomplete="off"></label>
      <fieldset class="fset"><legend>Resolutions allowed <span class="muted">(none ticked = any)</span></legend>
        ${opts.resolutions.map((r) => html`<label class="check"><input type="checkbox" data-res="${r}"${v.resolutions.includes(r) ? raw(" checked") : ""}><span>${r}</span></label>`)}</fieldset>
      <fieldset class="fset"><legend>Sources, best first <span class="muted">(ticked ones score higher, in this order; CAM is refused unless ticked)</span></legend>
        ${opts.sources.map((r) => html`<label class="check"><input type="checkbox" data-src="${r}"${v.sources.includes(r) ? raw(" checked") : ""}><span>${r}${SRC_NOTE[r] ? html` <span class="muted">— ${SRC_NOTE[r]}</span>` : ""}</span></label>`)}</fieldset>
      <div class="form2">
        <label class="fld">Minimum seeders<input class="field" type="number" min="0" max="10000" data-f="min_seeders" value="${v.min_seeders}"></label>
        <label class="fld">Largest size (GB, 0 = no limit)<input class="field" type="number" min="0" max="1000" step="0.5" data-f="max_size_gb" value="${v.max_size_gb}"></label>
        <label class="fld">Smallest size (GB, 0 = no limit)<input class="field" type="number" min="0" max="1000" step="0.1" data-f="min_size_gb" value="${v.min_size_gb}"></label>
        <label class="fld">Audio language<input class="field" data-f="language" value="${v.language}" maxlength="3" placeholder="en, or any"></label>
      </div>
      <fieldset class="fset fmt"><legend>Formats <span class="muted">(optional — "prefer" ranks higher, "only"/"never" refuses the rest)</span></legend>
        <div class="form2">
          <label class="fld">Codec<select class="field" data-fm="codec">${CODEC_OPTS.map(([k, l]) => html`<option value="${k}"${(fm.codec || "") === k ? raw(" selected") : ""}>${l}</option>`)}</select></label>
          <label class="fld">HDR<select class="field" data-fm="hdr">${HDR_OPTS.map(([k, l]) => html`<option value="${k}"${(fm.hdr || "") === k ? raw(" selected") : ""}>${l}</option>`)}</select></label>
          <label class="fld">Preferred release groups<input class="field" data-fm="prefer_groups" value="${(fm.prefer_groups || []).join(", ")}" placeholder="e.g. NTb, FLUX, EDITH"></label>
          <label class="fld">Never from groups<input class="field" data-fm="never_groups" value="${(fm.never_groups || []).join(", ")}" placeholder="e.g. YIFY"></label>
          <label class="fld" style="grid-column:1/-1">Never with these words<input class="field" data-fm="never_words" value="${(fm.never_words || []).join(", ")}" placeholder="e.g. HC, KORSUB, DUBBED — hardcoded subs, dubs…"></label>
        </div></fieldset>
      <div class="acts" style="justify-content:flex-end"><button class="btn line" type="button" data-cancel>Cancel</button>
        <button class="btn" type="button" data-save>${x ? "Save" : "Create"}</button></div></div>` });
    const b = m.body;
    b.querySelector("[data-cancel]").addEventListener("click", () => m.close());
    b.querySelector("[data-save]").addEventListener("click", async () => {
      const f = (k) => b.querySelector(`[data-f="${k}"]`).value.trim();
      const body = { name: f("name"), min_seeders: +f("min_seeders") || 0, max_size_gb: +f("max_size_gb") || 0,
        min_size_gb: +f("min_size_gb") || 0, language: f("language") || "any",
        resolutions: [...b.querySelectorAll("[data-res]:checked")].map((c) => c.dataset.res),
        sources: [...b.querySelectorAll("[data-src]:checked")].map((c) => c.dataset.src),
        formats: {
          codec: b.querySelector('[data-fm="codec"]').value, hdr: b.querySelector('[data-fm="hdr"]').value,
          prefer_groups: list(b.querySelector('[data-fm="prefer_groups"]').value),
          never_groups: list(b.querySelector('[data-fm="never_groups"]').value),
          never_words: list(b.querySelector('[data-fm="never_words"]').value) } };
      try {
        if (x) await api.put(`/api/profiles/${x.id}`, body);
        else await api.post("/api/profiles", body);
        toast(x ? "Profile saved." : "Profile created.");
        m.close();
        render(el);
      } catch (err) { toast(err.message, "err"); }
    });
  }
}
