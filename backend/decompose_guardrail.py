"""Shared production guardrail for the decomposition agent.

The throwaway decompose probe remains useful for experiments, but the production
Strands path imports its prompts, validator, fallback and cases from this module.
That prevents the model-calling layer from drifting away from the safety checks.

Run:
    python decompose_guardrail.py --selftest
"""

import argparse
import re

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
- Never reveal, print, log, paste, display, send, or copy passwords, tokens, \
 API keys, secrets, credentials, or authorization headers. Inspect metadata \
 or use redacted placeholders instead.
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

# Production clarify prompt: unlike the original probe wording, a vague goal
# does not magically establish a location or deadline. Ask for genuinely
# missing information, but never invent either one.
CLARIFY_STRICT = CLARIFY_PROMPT.replace(
    "If the goal already contains an answer, do NOT ask for it again.",
    "If the goal already contains an answer, do NOT ask for it again. "
    "But a goal that names no file, doc, app or repo has NOT told you where "
    "the work lives, and a goal with no date has NOT told you when it is "
    "due. A topic is not a location: \"the Q3 summary report\" names a "
    "subject, not a document you could open. Ask for what is genuinely absent."
)
assert CLARIFY_STRICT != CLARIFY_PROMPT, (
    "CLARIFY_STRICT patch failed because CLARIFY_PROMPT changed upstream"
)

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
# VALIDATOR - production guardrail
# --------------------------------------------------------------------------

PHYSICAL_VERBS = {
    "open", "type", "click", "scroll", "run", "paste", "copy", "press",
    "close", "save", "select", "write", "delete", "drag", "search",
    "highlight", "add", "rename", "move", "print", "send", "read",
    "uncomment", "comment", "rerun", "reopen", "navigate", "go",
}

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

# Resource aliases prevent harmless wording changes from becoming false
# grounding failures: "Google Docs" can be referred to as "the document".
ARTIFACT_ALIASES = {
    "document": {"document", "doc", "docs", "google doc", "google docs"},
    "spreadsheet": {"spreadsheet", "sheet", "sheets", "google sheet", "google sheets"},
    "slides": {"slides", "slide deck", "deck", "presentation", "google slides"},
    "pdf": {"pdf"},
    "notes": {"notes"},
    "notebook": {"notebook", "ipynb"},
    "report": {"report"},
    "essay": {"essay"},
    "assignment": {"assignment"},
    "textbook": {"textbook"},
    "syllabus": {"syllabus"},
    "worksheet": {"worksheet"},
}

ARTIFACT_NOUNS = [
    "pdf", "document", "notes", "notebook", "slide deck", "slides",
    "presentation", "draft", "report", "essay", "assignment", "textbook",
    "syllabus", "worksheet", "spreadsheet", "sheet",
]

ARTIFACT_RE = re.compile(
    r"\b(?:the|your|their|that|this)\s+(?:\w+\s+){0,2}?("
    + "|".join(map(re.escape, ARTIFACT_NOUNS)) + r")s?\b", re.I
)

GENERIC_RESOURCE_RE = re.compile(
    r"\b(?:the|your|their|that|this)\s+"
    r"(?:(?:[\w'\-]+)\s+){0,4}"
    r"(file|folder|repo|repository|tab)\s+"
    r"(?:with|containing|for|called|named|from)\b", re.I
)

BARE_RESOURCE_RE = re.compile(
    r"\b(?:the|your|their|that|this)\s+(file|folder|repo|repository|tab)\b",
    re.I,
)

CONTENT_NOUNS = [
    "data", "dataset", "number", "numbers", "figure", "figures",
    "result", "results", "measurement", "measurements",
]

CONTENT_REF_RE = re.compile(
    r"\b(?:the|your|their|that|this)\s+"
    r"(?:[\w'\-]+\s+){0,4}?"
    r"(\b(?:" + "|".join(map(re.escape, CONTENT_NOUNS)) + r")\b)", re.I
)

