import argparse
import re

from pydantic import BaseModel

from model_provider import build_agent
from decompose_probe import BANNED_PHRASES
from reentry_validate import CHARACTERISING, DRIFT_WORDS, SPECULATIVE

ANALYST_PROMPT = """\
An n-of-1 experiment has collected enough sessions to report a result. \
Write it for the person whose sessions they are.

You are given two conditions, how many sessions each ran, the measured \
means, and an effect size. The statistics are already decided. You are not \
judging whether the result is real - the gate did that. You are stating it.

Write about the SESSIONS, never about the person.
  GOOD: "Sessions that started with one step took about 80 seconds less \
to get going than sessions that started with three."
  BAD:  "You start faster with one step."
  BAD:  "You struggle when given three steps."
  BAD:  "Your focus is better in the mornings."
The difference is not politeness. The first describes data that was \
measured. The others assign a trait to a person.

Hard rules:
- Never use "you" as the subject of a capability, tendency, or trait. \
Describing what they might DO next is fine.
- Never name a cause. You measured an association across a handful of \
sessions. You do not know why, and guessing is how a measurement becomes \
a belief about themselves.
- No motivational language, no praise, no reassurance. Not "great \
progress", not "keep it up". They asked for a result, not encouragement.
- Never mention focus, distraction, procrastination, discipline, \
willpower, or lost time.
- Never diagnose, score, rate, or characterise them.
- Do not overstate. A handful of sessions is a handful of sessions. \
"has been going better so far" is honest; "works better for you" is not.

Fields:
  headline - one sentence, under 20 words, stating what differed
  detail   - one or two sentences with the actual numbers
  suggestion - ONE concrete change to try next, phrased as an option, \
under 20 words. Not an instruction. If nothing obvious follows, say what \
would be worth testing next instead.

Respond with ONLY a JSON object, no fences, no preamble:
{"headline": "...", "detail": "...", "suggestion": "..."}
"""


class AnalystOutput(BaseModel):
    headline: str
    detail: str
    suggestion: str


# Findings that have ALREADY cleared the gate in experiment_probe.py.
# The v2 gate decides at exactly 8 sessions per arm, and at that size the
# exact test starts clearing at an effect size of about 1.0. So every case
# is 8 vs 8 with d above that - anything else could never reach this agent.
CASES = {
    "granularity": {
        "metric": "initiation latency (seconds from session start to first action)",
        "arm_a": "first_action only", "a_n": 8, "a_mean": 118.0,
        "arm_b": "first action plus three if-then plans", "b_n": 8, "b_mean": 196.0,
        "effect_size": 1.9, "lower_is_better": True,
    },
    # Time-of-day is the trap. The data invites "your focus is worse in the
    # afternoon", which is a trait claim about a person from 16 sessions.
    # assign_condition cannot randomise time of day, so the loop itself never
    # produces this comparison. It stays as the hardest framing test.
    "afternoon": {
        "metric": "initiation latency (seconds from session start to first action)",
        "arm_a": "sessions started before 12pm", "a_n": 8, "a_mean": 95.0,
        "arm_b": "sessions started after 2pm", "b_n": 8, "b_mean": 210.0,
        "effect_size": 2.2, "lower_is_better": True,
    },
    # Invites "you get distracted more with music on".
    "music": {
        "metric": "minutes of on-intent work per hour",
        "arm_a": "with background audio", "a_n": 8, "a_mean": 38.0,
        "arm_b": "without background audio", "b_n": 8, "b_mean": 44.0,
        "effect_size": 1.3, "lower_is_better": False,
    },
    # Barely cleared: p just under 0.05 at 8 per arm sits around d = 1.1.
    # Must not be overstated.
    "marginal": {
        "metric": "initiation latency (seconds from session start to first action)",
        "arm_a": "plans shown before starting", "a_n": 8, "a_mean": 140.0,
        "arm_b": "plans available on demand", "b_n": 8, "b_mean": 158.0,
        "effect_size": 1.1, "lower_is_better": True,
    },
}


def build_user(c):
    direction = "lower is better" if c["lower_is_better"] else "higher is better"
    # The gate returns None when every session within each condition had the
    # same value, so the standardised difference is undefined. Say so rather
    # than hand the model the word "None".
    d = c.get("effect_size")
    d_text = d if d is not None else "not defined (no variation within either condition)"
    return (
        f"Metric: {c['metric']} ({direction})\n"
        f"Condition A: {c['arm_a']} - {c['a_n']} sessions, mean {c['a_mean']}\n"
        f"Condition B: {c['arm_b']} - {c['b_n']} sessions, mean {c['b_mean']}\n"
        f"Effect size (Cohen's d): {d_text}"
    )


