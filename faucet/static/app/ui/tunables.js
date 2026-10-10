// A behaviour-settings form (General / Hunting / Maintenance): grouped
// fields with their unit and a one-line explanation, Save for what changed,
// and a reset-to-default link on anything not at its default.
import { api, qs } from "../api.js";
import { html, mount, raw } from "../dom.js";
import { toast } from "./toast.js";

function field(it) {
  const id = `t-${it.key}`;
  let control;
  if (it.kind === "bool") {
    control = html`<label class="switch"><input type="checkbox" id="${id}" data-key="${it.key}"${it.value === "1" ? raw(" checked") : ""}>
      <span>${it.value === "1" ? "On" : "Off"}</span></label>`;
  } else if (it.kind === "choice") {
    control = html`<select class="field" id="${id}" data-key="${it.key}">${it.choices.map((c) =>
      html`<option value="${c.value}"${c.value === it.value ? raw(" selected") : ""}>${c.label}</option>`)}</select>`;
  } else {
    control = html`<span class="numwrap"><input class="field num" type="number" id="${id}" data-key="${it.key}"
      value="${it.value}" min="${it.min ?? ""}" max="${it.max ?? ""}" step="${it.kind === "float" ? "0.05" : "1"}">
      ${it.unit ? html`<span class="unit">${it.unit}</span>` : ""}</span>`;
  }
  return html`<div class="trow">
    <label class="tlabel" for="${id}">${it.label}
      ${it.help ? html`<span class="thelp">${it.help}</span>` : ""}</label>
    <div class="tctl">${control}
      ${it.changed ? html`<button type="button" class="linkbtn" data-reset="${it.key}" data-default="${it.default}">Reset to ${it.kind === "bool" ? (it.default === "1" ? "on" : "off") : it.kind === "choice" ? "default" : it.default}</button>` : ""}</div>
  </div>`;
}

export async function tunablesForm(box, section, { intro = "" } = {}) {
  const d = await api.get(`/api/settings/tunables?${qs({ section })}`);
  const initial = {};
  d.groups.forEach((g) => g.items.forEach((it) => { initial[it.key] = it.value; }));
  mount(box, html`${intro ? html`<p class="muted" style="margin:0 0 6px">${intro}</p>` : ""}
    ${d.groups.map((g) => html`<div class="panel tgroup"><h3>${g.name}</h3>${g.items.map(field)}</div>`)}
    <div class="acts savebar"><span class="muted" data-dirty>No changes.</span>
      <button class="btn" type="button" data-save disabled>Save changes</button></div>`);

  const current = () => {
    const out = {};
    box.querySelectorAll("[data-key]").forEach((x) => {
      out[x.dataset.key] = x.type === "checkbox" ? (x.checked ? "1" : "0") : x.value.trim();
    });
    return out;
  };
  const changed = () => Object.fromEntries(Object.entries(current()).filter(([k, v]) =>
    String(v) !== String(initial[k]) && !(Number.isFinite(+v) && Number.isFinite(+initial[k]) && +v === +initial[k])));
  const refresh = () => {
    const n = Object.keys(changed()).length;
    box.querySelector("[data-dirty]").textContent = n ? `${n} unsaved change${n === 1 ? "" : "s"}` : "No changes.";
    box.querySelector("[data-save]").disabled = !n;
  };
  box.querySelectorAll("[data-key]").forEach((x) => x.addEventListener(x.type === "checkbox" ? "change" : "input", () => {
    if (x.type === "checkbox") x.nextElementSibling.textContent = x.checked ? "On" : "Off";
    refresh();
  }));
  box.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", () => {
    const x = box.querySelector(`[data-key="${CSS.escape(b.dataset.reset)}"]`);
    if (x.type === "checkbox") { x.checked = b.dataset.default === "1"; x.nextElementSibling.textContent = x.checked ? "On" : "Off"; }
    else x.value = b.dataset.default;
    refresh();
  }));
  box.querySelector("[data-save]").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      await api.put("/api/settings/tunables", { values: changed() });
      toast("Saved — applies on the next tick.");
      await tunablesForm(box, section, { intro });
    } catch (err) {
      toast(err.message, "err");
      e.target.disabled = false;
    }
  });
}