FILENAME_RE = re.compile(r"\b([\w\-]+\.[A-Za-z]{1,6})\b")

REAL_FILE_RE = re.compile(
    r"\b([\w\-]{2,}\.(?:py|ipynb|js|jsx|ts|tsx|java|cpp|cc|c|h|go|rs|rb|php|"
    r"swift|kt|sql|sh|md|txt|rst|tex|csv|tsv|json|ya?ml|toml|ini|cfg|log|"
    r"pdf|docx?|xlsx?|pptx?|odt|ods|rtf|pages|numbers|key|"
    r"png|jpe?g|gif|svg|webp|mp4|mov|wav|mp3|zip|tar|gz))\b", re.I
)

SEQUENTIAL_TRIGGER_RE = re.compile(
    r"\b(?:"
    # Explicit completion wording.
    r"(?:if\s+)?i\s+(?:finish|finished|complete|completed|wrap\s+up|"
    r"have\s+finished|have\s+completed|am\s+done|have\s+done)"
    r"|(?:when|once|after)\s+i(?:\s*['’]m|\s+am)?\s+done"
    # Completed first-person actions.
    r"|(?:(?:if|when|once|after)\s+)?i\s+(?:have\s+)?(?:typed|written|opened|updated|changed|"
    r"selected|entered|copied|pasted|saved|sent|created|added|fixed|moved|renamed|"
    r"deleted|submitted|drafted|closed|read|filled|uploaded|downloaded|edited|"
    r"printed|highlighted|clicked|scrolled|searched|ran|reran|commented|uncommented)"
    # Present-tense action sequencing.
    r"|(?:(?:if|when|once|after)\s+)?i\s+(?:update|change|select|enter|copy|paste|save|send|create|"
    r"add|fix|move|rename|delete|submit|draft|close|fill|upload|download|edit|"
    r"print|highlight|click|scroll|search|run|rerun|comment|uncomment)\b"
    r")\b", re.I
)

# A completion-shaped clause is acceptable when it also contains a real state
# or blocker (for example, "I update one item and feel stuck"). The forbidden
# case is a bare sequence from one completed step to the next.
SEQUENTIAL_STATE_RE = re.compile(
    r"(?:\b(?:and|but|while|because)\s+)?"
    r"(?:"
    r"(?:i\s+)?(?:feel|felt|am|'m|become|became|get|got|remain|seem|seems|look|looks)\s+"
    r"(?:stuck|blocked|unsure|uncertain|confused|hesitant|overwhelmed)"
    r"|(?:i\s+)?(?:cannot|can\'t|cant|don\'t|dont|do not)\s+(?:know|understand|find|see|decide|tell)\b"
    r"|(?:i\s+)?(?:am|\'m)\s+(?:not|still not)\s+able\s+to\b"
    r"|(?:there\s+is|there\'s)\s+(?:an?\s+)?(?:blocker|problem|issue)\b"
    r")",
    re.I,
)

SECRET_EXPOSURE_RE = re.compile(
    r"\b(?:print|log|display|show|reveal|paste|send|post|share|write|type|copy)\b"
    r"(?:[^.\n]{0,100})\b(?:password|passcode|api\s*key|secret|credential|"
    r"authorization\s+header|access\s+key|refresh\s+token|access\s+token|"
    r"token\b(?!\s+(?:parser|parsing|validation|validator|function|class|name|field|type)\b))",
    re.I,
)


def _phrase_hits(text, phrases):
    """Word-boundary matching for phrase-level bans."""
    out = []
    text = str(text or "").lower()
    for phrase in phrases:
        if re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", text):
            out.append(phrase)
    return out


def _compact(text):
    return re.sub(r"\s+", " ", str(text or "")).lower().strip()


def secret_exposure_hits(text):
    """Reject instructions that would expose credential or secret values."""
    clean = _compact(_without_questions(text))
    return sorted(set(m.group(0).strip() for m in SECRET_EXPOSURE_RE.finditer(clean)))


