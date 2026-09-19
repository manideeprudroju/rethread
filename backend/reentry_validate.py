import re

try:
    # Production ships decompose_guardrail. The throwaway probe is not
    # guaranteed to be in the Lambda zip, and a failed import here takes the
    # whole function down, not just this path.
    from decompose_guardrail import BANNED_PHRASES, PHYSICAL_VERBS, VAGUE_STARTS
except ImportError:  # older layouts that only have the probe
    from decompose_probe import BANNED_PHRASES, PHYSICAL_VERBS, VAGUE_STARTS

# The prompt says: never mention the drift, the lost time, or their focus.
# Never diagnose, score, or characterise the person.
DRIFT_WORDS = [
    "drift", "distract", "off-task", "off task", "sidetrack", "derail",
    "wasted", "lost time", "time lost", "unproductive", "procrastinat",
    "entertainment", "scrolling", "doomscroll", "focus", "attention span",
]

CHARACTERISING = [
    "failed attempt", "failure to", "struggling to", "you struggled",
    "unable to focus", "gave up", "abandoned", "lack of", "poor ",
    "did not actually", "no actual work", "instead of working",
    "rather than working", "got distracted", "fell into",
]


# The prompt: "Never use knowledge about the topics in the titles - your only
# evidence is the timeline itself" and "If you cannot point at the tabs that
# support a statement, do not make it."
#
# Domain-knowledge leakage is hard to detect directly, but it arrives hedged.
# A claim that traces to a tab does not need "suggesting" or "probably" --
# the timeline either shows it or does not. Caught live: "the PyJWT docs show
# verify_exp controls expiration checking, suggesting the code might not be
# handling expiry" -- asserting page CONTENT from a tab TITLE.
SPECULATIVE = [
    "suggesting", "suggests that", "which means", "probably", "likely that",
    "might be", "might not", "may not be", "could be that", "appears to be",
    "presumably", "implies", "indicating that", "seems to be",
]


def validate_reentry(out, payload=None):
    """Returns (passed, checks). checks = [(rule, ok, detail)].

    `payload` is the timeline dict, used to confirm key_tabs trace to real
    events rather than being invented.
    """
    checks = []

    def add(rule, ok, detail=""):
        checks.append((rule, bool(ok), detail))

    if not isinstance(out, dict):
        add("output is an object", False, type(out).__name__)
        return False, checks

    found = out.get("session_found")
    add("session_found is a bool", isinstance(found, bool), repr(found)[:20])

    evidence = out.get("evidence") or ""
    add("evidence is a non-empty string", isinstance(evidence, str) and evidence.strip())

    # Every user-visible string, checked together.
    parts = [evidence]
    for k in ("doing", "why", "next_action"):
        v = out.get(k)
        if isinstance(v, str):
            parts.append(v)
    for t in (out.get("key_tabs") or []):
        if isinstance(t, str):
            parts.append(t)
    blob = " | ".join(parts).lower()

    hits = [w for w in DRIFT_WORDS if w in blob]
    add("never mentions drift, lost time, or focus", not hits, ", ".join(hits[:3]))

    hits = [w for w in CHARACTERISING if w in blob]
    add("never characterises the person", not hits, ", ".join(hits[:3]))

    hits = [b for b in BANNED_PHRASES if b in blob]
    add("no motivational or shame language", not hits, ", ".join(hits[:3]))

    hits = [w for w in SPECULATIVE if w in blob]
    add("no speculation beyond the timeline", not hits, ", ".join(hits[:3]))

    if found is False:
        for k in ("doing", "why", "key_tabs", "next_action"):
            add(f"{k} is null when no session found", out.get(k) is None, repr(out.get(k))[:30])
        return all(ok for _, ok, _ in checks), checks

    if found is not True:
        return all(ok for _, ok, _ in checks), checks

    # ---- session_found is True ----
    for k in ("doing", "why", "next_action"):
        v = out.get(k)
        add(f"{k} is a non-empty string", isinstance(v, str) and v.strip())

    tabs = out.get("key_tabs")
    add("key_tabs is a list", isinstance(tabs, list), type(tabs).__name__)
    if isinstance(tabs, list):
        add("2-4 key_tabs", 2 <= len(tabs) <= 4, f"got {len(tabs)}")

        # Each key_tab must trace to a real event. The prompt says every
        # claim must trace to specific tabs; this makes that checkable.
        if payload and payload.get("events"):
            corpus = " ".join(
                (e.get("title", "") + " " + e.get("domain", "")).lower()
                for e in payload["events"]
            )
            corpus = re.sub(r"\s+", " ", corpus)
            unmatched = []
            for t in tabs:
                if not isinstance(t, str):
                    continue
                words = [w for w in re.findall(r"[a-z0-9.]+", t.lower()) if len(w) > 3]
                if words and not any(w in corpus for w in words):
                    unmatched.append(t)
            add("every key_tab traces to a real event", not unmatched,
                ", ".join(unmatched[:2]))

    na = out.get("next_action")
    if isinstance(na, str) and na.strip():
        first = re.sub(r"[^a-z]", "", na.split()[0].lower())
        add("next_action starts with a physical verb", first in PHYSICAL_VERBS,
            f"'{first}' not in allowlist" if first not in PHYSICAL_VERBS else "")
        add("next_action is not a vague verb", first not in VAGUE_STARTS,
            first if first in VAGUE_STARTS else "")
        add("next_action under 20 words", len(na.split()) < 20, f"{len(na.split())} words")

    return all(ok for _, ok, _ in checks), checks


def report_reentry(out, payload=None):
    passed, checks = validate_reentry(out, payload)
    for rule, ok, detail in checks:
        if not ok:
            print(f"  FAIL {rule}" + (f"  ({detail})" if detail else ""))
    n = sum(1 for _, ok, _ in checks if not ok)
    print(f"  {'ALL CHECKS PASS' if passed else str(n) + ' CHECK(S) FAILED'}"
          f"   [{len(checks)} rules]")
    return passed


if __name__ == "__main__":
    print("--- the output from tonight's real run ---")
    bad = {
        "session_found": False,
        "evidence": "No work session thread found. Missing: returning to same "
                    "resource for work, tool/reference movement. Evidence shows: "
                    "multiple AWS sign-in attempts but no Bedrock work, extended "
                    "YouTube watching (drift), generic Claude.ai chats.",
        "doing": None, "why": None, "key_tabs": None, "next_action": None,
    }
    report_reentry(bad)

    print("\n--- the built-in work sample's output ---")
    good = {
        "session_found": True,
        "evidence": "Returned to auth.py twice, moved between editor and JWT "
                    "references, error-shaped Stack Overflow title.",
        "doing": "Debugging a JWT token refresh that fails on 401",
        "why": "Token expiry is not triggering a refresh",
        "key_tabs": ["auth.py - rethread - VS Code",
                     "Token expiry not refreshing on 401 - Stack Overflow",
                     "PyJWT - decode options verify_exp"],
        "next_action": "Open auth.py and check the decode call",
    }
    from reentry_probe import SAMPLE
    report_reentry(good, SAMPLE)
