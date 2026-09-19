import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  Check,
  Footprints,
  History,
  Layers,
  LifeBuoy,
  Minus,
  Plus,
  Quote,
  RotateCw,
  ScanSearch,
} from "lucide-react";
import { churn, drift, reentry } from "./api";
import {
  getDeclaredIntent,
  getEventsPayload,
  getLastUserMessage,
  getSnapshot,
  subscribe,
} from "./sessionStore";

/*
 * Re-entry panel. Six tiles:
 *
 *   Resume            what they were doing + the one button back in
 *   Last on it        when the work stopped
 *   The trail         the session as a strip, the work lit up
 *   Where you were    the key tabs the reconstruction is built on
 *   If you get stuck  the session's own if-then plans
 *   How I know        the evidence, so the reconstruction can be checked
 *
 * Two sources feed it. The session store (goal, step, plans, tab events) is
 * local and always there, so those tiles render instantly and survive a
 * failed call. The `reentry` reconstruction is the enrichment. When it finds
 * nothing (work in an editor never shows up in browser tabs) the panel falls
 * back to the session instead of telling someone with a live session that
 * there is nothing to hand back.
 *
 * Rules the design keeps, same as the backend's:
 *   - No verdicts. Tabs outside the work are folded into small grey dots of
 *     one size: never scaled by time, never labelled, never totalled.
 *   - Clock times, not durations. "2:14 pm" orients; "away 47 min" counts.
 *   - No red, no failure framing, no streaks.
 *   - Letter badges, not favicons: fetching favicons sends every domain to a
 *     third party, and this app only ever collects titles and domains.
 */

const AUTO_REFRESH_AFTER_AWAY_MS = 60 * 1000; // back after a minute away
const AUTO_REFRESH_EVERY_MS = 3 * 60 * 1000; // while visible, if tabs changed
const TRAIL_MAX = 80; // events drawn in the strip
const DOTS_MAX = 24; // when nothing is highlighted, sample down to this

