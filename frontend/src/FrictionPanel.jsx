import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronDown,
  Feather,
  FlaskConical,
  MessageCircle,
  RotateCw,
  ScanSearch,
  Send,
  Waypoints,
} from "lucide-react";
import { guide } from "./api";
import {
  attachPendingPlan,
  getFrictionSnapshot,
  markHeavy,
  runNightly,
  setAside,
  subscribeFriction,
  undoSetAside,
} from "./frictionStore";
import { getSnapshot, subscribe } from "./sessionStore";

/*
 * Friction panel. The clinical support agent's one screen. Five tiles:
 *
 *   Tomorrow's plan   the one if-then plan, and why it is this one
 *   Feels heavy       the only way "heavy" ever enters the system: one tap
 *   What showed up    each pattern, worked up before / during / after
 *   What's working    the experiment behind each plan, gate and all
 *   Why this plan     mechanism, source, and when a clinician wouldn't use it
 *   Ask the guide     a small chat that answers only from trusted sources
 *
 * Same rules as the Re-entry panel:
 *   - Describes the tabs, never the person. No scores, no severity, no red.
 *   - Clock times, not durations away.
 *   - Tabs outside the work are never named. "Messages or inbox" at most.
 *   - Nothing is called working before the gate says so.
 */

export default function FrictionPanel({ demo = false }) {
  useFonts();
  const fx = useFrictionSnapshot();
  const session = useSessionSlice();

  const [phase, setPhase] = useState(fx.result ? "ready" : "loading");
  const [announce, setAnnounce] = useState("");

  const load = useCallback(
    async (force = false) => {
      setPhase((p) => (p === "ready" && !force ? p : "loading"));
      try {
        await runNightly({ force, demo });
        setPhase("ready");
        setAnnounce("Friction check updated.");
      } catch (err) {
        console.error("Friction check failed:", err);
        setPhase(getFrictionSnapshot().result ? "ready" : "error");
        setAnnounce("Couldn't check just now.");
      }
    },
    [demo]
  );

  useEffect(() => {
    load(demo);
  }, [load, demo]);

  // A session just ended, so its tabs are in the archive now: look again.
  // Without this the panel kept yesterday's answer until the next day, which
  // reads as "the check stopped working".
  const wasLive = useRef(session.hasSession);
  useEffect(() => {
    if (!demo && wasLive.current && !session.hasSession) load(true);
    wasLive.current = session.hasSession;
  }, [session.hasSession, demo, load]);

  const result = fx.result;
  const plan = result?.plan || null;
  const pending = fx.pendingPlan;
  const busy = phase === "loading" || fx.running;
  const target = result?.target;

  return (
    <section className="fx" aria-label="Friction check">
      <style>{CSS}</style>
      <div className="fx-sr" aria-live="polite">
        {announce}
      </div>

      <header className="fx-head">
        <h2 className="fx-title">Friction check</h2>
        <div className="fx-status">
          {result?._demo && <span className="fx-chip">Sample week</span>}
          <span>
            {busy ? "Looking back over your sessions…" : fx.lastRunAt ? `Checked ${fmtTime(fx.lastRunAt)}` : ""}
          </span>
          <button
            type="button"
            className="fx-iconbtn"
            onClick={() => load(true)}
            disabled={busy}
            aria-label="Check again"
            title="Check again"
          >
            <RotateCw size={15} strokeWidth={2} className={busy ? "fx-spin" : undefined} />
          </button>
        </div>
      </header>

      <div className="fx-grid" aria-busy={busy}>
        {/* 1. Tomorrow's plan -------------------------------------------- */}
        <div className="fx-tile fx-hero">
          <PlanHero
            phase={phase}
            result={result}
            plan={plan}
            pending={pending}
            session={session}
            vetoes={fx.vetoes}
            onRetry={() => load(true)}
            onAnnounce={setAnnounce}
          />
        </div>

        {/* 2. Feels heavy ------------------------------------------------ */}
        <div className="fx-tile fx-heavy">
          <Heavy session={session} careLine={result?.care_line} onAnnounce={setAnnounce} />
        </div>

        {/* 3. What showed up --------------------------------------------- */}
        <div className="fx-tile fx-patterns">
          <h3 className="fx-label">
            <Waypoints size={14} strokeWidth={1.75} aria-hidden="true" />
            What showed up
            {result?.window?.sessions ? (
              <span className="fx-label-meta">
                last {result.window.sessions} session{result.window.sessions === 1 ? "" : "s"}
              </span>
            ) : null}
          </h3>
          {phase === "loading" && !result ? (
            <Shimmers rows={3} />
          ) : result?.patterns?.length ? (
            <ul className="fx-plist fx-reveal">
              {result.patterns.map((p) => (
                <PatternRow key={p.id} p={p} isTarget={p.id === target} />
              ))}
            </ul>
          ) : (
            <p className="fx-empty">
              {!result
                ? "Nothing checked yet."
                : result.window?.sessions
                  ? "Nothing recurring. Patterns show up here once they repeat."
                  : "No finished sessions in the last few days yet. A session is read once it ends."}
            </p>
          )}
        </div>

        {/* 4. What's working --------------------------------------------- */}
        <div className="fx-tile fx-exp">
          <h3 className="fx-label">
            <FlaskConical size={14} strokeWidth={1.75} aria-hidden="true" />
            What's working
          </h3>
          <Experiments experiments={result?.experiments} planSelection={plan?.selection} plan={plan} />
        </div>

        {/* 5. Why this plan ---------------------------------------------- */}
        <div className="fx-tile fx-why">
          <h3 className="fx-label">
            <BookOpen size={14} strokeWidth={1.75} aria-hidden="true" />
            Why this plan
          </h3>
          {plan ? (
            <div className="fx-reveal">
              <p className="fx-why-name">
                {plan.support_name} <span className="fx-kind">{kindLabel(plan.kind)}</span>
              </p>
              <p className="fx-why-text">{plan.mechanism}</p>
              <dl className="fx-dl">
                <dt>Domain</dt>
                <dd>{plan.ef_domain}</dd>
                <dt>Basis</dt>
                <dd>{plan.source}</dd>
                <dt>Not for</dt>
                <dd>{plan.not_for}</dd>
              </dl>
            </div>
          ) : (
            <p className="fx-empty">The reasoning behind each plan shows up here: how it works, where that comes from, and when not to use it.</p>
          )}
        </div>

        {/* 6. Ask the guide ---------------------------------------------- */}
        <div className="fx-tile fx-guide">
          <Guide />
        </div>

        {/* 7. How I know ------------------------------------------------- */}
        <div className="fx-tile fx-how">
          <h3 className="fx-label" style={{ margin: 0 }}>
            <ScanSearch size={14} strokeWidth={1.75} aria-hidden="true" />
            How I know
          </h3>
          <div>
            <p className="fx-how-text">
              Read from tab titles, site names and clock times
              {result?.window?.sessions ? ` in your last ${result.window.sessions} session${result.window.sessions === 1 ? "" : "s"}` : ""}.
              Never page content. Patterns describe what the tabs show, not how anyone is doing.
            </p>
            <p className="fx-how-meta">
              Plans are written from your goal and the names of your own pages, and checked before they're shown.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

const CARE_FALLBACK =
  "Support for the work, not treatment. If it's more than the work, Tele-MANAS is free and open 24x7: 14416.";

/* =====================================================================
   HERO
===================================================================== */

function PlanHero({ phase, result, plan, pending, session, vetoes, onRetry, onAnnounce }) {
  const [justSetAside, setJustSetAside] = useState(null);

  if (phase === "loading" && !result) {
    return (
      <>
        <p className="fx-eyebrow">Tomorrow's plan</p>
        <p className="fx-then">Looking back over your sessions…</p>
        <p className="fx-sub">Checking for patterns that repeat.</p>
        <div className="fx-hero-foot">
          <div className="fx-shimmer fx-shimmer-on-accent" style={{ height: 50, borderRadius: 12 }} />
        </div>
      </>
    );
  }

  if (phase === "error" && !result) {
    return (
      <>
        <p className="fx-eyebrow">Tomorrow's plan</p>
        <p className="fx-then">Couldn't check just now</p>
        <p className="fx-sub">A connection hiccup. Your sessions are all still here.</p>
        <div className="fx-hero-foot">
          <button type="button" className="fx-cta" onClick={onRetry}>
            <span>Try again</span>
            <RotateCw size={17} className="fx-cta-arrow" aria-hidden="true" />
          </button>
        </div>
      </>
    );
  }

  if (justSetAside) {
    return (
      <div className="fx-reveal" style={{ display: "contents" }}>
        <p className="fx-eyebrow">Set aside</p>
        <p className="fx-then">{justSetAside.support_name} won't be offered for this again.</p>
        <p className="fx-sub">Check again for the other kind of support.</p>
        <div className="fx-hero-foot">
          <button
            type="button"
            className="fx-quiet"
            onClick={() => {
              undoSetAside(justSetAside.pattern, justSetAside.support);
              setJustSetAside(null);
              onAnnounce("Put back.");
            }}
          >
            Undo
          </button>
        </div>
      </div>
    );
  }

  if (!plan) {
    // Nothing to look at and nothing recurring are different answers, and
    // saying so is the difference between "still gathering" and "broken".
    const noSessions = !!result && !result.window?.sessions;
    return (
      <div className="fx-reveal" style={{ display: "contents" }}>
        <p className="fx-eyebrow">Tomorrow's plan</p>
        <p className="fx-then">
          {result?.status === "all_set_aside"
            ? "You've set aside every support for this."
            : noSessions
              ? "Nothing to look at yet"
              : "Nothing recurring in your last few sessions"}
        </p>
        <p className="fx-sub">
          {noSessions
            ? "This reads a session once it ends. Finish one and it gets looked at."
            : "Your next session runs without an extra plan."}
        </p>
      </div>
    );
  }

  const samePlan = pending && pending.support === plan.support && pending.pattern === plan.pattern;
  const attached = samePlan && pending.attachedTo;
  const attachedHere = attached && pending.attachedTo === session.sessionStartedAt;
  const canAddNow = samePlan && !attached && session.hasSession && !session.done && !pending.demo;
  const isVetoed = (vetoes?.[plan.pattern] || []).includes(plan.support);

  return (
    <div className="fx-reveal" style={{ display: "contents" }}>
      <p className="fx-eyebrow">
        Tomorrow's plan <span className="fx-dot">·</span> for {plan.pattern_label.toLowerCase()}
      </p>
      <p className="fx-if">If {lower(plan.if)}</p>
      <p className="fx-then">
        <ArrowRight size={22} strokeWidth={2} className="fx-then-arrow" aria-hidden="true" />
        <span>{capitalise(plan.then)}</span>
      </p>
      {plan.why && <p className="fx-sub">{plan.why}</p>}
      <div className="fx-hero-foot">
        {canAddNow ? (
          <button
            type="button"
            className="fx-cta"
            onClick={() => {
              if (attachPendingPlan()) onAnnounce("Added to this session's plans.");
            }}
          >
            <span>Add it to this session now</span>
            <ArrowRight size={18} className="fx-cta-arrow" aria-hidden="true" />
          </button>
        ) : (
          <div className="fx-cta fx-cta-done" role="status">
            <Check size={17} aria-hidden="true" />
            <span>
              {attachedHere
                ? "In this session's plans"
                : attached
                  ? "Was in a recent session's plans"
                  : pending?.demo
                    ? "Sample plan — not added anywhere"
                    : "Joins your next session's plans"}
            </span>
          </div>
        )}
        {!isVetoed && (
          <button
            type="button"
            className="fx-quiet"
            onClick={() => {
              setAside(plan);
              setJustSetAside(plan);
              onAnnounce("Set aside.");
            }}
          >
            Not this kind of step
          </button>
        )}
      </div>
    </div>
  );
}

/* =====================================================================
   FEELS HEAVY
===================================================================== */

function Heavy({ session, careLine, onAnnounce }) {
  const [state, setState] = useState("idle"); // idle | busy | done | error
  const [plan, setPlan] = useState(null);
  const live = session.hasSession && !session.done;

  // A new session is a fresh start for this tile.
  useEffect(() => {
    setState("idle");
    setPlan(null);
  }, [session.sessionStartedAt]);

  const go = async () => {
    setState("busy");
    try {
      const r = await markHeavy();
      setPlan(r?.plan || null);
      setState("done");
      onAnnounce(r?.plan ? "Added a plan to this session." : "Nothing added.");
    } catch (err) {
      console.error("Heavy failed:", err);
      setState("error");
    }
  };

  return (
    <>
      <h3 className="fx-label">
        <Feather size={14} strokeWidth={1.75} aria-hidden="true" />
        Feels heavy
      </h3>
      {state === "done" && plan ? (
        <div className="fx-reveal fx-heavy-plan">
          <p className="fx-plan-if">If {lower(plan.if)}</p>
          <p className="fx-plan-then">
            <ArrowRight size={14} strokeWidth={2} className="fx-plan-arrow" aria-hidden="true" />
            <span>{capitalise(plan.then)}</span>
          </p>
          <p className="fx-small">Added to this session's plans.</p>
        </div>
      ) : (
        <>
          <p className="fx-heavy-text">
            {live
              ? "If this session's work feels heavy, say so. You get one small plan for that moment."
              : "During a session, you can mark the work as feeling heavy and get one small plan for it."}
          </p>
          <button
            type="button"
            className="fx-soft"
            onClick={go}
            disabled={!live || state === "busy"}
          >
            {state === "busy" ? "Making a plan…" : state === "error" ? "Try again" : "This one feels heavy"}
          </button>
        </>
      )}
      <p className="fx-care">{careLine || CARE_FALLBACK}</p>
    </>
  );
}

/* =====================================================================
   ASK THE GUIDE -- answers only from the trusted index in guide_probe.py.
   The conversation lives in this component only: nothing is saved, and a
   reload starts fresh. The last few turns go with each message.
===================================================================== */

const STARTERS = [
  "Why is starting so hard?",
  "How do I get assessed in India?",
  "What does the friction check do?",
];
const HISTORY_SENT = 6;

function Guide() {
  const [turns, setTurns] = useState([]); // {role, text, sources?, kind?}
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(null); // the message that failed
  const listRef = useRef(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, busy]);

  const send = async (text) => {
    const message = String(text || "").trim();
    if (!message || busy) return;
    const history = turns.slice(-HISTORY_SENT).map(({ role, text: t }) => ({ role, text: t }));
    setTurns((t) => [...t, { role: "user", text: message }]);
    setDraft("");
    setFailed(null);
    setBusy(true);
    try {
      const r = await guide({ message, history });
      setTurns((t) => [...t, { role: "assistant", text: r.reply, sources: r.sources || [], kind: r.kind }]);
    } catch (err) {
      console.error("Guide failed:", err);
      setTurns((t) => t.slice(0, -1));
      setDraft(message);
      setFailed(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3 className="fx-label">
        <MessageCircle size={14} strokeWidth={1.75} aria-hidden="true" />
        Ask the guide
        <span className="fx-label-meta">Answers from NIMH, NHS, Tele-MANAS and research</span>
      </h3>

      <div className="fx-chat" ref={listRef} aria-live="polite">
        {turns.length === 0 && (
          <div className="fx-chat-empty">
            <p className="fx-empty">
              Ask about ADHD, getting assessed, or getting unstuck on work. It won't guess
              about you, and it doesn't do medication.
            </p>
            <div className="fx-starters">
              {STARTERS.map((q) => (
                <button key={q} type="button" className="fx-starter" onClick={() => send(q)} disabled={busy}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className="fx-msg" data-role={t.role}>
            <p className="fx-bubble" data-kind={t.kind || undefined}>{t.text}</p>
            {t.role === "assistant" && t.sources?.length > 0 && (
              <ul className="fx-sources" aria-label="Sources">
                {t.sources.map((s) => (
                  <li key={s.id}>
                    {s.url ? (
                      <a href={s.url} target="_blank" rel="noopener noreferrer" className="fx-source">
                        <b>{s.org}</b> {s.title}
                        <ArrowUpRight size={12} aria-hidden="true" />
                      </a>
                    ) : (
                      <span className="fx-source">
                        <b>{s.org}</b> {s.title}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
        {busy && (
          <div className="fx-msg" data-role="assistant">
            <p className="fx-bubble fx-typing" aria-label="The guide is replying">
              <i /><i /><i />
            </p>
          </div>
        )}
      </div>

      {failed && <p className="fx-chat-err">Couldn't reach the guide. Your message is back in the box to send again.</p>}

      <form
        className="fx-chat-form"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <input
          className="fx-chat-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Ask anything about ADHD and work"
          maxLength={600}
          aria-label="Message the guide"
        />
        <button type="submit" className="fx-chat-send" disabled={busy || !draft.trim()} aria-label="Send">
          <Send size={16} aria-hidden="true" />
        </button>
      </form>
    </>
  );
}

/* =====================================================================
   WHAT SHOWED UP
===================================================================== */

function PatternRow({ p, isTarget }) {
  const [open, setOpen] = useState(isTarget);
  const id = `fx-p-${p.id}`;
  return (
    <li className="fx-prow" data-target={isTarget}>
      <button
        type="button"
        className="fx-phead"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        <span className="fx-pname">
          {p.label}
          <span className="fx-kind">{p.ef_domain}</span>
          {isTarget && <span className="fx-chip fx-chip-accent">Plan is for this</span>}
        </span>
        {/* Last seen, never a count: a tally of hard moments is a streak
            by another name. episodes / days_seen only rank the patterns. */}
        <span className="fx-pcount">
          {p.last_seen ? `Last seen ${p.last_seen}` : ""}
          <ChevronDown size={15} className="fx-chev" data-open={open} aria-hidden="true" />
        </span>
      </button>
      {open && (
        <div id={id} className="fx-pbody fx-reveal">
          <p className="fx-pdef">{p.definition}</p>
          <p className="fx-evidence">{p.latest.evidence}</p>
          <ol className="fx-abc" aria-label="Most recent time, before, during and after">
            <li>
              <b>Before</b>
              <span>{p.latest.before}</span>
            </li>
            <li>
              <b>During</b>
              <span>{p.latest.during}</span>
            </li>
            <li>
              <b>After</b>
              <span>{p.latest.after}</span>
            </li>
          </ol>
        </div>
      )}
    </li>
  );
}

/* =====================================================================
   WHAT'S WORKING -- the gate, shown honestly
===================================================================== */

function Experiments({ experiments, planSelection, plan }) {
  let list = Object.entries(experiments || {});
  // No history yet: still show the experiment the new plan starts, at 0 of 8.
  const a = planSelection?.analysis;
  if (!list.length && plan && planSelection?.mode === "experiment" && a?.counts) {
    list = [[plan.pattern, {
      label: plan.pattern_label,
      ui_text: a.ui_text,
      counts: a.counts,
      per_arm: a.per_arm,
      arm_names: Object.fromEntries(Object.keys(a.counts).map((k) => [k, k === plan.support ? plan.support_name : ARM_NAMES[k] || k.replace(/_/g, " ")])),
    }]];
  }
  if (!list.length) {
    return (
      <p className="fx-empty">
        Each plan runs as a small experiment: two kinds of support, {planSelection?.analysis?.per_arm || 8} sessions each,
        then one exact test. Nothing is called working before that.
        {planSelection?.mode === "only_option" ? " This one is the only kind you haven't set aside." : ""}
      </p>
    );
  }
  return (
    <ul className="fx-elist fx-reveal">
      {list.map(([pattern, e]) => (
        <li key={pattern} className="fx-erow">
          <p className="fx-ename">{e.label}</p>
          <p className="fx-etext">{e.ui_text}</p>
          {Object.entries(e.arm_names || {}).map(([arm, name]) => {
            const n = Math.min(e.per_arm, e.counts?.[arm] || 0);
            const better = e.finding?.better_arm === arm;
            return (
              <div key={arm} className="fx-arm">
                <span className="fx-arm-name">
                  {name}
                  {better && <Check size={13} strokeWidth={2.25} className="fx-arm-check" aria-label="went better" />}
                </span>
                <span className="fx-pips" aria-label={`${n} of ${e.per_arm} sessions`}>
                  {Array.from({ length: e.per_arm }, (_, i) => (
                    <i key={i} data-on={i < n} />
                  ))}
                </span>
              </div>
            );
          })}
        </li>
      ))}
    </ul>
  );
}

// Names for the arm that isn't today's plan, when no history is back yet.
// Mirrors the KB in friction_probe.py.
const ARM_NAMES = {
  first_step_cue: "Cue the first step",
  rough_draft: "Rough-draft framing",
  one_line_question: "Write the question down",
  good_enough: "Pre-decided stopping rule",
  park_the_check: "Park it, check in a batch",
  minute_away: "One-minute reset",
  one_tab: "Cut to one thread",
  step_on_paper: "Minute on paper",
  name_it: "Name it, then start",
  shrink_it: "Shrink it",
};

function Shimmers({ rows }) {
  return (
    <div>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="fx-shimmer" style={{ height: 14, width: `${86 - i * 14}%`, marginTop: i ? 14 : 0 }} />
      ))}
    </div>
  );
}

/* =====================================================================
   HOOKS & HELPERS
===================================================================== */

function useFrictionSnapshot() {
  const [snap, setSnap] = useState(getFrictionSnapshot);
  useEffect(() => subscribeFriction(setSnap), []);
  return snap;
}

function readSlice() {
  const s = getSnapshot();
  return {
    hasSession: !!s.hasSession,
    done: !!s.done,
    intent: s.intent,
    sessionStartedAt: s.sessionStartedAt || null,
  };
}

// The session store notifies every second for its timer; only re-render when
// something this panel shows changed.
function useSessionSlice() {
  const [slice, setSlice] = useState(readSlice);
  useEffect(
    () =>
      subscribe(() => {
        const next = readSlice();
        setSlice((prev) => (Object.keys(next).every((k) => prev[k] === next[k]) ? prev : next));
      }),
    []
  );
  return slice;
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

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

function fmtTime(ms) {
  if (ms == null) return "";
  return timeFmt.format(new Date(ms)).replace(/\s?([AP])\.?M\.?/i, (_, p) => ` ${p.toLowerCase()}m`);
}

function kindLabel(kind) {
  return { next_action: "Next action", reframe: "Reframe", reset: "Reset" }[kind] || kind;
}

function capitalise(s) {
  const t = String(s || "").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : t;
}

function lower(s) {
  const t = String(s || "").trim();
  return t && !/^I\b/.test(t) ? t[0].toLowerCase() + t.slice(1) : t;
}

/* =====================================================================
   STYLES — scoped under .fx, same palette and type as the Re-entry panel
===================================================================== */

const CSS = `
.fx {
  --fx-surface: #F1EFE8;
  --fx-tile: #FFFFFF;
  --fx-line: #E4E1D7;
  --fx-line-strong: #D3CEC1;
  --fx-ink: #1E2A28;
  --fx-ink-2: #5C5A4E;
  --fx-ink-3: #6B6656;
  --fx-accent: #3F5D54;
  --fx-accent-deep: #2F4A42;
  --fx-accent-soft: #E4ECE7;
  --fx-on-accent: #F1EFE8;
  --fx-on-accent-2: #C9D6CF;
  --fx-pending: #DCD7CA;
  --fx-ease: cubic-bezier(.2, .8, .2, 1);

  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  color: var(--fx-ink);
  background: var(--fx-surface);
  border: 1px solid var(--fx-line);
  border-radius: 24px;
  padding: 16px;
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
  box-sizing: border-box;
  container-type: inline-size;
  container-name: fx;
  -webkit-font-smoothing: antialiased;
}
.fx *, .fx *::before, .fx *::after { box-sizing: border-box; }
:where(.fx) :where(p, h2, h3, ul, ol, dl, dd) { margin: 0; }
:where(.fx) :where(ul, ol) { padding: 0; list-style: none; }
:where(.fx) :where(button) { font: inherit; }
.fx button:focus-visible { outline: 2px solid var(--fx-accent); outline-offset: 2px; }
.fx-hero button:focus-visible { outline-color: var(--fx-on-accent); }
.fx-sr {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap;
}

/* header */
.fx-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 4px 4px 14px 6px; }
.fx-title { font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 21px; letter-spacing: -0.01em; line-height: 1.2; }
.fx-status { display: flex; align-items: center; gap: 10px; min-width: 0; font-size: 12.5px; color: var(--fx-ink-3); text-align: right; }
.fx-iconbtn {
  flex-shrink: 0; width: 34px; height: 34px; border-radius: 10px;
  display: grid; place-items: center; cursor: pointer;
  background: var(--fx-tile); color: var(--fx-ink); border: 1px solid var(--fx-line);
  transition: border-color .15s, background-color .15s;
}
.fx-iconbtn:hover:not(:disabled) { border-color: var(--fx-line-strong); background: #FBFAF7; }
.fx-iconbtn:disabled { cursor: default; color: var(--fx-ink-3); }
.fx-spin { animation: fx-spin .9s linear infinite; }
.fx-chip {
  display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px;
  font-size: 11.5px; font-weight: 500; background: var(--fx-tile);
  border: 1px solid var(--fx-line); color: var(--fx-ink-2); white-space: nowrap;
}
.fx-chip-accent { background: var(--fx-accent-soft); border-color: transparent; color: var(--fx-accent-deep); }

/* grid */
.fx-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }
.fx-hero     { grid-column: span 4; }
.fx-heavy    { grid-column: span 2; }
.fx-patterns { grid-column: span 6; }
.fx-exp      { grid-column: span 3; }
.fx-why      { grid-column: span 3; }
.fx-guide    { grid-column: span 6; display: flex; flex-direction: column; }
.fx-how      { grid-column: span 6; }

.fx-tile { background: var(--fx-tile); border: 1px solid var(--fx-line); border-radius: 16px; padding: 16px 18px; min-width: 0; }
.fx-label {
  display: flex; align-items: center; gap: 7px; font-size: 12.5px; font-weight: 500;
  line-height: 1.2; color: var(--fx-ink-3); margin: 0 0 12px;
}
.fx-label svg { flex-shrink: 0; }
.fx-label-meta { margin-left: auto; font-weight: 400; }
.fx-empty { font-size: 13.5px; line-height: 1.5; color: var(--fx-ink-3); }
.fx-kind {
  display: inline-block; margin-left: 8px; padding: 1px 7px; border-radius: 6px;
  font-size: 11.5px; font-weight: 500; color: var(--fx-ink-2); background: var(--fx-surface);
  vertical-align: 1px;
}

/* 1. hero */
.fx-hero {
  background: var(--fx-accent); border-color: var(--fx-accent); color: var(--fx-on-accent);
  padding: 20px 22px 18px; display: flex; flex-direction: column; min-height: 250px;
}
.fx-eyebrow { font-size: 12.5px; line-height: 1.3; color: var(--fx-on-accent-2); margin-bottom: 12px; font-weight: 500; }
.fx-dot { margin: 0 4px; }
.fx-if { font-size: 14.5px; line-height: 1.45; color: var(--fx-on-accent-2); margin-bottom: 6px; text-wrap: pretty; }
.fx-then {
  display: flex; gap: 10px; align-items: flex-start;
  font-family: 'Fraunces', Georgia, serif; font-weight: 500;
  font-size: 24px; line-height: 1.22; letter-spacing: -0.01em; margin-bottom: 10px; text-wrap: balance;
}
.fx-then-arrow { flex-shrink: 0; margin-top: 4px; }
.fx-sub { font-size: 13.5px; line-height: 1.5; color: var(--fx-on-accent-2); margin-bottom: 18px; text-wrap: pretty; }
.fx-hero-foot { margin-top: auto; }
.fx-cta {
  width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 14px;
  padding: 14px 16px; border: none; border-radius: 12px;
  background: var(--fx-on-accent); color: var(--fx-ink);
  font-size: 15px; font-weight: 500; line-height: 1.35; text-align: left; cursor: pointer;
  transition: background-color .15s, box-shadow .15s; box-shadow: 0 1px 0 rgba(0,0,0,.06);
}
.fx-cta:hover { background: #FFFFFF; box-shadow: 0 2px 10px rgba(20, 35, 31, .18); }
.fx-cta-arrow { flex-shrink: 0; transition: transform .2s var(--fx-ease); }
.fx-cta:hover .fx-cta-arrow { transform: translateX(3px); }
.fx-cta-done {
  justify-content: center; gap: 8px; cursor: default; background: transparent; color: var(--fx-on-accent);
  box-shadow: inset 0 0 0 1px rgba(241, 239, 232, .45);
}
.fx-cta-done:hover { background: transparent; box-shadow: inset 0 0 0 1px rgba(241, 239, 232, .45); }
.fx-quiet {
  display: inline-block; margin-top: 10px; padding: 2px 0; background: none; border: none; cursor: pointer;
  font-size: 13px; color: var(--fx-on-accent-2); text-decoration: underline;
  text-decoration-color: transparent; text-underline-offset: 3px;
  transition: text-decoration-color .15s, color .15s;
}
.fx-quiet:hover { color: var(--fx-on-accent); text-decoration-color: currentColor; }

/* 2. heavy */
.fx-heavy { display: flex; flex-direction: column; }
.fx-heavy-text { font-size: 13.5px; line-height: 1.5; color: var(--fx-ink-2); margin-bottom: 14px; }
.fx-soft {
  width: 100%; padding: 11px 14px; border-radius: 12px; cursor: pointer;
  background: var(--fx-accent-soft); color: var(--fx-accent-deep); border: 1px solid transparent;
  font-size: 14px; font-weight: 500; transition: border-color .15s, background-color .15s;
}
.fx-soft:hover:not(:disabled) { border-color: var(--fx-accent); }
.fx-soft:disabled { cursor: default; background: var(--fx-surface); color: var(--fx-ink-3); }
.fx-heavy-plan { display: flex; flex-direction: column; gap: 4px; }
.fx-plan-if { font-size: 13px; line-height: 1.4; color: var(--fx-ink-3); }
.fx-plan-then { display: flex; align-items: flex-start; gap: 7px; font-size: 14px; line-height: 1.45; color: var(--fx-ink); }
.fx-plan-arrow { flex-shrink: 0; margin-top: 3px; color: var(--fx-accent); }
.fx-small { font-size: 12px; color: var(--fx-ink-3); margin-top: 4px; }
.fx-care {
  margin-top: auto; padding-top: 14px; font-size: 11.5px; line-height: 1.45; color: var(--fx-ink-3);
}

/* 3. patterns */
.fx-prow + .fx-prow { border-top: 1px solid var(--fx-line); }
.fx-phead {
  width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 0; background: none; border: none; cursor: pointer; text-align: left; color: var(--fx-ink);
}
.fx-prow:first-child .fx-phead { padding-top: 0; }
.fx-pname { font-size: 14.5px; font-weight: 500; display: flex; align-items: center; flex-wrap: wrap; gap: 6px 0; }
.fx-pname .fx-chip { margin-left: 8px; }
.fx-pcount { display: inline-flex; align-items: center; gap: 8px; flex-shrink: 0; font-size: 12.5px; color: var(--fx-ink-3); }
.fx-chev { transition: transform .2s var(--fx-ease); }
.fx-chev[data-open="true"] { transform: rotate(180deg); }
.fx-pbody { padding: 0 0 14px; }
.fx-pdef { font-size: 13px; line-height: 1.5; color: var(--fx-ink-3); }
.fx-evidence {
  font-family: 'Fraunces', Georgia, serif; font-size: 16px; line-height: 1.4;
  color: var(--fx-ink); margin: 8px 0 12px; padding-left: 12px; border-left: 2px solid var(--fx-accent);
}
.fx-abc { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.fx-abc li { background: var(--fx-surface); border-radius: 10px; padding: 9px 11px; display: flex; flex-direction: column; gap: 3px; }
.fx-abc b { font-size: 11.5px; font-weight: 600; color: var(--fx-ink-3); letter-spacing: .02em; }
.fx-abc span { font-size: 13px; line-height: 1.4; color: var(--fx-ink-2); }

/* 4. experiments */
.fx-erow + .fx-erow { border-top: 1px solid var(--fx-line); padding-top: 12px; margin-top: 12px; }
.fx-ename { font-size: 14px; font-weight: 500; }
.fx-etext { font-size: 13px; color: var(--fx-ink-3); margin: 2px 0 10px; line-height: 1.45; }
.fx-arm { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 6px; }
.fx-arm-name { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; color: var(--fx-ink-2); min-width: 0; }
.fx-arm-check { color: var(--fx-accent); flex-shrink: 0; }
.fx-pips { display: inline-flex; gap: 3px; flex-shrink: 0; }
.fx-pips i { width: 9px; height: 9px; border-radius: 3px; background: var(--fx-pending); }
.fx-pips i[data-on="true"] { background: var(--fx-accent); }

/* 5. why */
.fx-why-name { font-size: 14.5px; font-weight: 500; margin-bottom: 6px; }
.fx-why-text { font-size: 13.5px; line-height: 1.55; color: var(--fx-ink-2); margin-bottom: 12px; text-wrap: pretty; }
.fx-dl { display: grid; grid-template-columns: 62px minmax(0, 1fr); gap: 6px 10px; font-size: 12.5px; line-height: 1.45; }
.fx-dl dt { color: var(--fx-ink-3); font-weight: 500; }
.fx-dl dd { color: var(--fx-ink-2); }

/* 6. guide */
.fx-chat {
  max-height: 300px; min-height: 120px; overflow-y: auto; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 10px; padding: 2px 2px 8px;
}
.fx-chat-empty { display: flex; flex-direction: column; gap: 12px; }
.fx-starters { display: flex; flex-wrap: wrap; gap: 8px; }
.fx-starter {
  padding: 7px 12px; border-radius: 999px; cursor: pointer;
  background: var(--fx-surface); border: 1px solid var(--fx-line); color: var(--fx-ink-2);
  font-size: 13px; transition: border-color .15s, color .15s;
}
.fx-starter:hover:not(:disabled) { border-color: var(--fx-accent); color: var(--fx-accent-deep); }
.fx-msg { display: flex; flex-direction: column; gap: 6px; max-width: 86%; }
.fx-msg[data-role="user"] { align-self: flex-end; align-items: flex-end; }
.fx-msg[data-role="assistant"] { align-self: flex-start; }
.fx-bubble {
  padding: 9px 13px; border-radius: 14px; font-size: 14px; line-height: 1.5;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.fx-msg[data-role="user"] .fx-bubble { background: var(--fx-accent); color: var(--fx-on-accent); border-bottom-right-radius: 5px; }
.fx-msg[data-role="assistant"] .fx-bubble { background: var(--fx-surface); color: var(--fx-ink); border-bottom-left-radius: 5px; }
.fx-bubble[data-kind="crisis"] { background: var(--fx-accent-soft); border: 1px solid var(--fx-accent); }
.fx-sources { display: flex; flex-wrap: wrap; gap: 6px; }
.fx-source {
  display: inline-block; padding: 3px 9px; border-radius: 999px;
  font-size: 12px; line-height: 1.4; color: var(--fx-ink-2); background: var(--fx-tile);
  border: 1px solid var(--fx-line); text-decoration: none;
}
.fx-source svg { vertical-align: -1px; margin-left: 3px; }
.fx-source b { font-weight: 600; color: var(--fx-ink); }
a.fx-source:hover { border-color: var(--fx-accent); }
.fx-typing { display: inline-flex; gap: 4px; align-items: center; min-height: 21px; }
.fx-typing i { width: 6px; height: 6px; border-radius: 50%; background: var(--fx-ink-3); animation: fx-blink 1.2s infinite ease-in-out; }
.fx-typing i:nth-child(2) { animation-delay: .15s; }
.fx-typing i:nth-child(3) { animation-delay: .3s; }
@keyframes fx-blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
.fx-chat-err { font-size: 12.5px; color: var(--fx-ink-3); margin-top: 6px; }
.fx-chat-form { display: flex; gap: 8px; margin-top: 10px; }
.fx-chat-input {
  flex: 1; min-width: 0; padding: 11px 14px; border-radius: 12px; font: inherit; font-size: 14px;
  color: var(--fx-ink); background: var(--fx-tile); border: 1px solid var(--fx-line-strong);
}
.fx-chat-input:focus { outline: 2px solid var(--fx-accent); outline-offset: 1px; border-color: transparent; }
.fx-chat-send {
  flex-shrink: 0; width: 44px; border-radius: 12px; border: none; cursor: pointer;
  display: grid; place-items: center; background: var(--fx-accent); color: var(--fx-on-accent);
}
.fx-chat-send:disabled { cursor: default; background: var(--fx-pending); color: var(--fx-ink-3); }

/* 7. how */
.fx-how { display: grid; grid-template-columns: 128px minmax(0, 1fr); gap: 6px 16px; align-items: start; background: transparent; }
.fx-how-text { font-size: 13.5px; line-height: 1.55; color: var(--fx-ink-2); text-wrap: pretty; }
.fx-how-meta { font-size: 12px; color: var(--fx-ink-3); margin-top: 5px; }

/* loading + motion */
.fx-shimmer {
  border-radius: 6px; background: linear-gradient(90deg, #EFECE4 0%, #E6E2D8 40%, #EFECE4 80%);
  background-size: 200% 100%; animation: fx-shimmer 1.4s ease-in-out infinite;
}
.fx-shimmer-on-accent {
  background: linear-gradient(90deg, rgba(241,239,232,.10) 0%, rgba(241,239,232,.20) 40%, rgba(241,239,232,.10) 80%);
  background-size: 200% 100%;
}
.fx-reveal { animation: fx-rise .5s var(--fx-ease) both; }
@keyframes fx-rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes fx-spin { to { transform: rotate(360deg); } }
@keyframes fx-shimmer { from { background-position: 100% 0; } to { background-position: -100% 0; } }
@media (prefers-reduced-motion: reduce) {
  .fx *, .fx *::before, .fx *::after { animation: none !important; transition: none !important; }
}

@container fx (max-width: 580px) {
  .fx-hero, .fx-heavy, .fx-patterns, .fx-exp, .fx-why, .fx-guide, .fx-how { grid-column: span 6; }
  .fx-msg { max-width: 94%; }
  .fx-guide .fx-label-meta { display: none; }
  .fx-hero { min-height: 0; }
  .fx-then { font-size: 21px; }
  .fx-abc { grid-template-columns: 1fr; }
  .fx-how { grid-template-columns: 1fr; }
  .fx-status > span:not(.fx-chip) { display: none; }
}
`;
