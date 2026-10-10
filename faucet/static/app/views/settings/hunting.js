// Settings → Hunting: how hard, how often and how patiently Faucet hunts.
import { html, mount } from "../../dom.js";
import { tunablesForm } from "../../ui/tunables.js";

export async function render(el) {
  mount(el, html`<div class="stack"><div class="sec-intro"><div><h2>Hunting</h2>
    <p class="muted">How hard, how often and how patiently Faucet hunts. Changes apply on the next scheduler tick — no restart.</p></div></div>
    <div class="stack" data-form></div></div>`);
  await tunablesForm(el.querySelector("[data-form]"), "hunting");
}