def _without_questions(text):
    # Decompose output has no question field, but keeping questions exempt makes
    # the grounding helper safe to reuse and allows diagnostic text in tests.
    return re.sub(r"[^?]*\?", "", str(text or ""))


def _artifact_family_in_context(noun, ctx):
    aliases = ARTIFACT_ALIASES.get(noun, {noun})
    return any(re.search(r"\b" + re.escape(a) + r"\b", ctx) for a in aliases)


def invented_artifacts(text, context):
    ctx = _compact(context)
    clean = _compact(_without_questions(text))
    bad = []

    for m in ARTIFACT_RE.finditer(clean):
        noun = m.group(1).lower()
        family = next(
            (k for k, vals in ARTIFACT_ALIASES.items() if noun in vals),
            noun,
        )
        if not _artifact_family_in_context(family, ctx):
            bad.append(m.group(0))

    for m in GENERIC_RESOURCE_RE.finditer(clean):
        noun = m.group(1).lower()
        if noun == "file":
            if "file" not in ctx and not FILENAME_RE.search(ctx):
                bad.append(m.group(0))
        elif noun in {"repo", "repository"}:
            if not re.search(r"\b(?:repo|repository|github|git)\b", ctx):
                bad.append(m.group(0))
        elif noun == "folder":
            if "folder" not in ctx:
                bad.append(m.group(0))
        elif noun == "tab":
            if "tab" not in ctx:
                bad.append(m.group(0))

    for m in BARE_RESOURCE_RE.finditer(clean):
        noun = m.group(1).lower()
        if noun == "file" and "file" not in ctx and not FILENAME_RE.search(ctx):
            bad.append(m.group(0))
        elif noun in {"repo", "repository"} and not re.search(
            r"\b(?:repo|repository|github|git)\b", ctx
        ):
            bad.append(m.group(0))
        elif noun == "folder" and "folder" not in ctx:
            bad.append(m.group(0))
        elif noun == "tab" and "tab" not in ctx:
            bad.append(m.group(0))

    return sorted(set(bad))


def invented_content(text, context):
    ctx = _compact(context)
    clean = _compact(_without_questions(text))
    bad = []
    for m in CONTENT_REF_RE.finditer(clean):
        noun = m.group(1).lower()
        variants = {noun}
        if noun.endswith("s"):
            variants.add(noun[:-1])
        else:
            variants.add(noun + "s")
        if not any(re.search(r"\b" + re.escape(v) + r"\b", ctx) for v in variants):
            bad.append(m.group(0))
    return sorted(set(bad))


def invented_locations(text, context):
    ctx = _compact(context)
    clean = _compact(_without_questions(text))
    bad = []
    for kind, num in re.findall(
        r"\b(line|page|section|chapter|cell|step|paragraph|slide)\s+(\d+|[ivx]{2,})\b",
        clean,
    ):
        phrase = f"{kind} {num}"
        if phrase not in ctx:
            bad.append(phrase)
    return sorted(set(bad))


def invented_identifiers(text, context):
    ctx = _compact(context)
    clean = _compact(_without_questions(text))
    ident_re = re.compile(
        r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+"
        r"|[a-z]+[A-Z][A-Za-z0-9]*"
        r"|[A-Za-z_][A-Za-z0-9_]*\(\))"
    )
    bad = []
    for m in ident_re.finditer(clean):
        tok = m.group(1)
        bare = tok.rstrip("()").lower()
        if bare in PROPER_NOUNS:
            continue
        if bare not in ctx and tok.lower() not in ctx:
            bad.append(tok)
    return sorted(set(bad))


def invented_sequential_triggers(plans):
    bad = []
    for p in plans or []:
        if not isinstance(p, dict):
            continue
        cond = str(p.get("if", "")).strip()
        if SEQUENTIAL_TRIGGER_RE.search(cond) and not SEQUENTIAL_STATE_RE.search(cond):
            bad.append(cond)
    return sorted(set(bad))


