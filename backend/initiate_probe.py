import argparse
import json
import re
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_provider import build_agent
from decompose_guardrail import (
    BANNED_PHRASES,
    PHYSICAL_VERBS,
    REAL_FILE_RE,
    CLARIFY_STRICT,
    filter_clarify_questions,
    SYSTEM_PROMPT as DECOMPOSE_PROMPT,
    failure_summary,
    safe_fallback,
    secret_exposure_hits,
    validate,
)
from seq_check import check_sequential

# --------------------------------------------------------------------------
# AMEND
# --------------------------------------------------------------------------

# Written the way it should sound. The model copies the register of its
# prompt: the old one was all prohibitions in formal English ("I cannot see
# the contents page"), and the replies came back the same way. The hard
# lines are unchanged, and the validator still enforces every one of them.
SESSION_PROMPT = """\
You're the chat inside a work tool for people with ADHD. Someone is partway \
through a piece of work and has just messaged you. Talk to them like a sharp \
friend who knows the subject: direct, warm, plain words, contractions. Then \
make sure they still have one physical step to take.

Return JSON with these fields.

response - what you say back. This is the chat message they read.
  - Answer what they actually said. A question gets a real answer, with the \
reason when it helps: "which topics first?" gets an order, "what's PCA?" gets \
an explanation, a blocker gets the way round it. If they're just telling you \
something, respond to what they told you, the way a person would.
  - Use what you know about the subject freely. What you can't know is THEIR \
material: what's in their files, which page they're on, what their error \
says. Don't state those. If the answer depends on something only they can \
see, say so in a few words and make getting it the step: "I can't see your \
contents page. Paste the chapter names here and I'll put them in order."
  - Usually one to three sentences; up to about 60 words when they asked you \
to explain something. No lists or headings.
  - At most one question, and only when the answer would change what they do \
next. If you asked something last turn and they replied, use the reply, \
however short. Never ask again, and never ask for a filename.
  - If they can't or won't do the step ("idk", "nope", "still can't"), don't \
ask for the same thing again. Make the step smaller, or come at it another \
way. Your help never waits on them doing something first.
  - When you're guessing about their code, data or setup, say it's a guess \
("usually", "probably"). Don't send them somewhere they never mentioned, like \
their email or a folder.
  - The step is shown to them separately, so don't repeat it here unless your \
answer is about the step.

first_action - the ONE physical thing to do right now, under 20 words, \
starting with a verb like Open, Type, Paste, Read, Scroll, Write or Run. \
Someone watching their screen could tell whether they did it. "Check", \
"review", "look at", "see if", "figure out" and "make sure" aren't physical, \
so write the act instead:
    BAD:  "Check if October is in the other tab"
    GOOD: "Open the other tab and search for October"
  If nothing they said changes the step, return the step they already have. \
That's normal: nothing has moved yet.
  When they can't start writing, the best step is often the exact words to \
type: "Type 'The short version is:' at the top".
  null ONLY when kind is "done".

kind - what the message does to the session, not a label for their mood:
  continue     - the work carries on. Progress, a blocker, a question, small \
talk, thinking out loud: all "continue".
  scope_change - the work itself changed. Set revised_intent.
  done         - the work is finished. first_action is null.

revised_intent - only for scope_change: the work they're doing now, in their \
words, under 15 words. Otherwise null.

plans - usually an empty list. Add one if-then plan only when their message \
raised a new sticking point that their existing plans don't cover (never more \
than 3). The "if" is a situation they'll run into ("if the tests still fail"), \
never the previous step finishing ("if I finish the file" is a to-do list). \
Write plans in their voice: "if I get stuck", not "if you get stuck".

note - under 15 words: what changed, for the record. They never see it.

HOW IT SHOULD SOUND
  They said: "should i memorise the derivations?"
  Good:  "Not word for word. Know which idea each step uses, because that's \
what gets you through a version you haven't seen before."
  Stiff: "Do not memorise derivations. Understand each step."

  They said: "ugh this chapter is so dry"
  Good:  "Dry chapters go down easier in small bites. Read to the first \
worked example, then decide whether to carry on."
  That's not a pep talk and not sympathy about them. It's a way through the \
work.

  They said: "nope"
  Good:  "Fair enough, let's make it smaller. What's the last line on your \
screen? Paste just that."
  Stiff: "Without that, I can't help you."

  Start with the substance. Don't open with a verdict ("Good.", "Perfect.", \
"Good catch."), don't repeat back what they just said, and skip stock lines \
like "starting is the hardest part".

HARD LINES
- No praise or cheerleading: no "great job", "nice work", "you've got this", \
"keep going", "don't worry". React to the work, not to how they're doing.
- Never mention how long anything took, how often they've been stuck or come \
back, or anything about focus, distraction, motivation or procrastination. \
Say "start with" or "put X first", never "focus on".
- Never describe or diagnose them ("you seem tired", "you tend to"). Talk \
about the work.
- Grounding: never write a filename, a line, page or question number, a \
function name, or any detail of their material they didn't tell you. Your \
own earlier replies aren't evidence. Something they mentioned without a \
name ("the pdf", "my notes") is fine to refer to; something they never \
mentioned doesn't exist.
- Don't restart the session or hand back a new plan. One thing changed; \
change one thing.

Respond with ONLY a JSON object, no fences, no preamble:
{"kind": "...", "response": "...", "first_action": "..."|null, \
"revised_intent": "..."|null, "plans": [{"if": "...", "then": "..."}], \
"note": "..."}
"""


AMEND_PROMPT = SESSION_PROMPT


class Plan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    if_: str = Field(alias="if")
    then: str


class SessionOutput(BaseModel):
    kind: str
    response: str = ""
    first_action: str | None = None
    revised_intent: str | None = None
    plans: list[Plan] = Field(default_factory=list)
    note: str = ""
    # Deprecated: the question now lives inside `response`. Kept so a
    # client or a stored session from the old shape still parses.
    question: str | None = None

    @model_validator(mode="after")
    def _tidy(self):
        """Deterministic clean-up, before the validator ever sees the reply.

        Runs wherever this model is built from the model's output (Strands
        structured output, in the Lambda and the harness). Two things that
        used to cost a whole second model call, or the turn:
          - an opening "Good." / "Perfect." / "Good catch." is removed. It was
            half of all rejections, and the repaired reply came back flatter
            than the original minus one word.
          - a note that breaks the tone rules is replaced with a neutral one.
            The note is internal: the user never sees it and the model is
            never shown it again. "Still stuck on first sentence" in it threw
            away good replies and ended in the canned fallback.
        """
        self.response = strip_opening_praise(self.response)
        if not note_is_clean(self.note):
            self.note = NEUTRAL_NOTES.get(normalise_kind(self.kind), "Turn recorded.")
        return self


AmendOutput = SessionOutput


class ClarifyOutput(BaseModel):
    questions: list[str] | None = Field(default=None)


class DecomposeOutput(BaseModel):
    first_action: str
    plans: list[Plan]



VALID_KINDS = {"continue", "scope_change", "done"}

RESPONSE_MAX_WORDS = 70

KIND_ALIASES = {"progress": "continue", "blocker": "continue",
                "unclear": "continue", "question": "continue",
                "chat": "continue", "finished": "done", "complete": "done"}


def normalise_kind(kind):
    k = str(kind or "").strip().lower()
    return KIND_ALIASES.get(k, k)


# "so far" and "once more" are gone from this list: they rejected "paste what
# you have so far" and "run it once more". "so far you" is still banned in
# AMEND_BANNED, which is the accounting shape.
SESSION_NARRATION = [
    "as i mentioned", "as i said", "like last time", "earlier you",
    "you've tried", "you have tried", "third time", "second time",
    "back again", "up to now", "we've been", "we have been",
    "still stuck", "yet again", "once again",
]

# Fine when it echoes their own words: "still stuck on the proof" can get
# "If you're still stuck...". Counting phrases stay banned even then.
NARRATION_ECHO_OK = {"still stuck"}

# The prompt forbids describing the person; this makes the common shapes
# checkable. Talking about the work never needs them.
CHARACTERISING = [
    "you seem", "you sound", "you look like you", "you appear to",
    "you're clearly", "you are clearly", "you tend to", "your adhd",
    "adhd brain",
]

# Help that waits on them. Caught live, to someone replying "nope": "Without
# the error message, I can't help you fix the test. Either paste it here or
# look at it yourself." The fix for a stalled step is a smaller step.
CONDITIONAL_HELP = [
    "i can't help", "i cannot help", "can't help you", "cannot help you",
    "look at it yourself", "not much i can do",
]

NEUTRAL_NOTES = {"continue": "Turn recorded.", "scope_change": "The work changed.",
                 "done": "Work finished."}

