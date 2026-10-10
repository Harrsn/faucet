// <details class="menu"> dropdown: closes on an outside click or Esc. The
// document listeners exist only while the menu is open, so re-rendered
// pages don't pile them up.
export function wireMenu(menu) {
  if (!menu) return;
  const outside = (e) => { if (!menu.contains(e.target)) menu.open = false; };
  const esc = (e) => { if (e.key === "Escape") { menu.open = false; menu.querySelector("summary").focus(); } };
  menu.addEventListener("toggle", () => {
    if (menu.open) {
      document.addEventListener("click", outside, true);
      document.addEventListener("keydown", esc);
    } else {
      document.removeEventListener("click", outside, true);
      document.removeEventListener("keydown", esc);
    }
  });
}
