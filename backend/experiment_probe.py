import argparse
import hashlib
import itertools
import json
import math
import random
import statistics
from collections import defaultdict


PER_ARM = 8

# A finding needs the exact permutation p-value at or below this.
ALPHA = 0.05

# Old name, kept so nothing that imports it breaks. Decides nothing now.
MIN_PER_ARM = PER_ARM

# Exact enumeration up to this many label arrangements (8 per arm is
# 12,870). Beyond it, a seeded Monte Carlo estimate.
EXACT_LIMIT = 200_000
MC_RESAMPLES = 20_000
MC_SEED = 1729


def assign_condition(session_index, arms, seed="", per_arm=PER_ARM):
    """Which arm this session gets. Deterministic, balanced, shuffled.

    Sessions are grouped into blocks of per_arm * len(arms). Each block holds
    exactly per_arm of every arm, in an order fixed by (seed, block):
      - both arms reach per_arm together at the end of each block, which is
        what v1's alternation was for, and
      - the order has nothing to do with the person's routine. Alternation
        put every morning session in one arm for anyone who works twice a
        day, and the test cannot tell that apart from a real effect.

    session_index counts sessions from the start of THIS experiment. With a
    single experiment that is the user's session count. Pass the user id as
    seed so users do not all share one sequence.

    Hash-driven Fisher-Yates rather than random.shuffle, so the sequence is
    identical on every Python version: local runs and Lambda agree.
    """
    if len(arms) < 2:
        raise ValueError("assign_condition needs at least 2 arms")
    if session_index < 0:
        raise ValueError("session_index must be >= 0")
    block_size = per_arm * len(arms)
    block, pos = divmod(session_index, block_size)
    order = [arm for arm in arms for _ in range(per_arm)]
    for i in range(block_size - 1, 0, -1):
        h = hashlib.sha256(f"{seed}|{block}|{i}".encode()).digest()
        j = int.from_bytes(h[:8], "big") % (i + 1)
        order[i], order[j] = order[j], order[i]
    return order[pos]


