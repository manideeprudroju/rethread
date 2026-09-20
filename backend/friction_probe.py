"""
friction_probe.py -- the clinical support agent (nightly) + the "feels heavy" tap.

WHAT IT DOES
    Once a night (first dashboard open of the day), it reads the archived tab
    timeline and session records, finds recurring friction patterns, works
    each one up the way a behavioural clinician would (before / during /
    after), picks a support from an evidence-graded protocol, and hands
    tomorrow's session ONE if-then plan built for that pattern.

    It reasons like a clinician and is built so it cannot act like one:
    it describes what the tabs show, never the person.

THE SPLIT (same as drift vs churn, gate vs analyst)
    DETECTORS  pure Python, no model.   slow start, circling, rechecking, spread
    PROTOCOL   pure Python, no model.   KB entries: mechanism, EF domain, source,
                                        when not to use
    SELECTION  pure Python, no model.   experiment_probe.assign_condition until
                                        experiment_probe.analyse() clears
    PHRASING   ONE model call.          fit the chosen plan to their real work,
                                        validated, repaired once, template fallback

    Quiet day -> no pattern -> no model call. Cost is one call per night at most.

WHAT IT NEVER DOES
    Infer emotion, anxiety or any state from tabs. Score, screen or label the
    person. The "feels heavy" path exists only because the USER said so, with
    one tap -- the agent never raises it on its own.

    Every pattern name is observational:
        slow start  -- first tab for the work came late after the session was set
        circling    -- short repeated returns to the same page
        rechecking  -- messages / inbox opened again and again, briefly
        spread      -- many different sites in a few minutes, short visits

Usage:
    python friction_probe.py            # runs every check, no model needed
"""

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from experiment_probe import PER_ARM, analyse, assign_condition

# The same rules every other plan in a session meets. A friction plan sits in
# the same "If you get stuck" list as decompose's plans, so it has to clear
# the same bar, from the same source -- not a parallel list that drifts.
# Guarded like reentry_validate's import: a missing module must not take
# the whole Lambda down.
try:
    from decompose_guardrail import BANNED_PHRASES
    from decompose_guardrail import validate as _plan_rules
except ImportError:
    BANNED_PHRASES, _plan_rules = [], None
try:
    from reentry_validate import CHARACTERISING, DRIFT_WORDS
except ImportError:
    CHARACTERISING, DRIFT_WORDS = [], []

# --------------------------------------------------------------------------
# THRESHOLDS. Conservative on purpose: like drift, a false pattern invents a
# problem that did not happen, and the person cannot check it. Missing one is
# cheap; inventing one is not.
# --------------------------------------------------------------------------

SLOW_START_S = 15 * 60          # first work tab this long after the session was set

CIRCLE_MIN_RETURNS = 3          # visits to the same page
CIRCLE_MAX_DWELL_S = 90         # each one short
CIRCLE_MIN_SPAN_S = 3 * 60      # but spread over real time
CIRCLE_MAX_SPAN_S = 15 * 60     # and close together
CIRCLE_MAX_OTHER_DOMAINS = 3    # otherwise it is spread, not circling

RECHECK_MIN_VISITS = 5
RECHECK_MAX_DWELL_S = 60
RECHECK_WINDOW_S = 30 * 60

SPREAD_MIN_DOMAINS = 8
SPREAD_WINDOW_S = 10 * 60
SPREAD_MAX_MEDIAN_DWELL_S = 45

DWELL_CAP_S = 1800              # same ceiling sessionStore uses
DEFAULT_DAYS = 7

# Messaging and inbox sites. Only these count as "checking". Named as a
# category in anything shown; the site itself is never named.
CHECK_DOMAIN_RE = re.compile(
    r"(^|\.)(mail\.google\.com|outlook\.(live|office|office365)\.com|"
    r"web\.whatsapp\.com|app\.slack\.com|teams\.microsoft\.com|"
    r"discord\.com|web\.telegram\.org|messages\.google\.com|"
    r"mail\.yahoo\.com|mail\.proton\.me|linkedin\.com/messaging)$"
)

CARE_LINE = ("Support for the work, not treatment. If it's more than the work, "
             "Tele-MANAS is free and open 24x7: 14416.")

# --------------------------------------------------------------------------
# PATTERNS -- observational names only.
# --------------------------------------------------------------------------

PATTERNS = {
    "slow_start": {
        "label": "Slow start",
        "ef_domain": "Task initiation",
        "definition": "The first tab for the work came 15 or more minutes after "
                      "the session was set.",
        "metric": "latency",      # outcome: initiation_latency_s, lower is better
    },
    "circling": {
        "label": "Circling",
        "ef_domain": "Working memory",
        "definition": "Three or more short returns to the same page within "
                      "a few minutes.",
        "metric": "count",        # outcome: episodes in that session
    },
    "rechecking": {
        "label": "Rechecking",
        "ef_domain": "Inhibition",
        "definition": "Messages or an inbox opened five or more times in half "
                      "an hour, briefly each time.",
        "metric": "count",
    },
    "spread": {
        "label": "Spread",
        "ef_domain": "Working memory load",
        "definition": "Eight or more different sites in ten minutes, with "
                      "short visits.",
        "metric": "count",
    },
    "heavy": {
        "label": "Feels heavy",
        "ef_domain": "Motivation regulation",
        "definition": "You marked this work as feeling heavy.",
        # No outcome. The tap usually comes after the session has started, so
        # start latency can't measure the plan, and nothing else in the tabs
        # can either. The two supports alternate; no result is ever claimed.
        "metric": None,
    },
}

# --------------------------------------------------------------------------
# KNOWLEDGE BASE -- evidence-graded protocol. Two supports per pattern, so
# the single-case experiment has two arms. Every source below was checked.
#
#   kind       next_action | reframe | reset   (the three arms of the design)
#   mechanism  how it works, in plain words -- shown to the user
#   source     where the mechanism comes from -- shown to the user
#   not_for    when a clinician would not use it -- shown to the user
#   if / then  the template. {goal} = their words, {work} = the work page
# --------------------------------------------------------------------------

GOLLWITZER = ("Gollwitzer & Sheeran (2006), meta-analysis of implementation "
              "intentions, 94 tests, d = 0.65")
WEBB = ("Webb, Miles & Sheeran (2012), Psychological Bulletin, meta-analysis "
        "of 306 emotion-regulation comparisons")
BARKLEY = "Barkley's point-of-performance model of executive function"
KUSHLEV = ("Kushlev & Dunn (2015), Computers in Human Behavior, "
           "two-week within-person experiment, 124 adults")
SCHWEIGER = ("Schweiger Gallo, Keil, McCulloch, Rockstroh & Gollwitzer (2009), "
             "Journal of Personality and Social Psychology")