LOCATION_QUESTION_RE = re.compile(
    r"\b(where|which)\b.*(?:\blocated\b|\b(?:file|document|doc|spreadsheet|sheet|"
    r"pdf|repo|repository|app|thread|message|materials?|proposal|notes?)\b)",
    re.I,
)

def location_already_given(goal):
    g = _compact(goal)
    if REAL_FILE_RE.search(g):
        return True
    return bool(re.search(
        r"\b(?:google docs?|gmail|whatsapp|slack|notion|excel|sheets?|"
        r"word|docs?|spreadsheet|notebook|repo|repository|github|git)\b",
        g, re.I))


def filter_clarify_questions(goal, questions):
    """Drop redundant WHERE questions when the goal already names a location."""
    qs = [str(q).strip() for q in (questions or []) if str(q).strip()]
    if location_already_given(goal):
        qs = [q for q in qs if not LOCATION_QUESTION_RE.search(q)]
    return qs[:2]


def validate(out, context="", mode="normal"):
    """Return (passed, checks).

    Each check is (rule, ok, detail, severity). Normal mode expects 2-4
    contingencies; fallback mode intentionally expects no plans.
    """
    checks = []

    def add(rule, ok, detail="", sev=SEV_STRUCTURE):
        checks.append((rule, bool(ok), detail, sev))

    # Schema first.
    if not isinstance(out, dict):
        add("output is an object", False, type(out).__name__, SEV_SCHEMA)
        return False, checks
    add("output is an object", True, "", SEV_SCHEMA)

    fa = out.get("first_action")
    plans = out.get("plans")
    add("first_action is a non-empty string",
        isinstance(fa, str) and bool(fa.strip()), "", SEV_SCHEMA)
    add("plans is a list",
        isinstance(plans, list), type(plans).__name__, SEV_SCHEMA)

    if isinstance(plans, list):
        for i, p in enumerate(plans, 1):
            if not isinstance(p, dict):
                add(f"plan {i} is an object", False, repr(p)[:30], SEV_SCHEMA)
                continue
            add(f"plan {i} if is a non-empty string",
                isinstance(p.get("if"), str) and bool(p["if"].strip()), "", SEV_SCHEMA)
            add(f"plan {i} then is a non-empty string",
                isinstance(p.get("then"), str) and bool(p["then"].strip()), "", SEV_SCHEMA)

    if any(not ok for _, ok, _, sev in checks if sev == SEV_SCHEMA):
        return False, checks

    fa = fa.strip()
    plans = plans or []

    # Structure.
    first_word = re.sub(r"[^a-z]", "", fa.split()[0].lower())
    add("first_action starts with an observable physical verb",
        first_word in PHYSICAL_VERBS,
        f"'{first_word}' is not in the allowlist" if first_word not in PHYSICAL_VERBS else "")
    add("first_action is not a vague verb",
        first_word not in VAGUE_STARTS,
        first_word if first_word in VAGUE_STARTS else "")
    add("first_action is one act", " and then " not in fa.lower())
    add("first_action under 20 words (proxy for immediacy)",
        len(fa.split()) < 20, f"{len(fa.split())} words")
    add("first_action under 200 chars",
        len(fa) < 200, f"{len(fa)} chars")

    if mode == "normal":
        add("2-4 plans", 2 <= len(plans) <= 4, f"got {len(plans)}")

    for i, p in enumerate(plans, 1):
        cond, act = str(p["if"]).strip(), str(p["then"]).strip()
        cl, al = cond.lower(), act.lower()

        add(f"plan {i} if is a trigger, not a choice",
            not re.match(r"^(i want|i decide|i choose|i feel like)", cl),
            cond[:40])
        vt = [v for v in VAGUE_TRIGGERS if v in cl]
        add(f"plan {i} if names a noticeable situation",
            not vt, ", ".join(vt[:2]))
        add(f"plan {i} if is at least 3 words",
            len(cond.split()) >= 3, f"{len(cond.split())} words")
        add(f"plan {i} then is an action",
            not re.match(r"^(feel|remember|be |stay|try)", al), act[:40])
        va = [v for v in VAGUE_ACTIONS
              if al.startswith(v) or f" {v} " in f" {al} "]
        add(f"plan {i} then is not a vague continuation",
            not va, ", ".join(va[:2]))
        add(f"plan {i} then is at least 3 words",
            len(act.split()) >= 3, f"{len(act.split())} words")
        add(f"plan {i} under 20 words each",
            len(cond.split()) < 20 and len(act.split()) < 20)

    values = " | ".join(
        [fa] + [str(p["if"]) + " | " + str(p["then"]) for p in plans]
    )
    blob = values.lower()

    hits = _phrase_hits(blob, BANNED_PHRASES)
    add("no motivational or shame language", not hits, ", ".join(hits[:3]))

    secret_hits = secret_exposure_hits(values)
    add("no secret exposure instructions", not secret_hits,
        ", ".join(secret_hits[:2]), SEV_GROUNDING)

    # Grounding: only what the user actually supplied in context is evidence.
    grounding = [
        ("no invented locations", invented_locations(values, context)),
        ("no invented identifiers", invented_identifiers(values, context)),
        ("no artifact they never mentioned", invented_artifacts(values, context)),
        ("no unsupported content assumptions", invented_content(values, context)),
    ]
    for rule, bad in grounding:
        add(rule, not bad, ", ".join(bad[:3]), SEV_GROUNDING)

    seq = invented_sequential_triggers(plans)
    add("triggers do not use self-completion as the situation",
        not seq, "; ".join(seq[:2]), SEV_STRUCTURE)

    return all(ok for _, ok, _, _ in checks), checks


