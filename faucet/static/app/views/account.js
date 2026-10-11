// Account & security: two-factor sign-in (authenticator codes + recovery
// codes) and password. Admins without two-factor land here until it's on.
import { api } from "../api.js";
import { html, mount, raw } from "../dom.js";
import { confirmDialog, openModal } from "../ui/modal.js";
import { toast } from "../ui/toast.js";
import * as wa from "../webauthn.js";

export const title = "Account";

export async function render(el, ctx) {
  const [s, pk] = await Promise.all([api.get("/api/me/2fa"), api.get("/api/me/passkeys")]);
  const left = sessionStorage.getItem("faucet-recovery-left");
  sessionStorage.removeItem("faucet-recovery-left");
  mount(el, html`<div class="page" style="max-width:860px">
    <h1>Account</h1>
    ${ctx.mfaSetup ? html`<div class="panel" style="border-color:rgb(217 164 65 / .5)"><b>Turn on two-factor sign-in to continue.</b>
      <p class="muted" style="margin:6px 0 0">Admins can change everything in Faucet, and it's on the internet, so admin accounts need a second step at sign-in. It takes a minute.</p></div>` : ""}
    ${left != null ? html`<div class="panel"><b>You signed in with a recovery code — ${left} left.</b>
      <p class="muted" style="margin:6px 0 0">If your phone is gone, set two-factor up again on a new one below; that also gives you fresh codes.</p></div>` : ""}

    <section class="panel stack" aria-labelledby="mfa-h">
      <div class="integ-h"><h3 id="mfa-h">Two-factor sign-in</h3>
        <span class="istatus ${s.enabled ? "ok" : ""}"><span class="dot"></span>${s.enabled ? "On" : "Off"}</span></div>
      <p class="muted" style="margin:0">After your password, Faucet asks for a 6-digit code from an authenticator app — Bitwarden, 1Password, Google or Microsoft Authenticator.${s.required ? " Required for admin accounts." : ""}</p>
      ${s.enabled ? html`<p style="margin:0">On since ${new Date(s.since).toLocaleDateString()} · <b>${s.recovery_left}</b> recovery code${s.recovery_left === 1 ? "" : "s"} left${s.recovery_left <= 3 ? html` <span class="lib-warn">— make new ones</span>` : ""}.</p>
        <div class="acts"><button class="btn line" type="button" data-setup>Move to a new device</button>
          <button class="btn line" type="button" data-codes>New recovery codes</button>
          ${s.required ? "" : html`<button class="btn line" type="button" data-off>Turn off</button>`}</div>`
        : html`<div class="acts"><button class="btn" type="button" data-setup>Set up two-factor sign-in</button></div>`}
    </section>

    <section class="panel stack" aria-labelledby="pk-h">
      <div class="integ-h"><h3 id="pk-h">Passkeys</h3>
        ${wa.supported() ? html`<button class="btn line" type="button" data-addpk>Add a passkey</button>` : ""}</div>
      <p class="muted" style="margin:0">Sign in with Face ID, Touch ID, Windows Hello, a security key or your password manager — no password or code. They can't be phished, and they work alongside two-factor rather than replacing it.</p>
      ${!wa.supported() ? html`<p class="lib-warn" style="margin:0">This browser doesn't support passkeys.</p>` : ""}
      ${pk.passkeys.length ? html`<div class="pk-list">${pk.passkeys.map((p) => html`<div class="pk-row">
          <span class="fname"><b>${p.name}</b><span class="muted">added ${new Date(p.created_ts).toLocaleDateString()}${p.last_used ? ` · last used ${new Date(p.last_used).toLocaleString()}` : " · not used yet"}${p.synced ? " · synced across your devices" : " · on one device"}</span></span>
          <span class="acts"><button class="btn line small" type="button" data-pkren="${p.id}">Rename</button>
            <button class="btn line small" type="button" data-pkdel="${p.id}">Remove</button></span></div>`)}</div>`
        : html`<p style="margin:0" class="muted">No passkeys yet.</p>`}
    </section>

    <section class="panel stack" aria-labelledby="pw-h">
      <h3 id="pw-h" style="margin:0">Password</h3>
      <form class="form2" data-pw autocomplete="on">
        <input type="text" name="username" autocomplete="username" value="${ctx.me.username}" hidden>
        <label class="fld">Current password<input class="field" type="password" name="current" autocomplete="current-password" required></label>
        <label class="fld">New password<input class="field" type="password" name="new" autocomplete="new-password" minlength="10" required></label>
        <div class="acts"><button class="btn line" type="submit">Change password</button></div>
      </form>
      <p class="muted" style="margin:0;font-size:12.5px">Changing it signs out your other devices.</p>
    </section>
  </div>`);

  const again = () => render(el, ctx);
  el.querySelector("[data-setup]").addEventListener("click", () => setup(ctx, again));
  const codes = el.querySelector("[data-codes]");
  if (codes) codes.addEventListener("click", () => withPassword("New recovery codes", "Your old codes stop working.", async (password, close) => {
    const r = await api.post("/api/me/2fa/recovery", { password });
    close();
    showCodes(r.recovery_codes, again);
  }));
  const off = el.querySelector("[data-off]");
  if (off) off.addEventListener("click", () => withPassword("Turn off two-factor sign-in?", "You'll sign in with just your password.", async (password, close) => {
    await api.post("/api/me/2fa/disable", { password });
    close();
    toast("Two-factor sign-in is off.");
    again();
  }));
  const add = el.querySelector("[data-addpk]");
  if (add) add.addEventListener("click", () => withPassword("Add a passkey", "First, confirm it's you — then your device asks for your face, fingerprint or PIN.", async (password, close) => {
    const o = await api.post("/api/me/passkeys/options", { password });
    close();
    let credential;
    try { credential = await wa.create(o.options); }
    catch (err) {
      if (wa.cancelled(err)) { toast("Passkey not added."); return; }
      throw err;
    }
    const guess = /iPhone|iPad|Mac/.test(navigator.userAgent) ? "Apple device"
      : /Android/.test(navigator.userAgent) ? "Android phone" : /Windows/.test(navigator.userAgent) ? "Windows Hello" : "Passkey";
    const name = (await askName("Name this passkey", "So you can tell them apart — e.g. iPhone, Bitwarden, work laptop.", guess)) || guess;
    const r = await api.post("/api/me/passkeys", { challenge_id: o.challenge_id, credential, name });
    toast(`Added "${r.name}". Next time, use "Sign in with a passkey".`);
    again();
  }));
  el.querySelectorAll("[data-pkren]").forEach((b) => b.addEventListener("click", async () => {
    const cur = pk.passkeys.find((p) => p.id === Number(b.dataset.pkren));
    const name = await askName("Rename passkey", "", cur.name);
    if (!name) return;
    try { await api.patch(`/api/me/passkeys/${cur.id}`, { name }); again(); } catch (err) { toast(err.message, "err"); }
  }));
  el.querySelectorAll("[data-pkdel]").forEach((b) => b.addEventListener("click", async () => {
    const cur = pk.passkeys.find((p) => p.id === Number(b.dataset.pkdel));
    if (!await confirmDialog(`Remove "${cur.name}"?`, "It can't sign you in any more. Your device may still show it — delete it there too.", "Remove", true)) return;
    try { await api.del(`/api/me/passkeys/${cur.id}`); toast("Passkey removed."); again(); } catch (err) { toast(err.message, "err"); }
  }));

  el.querySelector("[data-pw]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api.post("/api/auth/change-password", { current_password: f.current.value, new_password: f.new.value });
      toast("Password changed. Other devices are signed out.");
      f.reset();
    } catch (err) { toast(err.message, "err"); }
  });
}

function withPassword(title, sub, fn) {
  const m = openModal({ title, sub, body: html`<form class="stack" data-f>
    <input type="text" autocomplete="username" hidden>
    <label class="fld">Your password<input class="field" type="password" name="password" autocomplete="current-password" required data-autofocus></label>
    <div class="acts" style="justify-content:flex-end"><button class="btn" type="submit">Continue</button></div></form>` });
  m.body.querySelector("[data-f]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const password = e.target.password.value;
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    // fn opens the next window itself; close this one first (one window at a time)
    try { await fn(password, () => m.close()); } catch (err) { toast(err.message, "err"); btn.disabled = false; }
  });
}

function setup(ctx, done) {
  withPassword("Set up two-factor sign-in", "First, confirm it's you.", async (password, close) => {
    const r = await api.post("/api/me/2fa/setup", { password });
    close();
    const m = openModal({ title: "Scan this with your authenticator app", wide: true, body: html`<div class="mfa-setup">
      <div class="qr">${raw(r.qr_svg)}</div>
      <div class="stack">
        <p style="margin:0">In <b>Bitwarden</b>: open the Faucet login → Edit → Authenticator key → the camera icon. Or any authenticator app: <i>Add account → Scan QR code</i>.</p>
        <p class="muted" style="margin:0;font-size:13px">Can't scan? Enter this key instead:</p>
        <code class="mono secret">${r.secret.replace(/(.{4})/g, "$1 ").trim()}</code>
        <form class="stack" data-f>
          <label class="fld">Then enter the 6-digit code it shows<input class="field mono code-in" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" required data-autofocus></label>
          <div class="acts"><button class="btn" type="submit">Turn on</button></div>
        </form>
      </div></div>` });
    m.body.querySelector("[data-f]").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const x = await api.post("/api/me/2fa/enable", { code: e.target.code.value });
        m.close();
        if (ctx.mfaDone) ctx.mfaDone();
        showCodes(x.recovery_codes, done, true);
      } catch (err) { toast(err.message, "err"); }
    });
  });
}

function showCodes(codes, done, first = false) {
  const text = `Faucet recovery codes — ${location.host}\n${new Date().toLocaleString()}\n\n${codes.join("\n")}\n\nEach code signs you in once, instead of an authenticator code.\n`;
  const m = openModal({ title: first ? "Two-factor sign-in is on — save your recovery codes" : "Your new recovery codes", wide: true, body: html`<div class="stack">
    <p style="margin:0">If you lose your phone, each of these signs you in <b>once</b> instead of a code. They won't be shown again — keep them in your password manager.</p>
    <div class="rcodes mono">${codes.map((c) => html`<span>${c}</span>`)}</div>
    <div class="acts"><button class="btn line" type="button" data-copy>Copy</button>
      <button class="btn line" type="button" data-dl>Download .txt</button>
      <span class="spacer" style="flex:1"></span>
      <button class="btn" type="button" data-done>I've saved them</button></div></div>` });
  m.body.querySelector("[data-copy]").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied."); } catch { toast("Copy failed — select them by hand.", "err"); }
  });
  m.body.querySelector("[data-dl]").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    a.download = "faucet-recovery-codes.txt";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  m.body.querySelector("[data-done]").addEventListener("click", () => {
    m.close();
    if (first) { location.hash = "#/"; location.reload(); } else done();
  });
}

function askName(title, sub, initial) {
  return new Promise((resolve) => {
    let done = false;
    const m = openModal({ title, sub, body: html`<form class="stack" data-f>
      <label class="fld">Name<input class="field" name="name" maxlength="60" value="${initial}" required data-autofocus></label>
      <div class="acts" style="justify-content:flex-end"><button class="btn" type="submit">Save</button></div></form>`,
      onClose: () => { if (!done) resolve(null); } });
    m.body.querySelector("[data-f]").addEventListener("submit", (e) => {
      e.preventDefault();
      done = true;
      const v = e.target.name.value.trim();
      m.close();
      resolve(v || null);
    });
  });
}
