// sessionStore.js

import { drift, logSession, saveSession, history as fetchHistory } from "./api";

const state = {
  intent: null,
  // Where the work lives and when it's due, as the user typed them.
  location: null,
  dueDate: null,
  firstAction: null,
  plans: [],
  events: [],
  chatHistory: [],
  elapsedSeconds: 0,
  running: false,

  // The backend's own session object, stored verbatim and sent back on
  // every amend call. It carries the chat history, the clarify answers
  // (notes), scope changes and `done`. The backend's memory, grounding
  // check and don't-ask-twice rule all read those fields, so rebuilding
  // the session from intent/step/plans alone silently switches them off.
  agentSession: null,
  done: false,
  // When the session reached "done". Its duration ends here.
  doneAt: null,

  // Nightly experiment
  condition: null,
  sessionStartedAt: null,
  firstActionAt: null,

  // Tabs already judged for initiation this session (see checkInitiation).
  initiationChecked: {},

  // Picked up from the account after starting on another device. This
  // browser never saw it start, so it takes no start-time measurement.
  restored: false,
};

const listeners = new Set();
const eventListeners = new Set();

let tickHandle = null;

const ACTIVITY_HISTORY_KEY =
  "rethread_activity_history_v1";

const NIGHTLY_SESSIONS_KEY =
  "rethread_nightly_sessions_v1";

// Every finished session, experiment or not, for the friction check.
// Separate from NIGHTLY_SESSIONS_KEY on purpose: that key feeds the main
// experiment, and only sessions with an experiment condition belong in it.
// Per user like every other key here (userKey adds the Cognito id).
const SESSION_LOG_KEY =
  "rethread_session_log_v1";

// The live session, so a reload or a crash doesn't lose it.
const ACTIVE_SESSION_KEY =
  "rethread_active_session_v1";

// Sessions ended in this browser. One of them is never picked back up from
// the account, even if its end didn't reach the server.
const ENDED_SESSIONS_KEY =
  "rethread_ended_sessions_v1";

// An unfinished session on the account is continued here only if it started
// this recently. Older ones are listed, not reopened.
const RESTORE_WINDOW_MS = 12 * 60 * 60 * 1000;

const PERSIST_MAX_EVENTS = 500;
const PERSIST_MAX_CHAT = 200;

// At most one drift call per distinct tab, and at most this many per
// session, before the first on-task tab is found.
const MAX_INITIATION_CHECKS = 50;

// Tabs whose drift call is still in flight. This page only.
const initiationPending = new Set();

/* =====================================================
   PER-USER STORAGE
===================================================== */

/*
 * Every localStorage key is suffixed with the signed-in user's Cognito id.
 * The keys used to be global, so two people signing in on one browser
 * shared one active session, one activity history and one set of nightly
 * records: user B's dashboard showed user A's work.
 *
 * Until bindUser() runs, there is no user, so nothing is read or written.
 * App.jsx calls bindUser(auth.user.profile.sub) once signed in, and that is
 * also when the saved session is restored (it used to restore at import,
 * before anyone was known).
 */
let currentUser = null;

function userKey(base) {
  return currentUser ? `${base}:${currentUser}` : null;
}

export function bindUser(sub) {
  if (!sub || sub === currentUser) return;

  currentUser = sub;

  // Another person's list must never show, even for a moment.
  serverSessions = [];
  serverStatus = "idle";

  // Drop whatever an earlier user left in memory, then load this user's.
  clearSessionState();
  restoreActiveSession();
  notify();

  // Then this account's sessions from DynamoDB, from any device. After this
  // render, not during it.
  setTimeout(() => {
    syncFromServer();
  }, 0);
}

// The Cognito id bindUser() was given, or null. frictionStore.js keys its
// own data with it, so friction plans and trials are per user too.
export function getBoundUser() {
  return currentUser;
}

/* =====================================================
   LOCAL STORAGE
===================================================== */

function readJson(baseKey, fallback = []) {
  const key = userKey(baseKey);
  if (!key) return fallback;
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(baseKey, value) {
  const key = userKey(baseKey);
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    console.error("localStorage error:", err);
  }
}

/* =====================================================
   ACTIVE SESSION (survives a reload)
===================================================== */

/*
 * The live session used to exist only in memory, so a reload, a crash or a
 * restarted browser lost the goal, the step, the chat, the condition and
 * every captured tab, and the re-entry panel had nothing to hand back at the
 * one moment it exists for. It is now saved as it changes and restored when
 * the app loads. endSession() removes it.
 *
 * User actions (a chat turn, a new session) are written at once. Tab events
 * arrive in bursts, so those writes are batched.
 */

let persistTimer = null;

function persistSoon() {
  if (persistTimer) return;
  persistTimer = setTimeout(persistNow, 500);
}

