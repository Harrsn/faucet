// Horizontal rail: native scroll with snap (touch, trackpad, keyboard all
// work), plus arrow buttons that page by most of a screen and hide at the
// ends. Replaces the bare scrollbar the old dashboard showed.
import { html } from "../dom.js";
import { icons } from "../icons.js";

export function rail(label, items) {
  return html`<div class="rail" data-rail>
    <button class="rail-arrow left" type="button" aria-label="Scroll ${label} left" hidden>${icons.left}</button>
    <div class="rail-track" role="list" aria-label="${label}">${items}</div>
    <button class="rail-arrow right" type="button" aria-label="Scroll ${label} right" hidden>${icons.right}</button>
  </div>`;
}

export function enhanceRails(root) {
  root.querySelectorAll("[data-rail]").forEach((el) => {
    const track = el.querySelector(".rail-track");
    const left = el.querySelector(".rail-arrow.left");
    const right = el.querySelector(".rail-arrow.right");
    const update = () => {
      const max = track.scrollWidth - track.clientWidth;
      left.hidden = track.scrollLeft <= 4;
      right.hidden = track.scrollLeft >= max - 4;
    };
    const page = (dir) => track.scrollBy({ left: dir * Math.max(200, track.clientWidth * 0.85) });
    left.addEventListener("click", () => page(-1));
    right.addEventListener("click", () => page(1));
    track.addEventListener("scroll", update, { passive: true });
    new ResizeObserver(update).observe(track);
    update();
  });
}
