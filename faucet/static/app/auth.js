// Sign-in page: sign in, request an account, set a new password from a reset
// link — and on a fresh install, create the first (admin) account, which
// starts the setup wizard.
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
let mode = location.pathname === "/reset" && params.get("token") ? "reset"
  : location.pathname === "/register" ? "register" : "login";
let firstRun = false;

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : `Request failed (${r.status})`);
  return d;
}

function msg(text, kind = "") {
  const m = $("msg");
  m.textContent = text;
  m.className = `auth-msg ${kind}`;
}

function render() {
  const copy = {
    login: ["Sign in", "Your shows and movies, hunted, sorted and ready.", "Sign in", "Password", "current-password"],
    register: ["Request an account", "An admin approves new accounts before you can sign in.", "Request account", "Password (10+ characters)", "new-password"],
    reset: ["Set a new password", "Choose a password for your account.", "Set password", "New password (10+ characters)", "new-password"],
    first: ["Welcome to Faucet", "First, create the admin account. Then a short setup connects your libraries, download client and indexers.", "Create admin account", "Password (10+ characters)", "new-password"],
  }[firstRun ? "first" : mode];
  $("auth-title").textContent = copy[0];
  $("auth-sub").textContent = copy[1];
  $("submit").textContent = copy[2];
  $("lbl-pass").textContent = copy[3];
  $("password").autocomplete = copy[4];
  document.title = `${copy[0]} · Faucet`;
  $("auth-tabs").hidden = firstRun || mode === "reset";
  $("row-user").hidden = mode === "reset" && !firstRun;
  $("row-email").hidden = !(mode === "register" || firstRun);
  document.querySelectorAll("#auth-tabs [data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  $("hint").textContent = firstRun ? "This account can do everything; you can add members later." : "";
  msg("");
}

document.querySelectorAll("#auth-tabs [data-mode]").forEach((b) => b.addEventListener("click", () => {
  mode = b.dataset.mode;
  history.replaceState(null, "", mode === "register" ? "/register" : "/login");
  render();
}));

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("submit");
  const username = $("username").value.trim();
  const password = $("password").value;
  btn.disabled = true;
  try {
    if (firstRun || mode === "register") {
      if (password.length < 10) throw new Error("Use at least 10 characters.");
      const d = await post("/api/auth/register", { username, password, email: $("email").value.trim() || null });
      if (d.admin_created) {
        await post("/api/auth/login", { username, password });
        location.href = "/#/setup";
        return;
      }
      mode = "login";
      render();
      msg(d.message || "Account requested — you can sign in once an admin approves it.", "ok");
    } else if (mode === "reset") {
      if (password.length < 10) throw new Error("Use at least 10 characters.");
      await post("/api/auth/reset/consume", { token: params.get("token"), new_password: password });
      mode = "login";
      history.replaceState(null, "", "/login");
      render();
      msg("Password set — sign in with it now.", "ok");
    } else {
      await post("/api/auth/login", { username, password });
      location.href = "/";
    }
  } catch (err) {
    msg(err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

(async () => {
  try {
    const d = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
    if (d.authenticated) { location.href = "/"; return; }
    firstRun = !!d.setup_needed;
  } catch { /* offline: show the form anyway */ }
  render();
  (firstRun || mode !== "reset" ? $("username") : $("password")).focus();
})();