KB = {
    "slow_start": [
        {
            "id": "first_step_cue", "kind": "next_action",
            "name": "Cue the first step",
            "if": "five minutes have passed since the session started and {work} isn't open yet",
            "then": "open {work} and do only the first physical step",
            "mechanism": "An if-then plan hands the start to a cue instead of a "
                         "decision, which lowers the cost of beginning.",
            "source": GOLLWITZER,
            "not_for": "Work with no clear first step yet. Break it down first.",
        },
        {
            "id": "rough_draft", "kind": "reframe",
            "name": "Rough-draft framing",
            "if": "I open {work} and don't want to start",
            "then": "call the next ten minutes a rough draft that is allowed to be bad, and start it",
            "mechanism": "Changing how the task itself is read, rather than the "
                         "feeling about it, is the reappraisal type with the "
                         "larger effect (d = 0.36).",
            "source": WEBB,
            "not_for": "Work with a hard quality bar right now, like a final submission.",
        },
    ],
    "circling": [
        {
            "id": "one_line_question", "kind": "reframe",
            "name": "Write the question down",
            "if": "I'm back on {work} for the third time in a few minutes",
            "then": "write the one question I'm stuck on as a single line at the top",
            "mechanism": "Puts the open question on the page, so working memory "
                         "isn't holding it while you look for the answer.",
            "source": BARKLEY,
            "not_for": "Loops caused by waiting on an input from someone else.",
        },
        {
            "id": "good_enough", "kind": "next_action",
            "name": "Pre-decided stopping rule",
            "if": "I'm back on {work} for the third time in a few minutes",
            "then": "mark this part as good enough for now and move to the next part",
            "mechanism": "The stopping point is decided in advance as an if-then "
                         "plan, so the choice isn't remade on every return.",
            "source": GOLLWITZER,
            "not_for": "Parts other work depends on being exact.",
        },
    ],
    "rechecking": [
        {
            "id": "park_the_check", "kind": "next_action",
            "name": "Park it, check in a batch",
            "if": "I reach for messages while {work} is open",
            "then": "write what I wanted to check on a note and check everything together at the next break",
            "mechanism": "Checking in batches instead of continuously lowered "
                         "daily stress when it was tested week against week.",
            "source": KUSHLEV,
            "not_for": "Days you're on call or waiting on something urgent.",
        },
        {
            "id": "minute_away", "kind": "reset",
            "name": "One-minute reset",
            "if": "I notice I've opened messages again",
            "then": "close that tab, stand up for one minute, and come back to {work}",
            "mechanism": "Briefly moving attention away from the pull had a "
                         "small but reliable effect (d = 0.27).",
            "source": WEBB,
            "not_for": "When a message really does need an answer now.",
        },
    ],
    "spread": [
        {
            "id": "one_tab", "kind": "next_action",
            "name": "Cut to one thread",
            "if": "I've opened several sites in a few minutes and nothing is moving",
            "then": "close all but {work} and the one reference I need right now",
            "mechanism": "Fewer open threads for working memory to track: "
                         "structure in the environment instead of effort.",
            "source": BARKLEY,
            "not_for": "Comparing sources side by side on purpose.",
        },
        {
            "id": "step_on_paper", "kind": "reset",
            "name": "Minute on paper",
            "if": "I've opened several sites in a few minutes and nothing is moving",
            "then": "stop for one minute, write the very next step on paper, and open only what that step needs",
            "mechanism": "A short step away, then the next step written down, "
                         "so the plan sits on paper instead of in working memory.",
            "source": BARKLEY + "; " + WEBB,
            "not_for": "Research sprints where scanning many sources is the task.",
        },
    ],
    "heavy": [
        {
            "id": "name_it", "kind": "reframe",
            "name": "Name it, then start",
            "if": "I sit down to {goal} and it feels heavy",
            "then": "write one line on what exactly feels heavy about it, then do the first step anyway",
            "mechanism": "If-then plans have reduced emotional reactions in "
                         "controlled studies, and reappraising the task is the "
                         "stronger kind of reappraisal (d = 0.36).",
            "source": SCHWEIGER + "; " + WEBB,
            "not_for": "Heaviness that isn't about this work. That's worth "
                       "talking to someone about.",
        },
        {
            "id": "shrink_it", "kind": "next_action",
            "name": "Shrink it",
            "if": "I sit down to {goal} and it feels heavy",
            "then": "shrink it to a version I could finish in ten minutes and do only that",
            "mechanism": "Lowers the cost of starting, and it's planned ahead so "
                         "it doesn't depend on effort in the moment.",
            "source": GOLLWITZER,
            "not_for": "Work that can't be made smaller without breaking it.",
        },
    ],
}

KB_INDEX = {s["id"]: (p, s) for p, supports in KB.items() for s in supports}

# --------------------------------------------------------------------------
# TIME
# --------------------------------------------------------------------------