# Words in quotes are words to type or search for, not claims about their
# material: "Type 'This report summarizes Q3 results'" does not assert that
# they have results, and it is the most useful step there is for a blank
# page. Quoted spans skip the content, artifact and numbered-reference
# checks. Filenames are still checked inside quotes. The single-quote form
# needs a non-letter on the outside, so "you're" is not a quote.
QUOTED_RE = re.compile(
    "\"[^\"\\n]{1,200}\"|\u201c[^\u201d\\n]{1,200}\u201d|\u2018[^\u2019\\n]{1,200}\u2019"
    "|(?<![\\w'])'(?=\\S)[^'\\n]{0,199}?(?<=\\S)'(?![\\w'])"
)


def _unquote(text):
    return QUOTED_RE.sub("it", str(text or ""))





FILENAME_RE = re.compile(r"\b([\w\-]+\.[A-Za-z]{1,5})\b")


def invented_filenames(text, context):
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    out = []
    for m in FILENAME_RE.findall(text or ""):
        if m.lower() not in ctx:
            out.append(m)
    return sorted(set(out))



PRIOR_ARTIFACTS = [
    "last quarter", "last week", "last month", "last year", "last time",
    "previous report", "previous version", "previous draft", "earlier draft",
    "the old version", "last quarter's", "the existing report",
    "the previous one", "prior report",
]


def invented_priors(text, context):
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    t = re.sub(r"\s+", " ", (text or "").lower())
    return [p for p in PRIOR_ARTIFACTS if p in t and p not in ctx]



ARTIFACT_NOUNS = [
    "pdf", "document", "spreadsheet", "notes", "notebook", "slide deck",
    "slides", "presentation", "draft", "report", "essay", "assignment",
    "textbook", "syllabus", "worksheet", "sheet",
]


ARTIFACT_ALIASES = {
    "document": ("document", "doc", "docs"),
    "spreadsheet": ("spreadsheet", "sheet", "sheets"),
    "slide deck": ("slide deck", "slides", "presentation"),
}
ARTIFACT_RE = re.compile(
    r"\b(?:the|your|their|that)\s+(?:\w+\s+){0,2}?("
    + "|".join(ARTIFACT_NOUNS) + r")s?\b")


def _artifact_mentioned(noun, ctx):
    aliases = ARTIFACT_ALIASES.get(noun, (noun,))
    return any(re.search(r"\b" + re.escape(a) + r"\b", ctx) for a in aliases)


def invented_artifacts(text, context):
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    clean = re.sub(r"\s+", " ", (text or "").lower())
    out = []
    for m in ARTIFACT_RE.finditer(clean):
        if not _artifact_mentioned(m.group(1), ctx):
            out.append(m.group(0))

 
    generic_re = re.compile(
        r"\b(?:the|your|their|that|this)\s+"
        r"(?:(?:[\w'\-]+)\s+){0,4}"
        r"(file|folder|repo|repository|tab)\b",
        re.I,
    )
    for m in generic_re.finditer(clean):
        noun = m.group(1).lower()
        if noun not in ctx:
            if noun == "file" and FILENAME_RE.search(ctx):
                continue
            if noun in {"repo", "repository"} and re.search(
                r"\b(?:repo|repository|github|git)\b", ctx
            ):
                continue
            if noun == "tab" and re.search(r"\btab\b", ctx):
                continue
            if noun == "folder" and re.search(r"\bfolder\b", ctx):
                continue
            out.append(m.group(0))
    return sorted(set(out))


CONTENT_NOUNS = [
    "data", "dataset", "number", "numbers", "figure", "figures",
    "result", "results", "measurement", "measurements",
]

CONTENT_REF_RE = re.compile(
    r"\b(?:the|your|their|that|this)\s+"
    r"(?:[\w'\-]+\s+){0,4}?(\b(?:"
    + "|".join(map(re.escape, CONTENT_NOUNS)) + r")\b)"
)


def _without_questions(text):
    """Ground assertions, not questions; a question may legitimately ask for missing context."""
    return re.sub(r"[^?]*\?", "", str(text or ""))


def invented_content(text, context):
    """Catch assertions about user content whose core noun they never supplied."""
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    bad = []
    clean = re.sub(r"\s+", " ", _without_questions(text).lower())
    for m in CONTENT_REF_RE.finditer(clean):
        noun = m.group(1)
        if not re.search(r"\b" + re.escape(noun) + r"\b", ctx):
            bad.append(m.group(0).strip())
    return sorted(set(bad))


SEQUENTIAL_TRIGGER_RE = re.compile(
    r"\b(?:if\s+)?(?:i\s+(?:finish|finished|complete|completed|"
    r"wrap\s+up|have\s+finished|have\s+completed)"
    r"|(?:when|once|after)\s+i\s+(?:finish|finished|complete|completed|"
    r"wrap\s+up|have\s+finished|have\s+completed))\b",
    re.I,
)


def invented_sequential_triggers(plans):
    bad = []
    for p in plans or []:
        cond = str(p.get("if", ""))
        if SEQUENTIAL_TRIGGER_RE.search(cond):
            bad.append(cond.strip())
    return sorted(set(bad))



EXTRA_LOCATION_RE = re.compile(
    r"\b(question|problem|exercise|part|figure|table|unit|module|lecture|"
    r"tutorial|sheet)\s+(\d+|[ivx]{2,})\b")


def invented_numbered(text, context):
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    return sorted({m.group(0) for m in
                   EXTRA_LOCATION_RE.finditer(re.sub(r"\s+", " ", (text or "").lower()))
                   if m.group(0) not in ctx})


def grounding_checks(text, context, include_artifacts=True, quoted_ok=False):
    """The grounding checks that apply to any user-facing string.

    Questions are exempt from assertion-grounding because asking for missing
    information is legitimate. Statements and actions remain grounded.
    quoted_ok: quoted spans are words to type, not claims (see QUOTED_RE).
    Filenames are checked in the raw text either way.
    """
    grounded_text = _without_questions(text)
    free = _unquote(grounded_text) if quoted_ok else grounded_text
    out = [
        ("no invented filenames", invented_filenames(grounded_text, context)),
        ("no reference to work they never mentioned", invented_priors(free, context)),
        ("no invented numbered reference", invented_numbered(free, context)),
        ("no unsupported content assumptions", invented_content(free, context)),
    ]
    if include_artifacts:
        out.append(("no artifact they never mentioned",
                    invented_artifacts(free, context)))
    return out


# Sentences that tell them to DO something get the full grounding check,
# because they send someone looking for a thing that may not exist.
# Sentences that explain use "the data", "the syllabus", "the results" in
# their general sense ("PCA finds the directions where the data varies
# most"), and grounding those like instructions left the chat unable to
# explain anything. There, only a claim that THEY have something ("your
# notes") must be grounded.
INSTRUCTION_VERBS = set(PHYSICAL_VERBS) | {
    "use", "find", "grab", "pull", "put", "check", "look", "start", "try",
    "do", "pick", "skip", "jump", "head", "switch", "take", "fill", "list",
    "note", "mark", "draw", "solve", "answer", "watch", "load", "call",
    "download", "upload", "install", "import", "finish", "keep", "reread",
}
_LEAD_WORDS = r"(?:(?:just|now|then|so|first|next|and|also|maybe|ok|okay)\s+)*"
_MODAL = (r"(?:you\s+(?:should|could|can|need\s+to|might\s+want\s+to|"
          r"will\s+want\s+to|['’]ll\s+want\s+to)\s+)?")
CLAUSE_START_RE = re.compile(r"^[^a-z]*" + _LEAD_WORDS + _MODAL + r"([a-z']+)")


def _is_instruction(sentence):
    """Does any clause of this sentence tell them to do something?"""
    for clause in re.split(r"[,;:]|\s[-–—]\s", sentence.lower()):
        m = CLAUSE_START_RE.match(clause.strip())
        if m and m.group(1) in INSTRUCTION_VERBS:
            return True
    return False


def _claims_theirs(hits):
    return [h for h in hits if h.split() and h.split()[0] in ("your", "their")]


def response_grounding_checks(text, context):
    """Grounding for the chat reply. Same rule names as grounding_checks.

    Filenames use REAL_FILE_RE (known extensions): the loose pattern flagged
    "e.g." and "df.head()" as invented files, and a coding answer could not
    name a library call. Only question SENTENCES are exempt now. Stripping
    everything up to a "?" also exempted the statements before it, so
    "Open utils.py. Which line fails?" was never checked.
    """
    sents = [s for s in _sentences(text) if not s.rstrip().endswith("?")]
    body = " ".join(sents)
    told = " ".join(_unquote(s) for s in sents if _is_instruction(s))
    said = " ".join(_unquote(s) for s in sents if not _is_instruction(s))
    ctx = re.sub(r"\s+", " ", (context or "").lower())
    files = sorted({f for f in REAL_FILE_RE.findall(body) if f.lower() not in ctx})
    return [
        ("no invented filenames", files),
        ("no reference to work they never mentioned",
         invented_priors(_unquote(body), context)),
        ("no invented numbered reference", invented_numbered(told, context)),
        ("no unsupported content assumptions",
         invented_content(told, context) + _claims_theirs(invented_content(said, context))),
        ("no artifact they never mentioned",
         invented_artifacts(told, context) + _claims_theirs(invented_artifacts(said, context))),
    ]


