// frictionStore.js
//
// Client side of the clinical support agent. Owns four things, in
// localStorage per signed-in user, next to sessionStore's keys:
//
//   result       the last nightly `friction` response (what the panel draws)
//   pendingPlan  tomorrow's if-then plan, waiting for the next session
//   trials       which support each session carried -> the experiment's data
//   vetoes       supports the person set aside ("Not this kind of step")
//
// Flow:
//   runNightlyIfDue()            first dashboard open of the day -> `friction`
//   attachPendingPlan()          right after createSession(): the plan joins
//                                that session's plans and a trial is recorded
//   markHeavy()                  the one-tap "this one feels heavy" -> `heavy`
//   setAside(plan)               veto; the agent never offers it again
//
// Nothing here scores or labels the person. The backend decides what counts
// as a finding; this file only carries data to it and back.

import { friction, heavy } from "./api";
import {
  addSessionPlan,
  getActivityHistory,
  getBoundUser,
  getSessionLog,
  getSnapshot,
  isOwnAppTab,
  subscribe,
} from "./sessionStore";

// Stored as `${KEY}:${Cognito id}`, the same scheme as sessionStore's keys,
// so two people on one browser never see each other's plans or trials.
// Nothing is read or written until App.jsx has called bindUser().
const KEY = "rethread_friction_v1";

// Main-experiment arms whose sessions must carry no if-then plans. Empty:
// in IntentPanel both arms carry the decompose plans, and the arm only
// decides whether the preview shows them open or one click away. The
// friction plan joins after that preview, so it goes into BOTH arms alike.
// Skipping one arm would put the plan in the other arm only, and the main
// experiment would count its effect as the arm's. Add an arm here only if
// its sessions ever start with no plans at all.
const NO_PLAN_CONDITIONS = new Set();
const MAX_TRIALS = 200;

// How long the first check waits for bindUser() before giving up.
const BIND_WAIT_MS = 10000;

const listeners = new Set();
let running = null; // in-flight nightly promise, so two panels share one call
let runningFor = null; // ...and whose data it is for

function empty() {
  return { v: 1, lastRunDay: null, lastRunAt: null, lastRunSig: null, result: null,
           pendingPlan: null, trials: [], vetoes: {}, feedback: [] };
}

/*
 * What a check was run on. "Once a day" alone was wrong: the check reads
 * sessions after they end, so a session that ended AFTER today's check was
 * not looked at until the next day, and the panel kept showing the older
 * answer. Same day plus the same archive means the answer cannot have
 * changed; anything new re-runs it.
 */
function archiveSig(data) {
  const last = data.events[data.events.length - 1];
  return `${data.sessions.length}|${data.events.length}|${last ? last.ts : ""}`;
}

function read(user) {
  try {
    const raw = localStorage.getItem(`${KEY}:${user}`);
    const v = raw ? JSON.parse(raw) : null;
    if (v && v.v === 1) return { ...empty(), ...v };
  } catch {
    // private mode, corrupt value: start clean
  }
  return empty();
}

let loadedFor = null; // the user whose data `state` holds
let state = empty();

// Demo results live in memory only. Recording the video must never touch the
// real result, pending plan, trials or vetoes.
let demoResult = null;

// Load the bound user's data if it isn't the one in memory: on first use
// after bindUser(), and again if someone else signs in. Returns the user.
function sync() {
  const user = getBoundUser();
  if (user !== loadedFor) {
    loadedFor = user;
    state = user ? read(user) : empty();
    demoResult = null;
  }
  return user;
}

function emit() {
  const snap = getFrictionSnapshot();
  listeners.forEach((cb) => cb(snap));
}

function save() {
  if (loadedFor) {
    try {
      localStorage.setItem(`${KEY}:${loadedFor}`, JSON.stringify(state));
    } catch (err) {
      console.error("friction store: localStorage error", err);
    }
  }
  emit();
}

