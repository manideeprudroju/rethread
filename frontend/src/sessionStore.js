// sessionStore.js

const state = {
  intent: null,
  location: null,
  dueDate: null,
  firstAction: null,
  plans: [],
  events: [],
  chatHistory: [],
  elapsedSeconds: 0,
  running: false,

  // Nightly experiment
  condition: null,
  sessionStartedAt: null,
  firstActionAt: null,
};

const listeners = new Set();
const eventListeners = new Set();

let tickHandle = null;

const ACTIVITY_HISTORY_KEY =
  "rethread_activity_history_v1";

const NIGHTLY_SESSIONS_KEY =
  "rethread_nightly_sessions_v1";

/* =====================================================
   LOCAL STORAGE
===================================================== */

function readJson(key, fallback = []) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    console.error("localStorage error:", err);
  }
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
    }

    notify();
  }, 1000);
}

export function beginTimer() {
  state.running = true;
  startTicking();
  notify();
}

export function pause() {
  state.running = false;
  notify();
}

export function resume() {
  state.running = true;
  startTicking();
  notify();
}

export function updateElapsed(seconds) {
  state.elapsedSeconds = seconds;
  notify();
}

export function setRunning(value) {
  state.running = value;
  notify();
}

/* =====================================================
   CHROME EXTENSION EVENTS
===================================================== */

if (typeof window !== "undefined") {
  window.addEventListener("message", (event) => {
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

    state.events.push(newEvent);

    /*
     * First captured activity after session start.
     * This is used for initiation_latency_s.
     */
    if (
      state.sessionStartedAt &&
      !state.firstActionAt
    ) {
      state.firstActionAt =
        new Date(newEvent.ts).getTime();
    }

    eventListeners.forEach((callback) => {
      callback(newEvent);
    });

    notify();
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

export function createSession({
  goal,
  location,
  dueDate,
  firstAction,
  plans,
  condition = null,
}) {
  state.intent = goal || null;
  state.location = location || null;
  state.dueDate = dueDate || null;
  state.firstAction = firstAction || null;
  state.plans = plans || [];

  state.events = [];
  state.chatHistory = [];

  state.elapsedSeconds = 0;
  state.running = false;

  state.condition = condition;

  state.sessionStartedAt = Date.now();

  state.firstActionAt = null;

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
  notify();
}

export function getSessionCondition() {
  return state.condition;
}

/* =====================================================
   EVENTS
===================================================== */

export function addEvent(event) {
  state.events.push(event);

  if (
    state.sessionStartedAt &&
    !state.firstActionAt
  ) {
    state.firstActionAt =
      new Date(event.ts).getTime();
  }

  eventListeners.forEach((callback) => {
    callback(event);
  });

  notify();
}

export function getEvents() {
  return state.events;
}

/* =====================================================
   EVENT PAYLOAD
===================================================== */

export function getEventsPayload() {
  return state.events.map((event, index) => {
    const current =
      new Date(event.ts).getTime();

    let dwellSeconds = 60;

    if (index < state.events.length - 1) {
      const next =
        new Date(
          state.events[index + 1].ts
        ).getTime();

      if (
        !Number.isNaN(current) &&
        !Number.isNaN(next)
      ) {
        dwellSeconds = Math.max(
          0,
          Math.min(
            1800,
            Math.round(
              (next - current) / 1000
            )
          )
        );
      }
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
  notify();
}

export function getChatHistory() {
  return state.chatHistory;
}

/* =====================================================
   AMEND
===================================================== */

export function getSessionForAmend() {
  return {
    declared_intent:
      state.intent,

    first_action:
      state.firstAction,

    plans:
      state.plans,
  };
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

export function applyAmendResult({
  first_action,
  plans,
}) {
  if (first_action !== undefined) {
    state.firstAction =
      first_action;
  }

  if (plans !== undefined) {
    state.plans = plans || [];
  }

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

function archiveCurrentSession() {
  const endedAt = Date.now();

  /* -----------------------------------------------
     ACTIVITY HISTORY
  ------------------------------------------------ */

  if (state.events.length > 0) {
    const history =
      readJson(
        ACTIVITY_HISTORY_KEY,
        []
      );

    const archivedEvents = state.events.map((event) => ({
      ...event,
      session_intent: state.intent,
      session_location: state.location,
      session_due_date: state.dueDate,
      session_started_at: state.sessionStartedAt,
      session_ended_at: endedAt,
    }));

    history.push(
      ...archivedEvents
    );

    writeJson(
      ACTIVITY_HISTORY_KEY,
      history.slice(-500)
    );
  }

  /* -----------------------------------------------
     NIGHTLY EXPERIMENT
  ------------------------------------------------ */

  if (
    state.condition &&
    state.sessionStartedAt
  ) {
    const sessions =
      readJson(
        NIGHTLY_SESSIONS_KEY,
        []
      );

    let initiationLatency = null;

    if (state.firstActionAt) {
      initiationLatency =
        Math.max(
          0,
          Math.round(
            (
              state.firstActionAt -
              state.sessionStartedAt
            ) / 1000
          )
        );
    }

    const durationSeconds =
      Math.max(
        0,
        Math.round(
          (
            endedAt -
            state.sessionStartedAt
          ) / 1000
        )
      );

    sessions.push({
      condition:
        state.condition,

      initiation_latency_s:
        initiationLatency,

      session_duration_s:
        durationSeconds,

      intent:
        state.intent,

      location: state.location,
      due_date: state.dueDate,

      started_at:
        new Date(
          state.sessionStartedAt
        ).toISOString(),

      ended_at:
        new Date(
          endedAt
        ).toISOString(),

      event_count:
        state.events.length,
    });

    writeJson(
      NIGHTLY_SESSIONS_KEY,
      sessions.slice(-100)
    );
  }
}

/* =====================================================
   END SESSION
===================================================== */

export function endSession() {
  /*
   * IMPORTANT:
   * Archive BEFORE clearing state.
   */
  archiveCurrentSession();

  state.intent = null;
  state.location = null;
  state.dueDate = null;
  state.firstAction = null;
  state.plans = [];

  state.events = [];
  state.chatHistory = [];

  state.elapsedSeconds = 0;
  state.running = false;

  state.condition = null;

  state.sessionStartedAt = null;
  state.firstActionAt = null;

  notify();
}