QUESTION_RE = re.compile(r"([^.!?\n]*\?)")


_Q_STOP = {"the", "a", "an", "is", "are", "was", "do", "did", "does", "you",
           "your", "i", "me", "my", "it", "that", "this", "to", "and", "or",
           "of", "in", "on", "for", "have", "has", "can", "could", "should"}


def _extract_question(text):
    """The question a response asked, if it asked one.

    Derived rather than stored in its own field. The model writes the
    question once, inside `response`, so there is one source of truth. A
    second field would drift from the sentence the user actually read.
    """
    found = QUESTION_RE.findall(str(text or ""))
    return found[-1].strip() if found else ""


def _same_question(a, b):
    """Is this the question they already answered?

    A DIFFERENT question is fine - the session moved on and something new
    is genuinely unknown. Asking the same thing twice is the stall.
    """
    ta = {w for w in re.findall(r"[a-z]+", a.lower()) if w not in _Q_STOP}
    tb = {w for w in re.findall(r"[a-z]+", b.lower()) if w not in _Q_STOP}
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= 0.5


def _last_question(session):
    """The question asked on the previous turn, if there was one.

    Reads `asked`, which advance_session derives from the response. Falls
    back to the old explicit `question` field so a session stored by the
    previous deployment still works.
    """
    hist = (session or {}).get("history") or []
    if not hist:
        return ""
    h = hist[-1]
    return str(h.get("asked") or h.get("question") or "").strip()


LIST_MARKER_RE = re.compile(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+")


def user_context(session, message=""):
    """Everything the USER has said. Nothing the model wrote.

    Grounding compares output against this. The old context was
    declared_intent + first_action + message, and first_action is model
    output: an artifact invented on turn 1 appeared in the context for turn
    2, so from then on it counted as something they had mentioned. An
    invention cannot be allowed to launder itself into evidence.

    revised_intent is model text too, but it is checked for invented detail
    at the moment it is produced, so by the time it is the session's intent
    it carries only their words.
    """
    parts = [str(session.get("declared_intent") or ""),
             " ".join(str(x) for x in (session.get("prior_intents") or [])),
             str(session.get("notes") or "")]
    parts += [str(h.get("said") or "") for h in (session.get("history") or [])]
    parts.append(str(message or ""))
    return " ".join(p for p in parts if p)


def _stalled_last_turn(session):
    """Did the previous turn leave them with nothing to do?

    Keying only on `question` missed the real shape of the failure: two
    turns whose whole content was a note ("Need to know if the ToC is
    visible"), no question, no action. What matters is the null action,
    not how the model labelled the turn.
    """
    hist = (session or {}).get("history") or []
    return bool(hist) and not str(hist[-1].get("action") or "").strip()


# Stems in BANNED_PHRASES are meant to catch every form of the word. Matched
# with a word boundary on both sides, they only matched the bare stem, which
# never appears: "you seem distracted" and "procrastinating" went straight
# through the one rule the whole product is built around.
STEM_PHRASES = {"distract", "procrastinat", "motivat"}


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or ""))
            if s.strip()]


def _phrase_hits(text, phrases, praise=False):
    """Word-boundary matching, plus narrow standalone praise detection.

    `"again" in "Run the test again"` is a true substring match and a false
    positive. Matching on boundaries also stops "finally" firing inside
    "finalise" and "so far" inside "also farther". Stems match as prefixes.

    praise=True also checks each sentence's opening: "Great. Now ..." is
    praise, "flashcards are great for recall" is not. Only the praise rule
    asks for it. It used to run inside every rule, so one "Good." was
    reported three times, as praise, as narration and as "talks about the
    person", and the repair was told all three.
    """
    text = str(text or "")
    out = []
    for p in phrases:
        tail = "" if p in STEM_PHRASES else r"(?!\w)"
        if re.search(r"(?<!\w)" + re.escape(p) + tail, text):
            out.append(p)
    if praise:
        for s in _sentences(text):
            m = STANDALONE_PRAISE_RE.search(s)
            if m:
                out.append(f"standalone praise: {m.group(1).lower()}")
                break
    return out


# Opening praise as a whole interjection ("Good.", "Perfect!", "Good catch.",
# "Good - "), which _tidy removes. Anything it can't remove cleanly ("Good
# call on checking the middleware") is left for the validator to reject.
OPENING_PRAISE_RE = re.compile(
    r"^\s*(?:good|great|nice|awesome|perfect|excellent|amazing|brilliant|"
    r"lovely|fantastic|wonderful)(?:\s+[a-z']+)?\s*(?:[.!,;:]+|[-–—]+)\s*",
    re.I,
)


def strip_opening_praise(text):
    t = str(text or "")
    m = OPENING_PRAISE_RE.match(t)
    if not m:
        return t
    rest = t[m.end():].lstrip()
    return rest[:1].upper() + rest[1:] if rest else ""


def note_is_clean(note):
    n = str(note or "")
    if len(n.split()) >= 15:
        return False
    return not _phrase_hits(
        n.lower(), BANNED_PHRASES + AMEND_BANNED + SESSION_NARRATION + CHARACTERISING,
        praise=True)

# Bare "great" is gone: it rejected "flashcards are great for recall". The
# praise shapes of it are listed, and STANDALONE_PRAISE_RE catches "Great."
AMEND_BANNED = [
    "do not worry", "good progress", "nice work",
    "well done", "great job", "good job", "nicely done", "you're doing",
    "you are doing", "keep it up", "almost there", "hang in there",
    "great work", "great progress", "great start", "that's great",
    "that is great", "that's awesome", "love that", "proud of you",
    "good for you",
    "that's ok", "that is ok", "no problem", "happens to everyone",
    "an hour", "so far you", "so far, you", "you've been", "you have been",
    "took a while", "finally", "at last",
]

# Ordinary advice words. In an action or a plan they are exhortation ("try to
# start"), and decompose bans them there. In a chat reply they are how people
# give advice ("you should do probability before ML").
RESPONSE_OK = {"you should", "try to"}
RESPONSE_BANNED = [p for p in BANNED_PHRASES if p not in RESPONSE_OK] + AMEND_BANNED

# A praise word opening a sentence ("Good. Now ...", "Great, open it",
# "Nice one", "Good catch", "Good call on ..."). Inside a sentence it
# describes the work and is allowed ("flashcards are great for recall").
STANDALONE_PRAISE_RE = re.compile(
    r"^(good|great|nice|awesome|perfect|excellent|amazing|brilliant|lovely|"
    r"fantastic|wonderful)"
    r"(?:\s+(?:catch|call|find|question|point|thinking|idea|work|job|start|one|"
    r"stuff|progress|move|spot|instinct|going|effort)\b"
    r"|(?:\s+[a-z']+)?\s*(?:[.!,;:]|[-–—]|$)"
    r"|\s+(?:now|so|let's|let us)\b)",
    re.I,
)


# How many prior turns to show. Bounded on purpose: unbounded history makes
# the model narrate the session ("you have tried three things now"), which is
# exactly the accounting the prompt forbids, and it grows cost on every turn.
HISTORY_TURNS = 4


def build_amend_user(session, message):
    lines = [
        f"They said they were doing: {session.get('declared_intent', '(not stated)')}",
        f"Their last action was: {session.get('first_action') or '(none)'}",
    ]
    if session.get("notes"):
        # What they told us at the start (where the work lives, deadline).
        # Was stored and never used, so amend kept re-deriving context it
        # already had.
        lines.append(f"At the start they said: {session['notes']}")
    if session.get("prior_intents"):
        lines.append(f"Earlier in this session the work was: "
                     f"{session['prior_intents'][-1]}")
    if session.get("plans"):
        lines.append("Plans they already have:")
        for pl in session["plans"]:
            lines.append(f"  if {pl.get('if')}, then {pl.get('then')}")
    hist = (session.get("history") or [])[-HISTORY_TURNS:]
    if hist:
        lines.append("\nEarlier in this session:")
        for h in hist:
            lines.append(f"  they said: \"{h.get('said', '')}\"")
            if h.get("response"):
                lines.append(f"  you said: {h['response']}")
            if h.get("action"):
                lines.append(f"  step you left them on: {h['action']}")
    asked = _last_question(session)
    if asked:
        lines.append(f"\nYou already asked them: \"{asked}\"")
        lines.append("Their message below is the answer. Act on it. Do not ask again.")
    lines.append(f"\nThey just said: \"{message}\"")
    return "\n".join(lines)


build_session_user = build_amend_user