def cohens_d(a, b):
    """Standardised difference, pooled SD. DISPLAY ONLY - decides nothing.

    None when it is undefined: both arms constant but different. v1 returned
    0.0 there, which reported 100s-vs-200s as "no difference".
    """
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    pooled = (((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2)) ** 0.5
    diff = statistics.mean(a) - statistics.mean(b)
    if pooled == 0:
        return 0.0 if diff == 0 else None
    return diff / pooled


def permutation_p(a, b):
    """Exact two-sided p-value for the difference in means.

    The share of all C(na+nb, na) relabellings of these sessions whose gap is
    at least as large as the observed one. At 8 per arm that is 12,870
    arrangements: enumerated, no randomness, no scipy.
    """
    pooled = list(a) + list(b)
    na, nb = len(a), len(b)
    total = math.fsum(pooled)
    observed = abs(math.fsum(a) / na - math.fsum(b) / nb)

    # For a relabelling whose first group sums to s, the gap is
    # |s*(1/na + 1/nb) - total/nb|. So "at least as large" is a pair of
    # thresholds on s, and only sums need enumerating.
    scale = 1 / na + 1 / nb
    centre = total / nb / scale
    half = observed / scale
    tol = 1e-9 * max(1.0, abs(total))
    hi, lo = centre + half - tol, centre - half + tol

    arrangements = math.comb(na + nb, na)
    if arrangements <= EXACT_LIMIT:
        hits = 0
        for s in map(sum, itertools.combinations(pooled, na)):
            if s >= hi or s <= lo:
                hits += 1
        return hits / arrangements

    rng = random.Random(MC_SEED)
    hits = 0
    for _ in range(MC_RESAMPLES):
        s = sum(rng.sample(pooled, na))
        if s >= hi or s <= lo:
            hits += 1
    return (hits + 1) / (MC_RESAMPLES + 1)


def _clean(v):
    """A usable measurement, or None.

    v1 accepted NaN and Infinity (Python's json module parses both). They
    fell through every comparison into "finding", and the response then
    carried NaN, which a browser's JSON.parse rejects.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def analyse(sessions, metric="initiation_latency_s", min_per_arm=None,
            lower_is_better=True, arms=None, labels=None):
    """sessions: [{"condition": str, "<metric>": number}, ...], OLDEST FIRST.

    Send sessions with no measurement too (metric missing or null). They are
    counted under "missing" and otherwise ignored.

    Returns a dict the UI can render directly. Never returns a winner before
    the decision point, and never one the test did not support.

    DECISION POINT: both arms have PER_ARM usable measurements. Only the first
    PER_ARM of each arm are used, so sessions recorded afterwards cannot
    reopen or flip a decision.

    counts: before the decision, sessions recorded per arm. After it, the
        sessions the decision used (PER_ARM each); total_counts has everything.
    arms: the experiment's two conditions. Sessions with any other condition
        (an earlier experiment) are ignored. If omitted, the conditions
        present are used, and more than two is an error rather than a guess.
    labels: {condition: human wording} for ui_text. Defaults to the id with
        underscores as spaces.
    min_per_arm: accepted so existing callers keep working, and IGNORED. The
        decision has to fall on one complete assignment block, and letting
        the caller move it would bring back repeated checking.
    """
    del min_per_arm

    values, seen = defaultdict(list), defaultdict(int)
    for s in sessions:
        cond = s.get("condition")
        if not isinstance(cond, str) or not cond:
            continue
        seen[cond] += 1
        v = _clean(s.get(metric))
        if v is not None:
            values[cond].append(v)

    if arms is None:
        arms = list(seen)
        if len(arms) > 2:
            raise ValueError(
                f"sessions contain {len(arms)} conditions ({', '.join(arms)}); "
                "pass arms=[a, b] for the experiment being analysed")
    else:
        arms = list(arms)
        if len(arms) != 2 or arms[0] == arms[1]:
            raise ValueError("analyse compares exactly two different arms")

    def label(k):
        return (labels or {}).get(k, k.replace("_", " "))

    counts = {k: len(values[k]) for k in arms}
    missing = {k: seen[k] - len(values[k]) for k in arms if seen[k] > len(values[k])}
    extra = {"missing": missing} if missing else {}

    if len(arms) < 2 or min(counts.values()) < PER_ARM:
        return {
            "status": "still_learning",
            "reportable": False,
            "counts": counts,
            "per_arm": PER_ARM,
            "needed": {k: PER_ARM - n for k, n in counts.items() if n < PER_ARM},
            # No means here on purpose: nothing for the UI to render as a
            # "leader" before the decision.
            "ui_text": ("Still learning" if len(arms) < 2 else
                        f"Still learning, {min(counts.values())} of {PER_ARM} sessions"),
            "finding": None,
            **extra,
        }

    used = {k: values[k][:PER_ARM] for k in arms}
    a, b = arms
    p = permutation_p(used[a], used[b])
    d = cohens_d(used[a], used[b])
    decided = {
        "reportable": True,
        "counts": {k: PER_ARM for k in arms},
        "total_counts": counts,
        "per_arm": PER_ARM,
        "p_value": round(p, 4),
        "effect_size": None if d is None else round(abs(d), 2),
        **extra,
    }

    if p > ALPHA:
        return {
            "status": "no_difference",
            **decided,
            "ui_text": f"No clear difference between these two after {PER_ARM} sessions each",
            "finding": None,
        }

    mean = {k: statistics.mean(v) for k, v in used.items()}
    best, other = sorted(arms, key=mean.get, reverse=not lower_is_better)
    return {
        "status": "finding",
        **decided,
        "means": {k: round(m, 1) for k, m in mean.items()},
        "ui_text": f"{label(best)} has been going better than {label(other)}",
        "finding": {"better_arm": best, "compared_to": other, "metric": metric,
                    "effect_size": decided["effect_size"],
                    "p_value": decided["p_value"], "sessions_per_arm": PER_ARM},
    }


# --------------------------------------------------------------------------
# TESTS
# --------------------------------------------------------------------------

def _sessions(spec, metric="initiation_latency_s"):
    return [{"condition": c, metric: v} for c, vals in spec.items() for v in vals]


SEPARATED = {
    "one_step":    [120, 130, 110, 125, 118, 122, 115, 128],
    "three_steps": [200, 190, 210, 195, 205, 198, 188, 207],
}
OVERLAP = {
    "one_step":    [150, 160, 140, 155, 148, 152, 145, 158],
    "three_steps": [152, 158, 145, 151, 149, 156, 143, 150],
}
# Looks like a result to v1 (d about 0.6), but the arms overlap throughout.
NOISY = {
    "one_step":    [150, 162, 139, 171, 144, 158, 133, 167],
    "three_steps": [161, 170, 148, 182, 155, 176, 140, 169],
}


def _expect_error(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def _json_strict(obj):
    def refuse(c):
        raise ValueError(c)
    json.loads(json.dumps(obj), parse_constant=refuse)
    return True


CASES = [
    ("one session only",
     lambda: analyse(_sessions({"one_step": [120]})),
     lambda r: r["status"] == "still_learning"),
    ("one arm only, plenty of data",
     lambda: analyse(_sessions({"one_step": SEPARATED["one_step"]})),
     lambda r: r["status"] == "still_learning"),
    ("8 and 7 - one short",
     lambda: analyse(_sessions({"one_step": SEPARATED["one_step"],
                                "three_steps": SEPARATED["three_steps"][:7]})),
     lambda r: r["status"] == "still_learning" and r["needed"] == {"three_steps": 1}),
    ("5 and 5, perfectly separated - v1 reported this, v2 waits",
     lambda: analyse(_sessions({k: v[:5] for k, v in SEPARATED.items()})),
     lambda r: r["status"] == "still_learning" and "means" not in r),
    ("8 and 8, clearly separated",
     lambda: analyse(_sessions(SEPARATED)),
     lambda r: r["status"] == "finding" and r["finding"]["better_arm"] == "one_step"
               and r["ui_text"] == "one step has been going better than three steps"),
    ("same data, higher is better - winner flips",
     lambda: analyse(_sessions(SEPARATED), lower_is_better=False),
     lambda r: r["status"] == "finding" and r["finding"]["better_arm"] == "three_steps"),
    ("8 and 8, overlapping",
     lambda: analyse(_sessions(OVERLAP)),
     lambda r: r["status"] == "no_difference" and r["reportable"]),
    ("8 and 8, noisy - v1 called this a finding",
     lambda: analyse(_sessions(NOISY)),
     lambda r: r["status"] == "no_difference"),
    ("a decision stays made when later sessions disagree",
     lambda: analyse(_sessions({"one_step": SEPARATED["one_step"] + [300, 310, 305],
                                "three_steps": SEPARATED["three_steps"] + [90, 95, 85]})),
     lambda r: r["status"] == "finding" and r["total_counts"]["one_step"] == 11),
    ("NaN, Infinity and booleans are ignored, not read as a finding",
     lambda: analyse(_sessions({"one_step": [120, 130, float("nan"), 110, 125],
                                "three_steps": [121, float("inf"), True, 131, 111]})
                     + [{"condition": "one_step"}]),
     lambda r: r["status"] == "still_learning"
               and r["missing"] == {"one_step": 2, "three_steps": 2} and _json_strict(r)),
    ("both arms constant, clearly different - v1 said no difference",
     lambda: analyse(_sessions({"one_step": [100] * 8, "three_steps": [200] * 8})),
     lambda r: r["status"] == "finding" and r["effect_size"] is None and _json_strict(r)),
    ("labels reach ui_text",
     lambda: analyse(_sessions(SEPARATED),
                     labels={"one_step": "Just the first step",
                             "three_steps": "First step plus plans"}),
     lambda r: r["ui_text"] == "Just the first step has been going better than First step plus plans"),
    ("three conditions and no arms - refuses to guess",
     lambda: _expect_error(lambda: analyse(_sessions({**SEPARATED, "old_arm": [1] * 8}))),
     lambda r: r is True),
    ("three conditions with arms given - stale arm ignored",
     lambda: analyse(_sessions({**SEPARATED, "old_arm": [1] * 8}),
                     arms=["one_step", "three_steps"]),
     lambda r: r["status"] == "finding" and "old_arm" not in r["counts"]),
    ("min_per_arm from the caller cannot lower the bar",
     lambda: analyse(_sessions({k: v[:3] for k, v in SEPARATED.items()}), min_per_arm=3),
     lambda r: r["status"] == "still_learning"),
]


def _v1_fires(a, b):
    """v1's rule, for comparison only."""
    d = cohens_d(a[:5], b[:5])
    return d is None or abs(d) >= 0.5


def _run_experiment(rng, seed, arms, routine_s, effect_s, sd, alternate=False):
    sessions = []
    for i in range(PER_ARM * 2):
        cond = arms[i % 2] if alternate else assign_condition(i, arms, seed=seed)
        v = rng.gauss(120, sd) + (routine_s if i % 2 else 0)
        if cond == arms[1]:
            v += effect_s
        sessions.append({"condition": cond, "initiation_latency_s": v})
    return sessions


def simulate(n, rng_seed=12345):
    rng = random.Random(rng_seed)
    arms = ["one_step", "three_steps"]
    rows = []

    v2 = v1 = 0
    for i in range(n):
        s = _run_experiment(rng, f"sim{i}", arms, 0, 0, 30)
        v2 += analyse(s)["status"] == "finding"
        per = defaultdict(list)
        for x in s:
            per[x["condition"]].append(x["initiation_latency_s"])
        v1 += _v1_fires(per[arms[0]], per[arms[1]])
    rows.append(("no real difference", "v1 rule (5 each, d >= 0.5)", v1 / n, None))
    rows.append(("no real difference", "v2 gate", v2 / n, 0.08))

    for alternate, name in [(True, "alternation + v2 test"), (False, "v2 blocks + v2 test")]:
        hits = 0
        for i in range(n):
            s = _run_experiment(rng, f"r{i}", arms, 60, 0, 20, alternate=alternate)
            hits += analyse(s)["status"] == "finding"
        rows.append(("two-a-day routine, +60s evenings", name, hits / n,
                     None if alternate else 0.08))
    return rows


def power_table(n, rng_seed=99):
    rng = random.Random(rng_seed)
    arms = ["one_step", "three_steps"]
    out = []
    for d in (0.5, 0.8, 1.0, 1.5, 2.0):
        hits = sum(analyse(_run_experiment(rng, f"p{d}-{i}", arms, 0, -d * 30, 30))
                   ["status"] == "finding" for i in range(n))
        out.append((d, hits / n))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=400,
                    help="simulated experiments per scenario (0 to skip)")
    ap.add_argument("--power", action="store_true",
                    help="also print how often real effects are caught")
    args = ap.parse_args()
    all_ok = True

    print("=" * 74)
    print(f"ASSIGNMENT - shuffled blocks of {PER_ARM * 2}, {PER_ARM} of each arm")
    print("=" * 74)
    arms = ["one_step", "three_steps"]
    seq = [assign_condition(i, arms, seed="demo") for i in range(PER_ARM * 4)]
    print("  first 16:", " ".join("A" if s == arms[0] else "B" for s in seq[:16]))
    checks = [
        ("every block holds exactly 8 of each",
         all(seq[k:k + 16].count(arms[0]) == PER_ARM for k in range(0, len(seq), 16))),
        ("same inputs, same answer",
         seq == [assign_condition(i, arms, seed="demo") for i in range(len(seq))]),
        ("different seed, different order",
         seq != [assign_condition(i, arms, seed="other") for i in range(len(seq))]),
        ("fewer than 2 arms refused",
         _expect_error(lambda: assign_condition(0, ["one_step"]))),
    ]
    mornings = [sum(1 for i in range(0, 16, 2)
                    if assign_condition(i, arms, seed=f"u{u}") == arms[0])
                for u in range(300)]
    spread = statistics.mean(mornings)
    checks.append(("mornings split evenly on average (alternation: always 8 vs 0)",
                   3.5 <= spread <= 4.5))
    for name, ok in checks:
        all_ok &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"        mornings in arm A across 300 users: mean {spread:.2f} of 8")

    print()
    print("=" * 74)
    print(f"GATE - decides once, at {PER_ARM} per arm, exact test at p <= {ALPHA}")
    print("=" * 74)
    for name, run, check in CASES:
        try:
            r = run()
            ok = bool(check(r))
        except Exception as e:
            r, ok = f"{type(e).__name__}: {e}", False
        all_ok &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if isinstance(r, dict):
            p = f"  p={r['p_value']}" if "p_value" in r else ""
            print(f"          status={r['status']:<15}{p}")
            print(f"          UI: \"{r['ui_text']}\"")

    if args.sims:
        print()
        print("=" * 74)
        print(f"SIMULATION - {args.sims} experiments per row, no real arm effect")
        print("=" * 74)
        for scenario, rule, rate, ceiling in simulate(args.sims):
            ok = ceiling is None or rate <= ceiling
            all_ok &= ok
            tag = "    " if ceiling is None else ("PASS" if ok else "FAIL")
            print(f"  {tag}  {scenario:<34} {rule:<27} false findings {rate:6.1%}")

    if args.power:
        print()
        print("=" * 74)
        print(f"POWER - real effects caught at {PER_ARM} per arm")
        print("=" * 74)
        for d, rate in power_table(max(args.sims, 400)):
            print(f"        true d = {d:<4}  caught {rate:5.0%}")

    print()
    print(f"  {'ALL CHECKS PASS' if all_ok else '*** FAILURES ABOVE ***'}")
    print("=" * 74)
    print("The number that matters: false findings when nothing is different.")
    print("Every one of those is a claim the user cannot check and will act on.")
    print("=" * 74)
    return all_ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
