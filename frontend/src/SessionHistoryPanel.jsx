import { useEffect, useState } from "react";
import { ArrowRight, History, RotateCw } from "lucide-react";
import {
  getServerSessions,
  getServerStatus,
  getSnapshot,
  subscribe,
  syncFromServer,
} from "./sessionStore";

/*
 * Earlier sessions, from your account (DynamoDB), so they show on any device
 * you sign in on. Newest first.
 *
 * Same rules as the other panels: clock times and dates, never durations or
 * counts; no verdicts, no red. A session is "In progress", "Done" or
 * "Ended", nothing more. The tab timeline is not here: it stays in the
 * browser that recorded it, and the footer says so.
 */

const OPEN_WINDOW_MS = 12 * 60 * 60 * 1000;

export default function SessionHistoryPanel({ onContinue, max = 6 }) {
  useFonts();
  const [view, setView] = useState(read);
  const [showAll, setShowAll] = useState(false);

  // The store notifies every second for its timer; only re-render when
  // something this panel shows changed.
  useEffect(
    () =>
      subscribe(() => {
        const next = read();
        setView((prev) =>
          prev.sessions === next.sessions &&
          prev.status === next.status &&
          prev.liveId === next.liveId &&
          prev.liveOpen === next.liveOpen
            ? prev
            : next
        );
      }),
    []
  );

  const { sessions, status, liveId, liveOpen } = view;
  const shown = showAll ? sessions : sessions.slice(0, max);
  const busy = status === "loading";

  return (
    <section className="sh" aria-label="Earlier sessions">
      <style>{CSS}</style>

      <header className="sh-head">
        <h2 className="sh-title">
          <History size={18} strokeWidth={1.75} aria-hidden="true" />
          Your sessions
        </h2>
        <button
          type="button"
          className="sh-iconbtn"
          onClick={() => syncFromServer()}
          disabled={busy}
          aria-label="Refresh"
          title="Refresh"
        >
          <RotateCw size={15} strokeWidth={2} className={busy ? "sh-spin" : undefined} />
        </button>
      </header>

      {sessions.length === 0 ? (
        <div className="sh-card sh-empty" aria-live="polite">
          {status === "error" ? (
            <>
              <p>Couldn't load your sessions just now.</p>
              <button type="button" className="sh-soft" onClick={() => syncFromServer()}>
                Try again
              </button>
            </>
          ) : status === "ready" ? (
            <p>Sessions you start show up here, on any device you sign in on.</p>
          ) : (
            <p>Loading your sessions…</p>
          )}
        </div>
      ) : (
        <ul className="sh-card sh-list">
          {shown.map((s) => {
            const isLive = liveOpen && s.session_id === liveId;
            const label = statusLabel(s, isLive);
            return (
              <li key={s.session_id} className="sh-row">
                <div className="sh-main">
                  <p className="sh-when">{fmtWhen(startedMs(s))}</p>
                  <p className="sh-goal">{s.goal || "Untitled session"}</p>
                  {isLive && s.first_action && (
                    <p className="sh-step">Next step: {s.first_action}</p>
                  )}
                </div>
                <div className="sh-side">
                  {label && <span className="sh-chip" data-live={isLive}>{label}</span>}
                  {isLive && onContinue && (
                    <button type="button" className="sh-cta" onClick={onContinue}>
                      Continue
                      <ArrowRight size={15} aria-hidden="true" />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {sessions.length > max && (
        <button type="button" className="sh-more" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Show fewer" : "Show all"}
        </button>
      )}

      <p className="sh-foot">
        Saved to your account, so they show on any device you sign in on. Tab activity stays in the
        browser that recorded it.
      </p>
    </section>
  );
}

function read() {
  const snap = getSnapshot();
  const live = snap.agentSession || null;
  return {
    sessions: getServerSessions(),
    status: getServerStatus(),
    liveId: live?.session_id || null,
    liveOpen: !!snap.hasSession && !snap.done,
  };
}

// Sessions saved before start times were recorded still carry one in their id.
function startedMs(s) {
  const t = Date.parse(s.started_at);
  if (Number.isFinite(t)) return t;
  const fromId = Number(String(s.session_id || "").split("-")[0]);
  return Number.isFinite(fromId) && fromId > 0 ? fromId : null;
}

function statusLabel(s, isLive) {
  if (isLive) return "In progress";
  if (s.done || s.end_reason === "done") return "Done";
  if (s.ended_at) return "Ended";
  const t = startedMs(s);
  return t && Date.now() - t < OPEN_WINDOW_MS ? "Open" : "";
}

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

function fmtWhen(ms) {
  if (ms == null) return "";
  const d = new Date(ms);
  const time = timeFmt.format(d).replace(/\s?([AP])\.?M\.?/i, (_, p) => ` ${p.toLowerCase()}m`);
  return `${dayFmt.format(d)} · ${time}`;
}

function useFonts() {
  useEffect(() => {
    if (document.getElementById("rt-fonts")) return;
    const link = document.createElement("link");
    link.id = "rt-fonts";
    link.rel = "stylesheet";
    link.href =
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500&family=Inter:wght@400;500;600&display=swap";
    document.head.appendChild(link);
  }, []);
}

const CSS = `
.sh {
  --sh-surface: #F1EFE8;
  --sh-tile: #FFFFFF;
  --sh-line: #E4E1D7;
  --sh-ink: #1E2A28;
  --sh-ink-2: #5C5A4E;
  --sh-ink-3: #6B6656;
  --sh-accent: #3F5D54;
  --sh-accent-soft: #E4ECE7;
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
  box-sizing: border-box;
  font-family: 'Inter', system-ui, sans-serif;
  color: var(--sh-ink);
  background: var(--sh-surface);
  border: 1px solid var(--sh-line);
  border-radius: 20px;
  padding: 24px 24px 20px;
}
.sh *, .sh *::before, .sh *::after { box-sizing: border-box; }
.sh-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.sh-title { display: flex; align-items: center; gap: 8px; margin: 0; font-family: 'Fraunces', serif; font-weight: 500; font-size: 22px; }
.sh-iconbtn { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; border-radius: 10px; border: 1px solid var(--sh-line); background: var(--sh-tile); color: var(--sh-ink-2); cursor: pointer; }
.sh-iconbtn:disabled { cursor: default; opacity: .6; }
.sh-spin { animation: sh-spin 1s linear infinite; }
@keyframes sh-spin { to { transform: rotate(360deg); } }
.sh-card { background: var(--sh-tile); border: 1px solid var(--sh-line); border-radius: 16px; }
.sh-empty { padding: 18px; font-size: 14px; color: var(--sh-ink-3); }
.sh-empty p { margin: 0; }
.sh-list { list-style: none; margin: 0; padding: 4px 18px; }
.sh-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 14px 0; border-bottom: 1px solid var(--sh-line); }
.sh-row:last-child { border-bottom: none; }
.sh-main { min-width: 0; }
.sh-when { margin: 0 0 3px; font-size: 12px; color: var(--sh-ink-3); }
.sh-goal { margin: 0; font-size: 15px; font-weight: 500; overflow-wrap: anywhere; }
.sh-step { margin: 4px 0 0; font-size: 13px; color: var(--sh-ink-2); overflow-wrap: anywhere; }
.sh-side { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.sh-chip { font-size: 12px; padding: 3px 9px; border-radius: 999px; background: var(--sh-surface); color: var(--sh-ink-2); border: 1px solid var(--sh-line); white-space: nowrap; }
.sh-chip[data-live="true"] { background: var(--sh-accent-soft); color: var(--sh-accent); border-color: transparent; }
.sh-cta { display: inline-flex; align-items: center; gap: 6px; font: inherit; font-size: 13px; font-weight: 500; padding: 7px 12px; border-radius: 10px; border: none; background: var(--sh-accent); color: #F1EFE8; cursor: pointer; }
.sh-soft { margin-top: 10px; font: inherit; font-size: 13.5px; padding: 8px 14px; border-radius: 10px; border: 1px solid var(--sh-line); background: var(--sh-surface); color: var(--sh-ink); cursor: pointer; }
.sh-more { margin: 10px 2px 0; font: inherit; font-size: 13px; background: none; border: none; color: var(--sh-accent); cursor: pointer; padding: 0; }
.sh-foot { margin: 12px 2px 0; font-size: 12.5px; line-height: 1.55; color: var(--sh-ink-3); }
@media (max-width: 520px) {
  .sh { padding: 18px 16px 16px; }
  .sh-row { flex-direction: column; align-items: flex-start; }
}
@media (prefers-reduced-motion: reduce) { .sh-spin { animation: none; } }
`;
