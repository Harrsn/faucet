// <details class="menu"> dropdown: closes on an outside click or Esc. The
// document listeners exist only while the menu is open, so re-rendered
// pages don't pile them up.
//
// The list is positioned against the viewport (position: fixed) so it's
// never clipped by a container with overflow: hidden (the users list, cards
// with rounded corners), and it opens upward when there isn't room below.
function place(menu) {
  const list = menu.querySelector(".items");
  const btn = menu.querySelector("summary");
  if (!list || !btn) return;
  const r = btn.getBoundingClientRect();
  list.classList.add("floating");
  list.style.visibility = "hidden";
  list.style.top = "0px";
  list.style.left = "0px";
  const w = list.offsetWidth;
  const h = list.offsetHeight;
  const gap = 6;
  const below = window.innerHeight - r.bottom - gap;
  const top = below >= h || r.top < h + gap ? r.bottom + gap : r.top - gap - h;
  const left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
  list.style.top = `${Math.max(8, top)}px`;
  list.style.left = `${left}px`;
  list.style.visibility = "";
}

export function wireMenu(menu) {
  if (!menu) return;
  const outside = (e) => { if (!menu.contains(e.target)) menu.open = false; };
  const esc = (e) => { if (e.key === "Escape") { menu.open = false; menu.querySelector("summary").focus(); } };
  const follow = () => place(menu);
  menu.addEventListener("toggle", () => {
    if (menu.open) {
      place(menu);
      document.addEventListener("click", outside, true);
      document.addEventListener("keydown", esc);
      window.addEventListener("scroll", follow, true);
      window.addEventListener("resize", follow);
    } else {
      document.removeEventListener("click", outside, true);
      document.removeEventListener("keydown", esc);
      window.removeEventListener("scroll", follow, true);
      window.removeEventListener("resize", follow);
    }
  });
}