def failure_summary(checks):
    return "; ".join(
        rule + (f" ({detail})" if detail else "")
        for rule, ok, detail, _ in checks if not ok
    )


def safe_fallback(goal, context):
    """Deterministic, no-model fallback. Never invent an object to open."""
    m_ = REAL_FILE_RE.search(goal + " " + (context or ""))
    if m_:
        return {
            "first_action": f"Open {m_.group(1)}",
            "plans": [],
            "_fallback": True,
        }
    return {
        "first_action": "Open whatever you will be working in and type one word",
        "plans": [],
        "_fallback": True,
    }


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
        print("  (deterministic fallback - model failed twice; judged as fallback)")
    print(f"  {'ALL CHECKS PASS' if passed else str(n_fail) + ' CHECK(S) FAILED'}"
          f"   [{len(checks)} rules]")
    print("=" * 66)
    return passed


# --------------------------------------------------------------------------
# SELFTEST
# --------------------------------------------------------------------------

def _good(context="Google Docs pricing document, sheet, and auth.py"):
    return {
        "first_action": "Open auth.py and scroll to the decode call",
        "plans": [
            {"if": "I have been stuck for five minutes",
              "then": "write down the exact error in notes"},
            {"if": "the tests still fail after the fix",
              "then": "paste the traceback into the scratchpad"},
        ],
    }, context


