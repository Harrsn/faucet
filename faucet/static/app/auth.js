// Sign-in page: sign in, request an account, set a new password from a reset
// link — and on a fresh install, create the first (admin) account, which
// starts the setup wizard.
import * as wa from "./webauthn.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
let mode = location.pathname === "/reset" && params.get("token") ? "reset"
  : location.pathname === "/register" ? "register" : "login";
let firstRun = false;
let ticket = null;      // password accepted; waiting for the authenticator code
let recovery = false;

function codeStep() {
  $("auth-title").textContent = "Two-factor sign-in";
  $("auth-sub").textContent = recovery
    ? "Enter one of your recovery codes. Each works once."
    : "Enter the 6-digit code from your authenticator app.";
  $("auth-tabs").hidden = true;
  $("row-user").hidden = true;
  $("row-email").hidden = true;
  $("row-pass").hidden = true;
  $("row-code").hidden = false;
  const c = $("code");
  c.value = "";
  c.inputMode = recovery ? "text" : "numeric";
  c.autocomplete = recovery ? "off" : "one-time-code";
  c.placeholder = recovery ? "xxxxxx-xxxxxx" : "123456";
  $("lbl-code").textContent = recovery ? "Recovery code" : "Code";
  $("submit").textContent = "Verify";
  $("hint").textContent = "";
  $("alt").hidden = false;
  $("alt").textContent = recovery ? "Use a code from my app instead" : "Lost your phone? Use a recovery code";
  msg("");
  c.focus();
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST",
    // "this page knows the two-factor step": an out-of-date page that doesn't
    // send it is told to refresh rather than silently bouncing back here
    headers: { "Content-Type": "application/json", "X-Faucet-Auth": "2" },
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
  $("passkey").hidden = !(wa.supported() && mode === "login" && !firstRun);
  $("hint").textContent = firstRun ? "This account can do everything; you can add members later." : "";
  msg("");
}

// ── passkeys ──
let conditional = null;     // the quiet "suggest my passkey in the username field" request

async function passkeySignIn(mediation) {
  const opts = await post("/api/auth/passkey/options", {});
  if (mediation === "conditional") conditional = new AbortController();
  const credential = await wa.get(opts.options, mediation === "conditional"
    ? { mediation, signal: conditional.signal } : {});
  if (!credential) return;
  const d = await post("/api/auth/passkey", { challenge_id: opts.challenge_id, credential });
  location.href = d.mfa_setup_required ? "/#/account" : "/";
}

$("passkey").addEventListener("click", async () => {
  if (conditional) { conditional.abort(); conditional = null; }
  $("passkey").disabled = true;
  try { await passkeySignIn(); }
  catch (err) { if (!wa.cancelled(err)) msg(err.message, "err"); }
  finally { $("passkey").disabled = false; }
});

$("alt").addEventListener("click", (e) => { e.preventDefault(); recovery = !recovery; codeStep(); });
$("code").addEventListener("input", (e) => {
  // on the code step, a full 6-digit code submits itself — but never before
  // it (a password manager can fill the hidden field as the page loads)
  if (ticket && !recovery && /^\d{6}$/.test(e.target.value.trim())) $("auth-form").requestSubmit();
});

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
    } else if (ticket) {
      const d = await post("/api/auth/login/mfa", { ticket, code: $("code").value.trim() });
      if (d.used === "recovery") sessionStorage.setItem("faucet-recovery-left", String(d.recovery_left));
      location.href = "/";
    } else {
      const d = await post("/api/auth/login", { username, password });
      if (d.status === "mfa_required") {
        ticket = d.ticket;
        codeStep();
        return;
      }
      if (d.status !== "ok") throw new Error("Sign-in didn't finish — refresh the page and try again.");
      location.href = d.mfa_setup_required ? "/#/account" : "/";
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
  if (mode === "login" && !firstRun && await wa.conditionalAvailable()) {
    // passkeys offered right in the username field's autofill
    passkeySignIn("conditional").catch((err) => { if (!wa.cancelled(err)) msg(err.message, "err"); });
  }
  if (params.get("retry")) {
    msg("Something interrupted signing in — please try again.", "err");
    history.replaceState(null, "", "/login");
  }
  (firstRun || mode !== "reset" ? $("username") : $("password")).focus();
})();
