// api.js
// Direct client for the Lambda Function URL. One endpoint, action-routed,
// per API_CONTRACT.md. Response is always {statusCode, body: "<json>"} —
// body is a JSON-encoded STRING, parsed here once so every caller gets
// plain objects.

// TODO: set VITE_LAMBDA_URL in .env.local (gitignored) — never hardcode
// the real URL here, since this file goes into a public repo.
import { OIDC_USER_KEY } from "./authConfig";

const FUNCTION_URL = import.meta.env.VITE_LAMBDA_URL;

if (!FUNCTION_URL) {
  console.error(
    "VITE_LAMBDA_URL is not set. Create a .env.local file with:\n" +
    "  VITE_LAMBDA_URL=<your friend's Lambda Function URL>\n" +
    "then restart `npm run dev`."
  );
}

// The Lambda verifies a Cognito ACCESS token on every call and takes the
// user's identity from it (never from the body). react-oidc-context keeps
// the signed-in user in sessionStorage; read it there so this module needs
// no React hook.
function getAccessToken() {
  try {
    const raw = sessionStorage.getItem(OIDC_USER_KEY);
    if (!raw) return null;
    const user = JSON.parse(raw);
    return user?.access_token || null;
  } catch {
    return null;
  }
}

async function callAgent(action, payload = {}) {
  const token = getAccessToken();
  if (!token) {
    const err = new Error("Not signed in");
    err.status = 401;
    throw err;
  }

  const res = await fetch(FUNCTION_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ action, ...payload }),
  });

  let data = null;
  try {
    data = await res.json();
  } catch {
    // Non-JSON error page (e.g. a 502 from the Function URL itself).
  }

  // Lambda Function URLs auto-unwrap {statusCode, body} into the real
  // HTTP response — `data` here is usually already the final result.
  // Defensive check for the other shape too, in case that ever changes
  // (e.g. calling through a raw SDK invoke or a proxy later).
  const looksWrapped =
    data && typeof data.statusCode === "number" && typeof data.body === "string";
  const body = looksWrapped ? JSON.parse(data.body) : data;

  if (!res.ok || (looksWrapped && data.statusCode >= 400)) {
    const err = new Error(body?.error || `Backend error ${res.status}`);
    err.status = looksWrapped ? data.statusCode : res.status;
    throw err;
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

  // Failures still default to relevant (never invent a lapse), but they are
  // FLAGGED, and not cached. sessionStore must not treat a failed check as
  // "this tab was on-task": that is how a 401 turned every session's first
  // tab into its initiation time.
  try {
    const result = await callAgent("drift", { intent, title, domain });
    if (result?._fallback) {
      return { ...result, _failed: true };
    }
    driftCache.set(key, result);
    return result;
  } catch (err) {
    console.error("Drift check failed, defaulting to relevant:", err);
    return {
      relevant: true,
      reason: "call failed, defaulted to relevant",
      _failed: true,
    };
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
//
// Every string in the response has passed the backend guardrail (no drift
// or focus language, nothing about the person, key_tabs traced to real
// events). Two flags can appear:
//   _evidence_replaced: true — the reconstruction was kept, but its
//                              evidence text was swapped for a neutral line
//   _fallback: true          — nothing safe to show; session_found is false
// Both render exactly like any other response.
// ---------------------------------------------------------------------------

export async function reentry({ declared_intent, events }) {
  return callAgent("reentry", { declared_intent, events });
}

// ---------------------------------------------------------------------------
// amend — one chat turn mid-session. Response:
//   kind          "continue" | "scope_change" | "done"
//   response      the chat reply. THIS is the message bubble.
//   first_action  the step that stands now. Never null unless kind is "done".
//   action_unchanged  true when the step is the same one they already had
//   plans         only the NEW if-then plans from this turn (usually 0-1)
//   note          short internal record. Don't render it.
//   session       the updated session. Store it and send it back unchanged
//                 on the next call — that is the chat's whole memory.
//
// So: send getSessionForAmend(), then pass the WHOLE response to
// applyAmendResult() and render `response`.
// ---------------------------------------------------------------------------

export async function amend({ session, message }) {
  return callAgent("amend", { session, message });
}

// ---------------------------------------------------------------------------
// assign / logSession / nightly — the experiment loop.
//
// All three are per-user on the server: identity comes from the token,
// the arm order is seeded by the user's Cognito id, and session_index is
// counted from that user's logged sessions in DynamoDB. Nothing the
// browser sends can pick another user's data.
// ---------------------------------------------------------------------------

// Call at session start; store the returned `condition` with
// createSession({ condition }). session_index is only a fallback now.
export async function assignArm({ arms, session_index } = {}) {
  return callAgent("assign", { arms, session_index });
}

// One finished session's measurements -> DynamoDB (EXP#<started_at>).
// Called by sessionStore on endSession(). Idempotent: same started_at,
// same item.
export async function logSession(record) {
  return callAgent("log_session", { record });
}

// Runs the gate (and analyst, if cleared) on THIS user's logged sessions
// from DynamoDB. The same analysis runs nightly for every user on the
// EventBridge schedule. `arms` is optional; pass it if old experiments'
// conditions are still in the table.
export async function nightly({ arms, metric_label } = {}) {
  return callAgent("nightly", { arms, metric_label });
}