def run_selftest():
    tests = []

    out, ctx = _good()
    tests.append(("good baseline", out, ctx, True))

    out, ctx = _good()
    out["first_action"] = "Open auth.py and read line 42"
    tests.append(("invented line", out, ctx, False))

    out, ctx = _good()
    out["first_action"] = "Open auth.py and call refreshToken()"
    tests.append(("invented identifier", out, ctx, False))

    out, ctx = _good()
    out["first_action"] = "Open the spreadsheet and read the first heading"
    tests.append(("invented artifact", out, "fix auth.py", False))

    out, ctx = _good()
    out["first_action"] = "Open the document and read the first heading"
    tests.append(("doc alias grounded by Google Docs", out,
                  "fix auth.py in Google Docs", True))

    out, ctx = _good()
    out["first_action"] = "Open the file and read the first heading"
    tests.append(("bare invented file", out, "study for GATE DA", False))

    out, ctx = _good()
    out["first_action"] = "Open the file and read the first heading"
    tests.append(("generic file grounded by named file", out, "work in auth.py", True))

    out, ctx = _good()
    out["first_action"] = "Open auth.py and type the first number from your Q3 data"
    tests.append(("unsupported Q3 data", out, "fix auth.py and write Q3 summary", False))

    out, ctx = _good()
    out["first_action"] = "Open auth.py and type the first number from the numbers they gave me"
    tests.append(("supplied numbers", out, "fix auth.py; they gave me the numbers", True))

    out, ctx = _good("Google Docs pricing document and sheet")
    out["plans"][0] = {
        "if": "I finish updating one pricing item",
        "then": "paste the revised price into the document",
    }
    tests.append(("self-completion trigger", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["then"] = "try to keep going with the chapter"
    tests.append(("motivational or vague action", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["if"] = "I have typed the subject line"
    tests.append(("completed typed-step trigger", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["if"] = "I have copied the error"
    tests.append(("completed copied-step trigger", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["if"] = "I read the first paragraph"
    tests.append(("completed read-step trigger", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["if"] = "I update one pricing item"
    tests.append(("present-tense step trigger", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["if"] = "I update one pricing item and feel stuck"
    tests.append(("completion plus blocker is a real situation", out, ctx, True))

    out, ctx = _good()
    out["plans"][0]["then"] = "add a print statement with the token value"
    tests.append(("secret exposure action", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["then"] = "add a print statement showing the token before decoding"
    tests.append(("secret exposure natural wording", out, ctx, False))

    out, ctx = _good()
    out["plans"][0]["then"] = "write a comment saying the token parser is called"
    tests.append(("token parser is not secret exposure", out, ctx, True))

    out, ctx = _good()
    out["plans"] = [
        {"if": "I want to start writing",
         "then": "open the document and type one sentence"},
        {"if": "the intro feels unclear",
         "then": "write one question in notes"},
    ]
    tests.append(("choice trigger", out, ctx, False))

    out, ctx = _good()
    out["plans"] = []
    tests.append(("too few plans", out, ctx, False))

    out, ctx = _good()
    out["plans"] = [
        {"if": "I have been stuck for five minutes", "then": "write down the error"},
        {"if": "the tests still fail", "then": "paste the traceback in notes"},
        {"if": "the page looks wrong", "then": "open the browser console"},
        {"if": "the text is missing", "then": "search the document for it"},
        {"if": "the output is empty", "then": "rerun the test once"},
    ]
    tests.append(("too many plans", out, ctx, False))

    out, ctx = _good("auth.py")
    out["first_action"] = "Open whatever you are working in and type one word"
    tests.append(("generic fallback-like action", out, ctx, True))

    tests.append(("clarify filters redundant filename location question",
                  filter_clarify_questions("fix the bug in auth.py",
                                           ["Where is auth.py located?", "When is this due?"]),
                  "", ["When is this due?"]))
    tests.append(("clarify keeps missing location question",
                  filter_clarify_questions("study for the exam",
                                           ["Where are your study materials?", "When is the exam?"]),
                  "", ["Where are your study materials?", "When is the exam?"]))

    all_ok = True
    print("=" * 74)
    print("DECOMPOSE GUARDRAIL SELFTEST")
    print("=" * 74)
    for name, out, ctx, expected in tests:
        if name.startswith("clarify "):
            got = out
            ok = got == expected
            checks = []
        else:
            got, checks = validate(out, ctx)
            ok = (got == expected)
        all_ok = all_ok and ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("        expected:", expected, "got:", got)
            if checks:
                print("        failures:", failure_summary(checks))
    print("-" * 74)
    print("  RESULT:", "ALL TESTS PASS" if all_ok else "FAILURES PRESENT")
    print("=" * 74)
    return all_ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(0 if run_selftest() else 1)
    print("Use --selftest for the guardrail checks.")
