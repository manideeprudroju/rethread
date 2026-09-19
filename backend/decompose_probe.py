import argparse
import json
import os
import re
import sys


def load_env(path=".env"):
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v


load_env()

# --------------------------------------------------------------------------
# Grounding:
#   Barkley   - externalise at the point of performance; never exhort.
#   Gollwitzer- contingent "if X then Y" format carries the effect size.
#   Gross     - reappraisal, never suppression. No shame, no cheerleading.
#   ADHD      - aversion usually loads onto ONE sub-step; find it and shrink it.
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You help someone with ADHD start a task they are struggling to begin.

You are NOT making a to-do list. A to-do list is what they already have and \
cannot act on. You are producing two things:

1. first_action - ONE immediately executable physical action that requires \
NO decision. They should be able to do it the second they read it, without \
choosing between options or working anything out first. Keep it to a few \
seconds of effort.
   GOOD: "Open auth.py and scroll to the decode call"
   GOOD: "Open a blank doc and type the email's subject line"
   BAD:  "Review the authentication code"   (not physical)
   BAD:  "Start writing the report"         (that IS the thing they can't do)
   BAD:  "Gather your materials and begin"  (two acts, and vague)
   The test: could a stranger watch them and say whether they did it?

2. plans - 2 to 4 contingent if-then plans, each in the form \
"if <situation>, then <action>".
   The "if" must be a SITUATION OR TRIGGER they will actually encounter, \
not an action they choose to take.
   GOOD: {"if": "the tests still fail after the fix", "then": "paste the \
traceback into the scratchpad and move on to the next call site"}
   GOOD: {"if": "I have been stuck for five minutes", "then": "write down \
the exact error and open the docs page for it"}
   BAD:  {"if": "I want to start", ...}   (not a real trigger)
   BAD:  {"if": "I finish", "then": "feel proud"}   (not an action)
   BAD:  {"if": "something happens", "then": "continue working"}   (empty \
both sides - names no situation and no act)
   Each "if" and each "then" must be at least three words and name something \
concrete. "Continue", "keep going", "focus", "make sure" are not actions.
   Cover the likely sticking points, especially the moment the task turns \
aversive.

If the goal is large or vague, do NOT produce a syllabus or a project plan. \
Pick the single nearest concrete entry point and work from there.

GROUNDING - this one matters most. You have only what they told you. You have \
NOT seen their files, documents, inbox, notebook or screen.

NEVER write any of these unless they appear in what they told you:
  - a line, page, section, chapter, question or cell number
  - a function, class, variable or method name
  - a filename, heading, sheet name or tab name
  - a person's name, or the name of anything inside their document
  - AN ARTIFACT THEY DID NOT MENTION: "the PDF", "the spreadsheet", "your \
notes", "the doc". Naming a document that does not exist is the same \
failure as inventing a line number, and harder to spot.
"Open auth.py and scroll to the decode call" is allowed ONLY if they said \
"decode call". "scroll to the refresh_token function" is FORBIDDEN unless \
they named that function. Otherwise write "Open auth.py" and stop there.

A made-up specific is worse than a general one. They will act on it, find \
nothing, and lose the session.

WHEN THEY TOLD YOU LITTLE: stay general and stay honest. A short or skipped \
answer means they are out of patience, not that you should fill the gap. \
"Open auth.py" with general plans is a CORRECT answer to a thin brief. Do \
not manufacture detail to look helpful.

IF THEY NAMED NO FILE, DOC OR APP AT ALL: name none. Write the act without \
the object - "Open whatever you are working in and type one word" - and \
give general plans. "Study for the exam" tells you there is studying, not \
that there is a PDF. Every turn after an invented document is spent \
arguing about a document nobody has.

USE WHAT THEY DID SAY. If they said the work is in a notebook, do not tell \
them to run a test suite or edit a file. Match the plans to the environment \
they named.

Write in the first person, as their own voice: "if I get stuck", not "if you \
get stuck".

Hard rules:
- No motivational language. No "you've got this", "don't worry", "take your \
time", "remember to", "try to". They already know. Exhortation is the \
intervention that does not work.
- Never mention focus, distraction, discipline, procrastination, or their \
past performance.
- Never diagnose or characterise the person. Talk about the work only.
- Keep every string under 20 words.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{"first_action": "...", "plans": [{"if": "...", "then": "..."}, ...]}
"""

CLARIFY_PROMPT = """\
Someone with ADHD has told you what they want to work on. Before you help \
them break it down, you may ask for what is genuinely missing.

You may ask AT MOST TWO questions, and only these two kinds:
  1. WHERE the work lives - the file, doc, page, repo, thread, or app.
     Without this you would have to invent details, which is not allowed.
  2. WHEN it is due - only if the answer would change what they do first.

Do NOT ask anything else. Not how they feel, not why it is hard, not how \
long they have, not what has been blocking them, not their goals. Those are \
about the person; you are here about the work.

If the goal already contains an answer, do NOT ask for it again. If it \
contains both, ask nothing and return an empty list. Asking a question they \
already answered wastes the one thing they are short of.

Each question must be under 12 words and answerable in a few words.

Respond with ONLY a JSON object, no fences, no preamble:
{"questions": ["...", "..."]}
"""

# Each case carries a canned answer blob, used in --clarify mode to simulate
# the user replying. Keep them realistic: what a person would actually type.
CASES = {
    "concrete": {
        "goal": "fix the JWT refresh bug in auth.py",
        "answers": "I'm in auth.py in the rethread repo, the decode call near "
                   "the bottom. Needs to work before the demo tomorrow.",
    },
    "aversive": {
        "goal": "email my professor about missing the assignment deadline",
        "answers": "Gmail, nothing drafted yet. Prof Das. Deadline was "
                   "yesterday so I want it sent today.",
    },
    "vague": {
        "goal": "write the report",
        "answers": "A Google Doc called Q3 summary, empty so far. Due Friday.",
    },
    "huge": {
        "goal": "study for GATE DA",
        "answers": "Probability notes PDF, chapter on random variables. "
                   "Exam is in February, no deadline today.",
    },
    "tiny": {
        "goal": "reply to Priya's message",
        "answers": "WhatsApp. She asked about the project split. No deadline.",
    },
    # --- adversarial ---
    # Names a file but nothing inside it. Maximum temptation to invent a
    # test function name. Correct answer stays general.
    "starved": {
        "goal": "fix the failing test in test_auth.py",
        "answers": "dunno",
    },
    # Names a document section vaguely. Tempts invented page/section numbers.
    "tempting": {
        "goal": "update the pricing section in the proposal",
        "answers": "google docs",
    },
}

# --------------------------------------------------------------------------
# VALIDATOR - this is the guardrail. Ship it.
# --------------------------------------------------------------------------

# An action is observable if someone watching the screen could say whether
# it happened. Allowlist beats banlist: a missing verb costs one regeneration,
# a missed vague action costs the user their session.
PHYSICAL_VERBS = {
    "open", "type", "click", "scroll", "run", "paste", "copy", "press",
    "close", "save", "select", "write", "delete", "drag", "search",
    "highlight", "add", "rename", "move", "print", "send", "read",
    "uncomment", "comment", "rerun", "reopen", "navigate", "go",
}

# Kept as a secondary net for actions that sneak past the allowlist.
VAGUE_STARTS = {
    "review", "research", "start", "begin", "continue", "work", "think",
    "plan", "consider", "look", "study", "prepare", "organise", "organize",
    "gather", "figure", "understand", "explore", "get", "make", "ensure",
    "check", "try", "attempt", "identify", "determine", "assess",
}

BANNED_PHRASES = [
    "you've got this", "you can do this", "don't worry", "no worries",
    "take your time", "remember to", "try to", "make sure to", "be sure to",
    "stay focused", "focus on", "avoid distractions", "procrastinat",
    "distract", "willpower", "lazy", "should have", "fall behind",
    "discipline", "motivat", "you should", "it's okay", "it is okay",
    "good luck", "keep going", "stay on track", "you deserve",
]


# Product names that are shaped exactly like camelCase identifiers. Without
# this, "open the iPhone settings" reads as an invented function name.
# A trigger must name a situation you would actually notice. A then-clause
# must name something you do. Denylist rather than allowlist here: then-clause
# vocabulary is wide ("put [DATA] in brackets", "apply that change"), so an
# allowlist would reject real output. These catch the empty ones.
VAGUE_TRIGGERS = [
    "something happens", "something comes up", "it is time", "its time",
    "necessary", "appropriate", "possible", "needed", "i am ready",
    "things go wrong", "there is a problem", "an issue arises", "i need to",
]

VAGUE_ACTIONS = [
    "continue", "keep going", "keep working", "proceed", "carry on",
    "work on", "focus", "remember", "try to", "consider", "think about",
    "ensure", "make sure", "be sure", "stay", "feel", "get started",
    "do the work", "move forward", "push through",
]

PROPER_NOUNS = {
    "iphone", "ipad", "ipod", "imac", "ios", "ipados", "macos",
    "ebay", "itunes", "icloud", "ithink",
}

SEV_SCHEMA, SEV_STRUCTURE, SEV_GROUNDING = "schema", "structure", "grounding"


def validate(out, context="", mode="normal"):
    """Returns (passed, checks). Each check is (rule, ok, detail, severity).

    mode="fallback" validates the deterministic fallback, which deliberately
    carries no plans. It checks only what the fallback must still guarantee:
    valid shape, an observable physical first_action, and nothing invented.
    Judging it against the normal 2-4 plan rule would report a correct
    fallback as a failure.

    Order matters: schema first, and if the shape is wrong the semantic checks
    are skipped - they would only produce noise about a malformed object.

    `context` is everything the model was actually told (goal + any answers).
    Grounding failures are not proof of hallucination, but the asymmetry is
    stark: a false positive costs one regeneration, a false negative sends
    someone hunting for a function that does not exist.
    """
    checks = []

    def add(rule, ok, detail="", sev=SEV_STRUCTURE):
        checks.append((rule, bool(ok), detail, sev))

    # ---------------- SCHEMA ----------------
    if not isinstance(out, dict):
        add("output is an object", False, type(out).__name__, SEV_SCHEMA)
        return False, checks
    add("output is an object", True, "", SEV_SCHEMA)

    fa = out.get("first_action")
    add("first_action is a non-empty string",
        isinstance(fa, str) and fa.strip(), "", SEV_SCHEMA)

    plans = out.get("plans")
    add("plans is a list", isinstance(plans, list), type(plans).__name__, SEV_SCHEMA)

    if isinstance(plans, list):
        for i, p in enumerate(plans, 1):
            if not isinstance(p, dict):
                add(f"plan {i} is an object", False, repr(p)[:30], SEV_SCHEMA)
                continue
            add(f"plan {i} if is a non-empty string",
                isinstance(p.get("if"), str) and p["if"].strip(), "", SEV_SCHEMA)
            add(f"plan {i} then is a non-empty string",
                isinstance(p.get("then"), str) and p["then"].strip(), "", SEV_SCHEMA)

    if any(not ok for _, ok, _, sev in checks if sev == SEV_SCHEMA):
        return False, checks

    fa = fa.strip()
    plans = plans or []

    # ---------------- STRUCTURE ----------------
    first_word = re.sub(r"[^a-z]", "", fa.split()[0].lower())
    add("first_action starts with an observable physical verb",
        first_word in PHYSICAL_VERBS,
        f"'{first_word}' is not in the allowlist" if first_word not in PHYSICAL_VERBS else "")
    add("first_action is not a vague verb", first_word not in VAGUE_STARTS,
        first_word if first_word in VAGUE_STARTS else "")
    add("first_action is one act", " and then " not in fa.lower())
    # Length is a PROXY for "immediately executable, no decision required".
    # We cannot measure seconds; we can measure that it is short and singular.
    add("first_action under 20 words (proxy for immediacy)",
        len(fa.split()) < 20, f"{len(fa.split())} words")
    add("first_action under 200 chars", len(fa) < 200, f"{len(fa)} chars")

    if mode == "normal":
        add("2-4 plans", 2 <= len(plans) <= 4, f"got {len(plans)}")

    for i, p in enumerate(plans, 1):
        cond, act = p["if"].strip(), p["then"].strip()
        cl, al = cond.lower(), act.lower()

        add(f"plan {i} if is a trigger, not a choice",
            not re.match(r"^(i want|i decide|i choose|i feel like)", cl), cond[:40])
        vt = [v for v in VAGUE_TRIGGERS if v in cl]
        add(f"plan {i} if names a noticeable situation", not vt, ", ".join(vt[:2]))
        add(f"plan {i} if is at least 3 words", len(cond.split()) >= 3,
            f"{len(cond.split())} words")

        add(f"plan {i} then is an action",
            not re.match(r"^(feel|remember|be |stay|try)", al), act[:40])
        va = [v for v in VAGUE_ACTIONS
              if al.startswith(v) or f" {v} " in f" {al} "]
        add(f"plan {i} then is not a vague continuation", not va, ", ".join(va[:2]))
        add(f"plan {i} then is at least 3 words", len(act.split()) >= 3,
            f"{len(act.split())} words")

        add(f"plan {i} under 20 words each",
            len(cond.split()) < 20 and len(act.split()) < 20)

    # " | " not " " - a plain space lets the end of one field and the
    # start of the next form a phantom match across the boundary.
    values = " | ".join([fa] + [p["if"] + " | " + p["then"] for p in plans])
    blob = values.lower()

    hits = [b for b in BANNED_PHRASES if b in blob]
    add("no motivational or shame language", not hits, ", ".join(hits[:3]))

    # ---------------- GROUNDING ----------------
    # Normalise whitespace and case on BOTH sides so "line  40" matches
    # "line 40". Still exact-phrase: a genuinely new location is refused.
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    blob = re.sub(r"\s+", " ", blob)

    # Exact-phrase only. A bare number matching somewhere in the context
    # ("40 minutes", "Q4") must NOT license an invented "line 40".
    invented = [
        m_.group(0) for m_ in re.finditer(
            r"\b(line|page|section|chapter|cell|step|paragraph|slide)\s+(\d+|[ivx]{2,})\b",
            blob)
        if m_.group(0) not in ctx
    ]
    add("no invented locations", not invented,
        ", ".join(sorted(set(invented))[:3]), SEV_GROUNDING)

    ident_re = re.compile(
        r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+"       # snake_case
        r"|[a-z]+[A-Z][A-Za-z0-9]*"                 # camelCase
        r"|[A-Za-z_][A-Za-z0-9_]*\(\))"            # foo()
    )
    fake_ids = []
    for m_ in ident_re.finditer(values):
        tok = m_.group(1)
        bare = tok.rstrip("()").lower()
        if bare in PROPER_NOUNS:
            continue
        if bare and bare not in ctx and tok.lower() not in ctx:
            fake_ids.append(tok)
    add("no invented identifiers", not fake_ids,
        ", ".join(sorted(set(fake_ids))[:3]), SEV_GROUNDING)

    return all(ok for _, ok, _, _ in checks), checks


def failure_summary(checks):
    """Compact text the repair turn can act on."""
    return "; ".join(
        rule + (f" ({detail})" if detail else "")
        for rule, ok, detail, _ in checks if not ok
    )


# Detecting a filename-shaped token and CHOOSING something to open are
# opposite jobs. Detection wants to be loose: anything that looks like a
# filename in model output is worth checking against what they said.
# Choosing wants to be strict, because acting on a bad guess sends them
# somewhere real. The loose pattern matched "e.g" out of "e.g." and
# "google.com" out of a URL, and the fallback then said "Open e.g".
REAL_FILE_RE = re.compile(
    r"\b([\w\-]{2,}\.(?:py|ipynb|js|jsx|ts|tsx|java|cpp|cc|c|h|go|rs|rb|php|"
    r"swift|kt|sql|sh|md|txt|rst|tex|csv|tsv|json|ya?ml|toml|ini|cfg|log|"
    r"pdf|docx?|xlsx?|pptx?|odt|ods|rtf|pages|numbers|key|"
    r"png|jpe?g|gif|svg|webp|mp4|mov|wav|mp3|zip|tar|gz))\b", re.I)


def safe_fallback(goal, context):
    """Deterministic, no model. Honest and general beats specific and wrong."""
    m_ = REAL_FILE_RE.search(goal + " " + (context or ""))
    if m_:
        return {"first_action": f"Open {m_.group(1)}", "plans": [], "_fallback": True}
    return {"first_action": "Open whatever you will be working in and type one word",
            "plans": [], "_fallback": True}


# --------------------------------------------------------------------------

def call_bedrock(system, user, model_id, region):
    import boto3
    c = boto3.client("bedrock-runtime", region_name=region)
    r = c.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    return r["output"]["message"]["content"][0]["text"]


def call_deepinfra(system, user, model_id):
    import urllib.request
    key = os.environ.get("DEEPINFRA_API_KEY")
    if not key:
        print("Set DEEPINFRA_API_KEY in your .env first.")
        sys.exit(1)
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 700,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepinfra.com/v1/openai/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def parse_output(raw):
    s = raw.strip()
    # Reasoning models (R1, V4 in reasoning mode) emit a think block first.
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Fallback: grab the outermost JSON object, ignoring prose either side.
    mm = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if mm:
        return json.loads(mm.group(0))
    raise ValueError("no JSON object found in model output")


def _call(system, user, args):
    try:
        if args.provider == "bedrock":
            model = args.model or "us.anthropic.claude-sonnet-4-20250514-v1:0"
            return call_bedrock(system, user, model, args.region)
        model = args.model or "meta-llama/Llama-3.3-70B-Instruct"
        return call_deepinfra(system, user, model)
    except ImportError:
        print("boto3 not installed.  pip install boto3   (or --provider deepinfra)")
        sys.exit(1)
    except Exception as e:
        text = str(e)
        print(f"Model call failed ({type(e).__name__}): {text}")
        if "AccessDenied" in text:
            print("-> model access, not credentials. Enable the model in Bedrock.")
        elif "HTTP Error 401" in text or "HTTP Error 403" in text:
            print("-> DeepInfra rejected the key. Check DEEPINFRA_API_KEY.")
        sys.exit(1)


def clarify_turn(goal, canned, args):
    """Turn 1. Returns (questions_asked, answers_text)."""
    raw = _call(CLARIFY_PROMPT, f'Goal, in their words: "{goal}"', args)
    try:
        qs = parse_output(raw).get("questions") or []
    except Exception:
        print("  (clarify turn returned unparseable output, skipping)")
        return [], ""

    qs = [q for q in qs if isinstance(q, str) and q.strip()][:2]
    if not qs:
        print("  ASKED NOTHING - goal already had what it needed")
        return [], ""

    print("  ASKED:")
    for q in qs:
        print("    ?", q)

    if args.interactive:
        parts = []
        for q in qs:
            parts.append(input(f"    > {q} ").strip())
        answers = " ".join(p for p in parts if p)
    else:
        answers = canned
        print("  ANSWERED (canned):", answers)
    return qs, answers


def run_one(goal, canned, args):
    context = goal
    qa_block = ""

    if args.dry_run:
        print("--- CLARIFY SYSTEM ---")
        print(CLARIFY_PROMPT)
        print("--- DECOMPOSE SYSTEM ---")
        print(SYSTEM_PROMPT)
        print("--- USER ---")
        print(f'Goal, in their words: "{goal}"')
        return None, context

    if args.clarify:
        qs, answers = clarify_turn(goal, canned, args)
        if answers:
            qa_block = "\n\nThey were asked and replied:\n" + answers
            context = goal + " " + answers
        print()

    user_msg = f'Goal, in their words: "{goal}"' + qa_block

    def attempt(msg):
        raw = _call(SYSTEM_PROMPT, msg, args)
        try:
            return parse_output(raw), None
        except Exception:
            return None, raw

    out, raw = attempt(user_msg)
    if out is not None:
        passed, checks = validate(out, context)
        if passed or args.no_repair:
            return out, context

    # ONE repair turn. Never a loop - a model that fails twice usually fails
    # five times, and this path is on the demo's critical latency.
    reason = ("your reply was not valid JSON"
              if out is None else failure_summary(checks))
    print("  REPAIRING:", reason[:110])
    repair_msg = (
        user_msg
        + "\n\nYour previous answer was REJECTED for: " + reason
        + "\nFix exactly those problems. If the reason is an invented "
          "identifier or location, remove the invented detail and stay "
          "general - do not swap it for a different made-up one. "
          "Return the corrected JSON object only."
    )
    out2, raw2 = attempt(repair_msg)
    if out2 is not None:
        passed2, _ = validate(out2, context)
        if passed2:
            print("  repair succeeded")
            return out2, context
        print("  repair failed validation too")
    else:
        print("  repair was unparseable")

    print("  -> deterministic fallback")
    fb = safe_fallback(goal, context)
    fb_ok, fb_checks = validate(fb, context, mode="fallback")
    if not fb_ok:
        # Should never happen - the fallback is constructed from the user's
        # own words. If it does, it is a bug in safe_fallback, not the model.
        print("  !! fallback failed its own safety check:", failure_summary(fb_checks))
    return fb, context


def report(goal, out, context=""):
    print("=" * 66)
    print("GOAL         :", goal)
    print("-" * 66)
    print("FIRST ACTION :", out.get("first_action"))
    print("PLANS        :")
    for p in (out.get("plans") or []):
        if isinstance(p, dict):
            print(f"               IF   {p.get('if')}")
            print(f"               THEN {p.get('then')}")
            print()
    mode = "fallback" if out.get("_fallback") else "normal"
    passed, checks = validate(out, context, mode=mode)
    print("-" * 66)
    for rule, ok, detail, sev in checks:
        if not ok:
            print(f"  FAIL [{sev}] {rule}" + (f"  ({detail})" if detail else ""))
    n_fail = sum(1 for _, ok, _, _ in checks if not ok)
    if out.get("_fallback"):
        print("  (deterministic fallback - model failed twice; "
              "judged against fallback rules, which expect no plans)")
    print(f"  {'ALL CHECKS PASS' if passed else str(n_fail) + ' CHECK(S) FAILED'}"
          f"   [{len(checks)} rules]")
    print("=" * 66)
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=list(CASES), default="concrete")
    ap.add_argument("--goal", default=None, help="your own goal, overrides --case")
    ap.add_argument("--all", action="store_true", help="run every built-in case")
    ap.add_argument("--provider", choices=["bedrock", "deepinfra"], default="bedrock")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--model", default=None)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clarify", action="store_true",
                    help="two-turn: let the model ask up to 2 questions first")
    ap.add_argument("--no-repair", action="store_true",
                    help="skip the repair retry, show the raw first attempt")
    ap.add_argument("--interactive", action="store_true",
                    help="with --clarify, you answer the questions yourself")
    args = ap.parse_args()

    if args.env != ".env":
        load_env(args.env)

    if args.goal:
        goals = [("custom", {"goal": args.goal, "answers": ""})]
    elif args.all:
        goals = list(CASES.items())
    else:
        goals = [(args.case, CASES[args.case])]

    results = []
    for name, spec in goals:
        goal, canned = spec["goal"], spec.get("answers", "")
        print(f"\n### {name}")
        out, context = run_one(goal, canned, args)
        if out is None:
            continue
        results.append((name, report(goal, out, context)))

    if len(results) > 1:
        print("\nSUMMARY")
        for name, ok in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    main()
