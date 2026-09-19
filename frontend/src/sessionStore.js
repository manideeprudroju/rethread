// sessionStore.js

import { drift, logSession } from "./api";

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
};

const listeners = new Set();
const eventListeners = new Set();

let tickHandle = null;

const ACTIVITY_HISTORY_KEY =
  "rethread_activity_history_v1";

const NIGHTLY_SESSIONS_KEY =
  "rethread_nightly_sessions_v1";

// The live session, so a reload or a crash doesn't lose it.
const ACTIVE_SESSION_KEY =
  "rethread_active_session_v1";

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

  // Drop whatever an earlier user left in memory, then load this user's.
  clearSessionState();
  restoreActiveSession();
  notify();
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

function checkInitiation(event) {
  if (!state.sessionStartedAt || !state.intent) return;

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

  state.intent = goal || null;
  state.location = location || null;
  state.dueDate = dueDate || null;
  state.firstAction = firstAction || null;
  state.plans = plans || [];

  // Same shape as new_session() in initiate_probe.py.
  state.doneAt = null;

  state.agentSession = session || {
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
  state.done = false;

  state.events = [];
  state.chatHistory = [];

  state.elapsedSeconds = 0;
  state.running = false;

  state.condition = condition;

  state.sessionStartedAt = Date.now();

  state.firstActionAt = null;

  state.initiationChecked = {};
  initiationPending.clear();

  persistNow();
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