export function subscribeFriction(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function getFrictionSnapshot() {
  sync();
  return {
    result: demoResult || state.result,
    pendingPlan: demoResult
      ? (demoResult.plan ? { ...demoResult.plan, demo: true } : null)
      : state.pendingPlan,
    lastRunAt: demoResult ? Date.now() : state.lastRunAt,
    vetoes: state.vetoes,
    trials: state.trials,
    running: !!running && runningFor === loadedFor,
  };
}

/*
 * App.jsx calls bindUser() once signed in, and if it does that in an effect,
 * React runs the panel's own effect first: the first check can arrive a
 * moment before anyone is bound. Wait for it rather than run on nobody's data.
 */
function whenBound() {
  const now = getBoundUser();
  if (now) return Promise.resolve(now);
  return new Promise((resolve, reject) => {
    let off = () => {};
    const timer = setTimeout(() => {
      off();
      const err = new Error("Not signed in");
      err.status = 401;
      reject(err);
    }, BIND_WAIT_MS);
    off = subscribe(() => {
      const user = getBoundUser();
      if (user) {
        clearTimeout(timer);
        off();
        resolve(user);
      }
    });
  });
}

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

// No user_id: the server takes identity from the Cognito token, and the
// Cognito id seeds the support experiment's arm order.
function basePayload() {
  return {
    trials: state.trials,
    vetoes: state.vetoes,
    tz_offset_min: new Date().getTimezoneOffset(),
    now: new Date().toISOString(),
  };
}

/*
 * What the detectors read: archived tabs plus the session log. Only the
 * fields they use are sent, so a session's location and due date stay in
 * this browser (tab titles and domains, never page content).
 *
 * A tab recorded after its session reached "done" is left out. The session
 * stays open after done, and the browsing that follows is not the work: the
 * check must never find a pattern in what someone did after finishing.
 */
function archive() {
  const sessions = getSessionLog();

  const endOf = new Map();
  for (const r of sessions) {
    const start = Date.parse(r?.started_at);
    const end = Date.parse(r?.ended_at);
    if (Number.isFinite(start) && Number.isFinite(end)) endOf.set(start, end);
  }

  const events = [];
  for (const e of getActivityHistory()) {
    if (!e) continue;
    // Dashboard tabs archived before sessionStore dropped them at the source.
    if (isOwnAppTab(e)) continue;
    const end = endOf.get(Number(e.session_started_at));
    const ts = Date.parse(e.ts);
    if (end !== undefined && Number.isFinite(ts) && ts > end) continue;
    const out = {
      ts: e.ts,
      title: e.title,
      domain: e.domain,
      session_intent: e.session_intent,
      session_started_at: e.session_started_at,
      session_ended_at: e.session_ended_at,
    };
    if (typeof e.dwell_seconds === "number") out.dwell_seconds = e.dwell_seconds;
    events.push(out);
  }

  return { events, sessions };
}

/* =====================================================
   NIGHTLY
===================================================== */

/*
 * "Nightly" = the first time the dashboard opens on a new day: yesterday's
 * archive goes to the agent once. This can't move to the EventBridge run:
 * the tab timeline is never stored server-side, so only the browser has what
 * the detectors read. The backend keeps nothing from this call. (A plan's
 * wording does reach DynamoDB later, inside the session object that amend
 * persists, like every other plan.)
 *
 * demo: true sends a built-in sample week instead of the real archive and
 * writes NOTHING back to the real trials, so recording the video can't
 * pollute anyone's data.
 */
export async function runNightly({ force = false, demo = false } = {}) {
  await whenBound();
  const user = sync();
  if (!user) return null;

  const data = demo ? buildDemoData() : archive();
  const sig = demo ? null : archiveSig(data);

  if (!force && !demo && state.lastRunDay === today() && state.lastRunSig === sig) {
    demoResult = null;
    // The panel may have drawn before this user's data was loaded.
    emit();
    return state.result;
  }
  if (running && runningFor === user) return running;

  const mine = (async () => {
    try {
      if (demo) {
        const result = await friction({ ...basePayload(), ...data, trials: [], vetoes: {} });
        if (sync() !== user) return null;
        demoResult = { ...result, _demo: true };
        return demoResult;
      }
      const result = await friction({ ...basePayload(), ...data });
      // Signed out or someone else signed in while this was in flight:
      // never write one user's result into another's data.
      if (sync() !== user) return null;
      demoResult = null;
      state.result = result;
      state.lastRunAt = Date.now();
      mergeTrialValues(result?.trial_values);
      state.lastRunDay = today();
      state.lastRunSig = sig;
      // A new plan replaces a pending one that no session picked up.
      state.pendingPlan = result?.plan ? { ...result.plan, createdAt: Date.now() } : null;
      return state.result;
    } finally {
      if (running === mine) {
        running = null;
        runningFor = null;
      }
      if (loadedFor === user) save();
      else emit();
    }
  })();

  running = mine;
  runningFor = user;
  emit();
  return mine;
}

export const runNightlyIfDue = () => runNightly();

/* =====================================================
   HAND THE PLAN TO THE NEXT SESSION
===================================================== */

/*
 * Each session's outcome is measured once, on the first night its tabs are
 * in the archive, and kept here. The archive only holds the last 500 tab
 * events, so without this an 8 + 8 experiment could never be decided.
 */
function mergeTrialValues(values) {
  if (!Array.isArray(values) || !values.length) return;
  const byKey = new Map(values.map((v) => [`${v.pattern}|${v.session_started_at}`, v.value]));
  state.trials = state.trials.map((t) => {
    const k = `${t.pattern}|${t.session_started_at}`;
    return t.value == null && byKey.has(k) ? { ...t, value: byKey.get(k) } : t;
  });
}

// Same work as the plan was written for? Shared words of 4+ letters.
function sameWork(a, b) {
  const words = (s) => new Set(String(s || "").toLowerCase().match(/[a-z0-9]{4,}/g) || []);
  const wa = words(a);
  const wb = words(b);
  if (!wa.size || !wb.size) return false;
  let shared = 0;
  wa.forEach((w) => { if (wb.has(w)) shared += 1; });
  return shared / Math.min(wa.size, wb.size) >= 0.5;
}

function recordTrial(plan, sessionStartedAt) {
  if (!plan || !sessionStartedAt) return;
  state.trials = [
    ...state.trials,
    { pattern: plan.pattern, support: plan.support,
      session_started_at: sessionStartedAt },
  ].slice(-MAX_TRIALS);
}

/*
 * Call once, right after createSession(). The plan becomes one of the
 * session's own if-then plans, so it shows in the Re-entry panel's
 * "If you get stuck" tile and churn lights it up live. Returns the plan
 * attached, or null.
 */
export function attachPendingPlan() {
  if (!sync()) return null;
  const plan = state.pendingPlan;
  const snap = getSnapshot();
  // One plan, one session. Carrying it into every session of the day would
  // give one arm several correlated trials from a single night.
  if (!plan || plan.attachedTo || demoResult || !snap.hasSession) return null;
  // Stays pending for the next session that can take a plan.
  if (NO_PLAN_CONDITIONS.has(snap.condition)) return null;

  // The plan names yesterday's work ("back on Q3 summary"). A session on
  // something else gets the plain version with the new goal put in.
  const specific = sameWork(plan.goal, snap.intent) || !plan.generic;
  const text = specific
    ? { if: plan.if, then: plan.then }
    : {
        if: plan.generic.if.replace("{goal}", snap.intent),
        then: plan.generic.then.replace("{goal}", snap.intent),
      };

  addSessionPlan({ ...text, source: "friction",
                   pattern: plan.pattern, support: plan.support });
  recordTrial(plan, snap.sessionStartedAt);
  state.pendingPlan = { ...plan, attachedTo: snap.sessionStartedAt, attachedText: text };
  save();
  return plan;
}

/* =====================================================
   "THIS ONE FEELS HEAVY"
===================================================== */

/*
 * User-stated, one tap, during a live session. The plan goes straight into
 * this session, whatever its experiment arm: someone asking for help gets
 * it. In a first_step_only session that bends the arm slightly. It's rare,
 * and the trial below records which session it happened in. The session's
 * start latency is the outcome the experiment reads later, same as every
 * other session.
 */
export async function markHeavy() {
  const user = sync();
  const snap = getSnapshot();
  if (!user || !snap.hasSession) throw new Error("No session running");
  const result = await heavy({
    ...basePayload(),
    goal: snap.intent,
    work: null,
    ...archive(),
  });
  // Only into the session the tap came from. If that one ended while the
  // plan was being written, nothing is added, and the panel says so.
  const now = getSnapshot();
  if (sync() !== user || now.sessionStartedAt !== snap.sessionStartedAt) {
    return { ...result, plan: null };
  }
  if (result?.plan) {
    addSessionPlan({ if: result.plan.if, then: result.plan.then,
                     source: "friction", pattern: "heavy",
                     support: result.plan.support });
    recordTrial(result.plan, snap.sessionStartedAt);
    save();
  }
  return result;
}

/* =====================================================
   FEEDBACK
===================================================== */

/*
 * "Not this kind of step": a veto. That support is never offered for that
 * pattern again, and its experiment stops. It is not a rating -- a tally of
 * taps would be a learner that reports "reset works for you" after three,
 * which is exactly what the gate exists to prevent.
 */
export function setAside(plan) {
  if (!plan?.pattern || !plan?.support) return;
  if (!sync()) return;
  if (demoResult) return; // the sample week never writes a real veto
  const cur = new Set(state.vetoes[plan.pattern] || []);
  cur.add(plan.support);
  state.vetoes = { ...state.vetoes, [plan.pattern]: [...cur] };
  state.feedback = [...state.feedback,
    { at: Date.now(), pattern: plan.pattern, support: plan.support, kind: "set_aside" }].slice(-100);
  if (state.pendingPlan?.support === plan.support && !state.pendingPlan.attachedTo) {
    state.pendingPlan = null;
  }
  save();
}

export function undoSetAside(pattern, support) {
  if (!sync()) return;
  if (demoResult) return;
  const cur = (state.vetoes[pattern] || []).filter((s) => s !== support);
  state.vetoes = { ...state.vetoes, [pattern]: cur };
  save();
}

/* =====================================================
   DEMO WEEK -- for recording the video. Sent to the
   backend as data; never written into the real archive.
===================================================== */

export function buildDemoData() {
  const events = [];
  const sessions = [];
  const day = 86400000;
  const base = new Date();
  base.setHours(14, 0, 0, 0);

  const DOC = ["Q3 summary - Google Docs", "docs.google.com"];
  const SHEET = ["Q3 revenue figures - Google Sheets", "docs.google.com"];
  const MAIL = ["Inbox (4) - Gmail", "mail.google.com"];
  const WIKI = ["Fiscal quarter - Wikipedia", "en.wikipedia.org"];

  // [daysAgo, latencySeconds, [[offsetSeconds, title, domain], ...]]
  const week = [
    [1, 240, [[240, ...DOC], [1200, ...DOC], [1260, ...SHEET], [1300, ...DOC],
              [1340, ...SHEET], [1400, ...DOC], [1460, ...SHEET], [1520, ...DOC],
              [1580, ...WIKI], [1900, ...DOC]]],
    [2, 1260, [[1260, ...DOC], [1500, ...MAIL], [1530, ...DOC], [1740, ...MAIL],
               [1770, ...DOC], [1980, ...MAIL], [2010, ...DOC], [2220, ...MAIL],
               [2250, ...DOC], [2460, ...MAIL], [2490, ...DOC]]],
    [3, 300, [[300, ...DOC], [1500, ...DOC], [1560, ...SHEET], [1600, ...DOC],
              [1650, ...SHEET], [1700, ...DOC], [1760, ...SHEET], [1800, ...DOC],
              [1860, ...WIKI], [2400, ...SHEET]]],
    [5, 1080, [[1080, ...DOC], [2000, ...SHEET], [2800, ...DOC]]],
  ];

  for (const [ago, latency, spec] of week) {
    const start = base.getTime() - ago * day;
    const end = start + 60 * 60000;
    sessions.push({
      started_at: new Date(start).toISOString(),
      ended_at: new Date(end).toISOString(),
      intent: "write the Q3 summary report",
      initiation_latency_s: latency,
      condition: null,
      event_count: spec.length,
      completed: true,
      end_reason: "done",
    });
    for (const [o, title, domain] of spec) {
      events.push({
        ts: new Date(start + o * 1000).toISOString(),
        title,
        domain,
        session_intent: "write the Q3 summary report",
        session_started_at: start,
        session_ended_at: end,
      });
    }
  }
  return { events, sessions };
}
