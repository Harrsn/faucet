// API client: JSON in/out, CSRF on writes, one place that handles 401.

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)faucet_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function call(method, url, body) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["X-CSRF-Token"] = csrf();
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }
  let r;
  try {
    r = await fetch(url, opts);
  } catch {
    throw new ApiError(0, "Can't reach Faucet");
  }
  if (r.status === 401) {
    location.href = "/login";
    throw new ApiError(401, "Signed out");
  }
  const data = await r.json().catch(() => ({}));
  if (r.status === 403 && data && data.code === "mfa_setup_required" && location.hash !== "#/account") {
    location.hash = "#/account";            // admins set up two-factor before anything else
  }
  if (!r.ok) {
    const d = data && data.detail;
    throw new ApiError(r.status, typeof d === "string" ? d : `Request failed (${r.status})`);
  }
  return data;
}

export const api = {
  get: (u) => call("GET", u),
  post: (u, b) => call("POST", u, b),
  put: (u, b) => call("PUT", u, b),
  patch: (u, b) => call("PATCH", u, b),
  del: (u) => call("DELETE", u),
};

export const qs = (o) => new URLSearchParams(
  Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
