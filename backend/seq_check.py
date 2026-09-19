"""
Proposed addition to decompose_probe.py's validate().

Catches the to-do-list-in-if-then-clothing failure: a plan whose "if" is
just the previous "then" having completed. Structural, not a wordlist --
it compares each trigger against the action that precedes it.

Gollwitzer's effect comes from the trigger being a situation you ENCOUNTER.
"if I finish step 2" is not a situation, it is step 3.
"""
import re

STOP = {
    "the", "a", "an", "i", "my", "is", "are", "was", "has", "have", "had",
    "to", "in", "on", "at", "of", "and", "or", "it", "that", "this", "with",
    "for", "from", "into", "up", "out", "then", "if", "be", "been", "am",
    "do", "does", "did", "get", "gets", "got", "one", "first", "next",
}

# Trigger openers that describe finishing the prior act rather than hitting
# a situation. Kept small and specific to avoid false positives.
COMPLETION_OPENERS = (
    "i finish", "i finished", "i have finished", "i complete", "i completed",
    "i have completed", "i have typed", "i typed", "i have written",
    "i have added", "i have opened", "i have entered", "i am done",
)


def _content(s):
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in STOP}


# When the action is an INVESTIGATION, the trigger naturally restates its
# subject, because the trigger is the RESULT of looking.
#   action:  "Check if October data is in another tab"
#   trigger: "if October data is in a different file"
# That is 75% word overlap and a perfectly good contingency -- it names an
# outcome you will encounter, not the step finishing. Flagging it was a
# false positive on real output.
INVESTIGATION_VERBS = (
    "check", "search", "look", "find", "see", "run", "test", "read",
    "open", "scroll", "inspect", "review", "verify",
)


def check_sequential(first_action, plans, overlap_threshold=0.6):
    """Returns list of (plan_index, detail) for triggers that are sequential."""
    problems = []
    prior = [first_action] + [p["then"] for p in plans[:-1]]

    for i, p in enumerate(plans, 1):
        cond = p["if"].strip()
        cl = cond.lower()

        if cl.startswith(COMPLETION_OPENERS):
            problems.append((i, f"trigger is the prior step completing: {cond[:44]!r}"))
            continue

        prev = prior[i - 1]

        # An investigation's result legitimately shares its subject.
        prev_verb = re.sub(r"[^a-z]", "", prev.split()[0].lower()) if prev.split() else ""
        if prev_verb in INVESTIGATION_VERBS:
            continue

        cw, pw = _content(cond), _content(prev)
        if not cw:
            continue
        overlap = len(cw & pw) / len(cw)
        if overlap >= overlap_threshold:
            problems.append((
                i,
                f"trigger restates the previous action "
                f"({int(overlap * 100)}% shared): {cond[:34]!r} <- {prev[:34]!r}"
            ))
    return problems


if __name__ == "__main__":
    BAD = ("Open Gmail and click Compose", [
        {"if": "the blank email opens", "then": "type Professor Das in the To field"},
        {"if": "the To field has Professor Das", "then": "type Missing assignment deadline in the subject line"},
        {"if": "the subject line is filled", "then": "write Dear Professor Das in the body"},
        {"if": "I have been stuck for two minutes", "then": "send what I have written and close the email"},
    ])
    BAD2 = ("Open the Probability notes PDF", [
        {"if": "I see the chapter on random variables", "then": "read the first paragraph out loud"},
        {"if": "I finish reading the first paragraph", "then": "write down one key definition in my own words"},
        {"if": "I get stuck on a concept", "then": "draw a simple example with dice or coins"},
    ])
    GOOD = ("Open auth.py and scroll to the decode call", [
        {"if": "the decode call looks correct but still fails", "then": "copy the error message into a new comment"},
        {"if": "I have been staring at the same lines for three minutes", "then": "run the test that uses this decode call"},
        {"if": "the test passes but the demo scenario still fails", "then": "open the demo script and check its auth setup"},
    ])
    GOOD2 = ("Open test_auth.py", [
        {"if": "the test file opens without errors", "then": "scroll to the first failing test"},
        {"if": "I see a test with an error message", "then": "copy the error message to a scratch file"},
        {"if": "I have been looking at the error for two minutes", "then": "run just that single test to see the output"},
    ])

    for name, (fa, plans) in [("BAD  aversive rerun", BAD), ("BAD  huge", BAD2),
                              ("GOOD concrete", GOOD), ("GOOD starved rerun", GOOD2)]:
        probs = check_sequential(fa, plans)
        print(f"\n{name}: {len(probs)} sequential trigger(s)")
        for i, d in probs:
            print(f"   plan {i}: {d}")