function persistNow() {
  if (persistTimer) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }

  if (typeof localStorage === "undefined") return;

  const activeKey = userKey(ACTIVE_SESSION_KEY);
  if (!activeKey) return;

  if (!state.intent && !state.sessionStartedAt) {
    try {
      localStorage.removeItem(activeKey);
    } catch {
      // no storage (private mode, tests)
    }
    return;
  }

  writeJson(ACTIVE_SESSION_KEY, {
    v: 1,
    intent: state.intent,
    location: state.location,
    dueDate: state.dueDate,
    firstAction: state.firstAction,
    plans: state.plans,
    events: state.events.slice(-PERSIST_MAX_EVENTS),
    chatHistory: state.chatHistory.slice(-PERSIST_MAX_CHAT),
    elapsedSeconds: state.elapsedSeconds,
    running: state.running,
    agentSession: state.agentSession,
    done: state.done,
    doneAt: state.doneAt,
    condition: state.condition,
    sessionStartedAt: state.sessionStartedAt,
    firstActionAt: state.firstActionAt,
    initiationChecked: state.initiationChecked,
    restored: state.restored,
  });
}

function applySaved(saved) {
  state.intent = saved.intent ?? null;
  state.location = saved.location ?? null;
  state.dueDate = saved.dueDate ?? null;
  state.firstAction = saved.firstAction ?? null;
  state.plans = Array.isArray(saved.plans) ? saved.plans : [];
  state.events = Array.isArray(saved.events) ? saved.events : [];
  state.chatHistory = Array.isArray(saved.chatHistory) ? saved.chatHistory : [];
  state.elapsedSeconds = Number(saved.elapsedSeconds) || 0;
  state.running = !!saved.running;
  state.agentSession = saved.agentSession ?? null;
  state.done = !!saved.done;
  state.doneAt = saved.doneAt ?? null;
  state.condition = saved.condition ?? null;
  state.sessionStartedAt = saved.sessionStartedAt ?? null;
  state.firstActionAt = saved.firstActionAt ?? null;
  state.initiationChecked =
    saved.initiationChecked && typeof saved.initiationChecked === "object"
      ? saved.initiationChecked
      : {};
  state.restored = !!saved.restored;

  if (state.running) startTicking();
}

function restoreActiveSession() {
  const saved = readJson(ACTIVE_SESSION_KEY, null);
  if (!saved || saved.v !== 1) return;
  if (!saved.intent && !saved.sessionStartedAt) return;
  applySaved(saved);
}

/*
 * Another tab of the app saved the session. Take its copy, so this tab's
 * next write doesn't put back an older chat or step over it.
 *
 * Built for ONE dashboard tab, where this never runs: the storage event only
 * fires in other tabs. With two, localStorage is last-writer-wins. A batched
 * tab-event write from one tab can land a few milliseconds after the other
 * tab saved a chat turn, and that turn is then replaced by the older copy.
 * Tab events are rarely at risk, since every open app tab receives them
 * (a tab still loading can miss one). The fix, if two tabs ever matter: a
 * revision counter on user actions, so an older copy can't replace a newer
 * chat.
 */
function onStorage(event) {
  const activeKey = userKey(ACTIVE_SESSION_KEY);
  if (!activeKey || event.key !== activeKey) return;

  let saved = null;
  try {
    saved = event.newValue ? JSON.parse(event.newValue) : null;
  } catch {
    return;
  }

  if (persistTimer) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }

  if (saved && saved.v === 1) {
    applySaved(saved);
  } else {
    // Ended in the other tab, which already archived it.
    clearSessionState();
  }

  notify();
}

function clearSessionState() {
  state.intent = null;
  state.location = null;
  state.dueDate = null;
  state.firstAction = null;
  state.plans = [];

  state.agentSession = null;
  state.done = false;
  state.doneAt = null;

  state.events = [];
  state.chatHistory = [];

  state.elapsedSeconds = 0;
  state.running = false;

  state.condition = null;

  state.sessionStartedAt = null;
  state.firstActionAt = null;

  state.initiationChecked = {};
  initiationPending.clear();

  state.restored = false;
}

/* =====================================================
   SNAPSHOT / SUBSCRIBE
===================================================== */

function makeSnapshot() {
  return {
    intent: state.intent,
    location: state.location,
    dueDate: state.dueDate,
    firstAction: state.firstAction,
    plans: state.plans,
    events: state.events,
    chatHistory: state.chatHistory,
    elapsedSeconds: state.elapsedSeconds,
    running: state.running,

    condition: state.condition,
    sessionStartedAt: state.sessionStartedAt,

    agentSession: state.agentSession,
    done: state.done,

    hasSession: state.intent !== null,
  };
}

export function getSnapshot() {
  return makeSnapshot();
}

export function getState() {
  return state;
}

export function subscribe(callback) {
  listeners.add(callback);

  return () => {
    listeners.delete(callback);
  };
}

function notify() {
  const snap = makeSnapshot();

  listeners.forEach((callback) => {
    callback(snap);
  });
}

/* =====================================================
   TIMER
===================================================== */

