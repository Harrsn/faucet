// Modal dialog: Esc and scrim close it, focus moves in and returns to the
// opener, Tab stays inside.
import { html, mount } from "../dom.js";
import { icons } from "../icons.js";

let host = null;

export function openModal({ title, sub = "", body, wide = false, onClose } = {}) {
  closeModal();
  const back = document.activeElement;
  host = document.createElement("div");
  document.body.appendChild(host);
  mount(host, html`<div class="modal-scrim" data-scrim>
    <div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-h"><div><h2 id="modal-title">${title}</h2>${sub ? html`<div class="sub">${sub}</div>` : ""}</div>
        <button class="icon-btn" type="button" aria-label="Close" data-x>${icons.close}</button></div>
      <div class="modal-b" data-body>${body || ""}</div></div></div>`);
  const scrim = host.querySelector("[data-scrim]");
  const dialog = host.querySelector(".modal");
  const close = () => {
    if (!host) return;
    document.removeEventListener("keydown", onKey, true);
    host.remove();
    host = null;
    if (onClose) onClose();
    if (back && back.focus) back.focus();
  };
  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    if (e.key === "Tab") {
      const f = [...dialog.querySelectorAll("button, a[href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
        .filter((el) => !el.disabled && el.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener("keydown", onKey, true);
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  host.querySelector("[data-x]").addEventListener("click", close);
  setTimeout(() => (dialog.querySelector("input, [data-autofocus]") || host.querySelector("[data-x]")).focus(), 0);
  return { body: host.querySelector("[data-body]"), close };
}

export function closeModal() {
  if (host) {
    host.remove();
    host = null;
  }
}

export async function confirmDialog(title, message, okLabel = "Confirm", danger = false) {
  return new Promise((resolve) => {
    let answered = false;
    const m = openModal({
      title,
      body: html`<p style="margin:0 0 18px;color:var(--text-2)">${message}</p>
        <div class="acts" style="justify-content:flex-end">
          <button class="btn line" type="button" data-no>Cancel</button>
          <button class="btn" type="button" data-yes data-autofocus${danger ? html` style="background:var(--bad);color:#fff"` : ""}>${okLabel}</button>
        </div>`,
      onClose: () => { if (!answered) resolve(false); },
    });
    m.body.querySelector("[data-no]").addEventListener("click", () => m.close());
    m.body.querySelector("[data-yes]").addEventListener("click", () => { answered = true; resolve(true); m.close(); });
  });
}
