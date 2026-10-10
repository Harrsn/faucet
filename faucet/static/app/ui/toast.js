let timer = null;

export function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `toast show${kind === "err" ? " err" : ""}`;
  clearTimeout(timer);
  timer = setTimeout(() => { el.className = "toast"; }, kind === "err" ? 5000 : 2800);
}