def _ms(v):
    """ISO string or epoch ms -> epoch ms (int), or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if re.fullmatch(r"\d{10,}", s):
            return int(s)
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    return None


def _local(ms, tz_offset_min):
    # JS getTimezoneOffset(): minutes BEHIND UTC. IST is -330.
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) - timedelta(minutes=tz_offset_min)


def clock(ms, tz_offset_min=0):
    if ms is None:
        return ""
    d = _local(ms, tz_offset_min)
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'}"


def day_key(ms, tz_offset_min=0):
    return _local(ms, tz_offset_min).strftime("%Y-%m-%d")


def when(ms, tz_offset_min=0):
    """'Sat 2:20 pm'. What the panel shows instead of a count."""
    return f"{_local(ms, tz_offset_min).strftime('%a')} {clock(ms, tz_offset_min)}"


# --------------------------------------------------------------------------
# NORMALISATION
# --------------------------------------------------------------------------


def norm_title(t):
    t = re.sub(r"^\(\d+\)\s*", "", str(t or ""))
    return re.sub(r"\s+", " ", t).strip().lower()


def page_name(title, domain=""):
    """'Q3 summary - Google Docs' -> 'Q3 summary'. Site-only segments go."""
    t = re.sub(r"^\(\d+\)\s*", "", str(title or "")).strip()
    parts = [p.strip() for p in re.split(r"\s+[-–—|·•]\s+", t) if p.strip()]
    dom = str(domain or "").lower()
    kept = [p for p in parts
            if not (dom and all(w in dom for w in re.findall(r"[a-z0-9]{3,}", p.lower())))]
    name = (kept or parts or [t])[0]
    return name[:60].strip()


def is_check_domain(domain):
    return bool(CHECK_DOMAIN_RE.search(str(domain or "").lower()))


def _group_sessions(events, sessions):
    """-> list of {start, end, intent, record, events[]} with dwell filled in."""
    records = {}
    for r in sessions or []:
        k = _ms(r.get("started_at"))
        if k is not None:
            records[k] = r

    groups = defaultdict(list)
    for e in events or []:
        ts = _ms(e.get("ts"))
        if ts is None or not e.get("title"):
            continue
        k = _ms(e.get("session_started_at"))
        groups[k].append({**e, "_ts": ts,
                          "_end": _ms(e.get("session_ended_at"))})

    out = []
    for k in set(groups) | set(records):
        evs = sorted(groups.get(k, []), key=lambda x: x["_ts"])
        rec = records.get(k) or {}
        end = _ms(rec.get("ended_at")) or next((x["_end"] for x in evs if x["_end"]), None)
        for i, e in enumerate(evs):
            given = e.get("dwell_seconds")
            if isinstance(given, (int, float)) and not isinstance(given, bool) and math.isfinite(given):
                d = given
            else:
                nxt = evs[i + 1]["_ts"] if i + 1 < len(evs) else end
                d = (nxt - e["_ts"]) / 1000 if nxt else 60
            e["_dwell"] = max(0, min(DWELL_CAP_S, int(round(d))))
        start = k if k is not None else (evs[0]["_ts"] if evs else None)
        out.append({"start": start, "end": end, "record": rec, "events": evs,
                    "intent": rec.get("intent") or next(
                        (x.get("session_intent") for x in evs if x.get("session_intent")), None)})
    return sorted([s for s in out if s["start"] is not None], key=lambda s: s["start"])


# --------------------------------------------------------------------------
# DETECTORS -- pure Python. Each returns episodes:
#   {pattern, at, evidence, before, during, after, work}
# before / during / after is the functional analysis: what came right
# before, what the tabs show, what happened next. Tabs outside the work are
# never named -- a category at most.
# --------------------------------------------------------------------------


def _set_at(sess, tz):
    """The antecedent every pattern shares: which session, set when."""
    return f"Session set {clock(sess['start'], tz)}: {sess['intent'] or 'no goal recorded'}"


def _work_tab(sess):
    """The first tab drift judged on-task, if the session recorded one."""
    lat = sess["record"].get("initiation_latency_s")
    if not isinstance(lat, (int, float)) or isinstance(lat, bool) or not sess["events"]:
        return None
    target = sess["start"] + lat * 1000
    best = min(sess["events"], key=lambda e: abs(e["_ts"] - target))
    return best if abs(best["_ts"] - target) <= 5000 else None


def _after(sess, idx, tz):
    """What happened right after event idx."""
    evs = sess["events"]
    # Clock times only, the Re-entry panel's rule: "2:26 pm" orients, a
    # duration counts. The same goes for how many times something happened.
    if idx + 1 >= len(evs):
        return f"Session ended {clock(sess['end'], tz)}" if sess["end"] else "That was the last tab recorded"
    nxt = evs[idx + 1]
    if nxt["_dwell"] >= 300:
        return f"Settled on one tab from {clock(nxt['_ts'], tz)}"
    return f"Next tab at {clock(nxt['_ts'], tz)}"


def detect_slow_start(sess, tz=0):
    rec = sess["record"]
    lat = rec.get("initiation_latency_s")
    # A missing latency is NOT a slow start: the work may have been in another
    # app. Same bias as drift -- never invent a lapse.
    if not isinstance(lat, (int, float)) or isinstance(lat, bool) or lat < SLOW_START_S:
        return []
    first = sess["start"] + int(lat * 1000)
    work = _work_tab(sess)
    idx = sess["events"].index(work) if work else None
    return [{
        "pattern": "slow_start", "at": first,
        "evidence": f"Session set {clock(sess['start'], tz)}; first tab for the work "
                    f"{clock(first, tz)}",
        "before": _set_at(sess, tz),
        # The browser can't see other apps. Say so rather than imply nothing
        # was happening.
        "during": f"No browser tab for the work before {clock(first, tz)}. "
                  f"It may have been in another app.",
        "after": (_after(sess, idx, tz) if idx is not None
                  else f"First tab for the work at {clock(first, tz)}"),
        "work": page_name(work["title"], work.get("domain")) if work else None,
    }]


def detect_circling(sess, tz=0):
    evs = sess["events"]
    visits = defaultdict(list)
    for i, e in enumerate(evs):
        if not is_check_domain(e.get("domain")):
            visits[(norm_title(e["title"]), str(e.get("domain", "")).lower())].append(i)

    episodes = []
    for key, idxs in visits.items():
        best = None
        for a in range(len(idxs)):
            for b in range(a + CIRCLE_MIN_RETURNS - 1, len(idxs)):
                run = idxs[a:b + 1]
                span = (evs[run[-1]]["_ts"] - evs[run[0]]["_ts"]) / 1000
                if span > CIRCLE_MAX_SPAN_S:
                    break
                if span < CIRCLE_MIN_SPAN_S:
                    continue
                if any(evs[i]["_dwell"] > CIRCLE_MAX_DWELL_S for i in run):
                    continue
                between = {str(evs[i].get("domain", "")).lower()
                           for i in range(run[0], run[-1] + 1)} - {key[1]}
                if len(between) > CIRCLE_MAX_OTHER_DOMAINS:
                    continue
                if best is None or len(run) > len(best):
                    best = run
        if best:
            first, last = evs[best[0]], evs[best[-1]]
            name = page_name(first["title"], first.get("domain"))
            episodes.append({
                "pattern": "circling", "at": first["_ts"],
                "evidence": f"Short, repeated visits to \u201c{name}\u201d, "
                            f"{clock(first['_ts'], tz)}\u2013{clock(last['_ts'], tz)}",
                "before": _set_at(sess, tz),
                "during": f"Back and forth on the same page between "
                          f"{clock(first['_ts'], tz)} and {clock(last['_ts'], tz)}, "
                          f"each visit brief",
                "after": _after(sess, best[-1], tz),
                "work": name,
                "_n": len(best), "_last": last["_ts"],
            })
    # Alternating between two pages circles both. One episode per stretch of
    # time, not one per page: keep the page with the most returns and drop any
    # episode that overlaps a kept one, so a loop is never counted twice.
    kept = []
    for ep in sorted(episodes, key=lambda e: (-e["_n"], e["at"])):
        if all(ep["_last"] < k["at"] or ep["at"] > k["_last"] for k in kept):
            kept.append(ep)
    for ep in kept:
        ep.pop("_n"), ep.pop("_last")
    return sorted(kept, key=lambda e: e["at"])


def detect_rechecking(sess, tz=0):
    evs = sess["events"]
    checks = [i for i, e in enumerate(evs)
              if is_check_domain(e.get("domain")) and e["_dwell"] <= RECHECK_MAX_DWELL_S]
    best = None
    for a in range(len(checks)):
        b = a
        while (b + 1 < len(checks) and
               (evs[checks[b + 1]]["_ts"] - evs[checks[a]]["_ts"]) / 1000 <= RECHECK_WINDOW_S):
            b += 1
        run = checks[a:b + 1]
        if len(run) >= RECHECK_MIN_VISITS and (best is None or len(run) > len(best)):
            best = run
    if not best:
        return []
    first, last = evs[best[0]], evs[best[-1]]
    work = _work_tab(sess)
    return [{
        "pattern": "rechecking", "at": first["_ts"],
        "evidence": f"Brief visits to messages or inbox, "
                    f"{clock(first['_ts'], tz)}\u2013{clock(last['_ts'], tz)}",
        "before": _set_at(sess, tz),
        "during": f"Short visits to messages or inbox between "
                  f"{clock(first['_ts'], tz)} and {clock(last['_ts'], tz)}",
        "after": _after(sess, best[-1], tz),
        "work": page_name(work["title"], work.get("domain")) if work else None,
    }]


def detect_spread(sess, tz=0):
    evs = sess["events"]
    best = None
    for a in range(len(evs)):
        b = a
        while b + 1 < len(evs) and (evs[b + 1]["_ts"] - evs[a]["_ts"]) / 1000 <= SPREAD_WINDOW_S:
            b += 1
        win = evs[a:b + 1]
        doms = {str(e.get("domain", "")).lower() for e in win}
        dwells = sorted(e["_dwell"] for e in win)
        med = dwells[len(dwells) // 2] if dwells else 0
        if len(doms) >= SPREAD_MIN_DOMAINS and med <= SPREAD_MAX_MEDIAN_DWELL_S:
            if best is None or len(doms) > best[2]:
                best = (a, b, len(doms))
    if not best:
        return []
    a, b, n = best
    first, last = evs[a], evs[b]
    work = _work_tab(sess)
    return [{
        "pattern": "spread", "at": first["_ts"],
        "evidence": f"Many different sites in a few minutes, "
                    f"{clock(first['_ts'], tz)}\u2013{clock(last['_ts'], tz)}",
        "before": _set_at(sess, tz),
        "during": f"Short visits across many sites between "
                  f"{clock(first['_ts'], tz)} and {clock(last['_ts'], tz)}",
        "after": _after(sess, b, tz),
        "work": page_name(work["title"], work.get("domain")) if work else None,
    }]


DETECTORS = [detect_slow_start, detect_circling, detect_rechecking, detect_spread]


def detect_all(sess, tz=0):
    eps = []
    for fn in DETECTORS:
        eps.extend(fn(sess, tz))
    return eps


# --------------------------------------------------------------------------
# SELECTION -- the existing experiment gate, per pattern.
# --------------------------------------------------------------------------


def _trial_rows(pattern, trials, by_start, tz=0):
    """trials: [{pattern, support, session_started_at}] -> rows for analyse().

    Outcome is measured on the session that carried the plan:
      latency patterns  -> that session's initiation_latency_s
      count patterns    -> episodes of that pattern in that session
    A session with nothing recorded is sent as missing, not as zero.
    """
    rows = []
    if PATTERNS[pattern]["metric"] is None:
        return rows
    for t in trials or []:
        if t.get("pattern") != pattern or not t.get("support"):
            continue
        stored = t.get("value")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool) and math.isfinite(stored):
            # Measured on an earlier night. The archive keeps only the last
            # 500 tab events, so the raw evidence for an 8 + 8 experiment is
            # long gone by the time it can be decided.
            rows.append({"condition": t["support"], "value": stored})
            continue
        sess = by_start.get(_ms(t.get("session_started_at")))
        value = None
        if sess:
            if PATTERNS[pattern]["metric"] == "latency":
                v = sess["record"].get("initiation_latency_s")
                value = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            elif sess["events"]:
                fn = {"circling": detect_circling, "rechecking": detect_rechecking,
                      "spread": detect_spread}[pattern]
                value = len(fn(sess, tz))
        rows.append({"condition": t["support"], "value": value})
    return rows


def trial_values(trials, by_start, tz=0):
    """Outcomes that can be measured now and aren't stored yet. The client
    keeps them, so each session is measured once, while its tabs still exist."""
    out = []
    for p in {t.get("pattern") for t in trials or []} & set(KB):
        pending = [t for t in trials if t.get("pattern") == p and t.get("support")
                   and t.get("value") is None]
        for t, row in zip(pending, _trial_rows(p, pending, by_start, tz)):
            if row["value"] is not None:
                out.append({"pattern": p, "support": t.get("support"),
                            "session_started_at": t.get("session_started_at"),
                            "value": row["value"]})
    return out


def select_support(pattern, trials, vetoes, by_start, user_id="", tz=0):
    supports = KB[pattern]
    vetoed = set((vetoes or {}).get(pattern) or [])
    arms = [s["id"] for s in supports if s["id"] not in vetoed]
    labels = {s["id"]: s["name"] for s in supports}

    if not arms:
        # Everything set aside. Respect it: say so, don't pick one anyway.
        return None, {"mode": "all_set_aside", "arms": [],
                      "ui_text": "You've set aside both supports for this one."}
    if len(arms) == 1:
        return arms[0], {"mode": "only_option", "arms": arms,
                         "ui_text": "Using the one you haven't set aside."}
    if PATTERNS[pattern]["metric"] is None:
        n = sum(1 for t in trials or [] if t.get("pattern") == pattern and t.get("support") in arms)
        return assign_condition(n, arms, seed=f"{user_id}|{pattern}"), {
            "mode": "rotation", "arms": arms,
            "ui_text": "Alternates between two kinds of support. No result is claimed for this one."}

    rows = _trial_rows(pattern, trials, by_start, tz)
    result = analyse(rows, metric="value", arms=arms, labels=labels, lower_is_better=True)
    if result["status"] == "finding":
        return result["finding"]["better_arm"], {"mode": "finding", "arms": arms,
                                                 "analysis": result,
                                                 "ui_text": result["ui_text"]}
    # still_learning or no_difference: keep assigning in balanced blocks.
    # The index counts plans already given for this pattern.
    n = sum(1 for t in trials or [] if t.get("pattern") == pattern and t.get("support") in arms)
    arm = assign_condition(n, arms, seed=f"{user_id}|{pattern}")
    return arm, {"mode": "experiment", "arms": arms, "analysis": result,
                 "ui_text": result["ui_text"]}


# --------------------------------------------------------------------------
# PHRASING -- the one model call.
# --------------------------------------------------------------------------

FRICTION_PROMPT = """\
You write ONE if-then plan for someone's next work session. You are the \
phrasing step of a clinically grounded support system: the pattern, the \
support and its mechanism are already chosen. Your job is to fit the plan to \
their actual work, in plain words.