function startTicking() {
  if (tickHandle) return;

  tickHandle = setInterval(() => {
    if (state.running) {
      state.elapsedSeconds += 1;

      if (state.elapsedSeconds % 30 === 0) {
        persistSoon();
      }
    }

    notify();
  }, 1000);
}

export function beginTimer() {
  state.running = true;
  startTicking();
  persistNow();
  notify();
}

export function pause() {
  state.running = false;
  persistNow();
  notify();
}

export function resume() {
  state.running = true;
  startTicking();
  persistNow();
  notify();
}

export function updateElapsed(seconds) {
  state.elapsedSeconds = seconds;
  persistSoon();
  notify();
}

export function setRunning(value) {
  state.running = value;
  persistNow();
  notify();
}

/* =====================================================
   CHROME EXTENSION EVENTS
===================================================== */

function onExtensionMessage(event) {
  if (event.source !== window) return;

  if (
    event.data?.source !==
    "rethread-extension"
  ) {
    return;
  }

  const payload = event.data.payload || {};

  if (!payload.title || !payload.domain) {
    return;
  }

  const newEvent = {
    ts:
      payload.ts ||
      new Date().toISOString(),

    title: payload.title,

    domain: payload.domain,
  };

  recordEvent(newEvent);
}

/*
 * One listener of each kind per page, however many times this module runs.
 * Vite's hot reload re-evaluates the module when it changes, and each run
 * used to add another "message" listener. Every one of them kept recording
 * tabs and making drift calls, into copies of the store the page had
 * stopped using. The previous run's listener is now removed first.
 */
function listenOnce(target, type, handler) {
  const slot = `__rethread_${type}`;
  const prev = window[slot];

  if (prev) {
    prev.target.removeEventListener(type, prev.handler);
  }

  target.addEventListener(type, handler);
  window[slot] = { target, handler };
}

/*
 * One path for every captured event, from the extension or from addEvent().
 */
function recordEvent(event) {
  // The dashboard itself is not activity (see isOwnAppTab). Dropped here,
  // before it is stored, so it never reaches the initiation check, churn,
  // the re-entry trail or the archive the friction check reads.
  if (isOwnAppTab(event)) return;

  state.events.push(event);

  checkInitiation(event);

  eventListeners.forEach((callback) => {
    callback(event);
  });

  persistSoon();
  notify();
}

/*
 * initiation_latency_s is the time from session start to the first tab
 * that is part of the declared work.
 *
 * It used to be the first captured tab of ANY kind, so opening WhatsApp
 * counted as "started". That is the one number the nightly experiment
 * compares, so the definition matters more than anything else here.
 *
 * Cost is bounded here, not left to api.js's cache: one drift call per
 * distinct tab (unread counters like "(3)" ignored), none once a relevant
 * tab is found, and at most MAX_INITIATION_CHECKS per session. A repeat of a
 * tab can't change the answer: if that tab was on-task, firstActionAt is
 * already at or before its first visit; if it wasn't, it still isn't.
 *
 * drift fails open (relevant) on any error, which degrades to the old
 * behaviour rather than losing the measurement. The earliest relevant tab
 * wins even if its check returns late.
 */