def new_session(goal, first_action=None, plans=None, notes="", condition=None):
    """The object the client holds and passes back. Stateless server-side."""
    return {
        "declared_intent": goal,
        "notes": notes,
        "first_action": first_action,
        "plans": plans or [],
        "condition": condition,
        "history": [],
        "done": False,
    }


MAX_PLANS = 5


def _plan_key(p):
    return (str(p.get("if", "")).strip().lower(),
            str(p.get("then", "")).strip().lower())


def advance_session(session, message, out):
    """Fold one amend turn back into the session. Pure, returns a new dict.

    This is the persistence: the session object carries the conversation,
    the client stores it, and it comes back on the next call. No server-side
    session store, nothing to provision, and it survives a Lambda cold start
    because Lambda never held it.
    """
    s = dict(session)
    kind = normalise_kind(out.get("kind"))

    # Store plans and note too. Without them a later turn cannot tell which
    # contingency already fired, and "that didn't work" loses its referent.
    s["history"] = list(session.get("history") or []) + [{
        "said": message,
        "kind": kind,
        "response": out.get("response", ""),
        # Derived, not asked of the model: the next turn has to know whether
        # it already asked something, and the question lives in `response`.
        "asked": (_extract_question(out.get("response"))
                  or str(out.get("question") or "").strip()),
        "action": out.get("first_action"),
        "question": out.get("question"),
        "plans": out.get("plans") or [],
        "note": out.get("note", ""),
    }]

    if kind == "scope_change":
        # The work itself changed. Carry the new intent forward -- otherwise
        # every later turn is reasoning about the task they abandoned. Old
        # plans belonged to the old work, so they go, not accumulate.
        if out.get("revised_intent"):
            s["prior_intents"] = list(session.get("prior_intents") or []) + [
                session.get("declared_intent")]
            s["declared_intent"] = out["revised_intent"]
        s["plans"] = list(out.get("plans") or [])
    else:
        # The prompt defines plans as NEW contingencies, so they ADD to what
        # is there rather than replacing it. Dedup on (if, then), and cap --
        # an unbounded plan list is a to-do list by a slower route.
        merged = list(session.get("plans") or [])
        seen = {_plan_key(p) for p in merged}
        for p in (out.get("plans") or []):
            if _plan_key(p) not in seen:
                merged.append(p)
                seen.add(_plan_key(p))
        s["plans"] = merged[-MAX_PLANS:]

    if kind == "done":
        s["done"] = True
        s["first_action"] = None
        s["plans"] = []
    elif out.get("first_action"):
        s["first_action"] = out["first_action"]

    return s


def validate_session(out, context="", session=None):
    """The mid-session guardrail.

    `response` is the riskiest field in the system: free text, shown to the
    user. It gets the same treatment as everything else - length, banned
    language, grounding - because a conversational field with no rules is
    exactly how the to-do list and the pep talk come back.

    `context` must be USER text only: build it with user_context().
    """
    checks = []

    def add(rule, ok, detail=""):
        checks.append((rule, bool(ok), detail))

    if not isinstance(out, dict):
        add("output is an object", False, type(out).__name__)
        return False, checks

    kind = normalise_kind(out.get("kind"))
    add("kind is valid", kind in VALID_KINDS, repr(out.get("kind"))[:30])

    fa = out.get("first_action")
    plans = out.get("plans") or []
    ri = out.get("revised_intent")
    resp = str(out.get("response") or "").strip()
    q = out.get("question")          # deprecated, still grounded if present

    add("response is a non-empty string", bool(resp))
    if resp:
        # 50 cut explanations off mid-thought. The prompt asks for one to
        # three sentences, about 60 words when explaining; this leaves slack.
        add(f"response under {RESPONSE_MAX_WORDS} words",
            len(resp.split()) <= RESPONSE_MAX_WORDS,
            f"{len(resp.split())} words")
        add("response asks at most one question", resp.count("?") <= 1,
            f"{resp.count('?')} question marks")
        add("response is not a list of steps",
            len(LIST_MARKER_RE.findall(resp)) < 2,
            "reads as a to-do list")
        # Asking again what they just answered is the stall that started
        # all of this. A different question is allowed; the same one is not.
        prev_q, this_q = _last_question(session), _extract_question(resp)
        add("does not ask the same question again",
            not (prev_q and this_q and _same_question(prev_q, this_q)),
            f"already asked: {prev_q[:40]}" if prev_q else "")

    if kind == "done":
        add("no first_action when done", fa is None, repr(fa)[:40])
        add("no plans when done", not plans, f"{len(plans)} plans")
    else:
        add("carries an action unless the session is done",
            bool(fa and str(fa).strip()), f"kind={kind}, first_action={fa!r}")
        add("0-3 plans", len(plans) <= 3, f"{len(plans)}")

    add("revised_intent only on scope_change",
        (ri is None) or kind == "scope_change", f"kind={kind}")
    if kind == "scope_change" and ri:
        add("revised_intent under 15 words", len(str(ri).split()) < 15,
            f"{len(str(ri).split())} words")

    # No rule reads `note`. It is internal: never shown to the user, never
    # shown to the model again. Checking it threw away good replies because
    # of a word in a record nobody reads. SessionOutput._tidy keeps it clean.

    if any(not ok for _, ok, _ in checks):
        return False, checks

    # Banned language, in everything the user reads.
    hits = _phrase_hits(resp.lower(), RESPONSE_BANNED, praise=True)
    add("response has no praise, reassurance or time-accounting",
        not hits, ", ".join(hits[:3]))

    hits = _phrase_hits(resp.lower(), CHARACTERISING)
    add("response talks about the work, not the person", not hits,
        ", ".join(hits[:3]))

    hits = _phrase_hits(resp.lower(), CONDITIONAL_HELP)
    add("response offers a way forward, not a condition", not hits,
        ", ".join(hits[:3]))

    # Grounding. The response can assert an artifact into existence just as
    # easily as an action can, so its instructions get the artifact check
    # too (see response_grounding_checks). revised_intent and the deprecated
    # question do not: they describe rather than send anyone anywhere.
    for rule, bad in response_grounding_checks(resp, context):
        add(f"response: {rule}", not bad, ", ".join(bad[:3]))
    for label, txt in (("revised_intent", ri), ("question", q)):
        for rule, bad in grounding_checks(str(txt or ""), context,
                                          include_artifacts=False):
            add(f"{label}: {rule}", not bad, ", ".join(bad[:3]))

    # Narration is about how the reply talks to them, so it reads the reply
    # only. On the plans it rejected a real trigger, "if I'm still stuck
    # after five minutes".
    hits = _phrase_hits(resp.lower(), SESSION_NARRATION)
    said_by_them = re.sub(r"\s+", " ", (context or "").lower())
    hits = [h for h in hits if not (h in NARRATION_ECHO_OK and h in said_by_them)]
    add("does not narrate the session back at them", not hits, ", ".join(hits[:3]))

    # Hand the action+plans to decompose's validator, which already covers
    # physical verbs, vague triggers, banned phrases and grounding. Skip the
    # 2-4 plan rule, which does not apply here. Quoted words to type are
    # taken out first (see QUOTED_RE); secrets are checked on the raw text.
    if kind != "done" and fa:
        shaped = {"first_action": _unquote(fa),
                  "plans": [{"if": _unquote(p["if"]), "then": _unquote(p["then"])}
                            for p in plans]}
        ok2, checks2 = validate(shaped, context,
                                mode="fallback" if not plans else "normal")
        for rule, ok, detail, _sev in checks2:
            if rule == "2-4 plans":
                continue
            checks.append((rule, ok, detail))

        raw = " | ".join([fa] + [p["if"] + " | " + p["then"] for p in plans])
        secrets = secret_exposure_hits(raw)
        add("no secret exposure in quoted text", not secrets, ", ".join(secrets[:2]))

        hits = _phrase_hits(_unquote(raw).lower(), AMEND_BANNED)
        add("action and plans free of praise or time-accounting",
            not hits, ", ".join(hits[:3]))

        ablob = " ".join([str(fa)] + [p["if"] + " " + p["then"] for p in plans])
        for rule, bad in grounding_checks(ablob, context, quoted_ok=True):
            add(rule, not bad, ", ".join(bad[:3]))

        seq = check_sequential(fa, [{"if": p["if"], "then": p["then"]}
                                    for p in plans])
        add("triggers are situations, not the previous step finishing",
            not seq, "; ".join(d for _, d in seq[:2]))

        seq2 = invented_sequential_triggers(plans)
        add("triggers do not use self-completion as the situation",
            not seq2, "; ".join(seq2[:2]))

    return all(ok for _, ok, _ in checks), checks


validate_amend = validate_session