You get the observed pattern with its evidence (tab titles and clock times \
only), the chosen support with its mechanism, a template plan, the goal in \
their words, and the work page if known.

RULES
- Keep the template's cue and its action. Make them concrete to THIS work \
using only words from the goal, the work page or the template. Never invent \
a file, a section, a number, a tool or a person.
- "if" is a situation they will run into, not a step finishing. First person. \
Do not start it with the word "if".
- "then" is ONE action they can do in under two minutes, starting with a verb. \
Do not start it with the word "then".
- "why" is one sentence in a clinician's voice: the mechanism, plainly. It \
describes how the plan works, never the person.
- Never assess or label the person. No diagnoses, no emotions attributed to \
them, no words like failure, lapse, lazy, avoid, distracted, procrastinate, \
anxious, symptom. No promises of results.
- Under 20 words for "if", under 20 for "then", under 30 for "why".

Respond with ONLY a JSON object, no fences, no preamble:
{"if": "...", "then": "...", "why": "..."}
"""


def fill(template, goal, work):
    def put(g):
        return (template.replace("{goal}", g).replace("{work}", work or "the work"))
    text = put((goal or "the work").strip().rstrip("."))
    # Plans stay under 20 words, the rule every plan in the session meets.
    return text if len(text.split()) < 20 else put("this work")


def build_user(pattern, episode, support, goal, work):
    p = PATTERNS[pattern]
    return (
        f"Pattern: {p['label']} ({p['ef_domain']}). {p['definition']}\n"
        f"Evidence: {episode.get('evidence') if episode else 'Marked by the user.'}\n"
        f"Support: {support['name']} ({support['kind']}). {support['mechanism']}\n"
        f"Template if: {fill(support['if'], goal, work)}\n"
        f"Template then: {fill(support['then'], goal, work)}\n"
        f"Goal, in their words: {goal or '(not given)'}\n"
        f"Work page: {work or '(not known)'}"
    )


def parse_json(raw):
    s = re.sub(r"<think>.*?</think>", "", str(raw or ""), flags=re.DOTALL).strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
    raise ValueError("no JSON object found")


BANNED = re.compile(
    r"\b(anxi\w*|adhd|disorder\w*|symptom\w*|diagnos\w*|procrastinat\w*|lazy|"
    r"fail\w*|lapse\w*|streak\w*|severe|severity|scor\w*|medic\w*|therap\w*|depress\w*|"
    r"panic\w*|addict\w*|compuls\w*|obsess\w*|avoid\w*|distract\w*|fear\w*|"
    r"disgust\w*|guarantee\w*|proven|cure\w*|treat\w*|discipline\w*|"
    r"you should|you will|you'll|you always|you never|"
    r"you(?:'re| are) (?:a|an|so|too|always|never))\b",
    re.IGNORECASE,
)
COMPLETION_OPENERS = ("i finish", "i finished", "i have finished", "i complete",
                      "i completed", "i have completed", "i am done", "i'm done")
FILE_RE = re.compile(r"\b[\w-]+\.(?:py|js|jsx|ts|tsx|pdf|docx?|xlsx?|pptx?|csv|md|txt|ipynb)\b",
                     re.IGNORECASE)
# Double quotes only: a single quote is usually an apostrophe ("I'm").
QUOTE_RE = re.compile(r"[\"“]([^\"”]{3,})[\"”]")
NUM_RE = re.compile(r"\b\d+\b")
LIMITS = {"if": 140, "then": 160, "why": 260}
STEMS = {"procrastinat", "distract", "motivat"}   # prefix match, as in initiate_probe
NUM_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}


def _strip_lead(s, word):
    return re.sub(rf"^\s*{word}[\s,:]+", "", str(s or ""), flags=re.IGNORECASE).strip()


def validate_plan(out, context):
    """-> (passed, [(rule, ok, detail)]). context: everything the user or the
    tabs actually said, plus the template. Anything not in it is invented."""
    ctx = str(context or "").lower()
    checks = []
    if not isinstance(out, dict):
        return False, [("is an object", False, type(out).__name__)]
    for k in ("if", "then", "why"):
        v = out.get(k)
        ok = isinstance(v, str) and 3 <= len(v.strip()) <= LIMITS[k]
        checks.append((f"{k} present and short", ok,
                       "" if ok else f"{len(v) if isinstance(v, str) else v!r}"))
    text = " ".join(str(out.get(k) or "") for k in ("if", "then", "why"))
    m = BANNED.search(text)
    checks.append(("describes the work, never the person", m is None,
                   m.group(0) if m else ""))
    cond = _strip_lead(out.get("if"), "if").lower()
    checks.append(("trigger is a situation, not a step finishing",
                   not cond.startswith(COMPLETION_OPENERS), cond[:40]))
    act = " ".join(str(out.get(k) or "") for k in ("if", "then"))
    invented = [f for f in FILE_RE.findall(act) if f.lower() not in ctx]
    invented += [q for q in QUOTE_RE.findall(act) if q.lower() not in ctx]
    # Same bar as every other plan: decompose's plan rules and shared word
    # lists. The first_action rules don't apply to a plan, so a known-good
    # action stands in and its rules are skipped.
    if _plan_rules is not None:
        _, rule_checks = _plan_rules(
            {"first_action": "Open whatever you are working in",
             "plans": [{"if": _strip_lead(out.get("if"), "if"),
                        "then": _strip_lead(out.get("then"), "then")}]},
            context, mode="fallback")
        for rule, ok, detail, *_ in rule_checks:
            if not rule.startswith("first_action") and not ok:
                checks.append((f"plan rules: {rule}", False, detail))
    low = text.lower()
    shared = [w for w in DRIFT_WORDS + CHARACTERISING if w in low]
    shared += [w for w in BANNED_PHRASES if re.search(
        r"(?<!\w)" + re.escape(w) + ("" if w in STEMS else r"(?!\w)"), low)]
    checks.append(("shared word lists (drift, characterising, banned)",
                   not shared, ", ".join(shared[:3])))
    ctx_nums = set(NUM_RE.findall(ctx)) | {NUM_WORDS[w] for w in re.findall(r"[a-z]+", ctx)
                                           if w in NUM_WORDS}
    invented += [n for n in NUM_RE.findall(act) if n not in ctx_nums]
    checks.append(("names nothing they didn't", not invented, ", ".join(invented)[:60]))
    return all(ok for _, ok, _ in checks), checks


def _reason(checks):
    return "; ".join(r + (f" ({d})" if d else "") for r, ok, d in checks if not ok)


def phrase(pattern, episode, support, goal, work, call_model):
    """One call, one repair, then the template. Never raises."""
    tmpl = {"if": fill(support["if"], goal, work),
            "then": fill(support["then"], goal, work),
            "why": support["mechanism"]}
    context = " ".join(filter(None, [goal, work, tmpl["if"], tmpl["then"],
                                     episode.get("evidence") if episode else ""]))
    user = build_user(pattern, episode, support, goal, work)

    def attempt(msg):
        out = parse_json(call_model(FRICTION_PROMPT, msg))
        if isinstance(out, dict):
            out = {"if": _strip_lead(out.get("if"), "if"),
                   "then": _strip_lead(out.get("then"), "then"),
                   "why": str(out.get("why") or "").strip()}
        return out

    if call_model is not None:
        try:
            out = attempt(user)
            ok, checks = validate_plan(out, context)
            if ok:
                return out
            reason = _reason(checks)
            print(f"[friction] attempt 1 failed: {reason}")
            out2 = attempt(user + "\n\nYour previous answer was REJECTED for: " + reason
                           + "\nFix exactly those problems. When unsure, stay closer "
                             "to the template.")
            if validate_plan(out2, context)[0]:
                return out2
            print("[friction] repair failed -> template")
        except Exception as e:  # model or parse failure: the template is honest
            print(f"[friction] model call raised {type(e).__name__}: {e}")
    return {**tmpl, "_fallback": True}


# --------------------------------------------------------------------------
# ENTRY POINTS -- called by lambda_handler
# --------------------------------------------------------------------------


# What {work} becomes when the next session is on different work.
GENERIC_WORK = {"circling": "the same page"}


def _plan(pattern, support_id, selection, episode, goal, work, call_model):
    support = KB_INDEX[support_id][1]
    text = phrase(pattern, episode, support, goal, work, call_model)
    generic_work = GENERIC_WORK.get(pattern, "the work")
    return {
        "pattern": pattern,
        "pattern_label": PATTERNS[pattern]["label"],
        "support": support_id,
        "support_name": support["name"],
        "kind": support["kind"],
        "if": text["if"], "then": text["then"], "why": text["why"],
        "mechanism": support["mechanism"],
        "source": support["source"],
        "not_for": support["not_for"],
        "ef_domain": PATTERNS[pattern]["ef_domain"],
        "selection": selection,
        # The plan above names yesterday's work. If the next session is on
        # something else, the client uses this instead and puts the new goal
        # in for {goal}. Template text, so it has already passed the guard.
        "goal": goal,
        "generic": {"if": support["if"].replace("{work}", generic_work),
                    "then": support["then"].replace("{work}", generic_work)},
        **({"_fallback": True} if text.get("_fallback") else {}),
    }


def _experiments(trials, by_start, vetoes, tz):
    """Every pattern that has had plans: where its experiment stands."""
    out = {}
    for pattern in sorted({t.get("pattern") for t in trials or []} & set(KB)):
        if PATTERNS[pattern]["metric"] is None:
            continue
        arms = [s["id"] for s in KB[pattern] if s["id"] not in set((vetoes or {}).get(pattern) or [])]
        if len(arms) != 2:
            continue
        labels = {s["id"]: s["name"] for s in KB[pattern]}
        r = analyse(_trial_rows(pattern, trials, by_start, tz), metric="value",
                    arms=arms, labels=labels, lower_is_better=True)
        out[pattern] = {"label": PATTERNS[pattern]["label"], "status": r["status"],
                        "ui_text": r["ui_text"], "counts": r["counts"],
                        "per_arm": PER_ARM,
                        "arm_names": {a: labels[a] for a in arms},
                        **({"finding": r["finding"]} if r.get("finding") else {})}
    return out


def run_friction(body, call_model=None):
    tz = int(body.get("tz_offset_min") or 0)
    days = int(body.get("days") or DEFAULT_DAYS)
    trials = body.get("trials") or []
    vetoes = body.get("vetoes") or {}
    user_id = str(body.get("user_id") or "")

    all_sessions = _group_sessions(body.get("events"), body.get("sessions"))
    by_start = {s["start"]: s for s in all_sessions}
    now = _ms(body.get("now")) or max(
        [s["end"] or s["start"] for s in all_sessions], default=None)
    window = [s for s in all_sessions
              if now is None or s["start"] >= now - days * 86400000]

    episodes = [ep for s in window for ep in detect_all(s, tz)]
    base = {"window": {"days": days, "sessions": len(window),
                       "from": clock(window[0]["start"], tz) if window else None},
            "experiments": _experiments(trials, by_start, vetoes, tz),
            "trial_values": trial_values(trials, by_start, tz),
            "care_line": CARE_LINE}

    if not episodes:
        return {"status": "nothing_found", "patterns": [], "plan": None,
                "ui_text": ("Nothing recurring in the last few sessions."
                            if window else "No sessions to look at yet."),
                **base}

    by_pattern = defaultdict(list)
    for ep in episodes:
        by_pattern[ep["pattern"]].append(ep)
    patterns = []
    for p, eps in by_pattern.items():
        eps.sort(key=lambda e: e["at"])
        patterns.append({
            "id": p, "label": PATTERNS[p]["label"],
            "ef_domain": PATTERNS[p]["ef_domain"],
            "definition": PATTERNS[p]["definition"],
            "episodes": len(eps),
            "days_seen": len({day_key(e["at"], tz) for e in eps}),
            "latest": {k: eps[-1][k] for k in ("evidence", "before", "during", "after")},
            # Shown instead of episodes / days_seen, which rank patterns but
            # are never displayed: a tally of hard moments is a streak by
            # another name.
            "last_seen": when(eps[-1]["at"], tz),
            "_last_at": eps[-1]["at"],
            "_work": next((e["work"] for e in reversed(eps) if e.get("work")), None),
        })
    patterns.sort(key=lambda x: (x["episodes"], x["_last_at"]), reverse=True)

    # Target the most frequent pattern that still has a support to offer.
    plan, target = None, None
    for cand in patterns:
        support_id, selection = select_support(cand["id"], trials, vetoes, by_start, user_id, tz)
        if support_id:
            target = cand
            latest_ep = by_pattern[cand["id"]][-1]
            goal = next((s["intent"] for s in reversed(window) if s["intent"]), None)
            plan = _plan(cand["id"], support_id, selection, latest_ep, goal,
                         cand["_work"], call_model)
            break

    for p in patterns:
        p.pop("_last_at", None)
        p.pop("_work", None)
    return {"status": "plan" if plan else "all_set_aside",
            "patterns": patterns, "target": target["id"] if target else None,
            "plan": plan,
            "ui_text": (f"Tomorrow's plan is for {target['label'].lower()}."
                        if plan else "You've set aside every support for what showed up."),
            **base}


def run_heavy(body, call_model=None):
    """The one-tap 'this one feels heavy'. User-stated, never inferred."""
    tz = int(body.get("tz_offset_min") or 0)
    trials = body.get("trials") or []
    vetoes = body.get("vetoes") or {}
    by_start = {s["start"]: s for s in _group_sessions(body.get("events"), body.get("sessions"))}
    goal = (body.get("goal") or "").strip() or None
    support_id, selection = select_support("heavy", trials, vetoes, by_start,
                                           str(body.get("user_id") or ""), tz)
    if not support_id:
        return {"status": "all_set_aside", "plan": None, "care_line": CARE_LINE,
                "ui_text": selection["ui_text"]}
    plan = _plan("heavy", support_id, selection, None, goal, body.get("work"), call_model)
    return {"status": "plan", "plan": plan, "care_line": CARE_LINE,
            "ui_text": "Added to this session's plans."}


# --------------------------------------------------------------------------
# TESTS -- no model needed. Run: python friction_probe.py
# --------------------------------------------------------------------------

T0 = _ms("2026-09-19T08:30:00Z")   # 2:00 pm IST
IST = -330


def _ev(ts_ms, title, domain, start=T0, end=None):
    return {"ts": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            .replace("+00:00", "Z"), "title": title, "domain": domain,
            "session_intent": "write the Q3 summary report",
            "session_started_at": start, "session_ended_at": end}


def _session(start=T0, latency=240, minutes=60, intent="write the Q3 summary report"):
    return {"started_at": datetime.fromtimestamp(start / 1000, tz=timezone.utc).isoformat(),
            "ended_at": datetime.fromtimestamp((start + minutes * 60000) / 1000,
                                               tz=timezone.utc).isoformat(),
            "intent": intent, "initiation_latency_s": latency, "condition": None,
            "event_count": 10}


def _timeline(spec, start=T0, minutes=60):
    """spec: [(offset_s, title, domain)]"""
    end = start + minutes * 60000
    return [_ev(start + o * 1000, t, d, start, end) for o, t, d in spec]


DOC = ("Q3 summary - Google Docs", "docs.google.com")
SHEET = ("Q3 revenue figures - Google Sheets", "docs.google.com")

STEADY = _timeline([(240, *DOC), (1800, *SHEET), (2400, *DOC)])
CIRCLING = _timeline([(240, *DOC), (1200, *DOC), (1260, *SHEET), (1300, *DOC),
                      (1340, *SHEET), (1400, *DOC), (1460, *SHEET), (1520, *DOC),
                      (1580, "Fiscal quarter - Wikipedia", "en.wikipedia.org")])
RECHECK = _timeline([(240, *DOC)] + [x for k in range(6) for x in
                    [(900 + k * 240, "Inbox (3) - Gmail", "mail.google.com"),
                     (930 + k * 240, *DOC)]])
SPREAD = _timeline([(240, *DOC)] + [(1000 + k * 30, f"page {k}", f"site{k}.com")
                                     for k in range(9)] + [(1300, *DOC)])


class FakeModel:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def __call__(self, system, user):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else self.replies_default
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)

    replies_default = '{"if": "x", "then": "y", "why": "z"}'


GOOD = {"if": "I'm back on Q3 summary for the third time in a few minutes",
        "then": "Write the one question I'm stuck on as a line at the top of Q3 summary",
        "why": "Putting the question on the page frees working memory to look for the answer."}


def _json_strict(obj):
    def refuse(c):
        raise ValueError(c)
    json.loads(json.dumps(obj), parse_constant=refuse)
    return True


def _body(events, sessions, **kw):
    return {"events": events, "sessions": sessions, "tz_offset_min": IST,
            "user_id": "demo", **kw}


def _circling_trials(better, worse):
    """8 + 8 plans, each on its own circling session; outcome = episodes."""
    evs, sess, trials = [], [], []
    for k in range(16):
        start = T0 - (k + 1) * 86400000
        arm = better if k % 2 else worse
        spec = [(240, *DOC)] + ([] if arm == better else
                                [(1200, *DOC), (1260, *SHEET), (1300, *DOC),
                                 (1340, *SHEET), (1400, *DOC), (1450, *SHEET),
                                 (1500, *DOC), (1560, "Fiscal quarter - Wikipedia",
                                                "en.wikipedia.org")])
        spec += [(2000, *SHEET)]
        evs += _timeline(spec, start)
        sess.append(_session(start))
        trials.append({"pattern": "circling", "support": arm,
                       "session_started_at": start})
    return evs, sess, trials


def run_checks():
    ok_all = True

    def check(name, cond, info=""):
        nonlocal ok_all
        ok_all &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{info}]" if info and not cond else ""))

    print("=" * 74)
    print("DETECTORS -- pure Python")
    print("=" * 74)
    s = lambda evs, lat=240: _group_sessions(evs, [_session(latency=lat)])[0]
    check("steady work: nothing", detect_all(s(STEADY), IST) == [])
    c = detect_circling(s(CIRCLING), IST)
    check("circling found, page named, clock in IST",
          len(c) == 1 and "Q3 summary" in c[0]["evidence"] and "pm" in c[0]["evidence"]
          and c[0]["work"] == "Q3 summary", c[0]["evidence"] if c else "none")
    check("circling has before / during / after",
          c and all(c[0][k] for k in ("before", "during", "after")))
    r = detect_rechecking(s(RECHECK), IST)
    check("rechecking found, site never named",
          len(r) == 1 and "gmail" not in json.dumps(r).lower(), json.dumps(r)[:120])
    check("rechecking visits are not circling", detect_circling(s(RECHECK), IST) == [])
    sp = detect_spread(s(SPREAD), IST)
    check("spread found", len(sp) == 1 and "different sites" in sp[0]["evidence"],
          json.dumps(sp)[:120])
    check("slow start: 20 min flagged", len(detect_slow_start(s(STEADY, 1200), IST)) == 1)
    check("slow start: 4 min not flagged", detect_slow_start(s(STEADY, 240), IST) == [])
    check("slow start: missing latency is NOT a slow start (work may be in another app)",
          detect_slow_start(s(STEADY, None), IST) == [])
    ss = detect_slow_start(s(STEADY, 1200), IST)[0]
    check("slow start names the work tab drift found",
          ss["work"] is None and "2:00 pm" in ss["evidence"], ss["evidence"])
    ss2 = detect_slow_start(_group_sessions(_timeline([(1200, *DOC), (2400, *SHEET)]),
                                            [_session(latency=1200)])[0], IST)[0]
    check("slow start work tab matched by latency", ss2["work"] == "Q3 summary", ss2["work"])
    check("dwell computed from the archive (no dwell_seconds sent)",
          s(STEADY)["events"][0]["_dwell"] == 1560)

    print()
    print("=" * 74)
    print("AGENT -- one model call at most, guarded")
    print("=" * 74)
    m = FakeModel([])
    out = run_friction(_body(STEADY, [_session()]), m)
    check("quiet day: nothing_found and ZERO model calls",
          out["status"] == "nothing_found" and m.calls == 0)
    out = run_friction(_body([], []), m)
    check("no data at all: honest empty state", out["status"] == "nothing_found"
          and out["ui_text"] == "No sessions to look at yet.")

    m = FakeModel([GOOD])
    out = run_friction(_body(CIRCLING, [_session()]), m)
    plan = out["plan"]
    check("circling -> plan, one call",
          out["status"] == "plan" and out["target"] == "circling" and m.calls == 1)
    check("plan carries mechanism, source, not_for, EF domain",
          all(plan.get(k) for k in ("mechanism", "source", "not_for", "ef_domain")))
    check("model's plan kept when it passes", plan["if"] == GOOD["if"] and "_fallback" not in plan)
    check("selection is the experiment, still learning",
          plan["selection"]["mode"] == "experiment"
          and plan["selection"]["ui_text"].startswith("Still learning"))
    check("response is strict JSON", _json_strict(out))

    bad_person = {**GOOD, "why": "You avoid hard parts because of anxiety."}
    m = FakeModel([bad_person, GOOD])
    plan = run_friction(_body(CIRCLING, [_session()]), m)["plan"]
    check("labels the person -> repaired", m.calls == 2 and plan["why"] == GOOD["why"])

    invented = {**GOOD, "then": "Open q3_notes.docx and fix section 4"}
    m = FakeModel([invented, invented])
    plan = run_friction(_body(CIRCLING, [_session()]), m)["plan"]
    check("invented file + number twice -> template fallback",
          plan.get("_fallback") and "q3_notes" not in plan["then"]
          and plan["if"] == "I'm back on Q3 summary for the third time in a few minutes",
          plan["then"])
    ok, checks = validate_plan(invented, "Q3 summary")
    check("validator names what was invented",
          not ok and "q3_notes.docx" in _reason(checks) and "4" in _reason(checks))
    check("completion trigger rejected",
          not validate_plan({**GOOD, "if": "I finish the intro"}, "Q3 summary intro")[0])

    check("apostrophes are not quotes",
          validate_plan({**GOOD, "then": "Write what I'm stuck on at the top"}, "Q3 summary")[0])
    check("quoted invented heading rejected",
          not validate_plan({**GOOD, "then": "Go to the “Revenue drivers” heading"},
                            "Q3 summary")[0])

    m = FakeModel([RuntimeError("throttled")])
    plan = run_friction(_body(CIRCLING, [_session()]), m)["plan"]
    check("model down -> still a plan, from the template", plan and plan.get("_fallback"))
    check("no model at all -> template", run_friction(_body(CIRCLING, [_session()]))["plan"]["_fallback"])

    for sid, (pat, sup) in KB_INDEX.items():
        t = {"if": fill(sup["if"], "write the Q3 summary report", "Q3 summary"),
             "then": fill(sup["then"], "write the Q3 summary report", "Q3 summary"),
             "why": sup["mechanism"]}
        ctx = " ".join([t["if"], t["then"], "write the Q3 summary report Q3 summary"])
        good, ch = validate_plan(t, ctx)
        check(f"template passes its own guard: {pat}/{sid}", good, _reason(ch))

    out = run_friction(_body(CIRCLING + RECHECK + SPREAD, [_session(latency=1200)]))
    shown = json.dumps([p["latest"] for p in out["patterns"]])
    tally = re.findall(r"\b\d+\s*(?:times|minutes?|mins?|seconds?|s)\b", shown)
    check("panel text uses clock times, never counts or durations", not tally, tally[:3])
    check("each pattern carries last_seen for the panel",
          all(re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}:\d{2} [ap]m", p["last_seen"])
              for p in out["patterns"]), [p.get("last_seen") for p in out["patterns"]])
    long_goal = "finish the literature review section of my thesis on federated learning fairness"
    t = fill(KB["heavy"][0]["if"], long_goal, None)
    check("a long goal can't push a plan past 20 words",
          len(t.split()) < 20 and "this work" in t, t)
    if _plan_rules is not None:
        for bad, word in [({**GOOD, "then": "Focus on the one question I'm stuck on"}, "focus"),
                          ({**GOOD, "then": "Try to write the question at the top"}, "try"),
                          ({**GOOD, "why": "It stops you procrastinating on the hard part."},
                           "procrastinat")]:
            ok, ch = validate_plan(bad, "Q3 summary")
            check(f"project's shared rules reject '{word}'", not ok, _reason(ch))
        ok, ch = validate_plan({**GOOD, "then": "Open the spreadsheet and write the question"},
                               "Q3 summary")
        check("project's grounding rejects an artifact they never mentioned",
              not ok and "artifact" in _reason(ch), _reason(ch))
    else:
        print("  ----  project rules not on the path: shared-rule checks skipped")

    print()
    print("=" * 74)
    print("SELECTION -- the existing gate, per pattern")
    print("=" * 74)
    arms = [x["id"] for x in KB["circling"]]
    out = run_friction(_body(CIRCLING, [_session()], vetoes={"circling": [arms[0]]}))
    check("veto -> the other support, no experiment",
          out["plan"]["support"] == arms[1] and out["plan"]["selection"]["mode"] == "only_option")
    out = run_friction(_body(CIRCLING, [_session()], vetoes={"circling": arms}))
    check("both vetoed -> nothing forced on them",
          out["plan"] is None and out["status"] == "all_set_aside")

    evs, sess, trials = _circling_trials(better=arms[1], worse=arms[0])
    out = run_friction(_body(evs + CIRCLING, sess + [_session()], trials=trials,
                             now="2026-09-19T12:00:00Z", days=30))
    sel = out["plan"]["selection"]
    check("8 v 8, clearly separated -> finding, winner used",
          sel["mode"] == "finding" and out["plan"]["support"] == arms[1], json.dumps(sel)[:160])
    check("experiments block reports the finding",
          out["experiments"]["circling"]["status"] == "finding")
    few = trials[:6]
    out = run_friction(_body(evs + CIRCLING, sess + [_session()], trials=few, days=30))
    check("6 plans -> still learning, never a winner",
          out["plan"]["selection"]["mode"] == "experiment"
          and out["experiments"]["circling"]["status"] == "still_learning")
    a1 = run_friction(_body(CIRCLING, [_session()], trials=few))["plan"]["support"]
    a2 = run_friction(_body(CIRCLING, [_session()], trials=few))["plan"]["support"]
    check("assignment is deterministic", a1 == a2)

    stored = [{**t, "value": 0 if t["support"] == arms[1] else 1} for t in trials]
    out = run_friction(_body(CIRCLING, [_session()], trials=stored, days=30))
    check("stored outcomes decide it after the raw tabs are gone",
          out["plan"]["selection"]["mode"] == "finding" and out["plan"]["support"] == arms[1])
    out = run_friction(_body(evs + CIRCLING, sess + [_session()], trials=trials, days=30))
    tv = {(v["session_started_at"], v["value"]) for v in out["trial_values"]}
    check("outcomes measured tonight come back to be stored",
          len(out["trial_values"]) == 16 and (trials[0]["session_started_at"], 1) in tv, len(tv))
    out = run_friction(_body(evs + CIRCLING, sess + [_session()], trials=stored, days=30))
    check("stored outcomes aren't measured twice", out["trial_values"] == [])

    plan = run_friction(_body(CIRCLING, [_session()]))["plan"]
    check("generic version for a session on different work",
          plan["generic"]["if"] == "I'm back on the same page for the third time in a few minutes"
          and plan["goal"] == "write the Q3 summary report")
    check("'10' allowed when the template says 'ten'",
          validate_plan({"if": "I sit down to write the report and it feels heavy",
                         "then": "Shrink it to a version I could finish in 10 minutes",
                         "why": "Starting smaller lowers the cost of starting."},
                        "shrink it to a version I could finish in ten minutes write the report")[0])

    print()
    print("=" * 74)
    print("FEELS HEAVY -- user-stated only")
    print("=" * 74)
    m = FakeModel([{"if": "I sit down to write the Q3 summary report and it feels heavy",
                    "then": "Shrink it to a version I could finish in ten minutes",
                    "why": "Starting smaller lowers the cost of starting."}])
    h = run_heavy({"goal": "write the Q3 summary report", "user_id": "demo"}, m)
    check("heavy -> one plan + care line, one call",
          h["status"] == "plan" and "14416" in h["care_line"] and m.calls == 1)
    check("heavy plan names the user's word, nothing more",
          "heavy" in h["plan"]["if"] and h["plan"]["pattern"] == "heavy")
    m = FakeModel([{"if": "I feel anxious about the report", "then": "breathe",
                    "why": "Anxiety drops."}] * 2)
    h = run_heavy({"goal": "write the Q3 summary report"}, m)
    check("model says 'anxious' -> rejected, template used",
          h["plan"].get("_fallback") and "anxi" not in json.dumps(h["plan"]).lower())
    ht = [{"pattern": "heavy", "support": s["id"], "session_started_at": T0 - k * 86400000,
           "value": k} for k, s in enumerate(KB["heavy"] * 8)]
    h = run_heavy({"goal": "write the Q3 summary report", "trials": ht})
    check("heavy rotates and never claims a result",
          h["plan"]["selection"]["mode"] == "rotation"
          and "heavy" not in run_friction(_body(CIRCLING, [_session()], trials=ht))["experiments"])
    check("friction never emits 'heavy' on its own",
          all(p["id"] != "heavy" for p in
              run_friction(_body(CIRCLING + SPREAD, [_session()]))["patterns"]))

    print()
    print(f"  {'ALL CHECKS PASS' if ok_all else '*** FAILURES ABOVE ***'}")
    print("=" * 74)
    return ok_all


if __name__ == "__main__":
    raise SystemExit(0 if run_checks() else 1)
