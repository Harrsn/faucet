// WebAuthn glue: the server speaks JSON (base64url strings), the browser API
// speaks ArrayBuffers. Hand-rolled rather than relying on the newer
// PublicKeyCredential.parse*FromJSON / toJSON helpers, which not every
// browser has yet.
const b2a = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const a2b = (s) => {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(b, (c) => c.charCodeAt(0)).buffer;
};

export const supported = () => !!(window.PublicKeyCredential && navigator.credentials);

export async function conditionalAvailable() {
  try {
    return supported() && PublicKeyCredential.isConditionalMediationAvailable
      && await PublicKeyCredential.isConditionalMediationAvailable();
  } catch { return false; }
}

export async function create(options) {
  const o = { ...options, challenge: a2b(options.challenge), user: { ...options.user, id: a2b(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c) => ({ ...c, id: a2b(c.id) })) };
  const cred = await navigator.credentials.create({ publicKey: o });
  const r = cred.response;
  return {
    id: cred.id, rawId: b2a(cred.rawId), type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || null,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    response: { clientDataJSON: b2a(r.clientDataJSON), attestationObject: b2a(r.attestationObject),
      transports: r.getTransports ? r.getTransports() : [] },
  };
}

export async function get(options, { mediation, signal } = {}) {
  const o = { ...options, challenge: a2b(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c) => ({ ...c, id: a2b(c.id) })) };
  const cred = await navigator.credentials.get({ publicKey: o, mediation, signal });
  if (!cred) return null;
  const r = cred.response;
  return {
    id: cred.id, rawId: b2a(cred.rawId), type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || null,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    response: { clientDataJSON: b2a(r.clientDataJSON), authenticatorData: b2a(r.authenticatorData),
      signature: b2a(r.signature), userHandle: r.userHandle ? b2a(r.userHandle) : null },
  };
}

// "Someone cancelled / timed out" vs a real failure.
export const cancelled = (e) => e && (e.name === "NotAllowedError" || e.name === "AbortError");
