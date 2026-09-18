// api.js
// Direct client for the Lambda Function URL. One endpoint, action-routed,
// per API_CONTRACT.md. Response is always {statusCode, body: "<json>"} —
// body is a JSON-encoded STRING, parsed here once so every caller gets
// plain objects.

// TODO: set VITE_LAMBDA_URL in .env.local (gitignored) — never hardcode
// the real URL here, since this file goes into a public repo.
const FUNCTION_URL = import.meta.env.VITE_LAMBDA_URL;

if (!FUNCTION_URL) {
  console.error(
    "VITE_LAMBDA_URL is not set. Create a .env.local file with:\n" +
    "  VITE_LAMBDA_URL=<your friend's Lambda Function URL>\n" +
    "then restart `npm run dev`."
  );
}

async function callAgent(action, payload = {}) {
  const res = await fetch(FUNCTION_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });

  const data = await res.json();

  // Lambda Function URLs auto-unwrap {statusCode, body} into the real
  // HTTP response — `data` here is usually already the final result.
  // Defensive check for the other shape too, in case that ever changes
  // (e.g. calling through a raw SDK invoke or a proxy later).
  const looksWrapped =
    data && typeof data.statusCode === "number" && typeof data.body === "string";
  const body = looksWrapped ? JSON.parse(data.body) : data;

  if (!res.ok || (looksWrapped && data.statusCode >= 400)) {
    throw new Error(body?.error || `Backend error ${res.status}`);
  }
  return body;
}

// ---------------------------------------------------------------------------
// decompose — first_action + if/then plans. _fallback:true is a valid,
// honest response, never rendered differently in the UI.
// ---------------------------------------------------------------------------

export async function decompose({ goal, answers = "" }) {
  return callAgent("decompose", { goal, answers });
}

// ---------------------------------------------------------------------------
// drift — one tab judged against intent. Cached client-side since the
// same handful of tabs recur constantly in a real session. Fails OPEN
// on any client-side error too, matching the backend's own bias.
// ---------------------------------------------------------------------------

const driftCache = new Map();

export async function drift({ intent, title, domain }) {
  const key = `${intent}::${domain}::${title.toLowerCase().trim()}`;
  if (driftCache.has(key)) return driftCache.get(key);

  try {
    const result = await callAgent("drift", { intent, title, domain });
    driftCache.set(key, result);
    return result;
  } catch (err) {
    console.error("Drift check failed, defaulting to relevant:", err);
    return { relevant: true, reason: "call failed, defaulted to relevant" };
  }
}

// ---------------------------------------------------------------------------
// churn — free, deterministic, no model call.
// ---------------------------------------------------------------------------

export async function churn({ events }) {
  return callAgent("churn", { events });
}

// ---------------------------------------------------------------------------
// reentry — session_found:false is common and correct, not an error.
// This action DOES surface a real error (500) on genuine failure —
// never silently downgrade a failure into "nothing found."
// ---------------------------------------------------------------------------

export async function reentry({ declared_intent, events }) {
  return callAgent("reentry", { declared_intent, events });
}

// ---------------------------------------------------------------------------
// amend — mid-session chat check-in (blocker / scope change / progress /
// done). INFERRED SHAPE — initiate_probe.py's actual field definitions
// weren't available; this matches what lambda_handler.py's fallback path
// reveals (kind, first_action, plans, note). Confirm against the real
// file once you have it.
// ---------------------------------------------------------------------------

export async function amend({ session, message }) {
  return callAgent("amend", { session, message });
}

// ---------------------------------------------------------------------------
// assign / nightly — the experiment loop. Exposed for when a future
// screen needs them; nothing in the current UI calls these yet since
// there's no scheduler triggering `nightly` on the backend either.
// ---------------------------------------------------------------------------

export async function assignArm({ arms, session_index }) {
  return callAgent("assign", { arms, session_index });
}

export async function nightly(payload) {
  return callAgent("nightly", payload);
}