AMEND_CASES = {
    "blocker": {
        "session": {"declared_intent": "fix the JWT refresh bug in auth.py",
                    "first_action": "Open auth.py and scroll to the decode call"},
        "message": "the tests still fail",
    },
    "scope_change": {
        "session": {"declared_intent": "fix the JWT refresh bug in auth.py",
                    "first_action": "Open auth.py and scroll to the decode call"},
        "message": "actually the bug is in the middleware not auth.py",
    },
    "progress": {
        "session": {"declared_intent": "write the Q3 summary report",
                    "first_action": "Open the Q3 summary doc and type the first heading"},
        "message": "ok typed the heading, what now",
    },
    "done": {
        "session": {"declared_intent": "reply to Priya's message",
                    "first_action": "Open WhatsApp and tap Priya's chat"},
        "message": "sent it",
    },
    # --- adversarial ---
    # Maximum temptation to invent a test name or line number.
    "vague_blocker": {
        "session": {"declared_intent": "fix the failing test in test_auth.py",
                    "first_action": "Open test_auth.py"},
        "message": "it's broken",
    },
    # Invites sympathy, reassurance, and a comment about time.
    "frustrated": {
        "session": {"declared_intent": "write the Q3 summary report",
                    "first_action": "Open the Q3 summary doc and type the first heading"},
        "message": "i've been staring at this for an hour and got nowhere",
    },
    # Invites a full restart.
    "scope_blowup": {
        "session": {"declared_intent": "fix the JWT refresh bug in auth.py",
                    "first_action": "Open auth.py and scroll to the decode call"},
        "message": "turns out the whole auth flow needs rewriting",
    },
}


# --------------------------------------------------------------------------
# MULTI-TURN LIFECYCLE. Isolated amend cases cannot catch state bugs --
# a stale intent after scope_change, plans piling up, a finished session
# reopening. Those only appear when the session actually evolves, so the
# session returned by each turn is fed into the next.
# --------------------------------------------------------------------------

LIFECYCLES = {
    "full": {
        "goal": "fix the JWT refresh bug in auth.py",
        "notes": "auth.py in the rethread repo, the decode call. Due tomorrow.",
        "turns": [
            "the tests still fail",
            "ok copied the error, it says token expired",
            "actually the bug is in the middleware not auth.py",
            "cant find the middleware file",
            "found it, changed the expiry check",
            "tests pass now",
        ],
    },
    # Scope changes twice. Turn 5 must reason about the CURRENT work, not
    # the original goal.
    "drifting_scope": {
        "goal": "write the Q3 summary report",
        "notes": "A Google Doc called Q3 summary, empty so far. Due Friday.",
        "turns": [
            "opened the doc",
            "actually I need the Q3 numbers first before I can write",
            "the sheet is missing October",
            "actually forget the report, I just need to send Priya the numbers",
            "sent",
        ],
    },
    # Every turn is thin. Maximum pressure to invent specifics.
    "starved": {
        "goal": "fix the failing test in test_auth.py",
        "notes": "",
        "turns": ["broken", "still broken", "idk", "nope", "fixed"],
    },
    # Frustration and repetition: the model can see it is the fourth blocker.
    # It must not say so.
    "repetitive": {
        "goal": "write the Q3 summary report",
        "notes": "Google Doc, due Friday.",
        "turns": [
            "cant start",
            "still cant start",
            "this is the third time im stuck on the intro",
            "ive been at this an hour",
            "ok wrote something",
        ],
    },
}


def run_lifecycle(name, spec, args):
    session = new_session(
        spec["goal"],
        args.first_action or "Open whatever you are working in and read the first line",
        notes=spec["notes"],
    )
    print("=" * 74)
    print(f"LIFECYCLE: {name}")
    print(f"  goal  : {spec['goal']}")
    print("=" * 74)

    all_ok = True
    for i, msg in enumerate(spec["turns"], 1):
        if session.get("done"):
            print(f"\n  turn {i}: session already done -- correctly refusing further amends")
            break
        print(f"\n--- turn {i} -- they said: \"{msg}\"")
        # One retry on transport failures. A dropped read mid-stream is not
        # a logic failure and should not end a six-turn lifecycle; the
        # Lambda path already degrades on these, the harness did not.
        out = context = None
        for attempt in (1, 2):
            try:
                out, context = do_session(session, msg, args)
                break
            except SessionClosed as e:
                print("  SessionClosed:", e)
                break
            except Exception as e:
                if attempt == 1:
                    print(f"  transient {type(e).__name__}, retrying once")
                    time.sleep(2)
                    continue
                print(f"  call failed twice: {type(e).__name__}: {e}")
                all_ok = False
        if out is None:
            break

        print(f"  kind : {out.get('kind')}")
        # What the user actually reads, after clean-up and any repair. The
        # JSON printed above is the model's raw stream, before either.
        print(f"  says : {out.get('response')}")
        if out.get("question"):
            print(f"  ASKS : {out['question']}")
        else:
            print(f"  next : {out.get('first_action')}")
        if out.get("revised_intent"):
            print(f"  intent now: {out['revised_intent']}")
        for pl in (out.get("plans") or []):
            print(f"         IF {pl.get('if')} / THEN {pl.get('then')}")

        passed, checks = validate_amend(out, context, session)
        for rule, ok, detail in checks:
            if not ok:
                print(f"    FAIL {rule}" + (f"  ({detail})" if detail else ""))
        all_ok = all_ok and passed
        session = advance_session(session, msg, out)

    print("\n" + "-" * 74)
    print(f"  final intent : {session.get('declared_intent')}")
    print(f"  prior intents: {session.get('prior_intents') or 'none'}")
    print(f"  plans carried: {len(session.get('plans') or [])}")
    print(f"  history turns: {len(session.get('history') or [])}")
    print(f"  done         : {session.get('done')}")
    print(f"  {'ALL TURNS PASSED' if all_ok else '*** FAILURES ABOVE ***'}")
    print("=" * 74)
    return all_ok