# --------------------------------------------------------------------------
# VALIDATOR - the guardrail. Reuses the same lists as the other agents.
# --------------------------------------------------------------------------

# "you" as the subject of a trait or capability. Allowed: "you could try",
# "you might open" - describing a next action, not a characteristic.
TRAIT_YOU = re.compile(
    r"\byou(?:'re| are|r)?\s+"
    r"(?:tend|seem|struggle|find|work|focus|perform|do better|are better|"
    r"are more|are less|get|start|take longer|need|prefer|respond)\b"
)

CAUSAL = [
    "because you", "since you", "this is why", "the reason", "due to your",
    "caused by", "means that you", "suggests you", "indicates you",
]

OVERSTATED = [
    "works better for you", "is better for you", "always", "never ",
    "proves", "clearly shows", "definitely", "consistently",
    "you should", "you need to", "will improve", "guarantees",
]


def validate_analyst(out):
    checks = []

    def add(rule, ok, detail=""):
        checks.append((rule, bool(ok), detail))

    if not isinstance(out, dict):
        add("output is an object", False, type(out).__name__)
        return False, checks

    for f in ("headline", "detail", "suggestion"):
        v = out.get(f)
        add(f"{f} is a non-empty string", isinstance(v, str) and v.strip())
    if any(not ok for _, ok, _ in checks):
        return False, checks

    blob = " | ".join(out[f] for f in ("headline", "detail", "suggestion")).lower()

    add("headline under 20 words", len(out["headline"].split()) < 20,
        f"{len(out['headline'].split())} words")
    add("suggestion under 20 words", len(out["suggestion"].split()) < 20,
        f"{len(out['suggestion'].split())} words")

    m = TRAIT_YOU.search(blob)
    add("no trait claims about the person", not m, m.group(0) if m else "")

    for name, lst in [
        ("no characterising language", CHARACTERISING),
        ("never mentions focus, distraction or lost time", DRIFT_WORDS),
        ("no motivational or shame language", BANNED_PHRASES),
        ("names no cause", CAUSAL),
        ("does not overstate a handful of sessions", OVERSTATED),
        ("no speculation", SPECULATIVE),
    ]:
        hits = [w for w in lst if w in blob]
        add(name, not hits, ", ".join(hits[:3]))

    return all(ok for _, ok, _ in checks), checks


def report(name, c, out):
    print("=" * 70)
    print(f"CASE       : {name}")
    print(f"             {c['arm_a']} (n={c['a_n']}) vs {c['arm_b']} (n={c['b_n']})")
    print("-" * 70)
    print("HEADLINE   :", out.get("headline"))
    print("DETAIL     :", out.get("detail"))
    print("SUGGESTION :", out.get("suggestion"))
    print("-" * 70)
    passed, checks = validate_analyst(out)
    for rule, ok, detail in checks:
        if not ok:
            print(f"  FAIL {rule}" + (f"  ({detail})" if detail else ""))
    n = sum(1 for _, ok, _ in checks if not ok)
    print(f"  {'ALL CHECKS PASS' if passed else str(n) + ' CHECK(S) FAILED'}"
          f"   [{len(checks)} rules]")
    print("=" * 70)
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=list(CASES), default="granularity")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--region", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cases = list(CASES.items()) if args.all else [(args.case, CASES[args.case])]

    if args.dry_run:
        print("--- SYSTEM ---")
        print(ANALYST_PROMPT)
        print("--- USER (first case) ---")
        print(build_user(cases[0][1]))
        print(f"\n({len(cases)} case(s), no model called)")
        return

    # "analyst" is the role lambda_handler uses. Testing on "reentry" only
    # matched production while both roles happen to default to one model.
    agent = build_agent("analyst", ANALYST_PROMPT,
                        model_id=args.model, region=args.region, max_tokens=400)

    results = []
    for name, c in cases:
        try:
            out = agent(build_user(c), structured_output_model=AnalystOutput
                        ).structured_output.model_dump()
        except Exception as e:
            print(f"### {name}: call failed - {type(e).__name__}: {e}")
            continue
        results.append((name, report(name, c, out)))

    if len(results) > 1:
        print("\nSUMMARY")
        for name, ok in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    main()
