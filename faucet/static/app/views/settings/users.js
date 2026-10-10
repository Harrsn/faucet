// Settings → Users & requests: members, roles, approval, password resets.
import { api, qs } from "../../api.js";
import { ago, html, mount, raw } from "../../dom.js";
import { confirmDialog, openModal } from "../../ui/modal.js";
import { toast } from "../../ui/toast.js";

export async function render(el, ctx) {
  const d = await api.get("/api/admin/users");
  const me = ctx.me.id;
  const pending = d.users.filter((u) => u.status === "pending");
  const row = (u) => html`<div class="urow">
    <span class="avatar" aria-hidden="true">${u.username.slice(0, 1).toUpperCase()}</span>
    <span class="uname"><b>${u.username}${u.id === me ? html` <span class="tagpill">you</span>` : ""}</b>
      <span class="muted">${u.email || "no email"} · ${u.last_login ? `last seen ${ago(u.last_login)}` : "never signed in"}</span></span>
    <span class="ubadges"><span class="status ${u.status === "active" ? "have" : u.status === "pending" ? "upgrade" : "skipped"}">${u.status}</span>
      <span class="tagpill">${u.role === "admin" ? "Admin" : "Member"}</span>
      ${u.role !== "admin" && u.can_autoapprove ? html`<span class="tagpill good">requests auto-approved</span>` : ""}</span>
    <details class="menu"><summary aria-label="Actions for ${u.username}">${raw('<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>')}</summary>
      <div class="items">
        ${u.status === "pending" ? html`<button type="button" data-a="approve" data-u="${u.id}">Approve</button>` : ""}
        ${u.id !== me && u.status === "active" ? html`<button type="button" data-a="disable" data-u="${u.id}">Disable</button>` : ""}
        ${u.status === "disabled" ? html`<button type="button" data-a="enable" data-u="${u.id}">Enable</button>` : ""}
        ${u.id !== me ? html`<button type="button" data-a="role" data-u="${u.id}">${u.role === "admin" ? "Make member" : "Make admin"}</button>` : ""}
        ${u.role !== "admin" ? html`<button type="button" data-a="trust" data-u="${u.id}">${u.can_autoapprove ? "Review their requests" : "Auto-approve their requests"}</button>` : ""}
        <button type="button" data-a="reset" data-u="${u.id}">Password reset link…</button>
        ${u.id !== me ? html`<button type="button" class="danger" data-a="delete" data-u="${u.id}">Delete…</button>` : ""}
      </div></details></div>`;
  mount(el, html`<div class="stack">
    <div class="sec-intro"><div><h2>Users & requests</h2>
      <p class="muted">Members browse and request; admins manage. A member's requests wait for approval unless you auto-approve them.</p></div></div>
    ${pending.length ? html`<div class="panel" style="border-color:rgb(217 164 65 / .4)"><b>${pending.length} account${pending.length === 1 ? "" : "s"} waiting for approval.</b></div>` : ""}
    <div class="ftable">${d.users.map(row)}</div>
    <div class="panel"><h3>Create an account</h3>
      <form class="form2" data-create autocomplete="off" data-bwignore data-lpignore="true" data-1p-ignore>
        <label class="fld">Username<input class="field" name="faucet-new-username" data-c="username" autocomplete="off" data-bwignore data-lpignore="true" data-1p-ignore required minlength="3" maxlength="32"></label>
        <label class="fld">Temporary password<input class="field" type="password" name="faucet-new-password" data-c="password" autocomplete="new-password" data-bwignore data-lpignore="true" data-1p-ignore required minlength="10"></label>
        <label class="fld">Email (optional)<input class="field" type="email" name="faucet-new-email" data-c="email" autocomplete="off" data-bwignore></label>
        <label class="fld">Role<select class="field" data-c="role"><option value="user">Member</option><option value="admin">Admin</option></select></label>
        <div class="acts"><button class="btn" type="submit">Create</button></div>
      </form></div>
  </div>`);

  el.querySelectorAll("details.menu").forEach((m) => {
    m.addEventListener("toggle", () => {
      if (!m.open) return;
      const close = (e) => { if (!m.contains(e.target)) { m.open = false; document.removeEventListener("click", close, true); } };
      document.addEventListener("click", close, true);
    });
  });
  el.querySelectorAll("[data-a]").forEach((b) => b.addEventListener("click", async () => {
    const u = d.users.find((x) => x.id === +b.dataset.u);
    const a = b.dataset.a;
    b.closest("details").open = false;
    try {
      if (a === "approve" || a === "enable") await api.post(`/api/admin/users/${u.id}/status?${qs({ status: "active" })}`);
      else if (a === "disable") await api.post(`/api/admin/users/${u.id}/status?${qs({ status: "disabled" })}`);
      else if (a === "role") await api.post(`/api/admin/users/${u.id}/role?${qs({ role: u.role === "admin" ? "user" : "admin" })}`);
      else if (a === "trust") await api.post(`/api/admin/users/${u.id}/autoapprove?${qs({ enabled: u.can_autoapprove ? 0 : 1 })}`);
      else if (a === "delete") {
        if (!await confirmDialog(`Delete ${u.username}?`, "Their account and sessions are removed. Requests they made stay.", "Delete", true)) return;
        await api.del(`/api/admin/users/${u.id}`);
      } else if (a === "reset") {
        const r = await api.post(`/api/admin/users/${u.id}/reset`);
        const link = `${location.origin}${r.reset_path}`;
        const m = openModal({ title: `Reset link for ${u.username}`, body: html`<p class="muted" style="margin:0 0 10px">Send this to them; it works once and expires. Email delivery comes later.</p>
          <input class="field mono" style="width:100%" readonly value="${link}" data-link>
          <div class="acts" style="justify-content:flex-end;margin-top:12px"><button class="btn" type="button" data-copy>Copy link</button></div>` });
        m.body.querySelector("[data-copy]").addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(link); toast("Copied."); }
          catch { m.body.querySelector("[data-link]").select(); }
        });
        return;
      }
      toast("Updated.");
      render(el, ctx);
    } catch (err) { toast(err.message, "err"); }
  }));
  el.querySelector("[data-create]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = (k) => el.querySelector(`[data-c="${k}"]`).value.trim();
    try {
      await api.post("/api/admin/users", { username: f("username"), password: el.querySelector('[data-c="password"]').value,
        email: f("email") || null, role: f("role"), status: "active" });
      toast(`Created ${f("username")}.`);
      render(el, ctx);
    } catch (err) { toast(err.message, "err"); }
  });
}