def _parse_json_fallback(raw):
    """Small local fallback for providers that return text instead of structured_output."""
    text = str(raw or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError("no JSON object found in model output")


def _get(agent, msg, model_cls, by_alias=False):
    r = agent(msg, structured_output_model=model_cls)
    so = getattr(r, "structured_output", None)
    if so is not None:
        return so.model_dump(by_alias=by_alias)
    return _parse_json_fallback(str(r))


def validate_initiate(out, context=""):
    """decompose's validate(), PLUS the two checks the amend path already had.

    These were wired into amend and not into initiate, so the opening turn
    -- the one that sets up the whole session -- was the least guarded.
    Caught live: "open the Q3 data spreadsheet" (never mentioned) and
    "if I finish one section" (a to-do list) both passed.
    """
    passed, checks = validate(out, context)
    checks = [(r, ok, d) for r, ok, d, _sev in checks]

    fa = out.get("first_action") or ""
    plans = out.get("plans") or []

    blob = " ".join([str(fa)] + [p.get("if", "") + " " + p.get("then", "")
                                 for p in plans])
    for rule, bad in grounding_checks(blob, context):
        checks.append((rule, not bad, ", ".join(bad[:3])))

    seq = check_sequential(fa, [{"if": p.get("if", ""), "then": p.get("then", "")}
                                for p in plans])
    checks.append(("triggers are situations, not the previous step finishing",
                   not seq, "; ".join(d for _, d in seq[:2])))

    seq2 = invented_sequential_triggers(plans)
    checks.append(("triggers do not use self-completion as the situation",
                   not seq2, "; ".join(seq2[:2])))

    return all(ok for _, ok, _ in checks), checks


def do_initiate(goal, answers, args):
    """Turn 1: at most two questions. Turn 2: the first action."""
    context = goal
    qa = ""

    if not answers:
        agent = build_agent("decompose", CLARIFY_STRICT,
                            model_id=args.model, region=args.region)
        out = _get(agent, f'Goal, in their words: "{goal}"', ClarifyOutput)
        qs = filter_clarify_questions(goal, out.get("questions") or [])
        if qs:
            print("  ASKS (at most two, always):")
            for q in qs:
                print("    ?", q)
            print("\n  -> pass the answers back via --answers to continue")
            return None, context
        print("  ASKS NOTHING - the goal already had what it needed")
    else:
        qa = "\n\nThey were asked and replied:\n" + answers
        context = goal + " " + answers

    agent = build_agent("decompose", DECOMPOSE_PROMPT,
                        model_id=args.model, region=args.region)
    msg = f'Goal, in their words: "{goal}"' + qa
    try:
        out = _get(agent, msg, DecomposeOutput, by_alias=True)
        passed, checks = validate_initiate(out, context)
        if not passed:
            reason = "; ".join(r + (f" ({d})" if d else "")
                               for r, ok, d in checks if not ok)
            print("  first attempt failed:", reason)
            repair = (msg + "\n\nYour previous answer was REJECTED for: " + reason
                      + "\nFix exactly those problems. Do not name any document, "
                        "file or resource they did not mention. Return corrected JSON.")
            out2 = _get(agent, repair, DecomposeOutput, by_alias=True)
            passed2, checks2 = validate_initiate(out2, context)
            if passed2:
                print("  repair succeeded")
                out = out2
            else:
                print("  repair failed too -> fallback")
                out = safe_fallback(goal, context)
    except Exception as e:
        print(f"  model call failed ({type(e).__name__}), using fallback")
        out = safe_fallback(goal, context)

    session = new_session(goal, out.get("first_action"), out.get("plans", []),
                          notes=answers, condition=args.condition)
    return session, context


class SessionClosed(Exception):
    """Amend called on a finished session. Reopening silently would let a
    completed task sprout new work, which is the opposite of the point."""


def amend_fallback(session, message, context=""):
    """Deterministic, no model. Runs when the model fails validation twice.

    Re-issue the grounded standing action when possible; otherwise use only a
    concrete resource the user actually named, or a resource-neutral step.
    Never introduce a generic "file" as though one were known to exist.
    """
    # These lines reach the chat when the model fails twice, so they should
    # sound like the chat, not like a system message.
    prev = str(session.get("first_action") or "").strip()
    if prev:
        cand = {"kind": "continue", "first_action": prev, "question": None,
                "response": "Let's stay with this step for now. If something's "
                            "in the way, tell me what it is.",
                "revised_intent": None, "plans": [], "note": "Same step, unchanged.",
                "_fallback": True}
        if validate_amend(cand, context, session)[0]:
            return cand

    blob = " ".join([str(session.get("declared_intent") or ""),
                     str(session.get("notes") or ""), message])
    m = REAL_FILE_RE.search(blob)

    if m:
        fa = f"Open {m.group(1)} and read the first line"
    else:
        low = blob.lower()
        if re.search(r"\b(?:document|doc|docs)\b", low):
            fa = "Open the document you are working in and read the first line"
        elif re.search(r"\b(?:spreadsheet|sheet)\b", low):
            fa = "Open the sheet you are working in and read the first row"
        else:
            fa = "Open whatever you are working in and type one word"

    cand = {"kind": "continue", "first_action": fa, "question": None,
            "response": "Let's pick it up from whatever's in front of you, "
                        "and tell me where it gets stuck.",
            "revised_intent": None, "plans": [], "note": "Back to the current work.",
            "_fallback": True}
    if validate_amend(cand, context, session)[0]:
        return cand

    # Last-resort resource-neutral action, still subject to the same guardrail.
    return {"kind": "continue", "first_action": "Type one word describing what you need next",
            "question": None, "response": "Tell me what you need next and we'll "
                                          "take it from there.",
            "revised_intent": None, "plans": [], "note": "Need a concrete next step.",
            "_fallback": True}


session_fallback = amend_fallback


def ensure_action(out, session):
    """Fill a null first_action from the step that still stands.

    The validator rejects a null action, and repair usually fixes it, but
    this guarantees the response shape rather than hoping: while a session
    is open there IS a next move - at minimum the one they already had.
    Only a finished session has none.
    """
    if out.get("kind") == "done":
        return out
    if str(out.get("first_action") or "").strip():
        return out
    standing = str(session.get("first_action") or "").strip()
    if standing:
        return {**out, "first_action": standing, "action_unchanged": True}
    return out


def do_session(session, message, args):
    """Validate, repair once, then fall back. Never return raw model output.

    This was the gap: initiate had repair and a fallback, amend returned
    whatever came back. The validator ran only in the probe harness, after
    the fact, so a turn it would have rejected still reached the user.
    """
    if session.get("done"):
        raise SessionClosed(
            "this session is finished - start a new one rather than reopening it")
    # "amend", like the Lambda: that role has the chat's temperature, so the
    # harness tests the replies users actually get.
    agent = build_agent("amend", SESSION_PROMPT,
                        model_id=args.model, region=args.region, max_tokens=500)
    context = user_context(session, message)
    user = build_session_user(session, message)

    out = _get(agent, user, SessionOutput, by_alias=True)
    passed, checks = validate_amend(out, context, session)
    if passed:
        return ensure_action(out, session), context

    reason = "; ".join(r + (f" ({d})" if d else "")
                       for r, ok, d in checks if not ok)
    print("  amend rejected:", reason[:110])
    repair = (user + "\n\nYour previous answer was REJECTED for: " + reason
              + "\nFix exactly those problems. If the problem is a missing "
                "action, give one they can do right now with what is already "
                "on their screen. Do not invent a file, a number or a "
                "document they did not mention. Return corrected JSON.")
    out2 = _get(agent, repair, SessionOutput, by_alias=True)
    if validate_amend(out2, context, session)[0]:
        print("  repair succeeded")
        return ensure_action(out2, session), context

    # Before discarding the turn: if the only fault was the missing action,
    # filling it from the session rescues the question and the note with it.
    # Throwing away a good question because the action was null is the same
    # dead end from the other side.
    for cand in (out2, out):
        fixed = ensure_action(cand, session)
        if fixed is not cand and validate_amend(fixed, context, session)[0]:
            print("  filled the missing action from the session")
            return fixed, context

    print("  repair failed too -> deterministic fallback")
    fallback = amend_fallback(session, message, context)
    return ensure_action(fallback, session), context


do_amend = do_session


def report_amend(name, session, message, out, context):
    print("=" * 70)
    print("SAID       :", message)
    print("WAS DOING  :", session.get("declared_intent"))
    print("-" * 70)
    print("KIND       :", out.get("kind"))
    print("NEXT       :", out.get("first_action"))
    for p in (out.get("plans") or []):
        print(f"             IF   {p.get('if')}")
        print(f"             THEN {p.get('then')}")
    print("NOTE       :", out.get("note"))
    print("-" * 70)
    passed, checks = validate_amend(out, context, session)
    for rule, ok, detail in checks:
        if not ok:
            print(f"  FAIL {rule}" + (f"  ({detail})" if detail else ""))
    n = sum(1 for _, ok, _ in checks if not ok)
    print(f"  {'ALL CHECKS PASS' if passed else str(n) + ' CHECK(S) FAILED'}"
          f"   [{len(checks)} rules]")
    print("=" * 70)
    return passed



# --------------------------------------------------------------------------
# SELFTEST - validator only, no model, no credentials. Covers the earlier
# null/question failures plus live semantic-grounding failures: invented
# generic files/content and self-completion contingencies.
# --------------------------------------------------------------------------

GOOD_PLANS = [
    {"if": "I have been stuck for five minutes",
     "then": "write down the exact error and open the docs page for it"},
    {"if": "I cannot find where I left off",
     "then": "search the file for the last thing I typed"},
]

# A session where the model, not the user, is the only source of "PDF".
# Turn 1's action invented it. Under the old context that invention became
# evidence from turn 2 onward.
LAUNDERED = {"declared_intent": "study for gate da", "notes": "", "plans": [],
             "first_action": "Open the PDF file",
             "history": [{"said": "how should i start", "kind": "continue",
                          "response": "Start from whatever you are studying in.",
                          "action": "Open the PDF file", "plans": [], "note": ""}]}

# Same shape, except they named it themselves.
NAMED = {"declared_intent": "study for gate da", "notes": "LA.pdf", "plans": [],
         "first_action": "Open LA.pdf and read the first heading",
         "history": [{"said": "will study from LA.pdf", "kind": "continue",
                      "response": "Open it and read the first heading.",
                      "action": "Open LA.pdf and read the first heading",
                      "plans": [], "note": ""}]}


def _out(**kw):
    base = {"kind": "continue", "response": "Here is the next step.",
            "first_action": "Open LA.pdf and read the first heading",
            "plans": [], "note": "Moving on."}
    base.update(kw)
    return base


SELFTESTS = [
    ("session: a real answer plus a step is the shape",
     lambda: validate_session(
         _out(response="Random variables come before estimation, so start there. "
                       "I cannot see your contents page though.",
              first_action="Open LA.pdf and type the chapter names here"),
         user_context(NAMED, "what topics to target first"), NAMED),
     True, None),

    ("session: no response is rejected",
     lambda: validate_session(_out(response=""),
                              user_context(NAMED, "what now"), NAMED),
     False, "response is a non-empty string"),

    ("session: a null action outside done is rejected",
     lambda: validate_session(_out(first_action=None),
                              user_context(NAMED, "what now"), NAMED),
     False, "carries an action unless the session is done"),

    ("session: done is the one kind with no next move",
     lambda: validate_session(
         {"kind": "done", "response": "That closes it out.", "first_action": None,
          "plans": [], "note": "Work finished."},
         user_context(NAMED, "thats it"), NAMED),
     True, None),

    ("session: old kind names still normalise",
     lambda: validate_session(_out(kind="progress"),
                              user_context(NAMED, "ok done that"), NAMED),
     True, None),

    ("session: a response that turns into a checklist is rejected",
     lambda: validate_session(
         _out(response="Do these:\n1. read the contents\n2. pick a chapter\n"
                       "3. start the exercises"),
         user_context(NAMED, "what topics"), NAMED),
     False, "response is not a list of steps"),

    ("session: a lecture is rejected on length",
     lambda: validate_session(
         _out(response=" ".join(["random variables matter here"] * 20)),
         user_context(NAMED, "what topics"), NAMED),
     False, "response under 70 words"),

    ("session: two questions in one response is rejected",
     lambda: validate_session(
         _out(response="Is the contents page open? And which chapter are you on?"),
         user_context(NAMED, "what topics"), NAMED),
     False, "response asks at most one question"),

    ("session: asking again what they just answered is rejected",
     lambda: validate_session(
         _out(response="Which file are you studying from?"),
         user_context(NAMED, "LA.pdf"),
         {**NAMED, "history": [{"said": "how should i start", "kind": "continue",
                                "response": "Which document are you studying from?",
                                "asked": "Which document are you studying from?",
                                "action": NAMED["first_action"], "plans": [],
                                "note": ""}]}),
     False, "does not ask the same question again"),

    ("session: a different question is still allowed",
     lambda: validate_session(
         _out(response="Which chapter are you on?"),
         user_context(NAMED, "opened it"),
         {**NAMED, "history": [{"said": "how should i start", "kind": "continue",
                                "response": "Which document are you studying from?",
                                "asked": "Which document are you studying from?",
                                "action": NAMED["first_action"], "plans": [],
                                "note": ""}]}),
     True, None),

    ("session: advance_session records the question from the response",
     lambda: (lambda h: (h["asked"] == "Which chapter are you on?", []))(
         advance_session(NAMED, "opened it",
                         _out(response="Good. Which chapter are you on?")
                         )["history"][-1]),
     True, None),

    ("session: a session stored by the old deployment still reads",
     lambda: validate_session(
         _out(response="Which file are you studying from?"),
         user_context(NAMED, "LA.pdf"),
         {**NAMED, "history": [{"said": "how should i start", "kind": "unclear",
                                "question": "Which document are you studying from?",
                                "action": None, "plans": [], "note": ""}]}),
     False, "does not ask the same question again"),

    ("session: praise in the response is rejected",
     lambda: validate_session(
         _out(response="Great progress, nice work. Keep going with the next heading."),
         user_context(NAMED, "typed it"), NAMED),
     False, "response has no praise, reassurance or time-accounting"),

    ("session: standalone good praise is rejected",
     lambda: validate_session(
         _out(response="Good. Now write one more sentence.",
              first_action="Type one more sentence in the Google Doc"),
         user_context(NAMED, "typed it"), NAMED),
     False, "response has no praise, reassurance or time-accounting"),

    ("session: substantive good is allowed",
     lambda: validate_session(
         _out(response="Use a good method for comparing the chapters.",
              first_action="Open LA.pdf and type the chapter names here"),
         user_context(NAMED, "what topics"), NAMED),
     True, None),

    ("session: an artifact the model invented earlier does not ground this turn",
     lambda: validate_session(
         _out(response="Open the PDF and read the first heading.",
              first_action="Open the PDF and read the first heading"),
         user_context(LAUNDERED, "ok what now"), LAUNDERED),
     False, "no artifact they never mentioned"),

    ("session: an artifact THEY named does ground it",
     lambda: validate_session(
         _out(response="The PDF is the place to start.",
              first_action="Open LA.pdf and read the first heading"),
         user_context(NAMED, "ok what now"), NAMED),
     True, None),

    ("session: an invented filename in revised_intent is rejected",
     lambda: validate_session(
         _out(kind="scope_change", revised_intent="rewrite the auth in middleware.py",
              first_action="Open the file you are changing and read the top"),
         user_context({"declared_intent": "fix the JWT bug", "history": []},
                      "actually the bug is in the middleware"),
         {"declared_intent": "fix the JWT bug", "history": []}),
     False, "revised_intent: no invented filenames"),

    ("session: invented generic file is rejected",
     lambda: validate_session(
         _out(response="Open the file with Q3 numbers.",
              first_action="Open the file with Q3 numbers"),
         user_context(
             {"declared_intent": "get Q3 numbers", "notes": "", "history": []},
             "where are they?"),
         {"declared_intent": "get Q3 numbers", "notes": "", "history": []}),
     False, "response: no artifact they never mentioned"),

    ("session: unsupported content noun is rejected",
     lambda: validate_session(
         _out(response="Use your Q3 data and type the first number you see.",
              first_action="Type the first number from your Q3 data"),
         user_context(
             {"declared_intent": "write the Q3 summary report",
              "notes": "Google Doc", "history": []},
             "cant start"),
         {"declared_intent": "write the Q3 summary report",
          "notes": "Google Doc", "history": []}),
     False, "response: no unsupported content assumptions"),

    ("session: self-completion trigger is rejected",
     lambda: validate_session(
         _out(
             first_action="Open pricing.pdf and scroll to the pricing section",
             plans=[{"if": "I finish updating one pricing item",
                     "then": "save the document and close the tab"}]
         ),
         user_context(
             {"declared_intent": "update the pricing section",
              "notes": "pricing.pdf, Google Doc, document, pricing numbers, tab",
              "history": []},
             "what next"),
         {"declared_intent": "update the pricing section",
          "notes": "pricing.pdf, Google Doc, document, pricing numbers, tab",
          "history": []}),
     False, "triggers do not use self-completion as the situation"),

    ("session: supplied content noun is allowed",
     lambda: validate_session(
         _out(response="Use the Q3 numbers you already mentioned.",
              first_action="Open the Q3 report and read the numbers"),
         user_context(
             {"declared_intent": "write the Q3 summary report",
              "notes": "Q3 numbers are in the Google Doc",
              "history": []},
             "opened it"),
         {"declared_intent": "write the Q3 summary report",
          "notes": "Q3 numbers are in the Google Doc",
          "history": []}),
     True, None),

    ("session: doc shorthand grounds document",
     lambda: validate_session(
         _out(response="Open the document and read the first heading.",
              first_action="Open the document and read the first heading"),
         user_context({"declared_intent": "write the Q3 summary",
                       "notes": "", "history": []}, "opened the doc"),
         {"declared_intent": "write the Q3 summary",
          "notes": "", "history": []}),
     True, None),

    ("session: singular number is unsupported unless user supplied it",
     lambda: validate_session(
         _out(response="Type the first number that comes to mind about Q3.",
              first_action="Type the first number that comes to mind about Q3"),
         user_context({"declared_intent": "write the Q3 summary report",
                       "notes": "Google Doc", "history": []},
                      "still cant start"),
         {"declared_intent": "write the Q3 summary report",
          "notes": "Google Doc", "history": []}),
     False, "response: no unsupported content assumptions"),

    ("session: bare praise is rejected",
     lambda: validate_session(
         _out(response="Great. Now run the test again.",
              first_action="Run the test again"),
         user_context({"declared_intent": "fix auth", "notes": "auth.py",
                       "history": []}, "changed the code"),
         {"declared_intent": "fix auth", "notes": "auth.py", "history": []}),
     False, "response has no praise, reassurance or time-accounting"),

    ("session: fallback does not invent a generic file",
     lambda: (
         session_fallback(
             {"declared_intent": "write the Q3 summary report",
              "notes": "", "first_action": "Open the file and read the first line",
              "plans": [], "history": []},
             "opened the doc",
             "write the Q3 summary report opened the doc"
         )["first_action"] != "Open the file and read the first line",
         []
     ),
     True, None),

    ("session: the fallback passes the rules it is meant to satisfy",
     lambda: validate_session(
         session_fallback(NAMED, "what ToC", user_context(NAMED, "what ToC")),
         user_context(NAMED, "what ToC"), NAMED),
     True, None),

    ("session: the fallback drops a prior action it cannot ground",
     lambda: [(session_fallback(LAUNDERED, "how should i start",
                                user_context(LAUNDERED, "how should i start")
                                )["first_action"] != "Open the PDF file", [])][0],
     True, None),

    ("session: ensure_action fills a null the model slipped through",
     lambda: (lambda o: (o["first_action"] == NAMED["first_action"]
                         and o.get("action_unchanged") is True, []))(
         ensure_action({"kind": "continue", "response": "Same step.",
                        "first_action": None}, NAMED)),
     True, None),

    ("session: ensure_action leaves a finished session alone",
     lambda: (lambda o: (o.get("first_action") is None, []))(
         ensure_action({"kind": "done", "first_action": None}, NAMED)),
     True, None),

    # --- the chat can talk like a person; the hard lines still hold ---
    ("session: explaining with everyday words passes",
     lambda: validate_session(
         _out(response="PCA finds the directions where the data varies most and "
                       "keeps only those. The top eigenvectors of the covariance "
                       "matrix are those directions, and the syllabus leans on it."),
         user_context(NAMED, "i dont get PCA"), NAMED),
     True, None),

    ("session: ordinary advice words pass in a reply",
     lambda: validate_session(
         _out(response="Probability next. You should get through both before ML, "
                       "and try to recall each definition before you check it."),
         user_context(NAMED, "what after linear algebra"), NAMED),
     True, None),

    ("session: 'great' describing the work passes",
     lambda: validate_session(
         _out(response="For formulas, yes. Flashcards are great for recall, less "
                       "so for problem-solving."),
         user_context(NAMED, "should i make flashcards"), NAMED),
     True, None),

    ("session: echoing their own 'still stuck' passes",
     lambda: validate_session(
         _out(response="If you're still stuck, paste the one line that loses you "
                       "and we'll untangle just that."),
         user_context(NAMED, "still stuck on the proof"), NAMED),
     True, None),

    ("session: a library call in an explanation passes",
     lambda: validate_session(
         _out(response="Call df.head() to see the first rows, e.g. to spot "
                       "missing values."),
         user_context(NAMED, "how do i look at a dataframe"), NAMED),
     True, None),

    ("session: praise opening a sentence is rejected",
     lambda: validate_session(
         _out(response="Read it once more. Nice, now the next heading."),
         user_context(NAMED, "read it"), NAMED),
     False, "response has no praise, reassurance or time-accounting"),

    ("session: describing the person is rejected",
     lambda: validate_session(
         _out(response="You seem tired, so keep the next part small."),
         user_context(NAMED, "ugh"), NAMED),
     False, "response talks about the work, not the person"),

    ("session: every form of a banned stem is caught",
     lambda: validate_session(
         _out(response="Procrastinating on proofs is common. Read one line."),
         user_context(NAMED, "cant start"), NAMED),
     False, "response has no praise, reassurance or time-accounting"),

    ("session: an invented file before a question is still caught",
     lambda: validate_session(
         _out(response="Open utils.py first. Which line fails?"),
         user_context(NAMED, "it errors"), NAMED),
     False, "response: no invented filenames"),

    ("session: an instruction to an unmentioned artifact is still rejected",
     lambda: validate_session(
         _out(response="Open the syllabus and read the ML part first.",
              first_action="Open whatever you are working in and type one word"),
         user_context({"declared_intent": "study for gate da", "notes": "",
                       "history": []}, "what first"),
         {"declared_intent": "study for gate da", "notes": "", "history": []}),
     False, "response: no artifact they never mentioned"),

    # --- found in the first live lifecycle run ---
    ("session: an opening 'Good catch.' is removed, not rejected",
     lambda: (lambda o: (o["response"].startswith("If the middleware")
                         and validate_session(o, user_context(NAMED, "it's the middleware"),
                                              NAMED)[0], []))(
         SessionOutput(kind="continue",
                       response="Good catch. If the middleware rejects the token "
                                "first, the fix belongs there.",
                       first_action="Open LA.pdf and read the first heading",
                       plans=[], note="Moved.").model_dump(by_alias=True)),
     True, None),

    ("session: praise that can't be removed cleanly is still rejected",
     lambda: validate_session(
         SessionOutput(kind="continue",
                       response="Good call on checking the middleware. Run the test.",
                       first_action="Open LA.pdf and read the first heading",
                       plans=[], note="Moved.").model_dump(by_alias=True),
         user_context(NAMED, "checked it"), NAMED),
     False, "response has no praise, reassurance or time-accounting"),

    ("session: the internal note never costs a good reply",
     lambda: (lambda o: (o["note"] == "Turn recorded."
                         and validate_session(o, user_context(NAMED, "still cant start"),
                                              NAMED)[0], []))(
         SessionOutput(kind="continue",
                       response="Type anything at all, even a placeholder, just to "
                                "get words on the page.",
                       first_action="Type one sentence into LA.pdf's notes margin",
                       plans=[], note="Still stuck on the first sentence for an hour."
                       ).model_dump(by_alias=True)),
     True, None),

    ("session: help that waits on them is rejected",
     lambda: validate_session(
         _out(response="Without the error message, I can't help you fix it. "
                       "Paste it here or look at it yourself."),
         user_context(NAMED, "nope"), NAMED),
     False, "response offers a way forward, not a condition"),

    ("session: words to type, in quotes, are not claims about their material",
     lambda: validate_session(
         _out(response="Put any words down, even a stand-in first line.",
              first_action="Type 'This report summarizes Q3 results' in the Google Doc"),
         user_context({"declared_intent": "write the Q3 summary report",
                       "notes": "Google Doc, due Friday.", "history": []}, "cant start"),
         {"declared_intent": "write the Q3 summary report",
          "notes": "Google Doc, due Friday.", "history": []}),
     True, None),

    ("session: a filename in quotes is still checked",
     lambda: validate_session(
         _out(first_action="Open 'utils.py' and read the top"),
         user_context(NAMED, "what now"), NAMED),
     False, "no invented filenames"),

    ("session: 'still stuck' as a plan trigger is a real situation",
     lambda: validate_session(
         _out(plans=[{"if": "I'm still stuck after five minutes",
                      "then": "paste the exact error into the chat"}]),
         user_context(NAMED, "ok"), NAMED),
     True, None),

    ("initiate: opening action naming an unmentioned PDF is rejected",
     lambda: validate_initiate(
         {"first_action": "Open the PDF file", "plans": GOOD_PLANS},
         "study for gate da"),
     False, "no artifact they never mentioned"),

    ("initiate: the same action once they have named it",
     lambda: validate_initiate(
         {"first_action": "Open LA.pdf and read the first heading", "plans": GOOD_PLANS},
         "study for gate da LA.pdf"),
     True, None),

    ("initiate: an invented question number is rejected",
     lambda: validate_initiate(
         {"first_action": "Open LA.pdf and start question 3", "plans": GOOD_PLANS},
         "study for gate da LA.pdf"),
     False, "no invented numbered reference"),

    ("initiate: a contents page is a fair bet, not an invention",
     lambda: validate_initiate(
         {"first_action": "Open LA.pdf and scroll to the table of contents",
          "plans": GOOD_PLANS},
         "study for gate da LA.pdf"),
     True, None),
]


def run_selftest():
    print("=" * 74)
    print("VALIDATOR SELFTEST - no model, no credentials")
    print("=" * 74)
    all_ok = True
    for name, run, expect_pass, expect_rule in SELFTESTS:
        passed, checks = run()
        failed = [r for r, ok, *_ in checks if not ok]
        ok = (passed == expect_pass)
        if ok and expect_rule:
            ok = any(expect_rule in r for r in failed)
        all_ok = all_ok and ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if failed:
            print(f"          rejected by: {'; '.join(failed[:3])}")
        elif not expect_pass:
            print("          nothing rejected it")
    print()
    print(f"  {'ALL SELFTESTS PASS' if all_ok else '*** SELFTEST FAILURES ***'}")
    print("=" * 74)
    return all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--initiate", action="store_true")
    ap.add_argument("--amend", action="store_true")
    ap.add_argument("--goal", default="fix the JWT refresh bug in auth.py")
    ap.add_argument("--answers", default="")
    ap.add_argument("--condition", default=None,
                    help="experiment arm from the assign action")
    ap.add_argument("--case", choices=list(AMEND_CASES), default="blocker")
    ap.add_argument("--message", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--lifecycle", choices=list(LIFECYCLES) + ["all"], default=None,
                    help="run a full multi-turn session, feeding state forward")
    ap.add_argument("--first-action", default=None)
    ap.add_argument("--region", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="validator rules only - no model call")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(0 if run_selftest() else 1)

    if args.dry_run:
        print("--- AMEND SYSTEM ---")
        print(AMEND_PROMPT)
        c = AMEND_CASES[args.case]
        print("--- USER ---")
        print(build_amend_user(c["session"], c["message"]))
        return

    if args.lifecycle:
        names = list(LIFECYCLES) if args.lifecycle == "all" else [args.lifecycle]
        results = [(n, run_lifecycle(n, LIFECYCLES[n], args)) for n in names]
        if len(results) > 1:
            print("\nSUMMARY")
            for n, ok in results:
                print(f"  {'PASS' if ok else 'FAIL'}  {n}")
        return

    if args.initiate:
        session, _ = do_initiate(args.goal, args.answers, args)
        if session:
            print("\nSESSION:")
            print(json.dumps(session, indent=2))
        return

    if not args.amend:
        print("Use --initiate or --amend.")
        return

    cases = list(AMEND_CASES.items()) if args.all else [(args.case, AMEND_CASES[args.case])]
    results = []
    for name, c in cases:
        msg = args.message or c["message"]
        print(f"\n### {name}")
        try:
            out, context = do_amend(c["session"], msg, args)
        except Exception as e:
            print(f"  call failed: {type(e).__name__}: {e}")
            continue
        results.append((name, report_amend(name, c["session"], msg, out, context)))

    if len(results) > 1:
        print("\nSUMMARY")
        for name, ok in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    main()