function initiationKey(event) {
  const title = String(event.title || "")
    .replace(/^\(\d+\)\s*/, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

  const domain = String(event.domain || "").trim().toLowerCase();
  const intent = String(state.intent || "").trim().toLowerCase();

  // The session is part of the key, so a check still in flight from an
  // earlier session can't block the same tab in a new one.
  return `${state.sessionStartedAt}|${intent}|${domain}|${title}`;
}

function initiationChecks() {
  return Object.keys(state.initiationChecked).length + initiationPending.size;
}

/*
 * A tab of this dashboard. The extension already skips the app's own pages,
 * but only the URLs listed in its manifest.json, so the dashboard on another
 * port (or on the deployed URL, if it isn't listed there) still came through
 * and was judged like any other tab. The page knows its own host, so it
 * checks too: the same host as this page is the dashboard, wherever it is
 * served. Locally, localhost and 127.0.0.1 both count. On the deployed site
 * a localhost tab is someone's own work and is kept.
 */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

export function isOwnAppTab(event) {
  const domain = String(event?.domain || "")
    .trim()
    .toLowerCase()
    .replace(/:\d+$/, "");
  if (!domain) return false;

  const own =
    typeof window !== "undefined" && window.location
      ? String(window.location.hostname || "").toLowerCase()
      : "";

  if (own && domain === own) return true;

  return LOCAL_HOSTS.has(domain) && (!own || LOCAL_HOSTS.has(own));
}

function checkInitiation(event) {
  if (!state.sessionStartedAt || !state.intent) return;
  // Continued from another device: the start happened there, so there is
  // nothing to measure here.
  if (state.restored) return;
  if (isOwnAppTab(event)) return;

  const ts = new Date(event.ts).getTime();
  if (Number.isNaN(ts) || ts < state.sessionStartedAt) return;
  if (state.firstActionAt && state.firstActionAt <= ts) return;

  const key = initiationKey(event);
  if (state.initiationChecked[key] || initiationPending.has(key)) return;
  if (initiationChecks() >= MAX_INITIATION_CHECKS) return;

  initiationPending.add(key);

  const startedAt = state.sessionStartedAt;

  drift({
    intent: state.intent,
    title: event.title || "",
    domain: event.domain || "",
  })
    .then((result) => {
      // The session ended or restarted while the check was in flight.
      if (state.sessionStartedAt !== startedAt) return;

      state.initiationChecked[key] = true;

      // A failed check says nothing about this tab. Counting it as on-task
      // would record initiation at whatever tab was open when the backend
      // was down (or the token had expired). Leave it unmeasured instead:
      // analyse() counts a null latency as "missing", which is honest.
      if (result && !result._failed && result.relevant !== false) {
        if (!state.firstActionAt || ts < state.firstActionAt) {
          state.firstActionAt = ts;
          notify();
        }
      }

      persistSoon();
    })
    .catch(() => {})
    .finally(() => {
      initiationPending.delete(key);
    });
}

export function subscribeEvents(callback) {
  eventListeners.add(callback);

  return () => {
    eventListeners.delete(callback);
  };
}

/* =====================================================
   SESSION
===================================================== */

/*
 * The user's own words about where the work lives and when it's due, as one
 * string for the backend's `notes`. Plain labels, the user's text unchanged,
 * so the grounding check finds their exact words in it.
 */
function buildNotes(answers, location, dueDate) {
  const parts = [];
  if (answers && String(answers).trim()) parts.push(String(answers).trim());
  if (location && String(location).trim()) {
    parts.push(`Where: ${String(location).trim()}`);
  }
  if (dueDate && String(dueDate).trim()) {
    parts.push(`Due: ${String(dueDate).trim()}`);
  }
  return parts.join(". ");
}

export function createSession({
  goal,
  // Where the work lives and when it's due, if the UI asks for them as
  // separate fields. Either these or `answers` (or both) may be given.
  location = null,
  dueDate = null,
  firstAction,
  plans,
  condition = null,
  // What they typed in reply to the clarify questions (where the work
  // lives, when it's due). The backend grounds every later turn on it.
  answers = "",
  // The `session` object from the `initiate` action, if the UI used it.
  // Stored verbatim when present.
  session = null,
}) {
  // Starting a new session used to overwrite the old one without archiving
  // it, so a session that ended by starting the next one was never logged.
  // Nothing in the UI has to remember to call endSession() first now.
  if (state.sessionStartedAt) {
    archiveCurrentSession("replaced");
  }

  const startedAt = Date.now();

  state.intent = goal || null;
  state.location = location || null;
  state.dueDate = dueDate || null;
  state.firstAction = firstAction || null;
  state.plans = plans || [];

  // Same shape as new_session() in initiate_probe.py.
  state.doneAt = null;

  const base = session || {
    declared_intent: goal || null,
    // The backend grounds every chat turn on `notes`: a file or doc the
    // model may name has to appear in something the user said. Location and
    // due date are exactly that, so they go in here too, not just into the
    // UI's own state. Without this the chat could never mention the place
    // the user told us the work lives.
    notes: buildNotes(answers, location, dueDate),
    first_action: firstAction || null,
    plans: plans || [],
    condition,
    history: [],
    done: false,
  };

  // Plus what lets the account keep it: an id made here, so the start save,
  // every chat turn and the end save all update one DynamoDB item; when it
  // started; and where and when, so another device can show them.
  state.agentSession = {
    ...base,
    session_id: base.session_id || newSessionId(startedAt),
    started_at: new Date(startedAt).toISOString(),
    ended_at: null,
    location: location || null,
    due_date: dueDate || null,
  };
  state.done = false;

  state.events = [];
  state.chatHistory = [];

  state.elapsedSeconds = 0;
  state.running = false;

  state.condition = condition;

  state.sessionStartedAt = startedAt;

  state.firstActionAt = null;

  state.initiationChecked = {};
  initiationPending.clear();

  state.restored = false;

  persistNow();
  saveToServer(state.agentSession);
  notify();
}

export function hasSession() {
  return state.intent !== null;
}

/* =====================================================
   INTENT
===================================================== */

export function getDeclaredIntent() {
  return state.intent;
}

/* =====================================================
   CONDITION
===================================================== */

export function setSessionCondition(condition) {
  state.condition = condition;
  persistNow();
  notify();
}

export function getSessionCondition() {
  return state.condition;
}

/* =====================================================
   EVENTS
===================================================== */

export function addEvent(event) {
  recordEvent(event);
}

export function getEvents() {
  return state.events;
}

/* =====================================================
   EVENT PAYLOAD
===================================================== */

/*
 * dwell_seconds: a visit lasts until the next tab. The newest tab has no
 * next tab yet, so it lasts until now: the real time since they switched to
 * it. It used to be a flat 60s, which drew the current tab as one minute
 * and told churn it was a short visit. It keeps the same 30-minute ceiling
 * as every other visit, since that span can include time away. Consumers
 * that need to treat it as open-ended can: it is always the last entry.
 */
export function getEventsPayload() {
  const now = Date.now();
  const lastIndex = state.events.length - 1;

  return state.events.map((event, index) => {
    const current =
      new Date(event.ts).getTime();

    const end =
      index < lastIndex
        ? new Date(
            state.events[index + 1].ts
          ).getTime()
        : now;

    // Only when a timestamp can't be read.
    let dwellSeconds = 60;

    if (
      !Number.isNaN(current) &&
      !Number.isNaN(end)
    ) {
      dwellSeconds = Math.max(
        0,
        Math.min(
          1800,
          Math.round(
            (end - current) / 1000
          )
        )
      );
    }

    return {
      ts: event.ts,
      title: event.title || "",
      domain: event.domain || "",
      dwell_seconds: dwellSeconds,
    };
  });
}

/* =====================================================
   CHAT
===================================================== */

export function addChatMessage(message) {
  state.chatHistory.push(message);
  persistNow();
  notify();
}

export function getChatHistory() {
  return state.chatHistory;
}

/* =====================================================
   AMEND
===================================================== */

/*
 * Send this back EXACTLY as the backend last returned it. The fallback
 * shape only covers sessions created before this store kept it.
 */
export function getSessionForAmend() {
  if (state.agentSession) {
    return state.agentSession;
  }

  return {
    declared_intent:
      state.intent,

    notes: "",

    first_action:
      state.firstAction,

    plans:
      state.plans,

    history: [],

    done: state.done,
  };
}

export function getAgentSession() {
  return state.agentSession;
}

/*
 * The last thing the user typed in this session, in their own words.
 * The re-entry panel shows it when the tabs don't show the work.
 */
export function getLastUserMessage() {
  const history =
    state.agentSession?.history || [];

  for (let i = history.length - 1; i >= 0; i--) {
    const said = history[i]?.said;
    if (typeof said === "string" && said.trim()) {
      return said.trim();
    }
  }

  // Older sessions: fall back to the UI's own chat log. The message shape
  // isn't fixed, so accept the common field names.
  for (let i = state.chatHistory.length - 1; i >= 0; i--) {
    const m = state.chatHistory[i] || {};
    const role = m.role || m.from || m.sender || m.author;
    const text = m.text || m.content || m.message;
    if (role === "user" && typeof text === "string" && text.trim()) {
      return text.trim();
    }
  }

  return null;
}

export function getChatSession() {
  return {
    intent: state.intent,

    firstAction:
      state.firstAction,

    plans:
      state.plans,

    events:
      getEventsPayload(),

    elapsedSeconds:
      state.elapsedSeconds,
  };
}

/*
 * Pass the WHOLE amend response here, not just first_action and plans.
 *
 * The backend returns the updated `session`, and that object is the source
 * of truth: it has the merged plans, the new goal after a scope change, and
 * `done`. The response's own `plans` field holds only the NEW plans from
 * this turn, so copying it over state.plans used to wipe the existing ones.
 */
export function applyAmendResult(result = {}) {
  const wasDone = state.done;

  const {
    first_action,
    plans,
    session,
    kind,
  } = result;

  if (session && typeof session === "object") {
    state.agentSession = session;
    // The backend saved this turn to the account; keep the list in step.
    rememberServerSession(session);

    if (session.declared_intent) {
      state.intent = session.declared_intent;
    }

    state.plans = session.plans || [];

    state.done = !!session.done;

    if (session.done) {
      state.firstAction = null;
    } else if (session.first_action) {
      state.firstAction = session.first_action;
    }
  } else {
    // Old response shape, no session attached. `plans` here are only the
    // new ones from this turn, so they merge in rather than replace.
    if (first_action !== undefined) {
      state.firstAction =
        first_action;
    }

    if (Array.isArray(plans) && plans.length) {
      const key = (p) =>
        `${p?.if ?? ""}|${p?.then ?? ""}`.trim().toLowerCase();
      const seen = new Set(state.plans.map(key));
      state.plans = [
        ...state.plans,
        ...plans.filter((p) => !seen.has(key(p))),
      ].slice(-5);
    }
  }

  if (kind === "done") {
    state.done = true;
    state.firstAction = null;
  }

  // Log a finished session the moment it finishes, not whenever the next
  // one happens to start: tonight's analysis should include today's work.
  // Archiving later resends the same record, which the server overwrites.
  if (!wasDone && state.done) {
    state.doneAt = Date.now();
    sendExperimentRecord(buildExperimentRecord("done", state.doneAt));
  }

  persistNow();
  notify();
}

/*
 * Add one if-then plan to the live session (the friction check's plan).
 *
 * It goes into BOTH state.plans (what the Re-entry panel's "If you get
 * stuck" tile shows) and agentSession.plans (what amend reads and returns),
 * so the next amend turn keeps it instead of overwriting it. Duplicates are
 * ignored. Extra fields (source, pattern) are dropped: plans stay {if, then}.
 */
export function addSessionPlan(plan) {
  if (!plan || !(plan.if || plan.then) || state.intent === null) return;

  const key = (p) =>
    `${p?.if ?? ""}|${p?.then ?? ""}`.trim().toLowerCase();
  if (state.plans.some((p) => key(p) === key(plan))) return;

  const clean = { if: plan.if, then: plan.then };
  state.plans = [...state.plans, clean];

  if (state.agentSession) {
    state.agentSession = {
      ...state.agentSession,
      plans: [...(state.agentSession.plans || []), clean],
    };
  }

  persistNow();
  notify();
}

/* =====================================================
   NIGHTLY DATA
===================================================== */

export function getActivityHistory() {
  return readJson(
    ACTIVITY_HISTORY_KEY,
    []
  );
}

export function getNightlySessions() {
  return readJson(
    NIGHTLY_SESSIONS_KEY,
    []
  );
}

// Every finished session, for the friction check (see SESSION_LOG_KEY).
export function getSessionLog() {
  return readJson(
    SESSION_LOG_KEY,
    []
  );
}

/* =====================================================
   ARCHIVE COMPLETED SESSION
===================================================== */

/*
 * One session's measurements, as the nightly analysis stores them. null when
 * the session is not part of the experiment (no condition was assigned).
 *
 * A finished session ends when it reached "done", not when the next one
 * starts, so its duration doesn't include however long the tab sat open
 * afterwards.
 */
function buildExperimentRecord(reason, now = Date.now()) {
  if (!state.condition || !state.sessionStartedAt) return null;
  // Continued from another device: its start was never observed here, and
  // sending a record would overwrite the one the first device measured.
  if (state.restored) return null;

  const endedAt = state.doneAt || now;

  let initiationLatency = null;
  if (state.firstActionAt) {
    initiationLatency = Math.max(
      0,
      Math.round((state.firstActionAt - state.sessionStartedAt) / 1000)
    );
  }

  // How the session ended, so the analysis can tell a finished session
  // from one that was stopped or replaced. Nothing here is shown to the
  // person as a result.
  const completed = !!state.done || reason === "done";
  const endReason = state.done ? "done" : reason || "ended";

  return {
    condition: state.condition,
    initiation_latency_s: initiationLatency,
    session_duration_s: Math.max(
      0,
      Math.round((endedAt - state.sessionStartedAt) / 1000)
    ),
    intent: state.intent,
    location: state.location,
    due_date: state.dueDate,
    started_at: new Date(state.sessionStartedAt).toISOString(),
    ended_at: new Date(endedAt).toISOString(),
    event_count: state.events.length,
    completed,
    end_reason: endReason,
    // True when initiation_latency_s is null only because the check hit
    // its per-session limit, not because no work tab was ever opened.
    initiation_capped:
      !state.firstActionAt && initiationChecks() >= MAX_INITIATION_CHECKS,
  };
}

/*
 * The nightly analysis reads DynamoDB, not this browser. Send the
 * measurements only: the goal text stays local, the analysis never needs
 * it. Safe to send more than once: the server keys the record on
 * started_at, so a resend overwrites rather than double-counts.
 */
function sendExperimentRecord(record) {
  if (!record) return;
  // Goal, location and due date are the user's own words: they stay in this
  // browser. The server would ignore them anyway (log_session keeps only
  // the measurement fields); not sending them at all is the stronger promise.
  const {
    intent: _intent,
    location: _location,
    due_date: _dueDate,
    ...measurements
  } = record;
  logSession(measurements).catch((err) => {
    console.error("Could not log session for nightly analysis:", err);
  });
}

function archiveCurrentSession(reason) {
  const endedAt = Date.now();

  /* -----------------------------------------------
     ACTIVITY HISTORY
  ------------------------------------------------ */

  if (state.events.length > 0) {
    const history = readJson(ACTIVITY_HISTORY_KEY, []);

    const archivedEvents = state.events.map((event) => ({
      ...event,
      session_intent: state.intent,
      session_location: state.location,
      session_due_date: state.dueDate,
      session_started_at: state.sessionStartedAt,
      session_ended_at: endedAt,
    }));

    history.push(...archivedEvents);
    writeJson(ACTIVITY_HISTORY_KEY, history.slice(-500));
  }

  /* -----------------------------------------------
     NIGHTLY EXPERIMENT
  ------------------------------------------------ */

  const record = buildExperimentRecord(reason, endedAt);
  if (record) {
    const sessions = readJson(NIGHTLY_SESSIONS_KEY, []);
    sessions.push(record);
    writeJson(NIGHTLY_SESSIONS_KEY, sessions.slice(-100));
    sendExperimentRecord(record);
  }

  /* -----------------------------------------------
     SESSION LOG (friction check)
     Every session, with or without a condition. It stays in this browser:
     the friction check reads it from here. The block above is unchanged.
  ------------------------------------------------ */

  if (state.sessionStartedAt) {
    const log = readJson(SESSION_LOG_KEY, []);

    log.push({
      started_at: new Date(state.sessionStartedAt).toISOString(),
      // Same end as the experiment record: when it reached done, if it did.
      // frictionStore leaves out tabs after this, so browsing after the work
      // was finished is never read as part of it.
      ended_at: new Date(state.doneAt || endedAt).toISOString(),
      intent: state.intent,
      condition: state.condition,
      // Same definition as the experiment record.
      initiation_latency_s: state.firstActionAt
        ? Math.max(
            0,
            Math.round((state.firstActionAt - state.sessionStartedAt) / 1000)
          )
        : null,
      event_count: state.events.length,
      initiation_capped:
        !state.firstActionAt && initiationChecks() >= MAX_INITIATION_CHECKS,
    });

    writeJson(SESSION_LOG_KEY, log.slice(-100));
  }

  /* -----------------------------------------------
     YOUR ACCOUNT
     Mark it ended, so every device lists it as ended and none tries to
     continue it.
  ------------------------------------------------ */

  if (state.agentSession && state.sessionStartedAt) {
    const ended = {
      ...state.agentSession,
      ended_at: new Date(state.doneAt || endedAt).toISOString(),
      end_reason: state.done ? "done" : reason || "ended",
      done: !!(state.done || state.agentSession.done),
    };
    saveToServer(ended);
    if (ended.session_id) {
      const list = readJson(ENDED_SESSIONS_KEY, []);
      list.push(ended.session_id);
      writeJson(ENDED_SESSIONS_KEY, list.slice(-50));
    }
  }
}

/* =====================================================
   SESSION HISTORY (your account, any device)
===================================================== */

/*
 * Every session is saved to the user's account in DynamoDB when it starts,
 * after each chat turn (the backend does that) and when it ends. So the
 * dashboard can list them on any device, and an unfinished one from the
 * last few hours is picked back up where you sign in.
 *
 * What is saved is the session object: goal, where and when, step, plans,
 * chat. The tab timeline is not: it stays in the browser that recorded it.
 */

let serverSessions = []; // newest first, like the backend's `history`
let serverStatus = "idle"; // "idle" | "loading" | "ready" | "error"

export function getServerSessions() {
  return serverSessions;
}

export function getServerStatus() {
  return serverStatus;
}

// Same shape as the backend's _new_session_id(): epoch ms, dash, 8 hex.
function newSessionId(ms) {
  let hex = "";
  for (let i = 0; i < 8; i++) {
    hex += Math.floor(Math.random() * 16).toString(16);
  }
  return `${ms}-${hex}`;
}

function byNewest(a, b) {
  return String(b.session_id).localeCompare(String(a.session_id));
}

function summarise(session) {
  return {
    session_id: session.session_id,
    started_at: session.started_at || null,
    ended_at: session.ended_at || null,
    end_reason: session.end_reason || null,
    done: !!session.done,
    goal: session.declared_intent || null,
    first_action: session.first_action || null,
    session,
  };
}

// Keep the list current without waiting for the next sign-in.
function rememberServerSession(session) {
  if (!session || typeof session !== "object" || !session.session_id) return;
  const entry = summarise(session);
  serverSessions = [
    entry,
    ...serverSessions.filter((s) => s.session_id !== entry.session_id),
  ].sort(byNewest);
}

function saveToServer(session) {
  if (!currentUser || !session || typeof session !== "object") return;
  rememberServerSession(session);
  saveSession(session).catch((err) => {
    console.error("Could not save the session to your account:", err);
  });
}

// The chat as the chat panel shows it, rebuilt from the session's own turns.
function chatFromSession(session) {
  const out = [];
  for (const turn of session?.history || []) {
    if (turn?.said) out.push({ role: "user", text: turn.said });
    if (turn?.response) out.push({ role: "assistant", text: turn.response });
  }
  return out;
}

function endedHere(id) {
  return readJson(ENDED_SESSIONS_KEY, []).includes(id);
}

/*
 * The live session in this browser, checked against the account's copy.
 * Another device may have moved it on or ended it since this browser last
 * saw it. Without this, this browser carries on with its older copy: a
 * session finished on the phone still shows as in progress on the laptop,
 * and the laptop's next save puts it back on the account, re-opened and
 * missing the phone's turns.
 */
function reconcileLiveSession() {
  const mine = state.agentSession;
  if (state.intent === null || !mine?.session_id) return;

  const entry = serverSessions.find((s) => s.session_id === mine.session_id);
  const theirs = entry?.session;
  if (!theirs || typeof theirs !== "object") return;

  if (entry.ended_at || entry.done) {
    // Ended on another device: end it here too, with that device's copy and
    // end time. This browser saw it start, so it sends the experiment record
    // (the device that continued it sends none).
    const endedMs = Date.parse(entry.ended_at);
    const savedMs = Number(entry.updated_at) * 1000;
    state.agentSession = theirs;
    state.done = !!entry.done;
    state.doneAt = Number.isFinite(endedMs)
      ? endedMs
      : savedMs > 0
        ? savedMs
        : Date.now();
    endSession(entry.end_reason || (entry.done ? "done" : "ended"));
    return;
  }

  const turns = (s) => (Array.isArray(s?.history) ? s.history.length : 0);
  if (turns(theirs) > turns(mine)) {
    // Moved on elsewhere (more chat turns there): carry on from that copy.
    state.agentSession = theirs;
    if (theirs.declared_intent) state.intent = theirs.declared_intent;
    state.firstAction = theirs.first_action ?? state.firstAction;
    state.plans = Array.isArray(theirs.plans) ? theirs.plans : state.plans;
    state.chatHistory = chatFromSession(theirs);
    persistNow();
  }
}

/*
 * Continue the account's newest session here, if it is unfinished, started
 * in the last few hours, and this browser has no live session of its own.
 * Only the newest: an older unfinished one was left behind for a newer one.
 */
function restoreOpenSessionFromServer() {
  if (state.intent !== null || state.sessionStartedAt) return;

  const newest = serverSessions[0];
  if (!newest || newest.done || newest.ended_at) return;
  if (endedHere(newest.session_id)) return;

  const session = newest.session;
  const started = Date.parse(newest.started_at);
  if (!session || typeof session !== "object" || !session.declared_intent) return;
  if (!Number.isFinite(started) || Date.now() - started > RESTORE_WINDOW_MS) return;

  state.intent = session.declared_intent;
  state.location = session.location ?? null;
  state.dueDate = session.due_date ?? null;
  state.firstAction = session.first_action ?? null;
  state.plans = Array.isArray(session.plans) ? session.plans : [];
  state.agentSession = session;
  state.done = false;
  state.doneAt = null;
  state.condition = session.condition ?? null;
  state.sessionStartedAt = started;
  state.firstActionAt = null;
  state.events = []; // the tab timeline stays on the device that recorded it
  state.chatHistory = chatFromSession(session);
  // The session's clock carries on from when it started on the other device.
  state.elapsedSeconds = Math.max(0, Math.round((Date.now() - started) / 1000));
  state.running = true;
  state.initiationChecked = {};
  initiationPending.clear();
  state.restored = true;

  persistNow();
  startTicking();
}

/*
 * Load this account's sessions. Runs after sign-in; call it again to
 * refresh. A failure leaves whatever the list already had.
 */
export async function syncFromServer() {
  const user = currentUser;
  if (!user) return;

  serverStatus = "loading";
  notify();

  try {
    const res = await fetchHistory({ limit: 30 });
    if (currentUser !== user) return;

    const byId = new Map(serverSessions.map((s) => [s.session_id, s]));
    for (const s of Array.isArray(res?.sessions) ? res.sessions : []) {
      if (!s || !s.session_id) continue;
      const mine = byId.get(s.session_id);
      // Ended here a moment ago and the server hasn't caught up: keep ours.
      if (mine && mine.ended_at && !s.ended_at) continue;
      byId.set(s.session_id, s);
    }
    serverSessions = [...byId.values()].sort(byNewest);
    serverStatus = "ready";

    reconcileLiveSession();
    restoreOpenSessionFromServer();
  } catch (err) {
    console.error("Could not load your earlier sessions:", err);
    if (currentUser === user) serverStatus = "error";
  }

  if (currentUser === user) notify();
}

/* =====================================================
   END SESSION
===================================================== */

/*
 * endSession()                          reason recorded as "done" if the
 *                                       session reached done, else "ended"
 * endSession({ reason: "stopped" })     any short, neutral string the UI
 *                                       chooses ("stopped", "replaced")
 * endSession("stopped")                 same
 *
 * Safe as a click handler (onClick={endSession}): an event object carries
 * no reason and is ignored.
 */
export function endSession(options) {
  const reason =
    typeof options === "string"
      ? options
      : typeof options?.reason === "string"
        ? options.reason
        : null;

  /*
   * IMPORTANT:
   * Archive BEFORE clearing state.
   */
  archiveCurrentSession(reason);

  clearSessionState();

  // Nothing left to save, so this removes the saved copy.
  persistNow();

  notify();
}

/* =====================================================
   STARTUP
===================================================== */

if (typeof window !== "undefined") {
  // No restore here: there is no user yet. bindUser() restores.

  listenOnce(window, "message", onExtensionMessage);
  listenOnce(window, "storage", onStorage);

  // Catch the last few seconds of the timer before a reload or close.
  listenOnce(window, "pagehide", () => {
    if (state.sessionStartedAt) persistNow();
  });
  listenOnce(document, "visibilitychange", () => {
    if (document.visibilityState === "hidden" && state.sessionStartedAt) {
      persistNow();
    }
  });
}