export default function ReentryPanel({
  onResume,
  onStartFresh,
  autoRefresh = true,
  refreshAfterAwayMs = AUTO_REFRESH_AFTER_AWAY_MS,
  refreshEveryMs = AUTO_REFRESH_EVERY_MS,
}) {
  useFonts();
  const session = useSessionSlice();

  // "loading" | "found" | "not_found" | "error"
  const [phase, setPhase] = useState("loading");
  const [data, setData] = useState(null);
  // The events the current reconstruction was built from. The trail draws
  // these, not the live list, so the picture always matches the answer.
  const [events, setEvents] = useState(() => getEventsPayload());
  const [refreshing, setRefreshing] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [loadedCount, setLoadedCount] = useState(null);
  const [churning, setChurning] = useState(false);
  const [resumedWith, setResumedWith] = useState(null);
  const [announce, setAnnounce] = useState("");
  // Per-tab relevance from the backend drift classifier. This is separate
  // from the re-entry reconstruction: it lets the UI classify the full raw
  // tab history even when key_tabs are missing or don't match titles.
  const [driftByEvent, setDriftByEvent] = useState({});


  const loadedCountRef = useRef(null);
  const lastLoadAt = useRef(0);
  const awaySince = useRef(null);
  const mounted = useRef(true);
  // Every load gets a number. Only the latest may write to the screen, and
  // only if its session is still the current one.
  const loadSeq = useRef(0);
  // { seq, sid } of the load in flight, if any.
  const inFlight = useRef(null);
  // The session whose reconstruction is on screen.
  const shownSid = useRef(undefined);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async ({ background = false } = {}) => {
    const sid = currentSid();

    // One load per session at a time. A load for a different session goes
    // ahead anyway, and the one it replaces is ignored when it returns:
    // an answer about the old session must never land on the new one.
    if (inFlight.current && inFlight.current.sid === sid) return;
    const seq = ++loadSeq.current;
    inFlight.current = { seq, sid };
    const stale = () =>
      !mounted.current || seq !== loadSeq.current || currentSid() !== sid;

    // A background refresh redraws over the picture on screen, so it only
    // applies when that picture is of this same session.
    const fresh = shownSid.current !== sid;
    const quiet = background && !fresh;

    const payload = getEventsPayload();
    if (quiet) {
      setRefreshing(true);
    } else {
      setRefreshing(false);
      setPhase("loading");
      setEvents(payload);
      if (fresh) {
        setData(null);
        setChurning(false);
        setResumedWith(null);
        setUpdatedAt(null);
        setLoadedCount(null);
        setRefreshFailed(false);
        loadedCountRef.current = null;
      }
    }

    try {
      let result;
      let churnResult = null;
      if (payload.length === 0) {
        // Nothing to reconstruct from. No model call for an empty timeline.
        result = {
          session_found: false,
          evidence: "Nothing to rebuild from yet.",
        };
      } else {
        [result, churnResult] = await Promise.all([
          reentry({ declared_intent: getDeclaredIntent(), events: payload }),
          churn({ events: payload.slice(-12) }).catch(() => null),
        ]);
      }

      if (stale()) return;
      shownSid.current = sid;

      setEvents(payload);
      setData(result);
      setChurning(!!churnResult?.churning);
      setPhase(result?.session_found ? "found" : "not_found");
      setUpdatedAt(Date.now());
      setLoadedCount(payload.length);
      setRefreshFailed(false);
      loadedCountRef.current = payload.length;
      lastLoadAt.current = Date.now();
      // Keep the "back to it" state only if the step didn't change.
      setResumedWith((prev) => (prev && prev === result?.next_action ? prev : null));
      setAnnounce(
        result?.session_found
          ? "Found where you left off."
          : payload.length === 0
            ? "Nothing recorded in this session yet."
            : "No single thread in these tabs. Showing your session instead."
      );
    } catch (err) {
      if (stale()) return;
      console.error("Reentry check failed:", err);
      if (quiet) {
        // Keep the last good render; say so quietly in the header.
        setRefreshFailed(true);
      } else {
        setPhase("error");
        setAnnounce("Couldn't check just now.");
      }
    } finally {
      if (inFlight.current?.seq === seq) {
        inFlight.current = null;
        if (mounted.current) setRefreshing(false);
      }
    }
  }, []);

  // On mount, and again whenever a different session starts or the current
  // one ends. It used to load on mount only, so a panel that stayed mounted
  // kept showing the previous session's reconstruction.
  useEffect(() => {
    load();
  }, [session.sessionStartedAt, load]);

  // Classify every captured tab against the declared session intent. The
  // backend already exposes drift() for this exact purpose. Cache results
  // locally so repeated visits to the same tab are cheap.
  useEffect(() => {
    const intent = getDeclaredIntent();
    if (!intent || !events.length) {
      setDriftByEvent({});
      return undefined;
    }

    let cancelled = false;

    const classify = async () => {
      const next = {};
      const cache = new Map();

      for (let i = 0; i < events.length; i += 1) {
        const e = events[i];
        const title = String(e.title || "").trim();
        const domain = String(e.domain || "").trim();
        if (!title) continue;

        const key = `${domain.toLowerCase()}::${title.toLowerCase()}`;
        if (!cache.has(key)) {
          cache.set(
            key,
            drift({ intent, title, domain }).catch(() => ({
              relevant: true,
            }))
          );
        }

        const result = await cache.get(key);
        next[i] = result?.relevant !== false;
      }

      if (!cancelled) setDriftByEvent(next);
    };

    classify();

    return () => {
      cancelled = true;
    };
  }, [events, session.sessionStartedAt]);

  // Auto-refresh. Re-entry matters at the moment they come BACK, so the
  // main trigger is returning to the tab or window after being away. A slow
  // interval covers a panel left open while new tabs pile up. Both only run
  // when there are new tabs to reconstruct from; the same tabs would give
  // the same answer.
  useEffect(() => {
    if (!autoRefresh) return undefined;

    const hasNewTabs = () =>
      loadedCountRef.current != null &&
      (getSnapshot().events || []).length !== loadedCountRef.current;

    const leave = () => {
      if (awaySince.current == null) awaySince.current = Date.now();
    };
    const back = () => {
      const since = awaySince.current;
      awaySince.current = null;
      if (since != null && Date.now() - since >= refreshAfterAwayMs && hasNewTabs()) {
        load({ background: true });
      }
    };
    const onVisibility = () =>
      document.visibilityState === "hidden" ? leave() : back();

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("blur", leave);
    window.addEventListener("focus", back);
    const timer = setInterval(() => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - lastLoadAt.current >= refreshEveryMs &&
        hasNewTabs()
      ) {
        load({ background: true });
      }
    }, 30 * 1000);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("blur", leave);
      window.removeEventListener("focus", back);
      clearInterval(timer);
    };
  }, [autoRefresh, refreshAfterAwayMs, refreshEveryMs, load]);

  // ---- derived -----------------------------------------------------------

  const keyTabs = useMemo(
    () => (phase === "found" ? data?.key_tabs || [] : []),
    [phase, data]
  );

  // Each key tab is matched to the one title in this session it fits best.
  const matched = useMemo(() => matchKeyTabs(keyTabs, events), [keyTabs, events]);

  const workIdx = useMemo(() => {
    const set = new Set();

    events.forEach((e, i) => {
      // Once the dedicated drift classifier has answered, use it as the
      // source of truth for Work vs Drift. While it is pending, fall back
      // to the re-entry key-tab match so the panel remains useful immediately.
      if (Object.prototype.hasOwnProperty.call(driftByEvent, i)) {
        if (driftByEvent[i]) set.add(i);
        return;
      }

      const title = norm(e.title);
      if (title && matched.includes(title)) set.add(i);
    });

    return set;
  }, [events, driftByEvent, matched]);

  const lastWork = workIdx.size ? Math.max(...workIdx) : null;

  // The trail follows the same Work classification as "Tabs you used".
  // Re-entry may fail to reconstruct one coherent thread even when the
  // drift classifier has identified relevant work tabs, so don't hide that
  // evidence behind session_found.
  const trailMode =
    phase === "loading"
      ? "loading"
      : workIdx.size
        ? "found"
        : "dots";

  // The strip draws the newest TRAIL_MAX tabs. When the work is older than
  // that, the window reaches back to include it: otherwise the tabs that were
  // matched would be missing from the strip, "Left here" with them. Everything
  // after the last work visit folds into a single dot, so the strip itself
  // doesn't get longer.
  const trailStart = useMemo(() => {
    const start = Math.max(0, events.length - TRAIL_MAX);
    if (trailMode === "found" && lastWork != null && lastWork < start) {
      return Math.max(0, lastWork - (TRAIL_MAX - 2));
    }
    return start;
  }, [events.length, trailMode, lastWork]);

  const segs = useMemo(
    () => buildTrail(events, trailStart, trailMode, workIdx, lastWork),
    [events, trailStart, trailMode, workIdx, lastWork]
  );
  const workVisits = segs.reduce((c, s) => c + (s.kind === "work" ? 1 : 0), 0);

  const firstMs = events.length ? toMs(events[0].ts) : null;
  const lastMs = events.length ? toMs(events[events.length - 1].ts) : null;
  const trailFromMs = events.length ? toMs(events[trailStart]?.ts) : null;

  // When the newest tab is the work, it has no end yet: its time runs up to
  // when the panel loaded and can include time away. So the stat says when
  // they got on it, not a made-up time they stopped.
  const onItNow = lastWork != null && lastWork === events.length - 1;
  const leftAt =
    lastWork == null
      ? null
      : onItNow
        ? toMs(events[lastWork].ts)
        : endMs(events[lastWork]);

  const keyRows = useMemo(
    () =>
      keyTabs.map((k, i) => {
        const title = matched[i];
        let hit = null;
        if (title) {
          for (let j = events.length - 1; j >= 0; j--) {
            if (norm(events[j].title) === title) {
              hit = events[j];
              break;
            }
          }
        }
        return { title: k, domain: hit?.domain || "", at: hit ? toMs(hit.ts) : null };
      }),
    [keyTabs, matched, events]
  );

  const tabGroups = useMemo(
    () => aggregateTabGroups(events, workIdx),
    [events, workIdx]
  );

  const newTabs =
    loadedCount != null ? Math.max(0, session.eventCount - loadedCount) : 0;
  const lastSaid = phase === "found" ? null : getLastUserMessage();
  const busy = phase === "loading" || refreshing;
  const n = events.length;

  // ---- actions -----------------------------------------------------------

  const resume = (action) => {
    setResumedWith(action);
    onResume?.(action);
  };

  // ---- render ------------------------------------------------------------

  return (
    <section className="rt" aria-label="Picking back up">
      <style>{CSS}</style>
      <div className="rt-sr" aria-live="polite">
        {announce}
      </div>

      <header className="rt-head">
        <h2 className="rt-title">Picking back up</h2>
        <div className="rt-status">
          <span>
            {phase === "loading"
              ? "Retracing your steps…"
              : refreshing
                ? "Updating…"
                : refreshFailed
                  ? `Couldn't update · showing ${fmtTime(updatedAt)}`
                  : updatedAt
                    ? `Updated ${fmtTime(updatedAt)}${
                        newTabs ? ` · ${newTabs} new tab${newTabs === 1 ? "" : "s"}` : ""
                      }`
                    : ""}
          </span>
          <button
            type="button"
            className="rt-iconbtn"
            onClick={() => load({ background: phase !== "error" && phase !== "loading" })}
            disabled={busy}
            aria-label="Refresh"
            title="Refresh"
          >
            <RotateCw size={15} strokeWidth={2} className={busy ? "rt-spin" : undefined} />
          </button>
        </div>
      </header>

      <div className="rt-grid" data-refreshing={refreshing} aria-busy={busy}>
        {/* 1. Resume ---------------------------------------------------- */}
        <div className="rt-tile rt-hero">
          <Hero
            phase={phase}
            data={data}
            session={session}
            tabCount={n}
            resumedWith={resumedWith}
            onResume={resume}
            onStartFresh={onStartFresh}
            onRetry={() => load()}
          />
        </div>

        {/* 2. Last on it ------------------------------------------------ */}
        <div className="rt-tile rt-last">
          <h3 className="rt-label">
            <History size={14} strokeWidth={1.75} aria-hidden="true" />
            {phase === "found" && onItNow
              ? "On it since"
              : phase === "loading" || (phase === "found" && leftAt != null)
                ? "Last on it"
                : "Last activity"}
          </h3>
          {phase === "loading" ? (
            <>
              <div className="rt-shimmer" style={{ width: 96, height: 34, marginBottom: 12 }} />
              <div className="rt-shimmer" style={{ width: "80%", height: 12 }} />
            </>
          ) : (
            // Only the moment the work stopped and the moment the session
            // started. Showing the latest tab's time as well would let the
            // gap be read off as time away, which this panel never counts.
            <StatTime
              ms={leftAt ?? lastMs ?? session.sessionStartedAt}
              lines={[
                session.sessionStartedAt ? `Session started ${fmtTime(session.sessionStartedAt)}` : null,
                n ? `${n} tab${n === 1 ? "" : "s"} this session` : null,
              ]}
            />
          )}
        </div>

        {/* 3. The trail ------------------------------------------------- */}
        <div className="rt-tile rt-trail">
          <div className="rt-trail-head">
            <h3 className="rt-label" style={{ margin: 0 }}>
              <Footprints size={14} strokeWidth={1.75} aria-hidden="true" />
              The trail
            </h3>
            {trailMode === "found" ? (
              <div className="rt-legend" aria-hidden="true">
                <span>
                  <i className="rt-key-work" />
                  This work
                </span>
                <span>
                  <i className="rt-key-other" />
                  Other tabs
                </span>
              </div>
            ) : phase === "loading" && n ? (
              <span className="rt-legend">Reading {n} tab titles…</span>
            ) : null}
          </div>
          <Trail
            segs={segs}
            mode={trailMode}
            summary={
              trailMode === "found"
                ? `The trail: ${workVisits} visit${workVisits === 1 ? "" : "s"} to tabs for this work, ${
                    onItNow ? "on it since" : "last at"
                  } ${fmtTime(leftAt)}.`
                : `The trail: ${n} tab${n === 1 ? "" : "s"} in this session.`
            }
          />
          {n ? (
            <div className="rt-axis" aria-hidden="true">
              <span>{fmtTime(trailFromMs)}</span>
              <span>{fmtTime(lastMs)}</span>
            </div>
          ) : (
            <p className="rt-caption">No tabs recorded yet in this session.</p>
          )}
          {/* Only when nothing was found: a reconstruction whose key tabs
              don't match any title still draws dots, but it did find a thread. */}
          {trailMode === "dots" && n > 0 && phase === "not_found" && (
            <p className="rt-caption">No single thread in these tabs.</p>
          )}
        </div>

        {/* 4. Tabs you used -------------------------------------------- */}
        <div className="rt-tile rt-where">
          <h3 className="rt-label">
            <Layers size={14} strokeWidth={1.75} aria-hidden="true" />
            Tabs you used
          </h3>

          {phase === "loading" ? (
            <div>
              {[0, 1, 2].map((i) => (
                <div key={i} className="rt-tabrow">
                  <div className="rt-shimmer" style={{ width: 30, height: 30, borderRadius: 9 }} />
                  <div style={{ flex: 1 }}>
                    <div className="rt-shimmer" style={{ width: `${80 - i * 12}%`, height: 11 }} />
                    <div className="rt-shimmer" style={{ width: "48%", height: 9, marginTop: 7 }} />
                  </div>
                </div>
              ))}
            </div>
          ) : tabGroups.work.length || tabGroups.drift.length ? (
            <div className="rt-tabgroups rt-reveal">
              <TabGroup title="Work tabs" items={tabGroups.work} tone="work" />
              <TabGroup title="Drift tabs" items={tabGroups.drift} tone="drift" />
            </div>
          ) : lastSaid ? (
            <>
              <h3 className="rt-label rt-subtle-label">
                <Quote size={14} strokeWidth={1.75} aria-hidden="true" />
                Last thing you said
              </h3>
              <blockquote className="rt-said rt-reveal">{lastSaid}</blockquote>
            </>
          ) : (
            <p className="rt-empty rt-reveal">
              Tabs opened during this session will appear here.
            </p>
          )}
        </div>

        {/* 5. If you get stuck ------------------------------------------ */}
        <div
          className="rt-tile rt-stuck"
          data-emph={churning && session.plans.length > 0 && phase === "found"}
        >
          <Stuck plans={session.plans} firstAction={session.firstAction} done={session.done} />
        </div>

        {/* 6. How I know ------------------------------------------------ */}
        <div className="rt-tile rt-how">
          <h3 className="rt-label" style={{ margin: 0 }}>
            <ScanSearch size={14} strokeWidth={1.75} aria-hidden="true" />
            How I know
          </h3>
          <div>
            <p className="rt-how-text">
              {phase === "loading"
                ? n
                  ? `Reading the titles of ${n} tab${n === 1 ? "" : "s"} from this session…`
                  : "Nothing recorded in this session yet."
                : phase === "error"
                  ? "Nothing was rebuilt this time, so everything here comes from your saved session."
                  : data?.evidence || ""}
            </p>
            <p className="rt-how-meta">
              {phase !== "loading" && phase !== "error" && n > 0
                ? `From ${n} tab title${n === 1 ? "" : "s"}${
                    firstMs != null && lastMs != null ? `, ${fmtRange(firstMs, lastMs)}` : ""
                  }. `
                : ""}
              Only titles and site names are read, never page content.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

/* =====================================================================
   HERO
===================================================================== */

function Hero({ phase, data, session, tabCount, resumedWith, onResume, onStartFresh, onRetry }) {
  const goal = session.intent;
  const liveSession = session.hasSession && !session.done;

  const goalRow = goal ? (
    <p className="rt-goal">
      <b>Goal</b>
      <span title={goal}>{goal}</span>
    </p>
  ) : null;

  if (phase === "loading") {
    return (
      <>
        {goalRow}
        <p className="rt-doing">Retracing your steps…</p>
        <p className="rt-why">Matching this session's tabs to what you set out to do.</p>
        <div className="rt-hero-foot">
          <div className="rt-shimmer rt-shimmer-on-accent" style={{ height: 50, borderRadius: 12 }} />
        </div>
      </>
    );
  }

  if (phase === "error") {
    return (
      <>
        {goalRow}
        <p className="rt-doing">Couldn't check just now</p>
        <p className="rt-why">A connection hiccup, not a lost session.</p>
        <div className="rt-hero-foot">
          <button type="button" className="rt-cta" onClick={onRetry}>
            <span>Try again</span>
            <RotateCw size={17} className="rt-cta-arrow" aria-hidden="true" />
          </button>
        </div>
      </>
    );
  }

  if (phase === "found") {
    const action = data?.next_action || session.firstAction;
    return (
      <div className="rt-reveal" style={{ display: "contents" }}>
        {goalRow}
        <p className="rt-doing">{data?.doing}</p>
        {data?.why && <p className="rt-why">{data.why}</p>}
        <div className="rt-hero-foot">
          <ResumeButton action={action} resumedWith={resumedWith} onResume={onResume} />
          {onStartFresh && !resumedWith && (
            <button type="button" className="rt-quiet" onClick={() => onStartFresh()}>
              Not this — start something new
            </button>
          )}
        </div>
      </div>
    );
  }

  // not_found. The session is the floor: if there is a live one, hand THAT
  // back instead of a dead end.
  if (liveSession) {
    return (
      <div className="rt-reveal" style={{ display: "contents" }}>
        <p className="rt-goal">
          <b>Your session</b>
        </p>
        <p className="rt-doing">{capitalise(goal)}</p>
        <p className="rt-why">
          {tabCount === 0
            ? session.firstAction
              ? "No tabs recorded yet. This is the step you set."
              : "No tabs recorded yet in this session."
            : "The browser tabs don't show this work. It may have been in another app."}
        </p>
        <div className="rt-hero-foot">
          {session.firstAction ? (
            <>
              <ResumeButton
                action={session.firstAction}
                resumedWith={resumedWith}
                onResume={onResume}
              />
              {onStartFresh && !resumedWith && (
                <button type="button" className="rt-quiet" onClick={() => onStartFresh()}>
                  Start something new instead
                </button>
              )}
            </>
          ) : (
            <StartButton onStartFresh={onStartFresh} />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="rt-reveal" style={{ display: "contents" }}>
      {goal && session.done ? (
        <p className="rt-goal">
          <b>Finished</b>
          <span title={goal}>{goal}</span>
        </p>
      ) : null}
      <p className="rt-doing">Nothing to pick back up right now</p>
      <p className="rt-why">Start a session and it will be here when you come back.</p>
      <div className="rt-hero-foot">
        <StartButton onStartFresh={onStartFresh} />
      </div>
    </div>
  );
}

function ResumeButton({ action, resumedWith, onResume }) {
  if (resumedWith) {
    return (
      <div className="rt-cta rt-cta-done" role="status">
        <Check size={17} aria-hidden="true" />
        <span>Back to it</span>
      </div>
    );
  }
  return (
    <button type="button" className="rt-cta" onClick={() => onResume(action)}>
      <span>{action}</span>
      <ArrowRight size={18} className="rt-cta-arrow" aria-hidden="true" />
    </button>
  );
}

function StartButton({ onStartFresh }) {
  return (
    <button type="button" className="rt-cta" onClick={() => onStartFresh?.()}>
      <span>Start something new</span>
      <ArrowRight size={18} className="rt-cta-arrow" aria-hidden="true" />
    </button>
  );
}

/* =====================================================================
   STAT: a clock time as a figure
===================================================================== */

function StatTime({ ms, lines }) {
  const p = timeParts(ms);
  return (
    <div className="rt-stat rt-reveal">
      {p ? (
        <p className="rt-stat-value">
          {p.time}
          {p.period && <small>{p.period}</small>}
        </p>
      ) : (
        <p className="rt-stat-value rt-stat-none">Not yet</p>
      )}
      <div className="rt-stat-foot">
        {lines.filter(Boolean).map((line, i) => (
          <p key={i} className="rt-stat-sub">
            {line}
          </p>
        ))}
      </div>
    </div>
  );
}

/* =====================================================================
   TRAIL: emphasis strip. The work in the accent, everything else folded.
===================================================================== */

/*
 * With a thread found, the strip is one keyboard stop (Tab), and the arrow
 * keys step through the work visits, Home/End jump to the first/last, Escape
 * closes. Screen readers get the same visits as a plain list, since the
 * strip itself is only a picture. Touch: tap a visit (or near one) to see
 * it, tap again or anywhere else to close. Tabs outside the work are never
 * listed or labelled, here or anywhere.
 */
let trailUid = 0;

function Trail({ segs, mode, summary }) {
  const wrapRef = useRef(null);
  const segEls = useRef(new Map());
  const pointer = useRef("mouse");
  const ids = useRef(null);
  if (!ids.current) {
    trailUid += 1;
    ids.current = { hint: `rt-trail-hint-${trailUid}` };
  }

  // { key, x, align }. The segment is looked up by key on every render, so a
  // refresh that removes it also removes the tooltip.
  const [tip, setTip] = useState(null);
  const [said, setSaid] = useState("");

  const interactive = mode === "found";
  const work = useMemo(() => segs.filter((s) => s.kind === "work"), [segs]);
  const tipSeg = (interactive && tip && work.find((s) => s.key === tip.key)) || null;

  const place = (seg) => {
    const el = segEls.current.get(seg.key);
    const box = wrapRef.current?.getBoundingClientRect();
    if (!el || !box) return;
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2 - box.left;
    const align = x < 130 ? "start" : x > box.width - 130 ? "end" : "center";
    setTip({ key: seg.key, x, align });
  };

  // Touch has no mouseleave: a tap anywhere outside closes it.
  useEffect(() => {
    if (!tipSeg) return undefined;
    const onDown = (e) => {
      if (!wrapRef.current?.contains(e.target)) setTip(null);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [tipSeg]);

  // Fingers are wide and a visit can be a few pixels: a tap picks the
  // nearest visit within reach, not only the one exactly under it.
  const nearest = (clientX) => {
    let best = null;
    let bestD = Infinity;
    for (const s of work) {
      const el = segEls.current.get(s.key);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const d = clientX < r.left ? r.left - clientX : clientX > r.right ? clientX - r.right : 0;
      if (d < bestD) {
        best = s;
        bestD = d;
      }
    }
    return bestD <= 24 ? best : null;
  };

  const onClick = (e) => {
    const seg = nearest(e.clientX);
    if (!seg) {
      setTip(null);
    } else if (pointer.current !== "mouse" && tipSeg?.key === seg.key) {
      setTip(null); // second tap on the same visit
    } else {
      place(seg);
    }
  };

  const step = (to) => {
    if (!work.length) return;
    const cur = tipSeg ? work.findIndex((s) => s.key === tipSeg.key) : -1;
    const next =
      to === "first"
        ? 0
        : to === "last" || cur === -1
          ? work.length - 1
          : Math.min(work.length - 1, Math.max(0, cur + to));
    const seg = work[next];
    place(seg);
    setSaid(`${seg.when}, ${seg.title}${seg.flag ? ". Left here." : ""}`);
  };

  const onKeyDown = (e) => {
    if (e.key === "Escape") {
      if (tipSeg) {
        e.preventDefault();
        setTip(null);
      }
      return;
    }
    const to = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    step(to);
  };

  // Keyboard focus opens on the last visit, where they left off. A click
  // also focuses, but picks its own visit, so only :focus-visible counts.
  const onFocus = (e) => {
    if (tipSeg || !work.length) return;
    let keyboard = false;
    try {
      keyboard = e.currentTarget.matches(":focus-visible");
    } catch {
      keyboard = false;
    }
    if (keyboard) place(work[work.length - 1]);
  };

  const onBlur = (e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setTip(null);
  };

  const a11y = interactive
    ? {
        role: "group",
        tabIndex: 0,
        "aria-label": summary,
        "aria-describedby": ids.current.hint,
        onKeyDown,
        onFocus,
        onBlur,
        onClick,
      }
    : { role: "img", "aria-label": summary };

  return (
    <div
      ref={wrapRef}
      className="rt-strip-wrap"
      data-tip={!!tipSeg}
      data-interactive={interactive}
      {...a11y}
      onPointerDown={(e) => {
        pointer.current = e.pointerType || "mouse";
      }}
      onPointerLeave={(e) => {
        if (e.pointerType === "mouse") setTip(null);
      }}
    >
      {tipSeg && (
        <div className="rt-tip" data-align={tip.align} style={{ left: tip.x }} aria-hidden="true">
          <strong>{tipSeg.when}</strong>
          <span>{tipSeg.title}</span>
        </div>
      )}
      <div className="rt-strip" data-mode={mode} aria-hidden="true">
        {segs.length === 0 && <div className="rt-rail" />}
        {/* In dots mode, hidden segments would take a share of the
            space-between spacing and make the dots uneven. */}
        {(mode === "dots" ? segs.filter((s) => s.kind !== "gone") : segs).map((s) => (
          <div
            key={s.key}
            ref={(el) => {
              if (el) segEls.current.set(s.key, el);
              else segEls.current.delete(s.key);
            }}
            className="rt-seg"
            data-kind={s.kind}
            data-open={s.open}
            data-active={tipSeg?.key === s.key}
            style={{
              flexGrow: s.grow,
              flexBasis: s.basis,
              flexShrink: s.shrink,
              marginLeft: s.ml,
            }}
            onPointerEnter={
              interactive && s.kind === "work"
                ? (e) => {
                    if (e.pointerType === "mouse") place(s);
                  }
                : undefined
            }
          >
            {s.flag && <span className="rt-flag">Left here</span>}
          </div>
        ))}
      </div>
      {interactive && (
        <>
          <span id={ids.current.hint} className="rt-sr">
            Arrow keys step through the visits.
          </span>
          <ol className="rt-sr" aria-label="Visits to this work">
            {work.map((s) => (
              <li key={s.key}>
                {s.when}, {s.title}
                {s.flag ? ". Left here." : ""}
              </li>
            ))}
          </ol>
          <span className="rt-sr" aria-live="polite">
            {said}
          </span>
        </>
      )}
    </div>
  );
}

function buildTrail(events, start, mode, workIdx, lastWork) {
  const newest = events.length - 1;
  const segs = events.slice(start).map((e, j) => {
    const i = j + start;
    const from = toMs(e.ts);
    const dwell = Math.max(1, Number(e.dwell_seconds) || 1);
    // The newest tab hasn't ended as far as the capture knows. Its dwell
    // runs to when the panel loaded (and can include time away), so it gets
    // no end time, and the strip draws it fading out instead of with an edge.
    const open = i === newest;
    const to = open || from == null ? null : from + dwell * 1000;
    return {
      key: `${i}-${e.ts}`,
      i,
      from,
      to,
      open,
      when: open ? (from == null ? "" : `Since ${fmtTime(from)}`) : fmtRange(from, to),
      dwell,
      title: e.title || "",
      kind: "pending",
    };
  });

  if (mode === "found") {
    segs.forEach((s, j) => {
      if (workIdx.has(s.i)) s.kind = "work";
      // A run of tabs outside the work collapses into ONE dot.
      else s.kind = j > 0 && !workIdx.has(segs[j - 1].i) ? "gone" : "other";
      s.flag = s.i === lastWork;
    });
  } else if (mode === "dots") {
    const step = Math.max(1, Math.ceil(segs.length / DOTS_MAX));
    segs.forEach((s, j) => {
      s.kind = j % step === 0 ? "dot" : "gone";
    });
  }

  let prev = null;
  for (const s of segs) {
    switch (s.kind) {
      case "pending":
      case "work":
        // Width follows time on that tab. Blocks may shrink when there are
        // many; a 2px surface gap separates touching blocks.
        s.grow = s.dwell;
        s.basis = "8px";
        s.shrink = 1;
        break;
      case "other":
      case "dot":
        s.grow = 0;
        s.basis = "8px";
        s.shrink = 0;
        break;
      default: // gone
        s.grow = 0;
        s.basis = "0px";
        s.shrink = 1;
    }
    if (s.kind === "gone" || s.kind === "dot") {
      s.ml = 0;
    } else {
      s.ml = prev == null ? 0 : s.kind === "other" || prev === "other" ? 7 : 2;
      prev = s.kind;
    }
  }
  return segs;
}

/* =====================================================================
   TABS USED — aggregated work vs drift
===================================================================== */

function TabGroup({ title, items, tone }) {
  if (!items.length) return null;

  return (
    <section className="rt-tabgroup" aria-label={title}>
      <div className="rt-tabgroup-head">
        <span className="rt-tabgroup-title">
          <i className={`rt-tabgroup-dot rt-tabgroup-dot-${tone}`} />
          {title}
        </span>
        <span className="rt-tabgroup-count">
          {items.length} tab{items.length === 1 ? "" : "s"}
        </span>
      </div>

      <ul className="rt-tabs">
        {items.map((row) => {
          const tint = tintFor(row.domain || row.title);
          return (
            <li key={`${tone}-${row.key}`} className="rt-tabrow">
              <span
                className="rt-badge"
                style={{ background: tint.bg, color: tint.fg }}
                aria-hidden="true"
              >
                {letterFor(row.domain, row.title)}
              </span>
              <span className="rt-tabtext">
                <span className="rt-tabtitle" title={row.title}>
                  {row.title}
                </span>
                <span className="rt-tabmeta">
                  {[row.domain, `${row.visits} visit${row.visits === 1 ? "" : "s"}`, fmtDuration(row.totalSeconds)]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function aggregateTabGroups(events, workIdx) {
  const work = new Map();
  const drift = new Map();

  events.forEach((event, index) => {
    const title = norm(event.title);
    if (!title) return;

    const bucket = workIdx.has(index) ? work : drift;
    const domain = String(event.domain || "").trim().toLowerCase();
    const key = `${title}|${domain}`;
    const dwell = Math.max(0, Number(event.dwell_seconds) || 0);

    const existing = bucket.get(key);
    if (existing) {
      existing.visits += 1;
      existing.totalSeconds += dwell;
    } else {
      bucket.set(key, {
        key,
        title: event.title || title,
        domain: event.domain || "",
        visits: 1,
        totalSeconds: dwell,
      });
    }
  });

  const sortTabs = (map) =>
    [...map.values()].sort((a, b) => {
      if (b.totalSeconds !== a.totalSeconds) {
        return b.totalSeconds - a.totalSeconds;
      }
      return b.visits - a.visits;
    });

  return {
    work: sortTabs(work),
    drift: sortTabs(drift),
  };
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;

  const minutes = Math.floor(total / 60);
  const secs = total % 60;

  if (minutes < 60) {
    return secs ? `${minutes}m ${secs}s` : `${minutes}m`;
  }

  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}

/* =====================================================================
   IF YOU GET STUCK
===================================================================== */

function Stuck({ plans, firstAction, done }) {
  const [open, setOpen] = useState(false);
  const usable = (plans || []).filter((p) => p && (p.if || p.then));

  if (!usable.length) {
    return (
      <>
        <h3 className="rt-label">
          <LifeBuoy size={14} strokeWidth={1.75} aria-hidden="true" />
          {firstAction && !done ? "Your current step" : "If you get stuck"}
        </h3>
        {firstAction && !done ? (
          <p className="rt-plan-then" style={{ marginTop: 0 }}>
            {capitalise(firstAction)}
          </p>
        ) : (
          <p className="rt-empty">If-then plans from the start of a session show up here.</p>
        )}
      </>
    );
  }

  const shown = open ? usable : usable.slice(0, 2);
  const hidden = usable.length - shown.length;

  return (
    <>
      <h3 className="rt-label">
        <LifeBuoy size={14} strokeWidth={1.75} aria-hidden="true" />
        If you get stuck
      </h3>
      <ul className="rt-plans">
        {shown.map((p, i) => (
          <li key={i} className="rt-plan">
            <span className="rt-plan-if">If {lower(p.if)}</span>
            <span className="rt-plan-then">
              <ArrowRight size={14} strokeWidth={2} className="rt-plan-arrow" aria-hidden="true" />
              <span>{capitalise(p.then)}</span>
            </span>
          </li>
        ))}
      </ul>
      {usable.length > 2 && (
        <button type="button" className="rt-more" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? <Minus size={13} aria-hidden="true" /> : <Plus size={13} aria-hidden="true" />}
          {open ? "Show fewer" : `${hidden} more plan${hidden === 1 ? "" : "s"}`}
        </button>
      )}
    </>
  );
}

/* =====================================================================
   HOOKS & HELPERS
===================================================================== */

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

// Subscribe to the store, but only re-render when something this panel
// shows actually changed. The store notifies every second for its timer.
function readSlice() {
  const s = getSnapshot();
  return {
    intent: s.intent,
    firstAction: s.firstAction,
    plans: s.plans || [],
    eventCount: (s.events || []).length,
    done: !!s.done,
    hasSession: !!s.hasSession,
    sessionStartedAt: s.sessionStartedAt || null,
  };
}

function useSessionSlice() {
  const [slice, setSlice] = useState(readSlice);
  useEffect(
    () =>
      subscribe(() => {
        const next = readSlice();
        setSlice((prev) =>
          Object.keys(next).every((k) => prev[k] === next[k]) ? prev : next
        );
      }),
    []
  );
  return slice;
}

function currentSid() {
  return getSnapshot().sessionStartedAt ?? null;
}

function toMs(ts) {
  const ms = new Date(ts).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function endMs(e) {
  const start = toMs(e.ts);
  return start == null ? null : start + (Number(e.dwell_seconds) || 0) * 1000;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

function timeParts(ms) {
  if (ms == null || Number.isNaN(ms)) return null;
  const parts = timeFmt.formatToParts(new Date(ms));
  const period = parts.find((p) => p.type === "dayPeriod")?.value || "";
  const time = parts
    .filter((p) => p.type !== "dayPeriod")
    .map((p) => p.value)
    .join("")
    .trim();
  return { time, period: period.toLowerCase().replace(/\./g, "") };
}

function fmtTime(ms) {
  const p = timeParts(ms);
  return p ? `${p.time}${p.period ? ` ${p.period}` : ""}` : "";
}

function fmtRange(a, b) {
  const pa = timeParts(a);
  const pb = timeParts(b);
  if (!pa || !pb) return fmtTime(a ?? b);
  if (pa.time === pb.time && pa.period === pb.period) return fmtTime(a);
  if (pa.period === pb.period) return `${pa.time}–${pb.time}${pb.period ? ` ${pb.period}` : ""}`;
  return `${fmtTime(a)}–${fmtTime(b)}`;
}

function norm(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/^\(\d+\)\s*/, "") // "(3) WhatsApp" and "(12) WhatsApp" are one tab
    .replace(/\s+/g, " ")
    .trim();
}

/*
 * key_tabs come back from the model as titles, sometimes lightly reworded.
 *
 * Each key tab is matched to the ONE title in the session it fits best,
 * rather than to every title that clears a bar. Matching every title over
 * 60% shared words lit up unrelated tabs from the same site: "Q3 summary -
 * Google Docs" and "Budget - Google Docs" share "google" and "docs". Site
 * names are also discounted when scoring, so they can't carry a match.
 *
 * Returns, per key tab, the normalised title it refers to, or null.
 */
const MATCH_MIN = 0.6;

function matchKeyTabs(keyTabs, events) {
  if (!keyTabs.length) return [];
  const titles = new Map(); // normalised title -> an event with it
  for (const e of events) {
    const t = norm(e.title);
    if (t) titles.set(t, e); // the newest event with this title wins
  }
  return keyTabs.map((k) => {
    let best = null;
    let bestScore = 0;
    for (const [t, e] of titles) {
      const s = titleScore(k, e);
      // >= so a tie goes to the title that first appeared later
      if (s >= bestScore) {
        best = t;
        bestScore = s;
      }
    }
    return bestScore >= MATCH_MIN ? best : null;
  });
}

function titleScore(keyTab, event) {
  const k = norm(keyTab);
  const t = norm(event.title);
  if (!k || !t) return 0;
  if (k === t) return 1;

  const domain = String(event.domain || "").toLowerCase();
  const kp = pageText(k, domain);
  const tp = pageText(t, domain);
  if (kp && kp === tp) return 0.95; // same page, site name added or dropped

  // Words that are part of the site's own name count for a quarter.
  const weight = (w) => (domain && domain.includes(w) ? 0.25 : 1);
  const kw = [...new Set(words(kp))];
  const tw = new Set(words(tp));
  const total = kw.reduce((sum, w) => sum + weight(w), 0);
  const shared = kw.filter((w) => tw.has(w));
  // A site word alone is never a match.
  if (!total || !shared.some((w) => weight(w) === 1)) return 0;

  let score = shared.reduce((sum, w) => sum + weight(w), 0) / total;
  // Cut short: one title contains the other.
  if (Math.min(kp.length, tp.length) >= 12 && (kp.includes(tp) || tp.includes(kp))) {
    score = Math.max(score, 0.8);
  }
  return Math.min(score, 0.9); // below any exact match
}

// The page part of a title: "q3 summary - google docs" on docs.google.com
// is "q3 summary". Segments made only of the domain's own words go.
function pageText(normTitle, domain) {
  const parts = normTitle.split(/\s+[-–—|·•]\s+/);
  const kept = parts.filter((p) => {
    const ws = p.split(/[^a-z0-9]+/).filter((w) => w.length >= 3);
    return !(domain && ws.length && ws.every((w) => domain.includes(w)));
  });
  return (kept.length ? kept : parts).join(" ").trim();
}

function words(s) {
  return s
    .split(/[^a-z0-9.]+/)
    .map((w) => w.replace(/^\.+|\.+$/g, "")) // "LA.pdf" stays, "report." doesn't
    .filter((w) => w.length > 3);
}

// Muted tints from the same family as the panel. The letter carries the
// identity; the tint only separates neighbours.
const TINTS = [
  { bg: "#E4ECE7", fg: "#2F4A42" },
  { bg: "#F0E9D8", fg: "#6A5526" },
  { bg: "#E5E8EE", fg: "#39465E" },
  { bg: "#E9ECD9", fg: "#4A5227" },
];

function tintFor(s) {
  let h = 0;
  for (const c of String(s || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return TINTS[h % TINTS.length];
}

function letterFor(domain, title) {
  const src = String(domain || "").replace(/^www\./, "") || String(title || "");
  const m = src.match(/[a-z0-9]/i);
  return m ? m[0].toUpperCase() : "·";
}

function capitalise(s) {
  const t = String(s || "").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : t;
}

function lower(s) {
  const t = String(s || "").trim();
  // Plans are written in the first person: "if I get stuck". Keep "I".
  return t && !/^I\b/.test(t) ? t[0].toLowerCase() + t.slice(1) : t;
}

/* =====================================================================
   STYLES — scoped under .rt, same palette and type as the rest of the app
===================================================================== */

const CSS = `
.rt {
  --rt-surface: #F1EFE8;
  --rt-tile: #FFFFFF;
  --rt-line: #E4E1D7;
  --rt-line-strong: #D3CEC1;
  --rt-ink: #1E2A28;
  --rt-ink-2: #5C5A4E;
  --rt-ink-3: #6B6656;
  --rt-accent: #3F5D54;
  --rt-accent-deep: #2F4A42;
  --rt-on-accent: #F1EFE8;
  --rt-on-accent-2: #C9D6CF;
  --rt-pending: #DCD7CA;
  --rt-other: #999382;
  --rt-ease: cubic-bezier(.2, .8, .2, 1);

  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  color: var(--rt-ink);
  background: var(--rt-surface);
  border: 1px solid var(--rt-line);
  border-radius: 24px;
  padding: 16px 16px 16px;
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
  box-sizing: border-box;
  container-type: inline-size;
  container-name: rt;
  -webkit-font-smoothing: antialiased;
}
.rt *, .rt *::before, .rt *::after { box-sizing: border-box; }
/* Resets at zero specificity, so every component rule below wins. */
:where(.rt) :where(p, h2, h3, ul, blockquote) { margin: 0; }
:where(.rt) :where(ul) { padding: 0; list-style: none; }
:where(.rt) :where(button) { font: inherit; }
.rt button:focus-visible { outline: 2px solid var(--rt-accent); outline-offset: 2px; }
.rt-hero button:focus-visible { outline-color: var(--rt-on-accent); }
.rt-sr {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap;
}

/* header */
.rt-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 4px 4px 14px 6px;
}
.rt-title {
  font-family: 'Fraunces', Georgia, serif; font-weight: 500;
  font-size: 21px; letter-spacing: -0.01em; line-height: 1.2;
}
.rt-status {
  display: flex; align-items: center; gap: 10px; min-width: 0;
  font-size: 12.5px; color: var(--rt-ink-3); text-align: right;
}
.rt-iconbtn {
  flex-shrink: 0; width: 34px; height: 34px; border-radius: 10px;
  display: grid; place-items: center; cursor: pointer;
  background: var(--rt-tile); color: var(--rt-ink);
  border: 1px solid var(--rt-line);
  transition: border-color .15s, background-color .15s;
}
.rt-iconbtn:hover:not(:disabled) { border-color: var(--rt-line-strong); background: #FBFAF7; }
.rt-iconbtn:disabled { cursor: default; color: var(--rt-ink-3); }
.rt-spin { animation: rt-spin .9s linear infinite; }

/* grid */
.rt-grid {
  display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px; transition: opacity .25s;
}
.rt-grid[data-refreshing="true"] { opacity: .6; }
.rt-hero  { grid-column: span 4; }
.rt-last  { grid-column: span 2; }
.rt-trail { grid-column: span 6; }
.rt-where { grid-column: span 3; }
.rt-stuck { grid-column: span 3; }
.rt-how   { grid-column: span 6; }

.rt-tile {
  background: var(--rt-tile); border: 1px solid var(--rt-line);
  border-radius: 16px; padding: 16px 18px; min-width: 0;
}
.rt-label {
  display: flex; align-items: center; gap: 7px;
  font-size: 12.5px; font-weight: 500; line-height: 1.2;
  color: var(--rt-ink-3); margin: 0 0 12px;
}
.rt-label svg { flex-shrink: 0; }

/* 1. hero */
.rt-hero {
  background: var(--rt-accent); border-color: var(--rt-accent);
  color: var(--rt-on-accent); padding: 20px 22px 18px;
  display: flex; flex-direction: column; min-height: 236px;
}
.rt-goal {
  display: flex; gap: 8px; align-items: baseline; min-width: 0;
  font-size: 12.5px; line-height: 1.3; color: var(--rt-on-accent-2);
  margin-bottom: 10px;
}
.rt-goal b { font-weight: 500; flex-shrink: 0; }
.rt-goal span {
  min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  color: var(--rt-on-accent);
}
.rt-doing {
  font-family: 'Fraunces', Georgia, serif; font-weight: 500;
  font-size: 26px; line-height: 1.18; letter-spacing: -0.012em;
  margin-bottom: 8px; text-wrap: balance;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.rt-why {
  font-size: 14px; line-height: 1.5; color: var(--rt-on-accent-2);
  margin-bottom: 18px; text-wrap: pretty;
}
.rt-hero-foot { margin-top: auto; }
.rt-cta {
  width: 100%; display: flex; align-items: center; justify-content: space-between;
  gap: 14px; padding: 14px 16px; border: none; border-radius: 12px;
  background: var(--rt-on-accent); color: var(--rt-ink);
  font-size: 15px; font-weight: 500; line-height: 1.35; text-align: left;
  cursor: pointer; transition: background-color .15s, box-shadow .15s;
  box-shadow: 0 1px 0 rgba(0,0,0,.06);
}
.rt-cta:hover { background: #FFFFFF; box-shadow: 0 2px 10px rgba(20, 35, 31, .18); }
.rt-cta-arrow { flex-shrink: 0; transition: transform .2s var(--rt-ease); }
.rt-cta:hover .rt-cta-arrow { transform: translateX(3px); }
.rt-cta-done {
  justify-content: center; gap: 8px; cursor: default;
  background: transparent; color: var(--rt-on-accent);
  box-shadow: inset 0 0 0 1px rgba(241, 239, 232, .45);
}
.rt-cta-done:hover { background: transparent; box-shadow: inset 0 0 0 1px rgba(241, 239, 232, .45); }
.rt-quiet {
  display: inline-block; margin-top: 10px; padding: 2px 0;
  background: none; border: none; cursor: pointer;
  font-size: 13px; color: var(--rt-on-accent-2);
  text-decoration: underline; text-decoration-color: transparent;
  text-underline-offset: 3px; transition: text-decoration-color .15s, color .15s;
}
.rt-quiet:hover { color: var(--rt-on-accent); text-decoration-color: currentColor; }

/* 2. stat */
.rt-last { display: flex; flex-direction: column; }
.rt-stat { display: flex; flex-direction: column; flex: 1; }
.rt-stat-value {
  /* centred in whatever height the row gives the tile */
  margin: auto 0; padding: 6px 0 16px;
  font-size: 44px; font-weight: 600; letter-spacing: -0.035em;
  line-height: 1; color: var(--rt-ink);
}
.rt-stat-value small {
  font-size: 16px; font-weight: 500; letter-spacing: 0;
  color: var(--rt-ink-3); margin-left: 5px;
}
.rt-stat-none {
  font-size: 22px; font-weight: 500; letter-spacing: -0.01em; color: var(--rt-ink-3);
}
.rt-stat-sub { font-size: 13px; line-height: 1.45; color: var(--rt-ink-2); }
.rt-stat-sub + .rt-stat-sub { color: var(--rt-ink-3); margin-top: 2px; }

/* 3. trail */
.rt-trail-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
}
.rt-legend {
  display: flex; align-items: center; gap: 14px;
  font-size: 12px; color: var(--rt-ink-3);
}
.rt-legend span { display: inline-flex; align-items: center; gap: 6px; }
.rt-legend i { display: inline-block; }
.rt-key-work { width: 16px; height: 8px; border-radius: 3px; background: var(--rt-accent); }
.rt-key-other { width: 8px; height: 8px; border-radius: 50%; background: var(--rt-other); }
.rt-strip-wrap { position: relative; padding-top: 30px; }
.rt-strip { display: flex; align-items: center; height: 28px; }
.rt-strip[data-mode="dots"] { justify-content: space-between; }
.rt-rail { flex: 1; height: 1px; background: var(--rt-line); }
.rt-seg {
  position: relative; height: 20px; min-width: 0; border-radius: 4px;
  background: var(--rt-pending);
  transition:
    flex-grow .8s var(--rt-ease), flex-basis .8s var(--rt-ease),
    margin .8s var(--rt-ease), height .5s var(--rt-ease),
    background-color .45s, opacity .45s;
}
.rt-seg[data-kind="work"] { background: var(--rt-accent); cursor: default; }
.rt-seg[data-kind="work"][data-active="true"] { background: var(--rt-accent-deep); }
.rt-seg[data-kind="other"],
.rt-seg[data-kind="dot"] { height: 8px; background: var(--rt-other); }
.rt-seg[data-kind="gone"] { height: 8px; opacity: 0; }
/* The newest tab has no end yet: it fades out instead of stopping at an edge. */
.rt-seg[data-open="true"]:is([data-kind="work"], [data-kind="pending"]) {
  background-image: linear-gradient(90deg, rgba(255,255,255,0) 30%, rgba(255,255,255,.78));
}
.rt-strip-wrap:focus { outline: none; }
.rt-strip-wrap:focus-visible .rt-strip {
  outline: 2px solid var(--rt-accent); outline-offset: 5px; border-radius: 6px;
}
.rt-strip-wrap[data-interactive="true"] { -webkit-tap-highlight-color: transparent; }
.rt-flag {
  position: absolute; right: 0; bottom: calc(100% + 3px);
  padding: 0 7px 7px 0; border-right: 1.5px solid var(--rt-ink);
  font-size: 11.5px; font-weight: 500; line-height: 1; white-space: nowrap;
  color: var(--rt-ink); pointer-events: none;
  animation: rt-fade .4s .75s both; transition: opacity .15s;
}
/* visibility, not opacity: the fade-in animation holds opacity at 1 */
.rt-strip-wrap[data-tip="true"] .rt-flag { visibility: hidden; }
.rt-tip {
  position: absolute; bottom: 36px; z-index: 2; pointer-events: none;
  display: flex; flex-direction: column; gap: 2px;
  /* max-content: otherwise the width is capped by the space between the
     tooltip's left edge and the container, and it wraps into a column */
  width: max-content; max-width: 260px; padding: 8px 10px; border-radius: 8px;
  background: var(--rt-ink); color: var(--rt-on-accent);
  box-shadow: 0 6px 20px rgba(20, 30, 28, .22);
  transform: translateX(-50%);
}
.rt-tip[data-align="start"] { transform: translateX(-16px); }
.rt-tip[data-align="end"] { transform: translateX(calc(-100% + 16px)); }
.rt-tip strong { font-size: 13px; font-weight: 600; }
.rt-tip span { font-size: 12px; color: #C9D2CE; line-height: 1.35; }
.rt-axis {
  display: flex; justify-content: space-between; margin-top: 8px;
  font-size: 12px; color: var(--rt-ink-3);
}
.rt-caption { font-size: 12.5px; color: var(--rt-ink-3); margin-top: 6px; }

/* 4. tabs used */
.rt-tabgroups { display: flex; flex-direction: column; gap: 14px; }
.rt-tabgroup { min-width: 0; }
.rt-tabgroup + .rt-tabgroup {
  border-top: 1px solid var(--rt-line);
  padding-top: 13px;
}
.rt-tabgroup-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; margin-bottom: 5px;
}
.rt-tabgroup-title {
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 12px; font-weight: 600; color: var(--rt-ink-2);
}
.rt-tabgroup-dot {
  width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
}
.rt-tabgroup-dot-work { background: var(--rt-accent); }
.rt-tabgroup-dot-drift { background: var(--rt-other); }
.rt-tabgroup-count { font-size: 11.5px; color: var(--rt-ink-3); }
.rt-tabrow {
  display: flex; align-items: center; gap: 12px;
  padding: 7px 0;
}
.rt-tabrow + .rt-tabrow { border-top: 1px solid var(--rt-line); }
.rt-tabrow:first-child { padding-top: 5px; }
.rt-tabrow:last-child { padding-bottom: 2px; }
.rt-badge {
  flex-shrink: 0; width: 30px; height: 30px; border-radius: 9px;
  display: grid; place-items: center; font-size: 13px; font-weight: 600;
}
.rt-tabtext { display: flex; flex-direction: column; min-width: 0; }
.rt-tabtitle {
  font-size: 13.5px; line-height: 1.35; color: var(--rt-ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rt-tabmeta {
  font-size: 12px; line-height: 1.35; color: var(--rt-ink-3); margin-top: 1px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rt-subtle-label { margin-top: 2px; }
.rt-said {
  font-family: 'Fraunces', Georgia, serif; font-size: 17px; line-height: 1.4;
  color: var(--rt-ink); padding-left: 12px; border-left: 2px solid var(--rt-line-strong);
}
.rt-empty { font-size: 13.5px; line-height: 1.5; color: var(--rt-ink-3); }

/* 5. if you get stuck */
.rt-stuck[data-emph="true"] { border-color: var(--rt-accent); }
.rt-plan { display: flex; flex-direction: column; gap: 3px; }
.rt-plan + .rt-plan { border-top: 1px solid var(--rt-line); padding-top: 10px; margin-top: 10px; }
.rt-plan-if { font-size: 13px; line-height: 1.4; color: var(--rt-ink-3); }
.rt-plan-then {
  display: flex; align-items: flex-start; gap: 7px;
  font-size: 14px; line-height: 1.45; color: var(--rt-ink);
}
.rt-plan-arrow { flex-shrink: 0; margin-top: 3px; color: var(--rt-accent); }
.rt-more {
  display: inline-flex; align-items: center; gap: 5px; margin-top: 10px;
  padding: 2px 0; background: none; border: none; cursor: pointer;
  font-size: 13px; font-weight: 500; color: var(--rt-accent);
}
.rt-more:hover { color: var(--rt-accent-deep); }

/* 6. how I know */
.rt-how {
  display: grid; grid-template-columns: 128px minmax(0, 1fr); gap: 6px 16px;
  align-items: start; background: transparent;
}
.rt-how-text { font-size: 13.5px; line-height: 1.55; color: var(--rt-ink-2); text-wrap: pretty; }
.rt-how-meta { font-size: 12px; color: var(--rt-ink-3); margin-top: 5px; }

/* loading */
.rt-shimmer {
  border-radius: 6px;
  background: linear-gradient(90deg, #EFECE4 0%, #E6E2D8 40%, #EFECE4 80%);
  background-size: 200% 100%;
  animation: rt-shimmer 1.4s ease-in-out infinite;
}
.rt-shimmer-on-accent {
  background: linear-gradient(90deg,
    rgba(241,239,232,.10) 0%, rgba(241,239,232,.20) 40%, rgba(241,239,232,.10) 80%);
  background-size: 200% 100%;
}

/* motion */
.rt-reveal { animation: rt-rise .5s var(--rt-ease) both; }
.rt-hero .rt-reveal > * { animation: rt-rise .5s var(--rt-ease) both; }
@keyframes rt-rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes rt-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes rt-spin { to { transform: rotate(360deg); } }
@keyframes rt-shimmer { from { background-position: 100% 0; } to { background-position: -100% 0; } }
@media (prefers-reduced-motion: reduce) {
  .rt *, .rt *::before, .rt *::after {
    animation: none !important; transition: none !important;
  }
}

/* Narrow: one column. Last in the sheet so it overrides the rules above.
   A container query, so it follows the panel's own width, not the window's. */
@container rt (max-width: 580px) {
  .rt-hero, .rt-last, .rt-trail, .rt-where, .rt-stuck, .rt-how { grid-column: span 6; }
  .rt-hero { min-height: 0; }
  .rt-doing { font-size: 23px; }
  .rt-how { grid-template-columns: 1fr; }
  .rt-status > span { display: none; }
}
`;
