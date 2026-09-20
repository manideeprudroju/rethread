"""
guide_probe.py -- the ADHD guide chat inside the Friction panel.

A small conversational bot that answers from a fixed index of trusted
sources only. It searches that index, never the open web: every fact it
gives traces to a page someone chose, and every source link it shows is
real because the link comes from the index, not from the model.

THE SPLIT (same shape as every other agent here)
    SAFETY ROUTER  pure Python, runs first.  Self-harm -> fixed crisis reply
                                             (Tele-MANAS 14416, 112).
                                             Medication, "do I have ADHD?"
                                             -> warm redirect to a clinician.
                                             No model call on these.
    SEARCH         pure Python.              BM25 over the index below.
    REPLY          ONE model call.           Conversational, grounded in the
                                             sources found; validated, repaired
                                             once, then a plain fallback.

THE LINE (the project's): no diagnosis or assessment of the user, no
medication content, no scores. The bot talks about ADHD and about the
work, never about what the person "has".

Usage:
    python guide_probe.py        # runs every check, no model needed
"""

import json
import math
import re
from collections import Counter

# --------------------------------------------------------------------------
# THE INDEX. Written in our own words from the pages linked, each checked
# against its source. Add an entry: id, title, org, url, tags, text.
# `url` None marks Rethread's own explainers.
# --------------------------------------------------------------------------

NIMH = "https://www.nimh.nih.gov/health/topics/attention-deficit-hyperactivity-disorder-adhd"
NHS_ADULTS = "https://www.nhs.uk/conditions/adhd-adults/"
TELE_MANAS = "https://telemanas.mohfw.gov.in/"
ERSS_112 = "https://112.gov.in/"
GOLLWITZER_URL = "https://www.sciencedirect.com/science/chapter/bookseries/abs/pii/S0065260106380021"
WEBB_URL = "https://pubmed.ncbi.nlm.nih.gov/22582737/"
SCHWEIGER_URL = "https://pubmed.ncbi.nlm.nih.gov/19210061/"
KUSHLEV_URL = "https://www.sciencedirect.com/science/article/abs/pii/S0747563214005810"

INDEX = [
    {
        "id": "what_is_adhd", "title": "What ADHD is", "org": "NIMH", "url": NIMH,
        "tags": "what is adhd definition meaning inattention hyperactivity impulsivity "
                "neurodevelopmental adult adults grown up childhood",
        "text": "NIMH describes ADHD as a neurodevelopmental condition with ongoing "
                "patterns of inattention (trouble keeping on task or staying "
                "organised), hyperactivity (restlessness, talking a lot) and "
                "impulsivity (interrupting, finding it hard to wait). It starts in "
                "childhood and often carries on into adulthood.",
    },
    {
        "id": "treatment_overview", "title": "How ADHD is treated", "org": "NIMH", "url": NIMH,
        "tags": "treatment therapy therapies cbt cognitive behavioural behavioral help "
                "options mindfulness sleep apps support what helps",
        "text": "NIMH lists medication alongside psychosocial approaches such as "
                "cognitive behavioural therapy. Researchers are also studying "
                "mindfulness, cognitive training, sleep-focused approaches and "
                "support delivered through apps. Which mix fits someone is decided "
                "with a clinician.",
    },
    {
        "id": "adult_signs", "title": "ADHD in adults", "org": "NHS", "url": NHS_ADULTS,
        "tags": "adults adult signs symptoms forgetful distracted organise organize time "
                "finish tasks instructions restless decisions work",
        "text": "The NHS lists signs in adults such as being easily distracted or "
                "forgetful, finding it hard to organise time, and finding it hard to "
                "follow instructions or finish tasks, plus restlessness and quick "
                "decisions. Most people have some of both the inattentive and the "
                "hyperactive-impulsive kind. Only a specialist assessment can say "
                "whether it's ADHD.",
    },
    {
        "id": "getting_assessed", "title": "Getting assessed as an adult", "org": "NHS",
        "url": NHS_ADULTS,
        "tags": "assessment assessed diagnosis diagnosed how to get gp doctor psychiatrist "
                "specialist referral refer waiting evaluation evaluated",
        "text": "In the UK route the NHS describes, you start with a GP, who can refer "
                "you to a mental health professional who specialises in ADHD, usually "
                "a psychiatrist. The assessment looks at your history and how things "
                "affect different parts of your life. Waiting times vary a lot.",
    },
    {
        "id": "help_india", "title": "Getting help in India", "org": "Tele-MANAS",
        "url": TELE_MANAS,
        "tags": "india indian helpline tele-manas telemanas counsellor counselor psychiatrist "
                "referral where get help free call phone support talk someone",
        "text": "Tele-MANAS is the Government of India's free, 24x7 mental health "
                "helpline: 14416 or 1-800-891-4416, in English and 20 regional "
                "languages. A trained counsellor listens and can connect you to a "
                "mental health professional, such as a psychiatrist, for further care.",
    },
    {
        "id": "emergency_india", "title": "Emergencies in India", "org": "ERSS-112",
        "url": ERSS_112,
        "tags": "emergency danger urgent unsafe 112 police ambulance",
        "text": "112 is India's single emergency number for police, fire and ambulance.",
        # Only surfaces when the question is about an emergency, not for every
        # question that mentions India.
        "requires": "emergency danger urgent unsafe 112 police ambulance fire",
    },
    {
        "id": "if_then_plans", "title": "If-then plans", "org": "Gollwitzer & Sheeran, 2006",
        "url": GOLLWITZER_URL,
        "tags": "start starting begin getting started procrastinate procrastination stuck "
                "initiation if then plan plans cue cues habit goal goals follow through",
        "text": "A meta-analysis of 94 tests found that if-then plans ('if X happens, "
                "then I'll do Y') had a medium-to-large effect on reaching goals "
                "(d = 0.65). Tying a specific moment to a specific action hands the "
                "start to a cue instead of a decision in the moment.",
    },
    {
        "id": "reframing", "title": "Changing how a task reads",
        "org": "Webb, Miles & Sheeran, 2012", "url": WEBB_URL,
        "tags": "dread heavy overwhelmed overwhelm stress stressed anxious anxiety nervous "
                "feelings feeling emotions reframe reappraise worry worried pressure",
        "text": "Across 306 experiments, changing how you read the situation itself "
                "worked better than other ways of handling feelings (d = 0.36). "
                "Briefly turning attention elsewhere helped a little (d = 0.27), "
                "while concentrating on the feeling didn't help (d = -0.26).",
    },
    {
        "id": "if_then_feelings", "title": "If-then plans for strong feelings",
        "org": "Schweiger Gallo et al., 2009", "url": SCHWEIGER_URL,
        "tags": "heavy dread anxious anxiety nervous calm fear feelings emotions if then "
                "plan panic upset overwhelmed",
        "text": "In lab studies, people who added an if-then plan ('if I see X, then "
                "I'll stay calm') reduced their fear and disgust reactions more than "
                "people who only set the goal to stay calm.",
    },
    {
        "id": "batch_checking", "title": "Checking messages in batches",
        "org": "Kushlev & Dunn, 2015", "url": KUSHLEV_URL,
        "tags": "email emails messages inbox notifications checking check phone whatsapp "
                "slack interruptions interrupted stress batch rechecking recheck refresh",
        "text": "124 adults spent one week checking email only three times a day and "
                "another week checking freely. The limited week brought lower daily "
                "stress.",
    },
    {
        "id": "point_of_performance", "title": "Help where the work happens",
        "org": "Rethread", "url": None,
        "tags": "why rethread design barkley point of performance structure external "
                "reminders knowing doing know what to do",
        "text": "Rethread follows Barkley's point-of-performance idea: ADHD is less "
                "about not knowing what to do than about doing it at the moment it "
                "matters. So support works best as structure at that moment: a cue, a "
                "first step, a visible plan, rather than more advice.",
    },
    {
        "id": "friction_check", "title": "What the friction check does",
        "org": "Rethread", "url": None,
        "tags": "friction check panel circling slow start rechecking spread pattern plan "
                "experiment how does this work what is this",
        "text": "Each day the friction check reads your tab titles and times from recent "
                "sessions, looks for four patterns (slow start, circling, rechecking, "
                "spread) and turns the most frequent one into an if-then plan for your "
                "next session. It tries two kinds of support, eight sessions each, and "
                "only calls one better after an exact statistical test.",
    },
    {
        "id": "privacy", "title": "What Rethread reads and keeps", "org": "Rethread",
        "url": None,
        "tags": "privacy private data what do you read store see tabs titles content "
                "share clinician delete export",
        "text": "Rethread reads tab titles, site names and times, never page content. "
                "The friction check stores nothing on the server. The data is yours, "
                "and you can choose to share it with your own clinician.",
    },
    {
        "id": "reentry", "title": "Picking back up after an interruption",
        "org": "Rethread", "url": None,
        "tags": "interrupted interruption lost track where was i resume pick back up "
                "reentry re-entry come back forgot what i was doing",
        "text": "When you come back to a session, the Re-entry panel rebuilds what you "
                "were doing from that session's tabs and hands you the next step, "
                "plus the if-then plans you set.",
    },
]

INDEX_BY_ID = {e["id"]: e for e in INDEX}

CARE_LINE = ("Support for the work, not treatment. If it's more than the work, "
             "Tele-MANAS is free and open 24x7: 14416.")

# --------------------------------------------------------------------------
# SEARCH -- BM25 over title + tags + text. Tiny index, so no library.
# --------------------------------------------------------------------------

STOP = set("""a an the and or but if then so of to in on at for with from by is are
was were be been am i me my you your it its this that these those do does did
can could should would will how what why when where who which there their they
about any some just really very get got have has had not no yes please ok okay
never always ever keep kept still even much many lot lots like need needs want
wants cant can't dont don't doesnt doesn't wont won't im i'm ive i've make made
thing things something anything everything every few best good better bad worst
one two go going gone way ways kind sort bit maybe also too hard easy
difficult tough
""".split())


def _tokens(text):
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", str(text or "").lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        # crude stemming, enough for "starting"/"start", "checks"/"check"
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


_DOCS = [_tokens(" ".join([e["title"], e["title"], e["tags"], e["text"]])) for e in INDEX]
_DF = Counter(t for d in _DOCS for t in set(d))
_AVG = sum(len(d) for d in _DOCS) / len(_DOCS)
K1, B = 1.4, 0.75
MIN_SCORE = 1.2        # below this, nothing in the index is about the question


def search(query, k=3):
    """-> [(entry, score)], best first, only entries that clear MIN_SCORE."""
    q = _tokens(query)
    n = len(_DOCS)
    scored = []
    qset = set(q)
    for e, d in zip(INDEX, _DOCS):
        if e.get("requires") and not qset & set(_tokens(e["requires"])):
            continue
        tf = Counter(d)
        s = 0.0
        for t in set(q):
            if t not in tf:
                continue
            idf = math.log(1 + (n - _DF[t] + 0.5) / (_DF[t] + 0.5))
            s += idf * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * len(d) / _AVG))
        if s >= MIN_SCORE:
            scored.append((e, round(s, 2)))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


def source_chip(e):
    return {"id": e["id"], "title": e["title"], "org": e["org"], "url": e["url"]}


# --------------------------------------------------------------------------
# SAFETY ROUTER -- runs before anything else. Fixed replies, no model.
# --------------------------------------------------------------------------

CRISIS_RE = re.compile(
    r"\b(suicid\w*|kill (?:my ?self|me)|end (?:my|it all)\b|end my life|"
    r"self[- ]?harm\w*|hurt(?:ing)? my ?self|cut(?:ting)? my ?self|"
    r"(?:want|wanna|going) to die|don'?t want to (?:live|be alive|be here)|"
    r"no reason to live|better off dead|can'?t go on)",
    re.I,
)
MEDS_RE = re.compile(
    r"\b(medic(?:ation|ine)s?|meds|pills?|dos(?:e|es|age|ing)|\d+\s?mg|prescri\w*|"
    r"stimulants?|adderall|ritalin|methylphenidate|concerta|atomoxetine|strattera|"
    r"vyvanse|lisdexamfetamine|amphetamines?|dexamfetamine|guanfacine|modafinil|"
    r"bupropion|wellbutrin|side ?effects?)\b",
    re.I,
)
SELF_DX_RE = re.compile(
    r"\b(do i have|have i got|am i|could i have|might i have|i think i have|"
    r"is it|is this|does (?:this|that) (?:mean|sound like))\b[^?.!]{0,40}\badhd\b"
    r"|\b(diagnose|test|screen|assess) me\b|\bsounds? like (?:i have )?adhd\b",
    re.I,
)

CRISIS_REPLY = (
    "I'm really glad you said something. This is bigger than a work tool can help "
    "with, and you deserve to talk to a person right now. In India you can call "
    "Tele-MANAS on 14416 or 1-800-891-4416, free and open 24x7. If you're in "
    "immediate danger, call 112."
)
MEDS_REPLY = (
    "That one's for a psychiatrist or your doctor. Medication questions depend on "
    "your health history, so I stay out of them. If you don't have someone to ask, "
    "Tele-MANAS (14416) can point you to a professional. I'm happy to talk "
    "strategies for the work side any time."
)
SELF_DX_REPLY = (
    "I can't tell that from here, and nobody can from a chat. Only a proper "
    "assessment with a specialist, usually a psychiatrist, can. If you're in India, "
    "Tele-MANAS (14416) can connect you with one. Meanwhile, the patterns Rethread "
    "records are yours to show them if you want."
)


def route(message):
    """-> ("crisis" | "medication" | "self_assessment" | None)."""
    m = str(message or "")
    if CRISIS_RE.search(m):
        return "crisis"
    if MEDS_RE.search(m):
        return "medication"
    if SELF_DX_RE.search(m):
        return "self_assessment"
    return None


FIXED = {
    "crisis": (CRISIS_REPLY, ["help_india", "emergency_india"]),
    "medication": (MEDS_REPLY, ["help_india"]),
    "self_assessment": (SELF_DX_REPLY, ["getting_assessed", "help_india"]),
}

# --------------------------------------------------------------------------
# THE ONE MODEL CALL
# --------------------------------------------------------------------------

GUIDE_PROMPT = """\
You're the guide inside Rethread, a work tool for adults with ADHD. Talk like a \
friend who knows the research: warm, direct, plain words, contractions. You're \
chatting, not writing an article.

HOW TO ANSWER
- One to four sentences. No lists, no headings.
- Facts come ONLY from the SOURCES below. If they don't cover the question, say \
so in a few words and offer what you can help with: what ADHD is, getting \
assessed, strategies for starting, staying with and coming back to work, and how \
Rethread works.
- Turn research into one small thing they could try, in the words of their \
situation. You can ask one short follow-up question when it would help.
- Don't write links or URLs; the sources are shown under your reply.
- Small talk ("hi", "thanks") gets a short friendly reply and nothing more.

HARD LINES
- Never say or suggest that the person has ADHD or any condition, and never \
assess them. Talk about ADHD in general and about the work.
- Nothing about medication, doses or prescriptions.
- No praise or pep talk ("great question", "you've got this"), no shame words \
(lazy, willpower, discipline), no promises that something will work.

Reply with the message only: plain text, no JSON, no quotes around it.
"""

HISTORY_TURNS = 6


def build_user(message, history, hits):
    lines = []
    if hits:
        lines.append("SOURCES:")
        for e, _ in hits:
            lines.append(f"- [{e['org']}] {e['title']}: {e['text']}")
    else:
        lines.append("SOURCES: none match this message.")
    turns = [h for h in (history or []) if isinstance(h, dict)][-HISTORY_TURNS:]
    if turns:
        lines.append("\nCONVERSATION SO FAR:")
        for h in turns:
            who = "They" if h.get("role") == "user" else "You"
            lines.append(f"{who}: {str(h.get('text') or '')[:400]}")
    lines.append(f"\nTHEY JUST SAID: {str(message)[:600]}")
    return "\n".join(lines)


def clean_reply(raw):
    s = re.sub(r"<think>.*?</think>", "", str(raw or ""), flags=re.DOTALL).strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
    # A model that answers in JSON anyway: take the text out.
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            s = str(obj.get("reply") or obj.get("response") or obj.get("text") or "")
        except (json.JSONDecodeError, AttributeError):
            pass
    s = s.strip().strip('"').strip()
    return re.sub(r"\n{2,}", "\n", s)


PRAISE = ["great question", "good question", "great job", "well done", "nice work",
          "you've got this", "you can do this", "proud of you", "don't worry",
          "no worries", "keep it up", "hang in there", "you got this"]
SHAME = ["lazy", "willpower", "discipline", "try harder", "should have"]
ASSESSING = re.compile(
    r"\byou(?:'re| are)?\s+(?:probably |might |may |could |likely |definitely )?"
    r"(?:have|has|got)\s+(?:adhd|add)\b|\bsounds? like (?:you have )?adhd\b|"
    r"\byour (?:adhd|symptoms|condition|disorder)\b|\byou seem\b|\byou tend to\b|"
    r"\badhd brain\b|\bdiagnos\w*\b",
    re.I,
)
URL_RE = re.compile(r"https?://|www\.|\.(?:com|org|gov|in|uk)\b", re.I)
LIST_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
MAX_WORDS = 90


def validate_reply(text):
    t = str(text or "").strip()
    low = t.lower()
    checks = [
        ("not empty", bool(t), ""),
        (f"under {MAX_WORDS} words", len(t.split()) <= MAX_WORDS, f"{len(t.split())} words"),
        ("at most one question", t.count("?") <= 1, f"{t.count('?')}"),
        ("not a list", len(LIST_RE.findall(t)) < 2, ""),
        ("no links in the text", not URL_RE.search(t), ""),
    ]
    m = MEDS_RE.search(t)
    checks.append(("nothing about medication", m is None, m.group(0) if m else ""))
    m = ASSESSING.search(t)
    checks.append(("never assesses the person", m is None, m.group(0) if m else ""))
    hits = [p for p in PRAISE + SHAME if re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", low)]
    checks.append(("no praise, pep talk or shame", not hits, ", ".join(hits[:3])))
    return all(ok for _, ok, _ in checks), checks


def _reason(checks):
    return "; ".join(r + (f" ({d})" if d else "") for r, ok, d in checks if not ok)


def fallback(hits):
    """Deterministic, honest, still conversational."""
    if hits:
        e = hits[0][0]
        first = re.split(r"(?<=[.!?])\s+", e["text"])[0]
        lead = "Here's the short version" if e["org"] == "Rethread" else f"Here's what {e['org']} says"
        return f"{lead}: {first}"
    return ("I don't have a trusted source on that one. I can help with what ADHD "
            "is, getting assessed, and ways to start, stay with and come back to "
            "work.")


def run_guide(body, call_model=None):
    """body: {message, history: [{role: "user"|"assistant", text}]}.

    -> {reply, sources: [{id,title,org,url}], kind, care_line}
       kind: "answer" | "crisis" | "medication" | "self_assessment" | "fallback"
    """
    message = str(body.get("message") or "").strip()[:1000]
    history = body.get("history") or []
    if not message:
        raise ValueError("message is required")

    kind = route(message)
    if kind:
        reply, ids = FIXED[kind]
        return {"reply": reply, "kind": kind, "care_line": CARE_LINE,
                "sources": [source_chip(INDEX_BY_ID[i]) for i in ids]}

    # Search on the message, plus the last thing they said if the message is
    # a short follow-up ("why?", "how?") that carries no topic of its own.
    hits = search(message)
    if not hits:
        prev = next((str(h.get("text") or "") for h in reversed(history)
                     if isinstance(h, dict) and h.get("role") == "user"), "")
        if prev and len(_tokens(message)) <= 3:
            hits = search(prev + " " + message)
    sources = [source_chip(e) for e, _ in hits]
    user = build_user(message, history, hits)

    if call_model is not None:
        try:
            out = clean_reply(call_model(GUIDE_PROMPT, user))
            ok, checks = validate_reply(out)
            if ok:
                return {"reply": out, "kind": "answer", "sources": sources,
                        "care_line": CARE_LINE}
            reason = _reason(checks)
            print(f"[guide] attempt 1 failed: {reason}")
            out2 = clean_reply(call_model(
                GUIDE_PROMPT, user + "\n\nYour previous reply was REJECTED for: " + reason
                + "\nWrite it again without those problems. Keep it short and friendly."))
            if validate_reply(out2)[0]:
                return {"reply": out2, "kind": "answer", "sources": sources,
                        "care_line": CARE_LINE}
            print("[guide] repair failed -> fallback")
        except Exception as e:
            print(f"[guide] model call raised {type(e).__name__}: {e}")
    return {"reply": fallback(hits), "kind": "fallback", "sources": sources,
            "care_line": CARE_LINE}


# --------------------------------------------------------------------------
# TESTS -- no model needed. Run: python guide_probe.py
# --------------------------------------------------------------------------

class FakeModel:
    def __init__(self, replies):
        self.replies, self.calls, self.last_user = list(replies), 0, ""

    def __call__(self, system, user):
        self.calls += 1
        self.last_user = user
        r = self.replies.pop(0) if self.replies else "Sure."
        if isinstance(r, Exception):
            raise r
        return r


def run_checks():
    ok_all = True

    def check(name, cond, info=""):
        nonlocal ok_all
        ok_all &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{info}]" if info and not cond else ""))

    print("=" * 74)
    print("SEARCH -- trusted index only")
    print("=" * 74)
    top = lambda q: (search(q) or [({"id": None}, 0)])[0][0]["id"]
    for q, want in [
        ("what even is adhd", "what_is_adhd"),
        ("how do I get diagnosed as an adult", "getting_assessed"),
        ("I can never start my assignments", "if_then_plans"),
        ("I keep checking my email every few minutes", "batch_checking"),
        ("the report feels so heavy I dread opening it", "reframing"),
        ("where can I get help in india", "help_india"),
        ("how do I get assessed in India", "getting_assessed"),
        ("what does the friction check do", "friction_check"),
        ("what data do you store about my tabs", "privacy"),
        ("I got interrupted and forgot what I was doing", "reentry"),
    ]:
        check(f"'{q}' -> {want}", top(q) == want, top(q))
    check("112 only surfaces for emergencies",
          all(e["id"] != "emergency_india" for e, _ in search("how do I get assessed in India"))
          and search("is there an emergency number in india")[0][0]["id"] == "emergency_india")
    check("off-topic finds nothing", search("best pizza in bangalore") == [],
          search("best pizza in bangalore"))
    check("every index entry has title, org, text; urls are https",
          all(e["title"] and e["org"] and e["text"] and (e["url"] is None or
              e["url"].startswith("https://")) for e in INDEX))

    print()
    print("=" * 74)
    print("SAFETY ROUTER -- fixed replies, zero model calls")
    print("=" * 74)
    m = FakeModel([])
    for msg, kind in [
        ("sometimes I just want to die", "crisis"),
        ("I've been thinking about hurting myself", "crisis"),
        ("should I up my adderall dose?", "medication"),
        ("is 20 mg of ritalin a lot", "medication"),
        ("what are the side effects of stimulants", "medication"),
        ("do I have ADHD?", "self_assessment"),
        ("I forget everything, does that mean ADHD?", "self_assessment"),
        ("can you test me", "self_assessment"),
    ]:
        out = run_guide({"message": msg}, m)
        check(f"'{msg}' -> {kind}", out["kind"] == kind, out["kind"])
    check("no model call on any of them", m.calls == 0, m.calls)
    out = run_guide({"message": "i want to die"}, m)
    check("crisis reply gives 14416 and 112, with both sources",
          "14416" in out["reply"] and "112" in out["reply"]
          and [s["id"] for s in out["sources"]] == ["help_india", "emergency_india"])
    check("'how do I get assessed' is info, not a redirect",
          route("how do I get assessed for adhd as an adult") is None)
    check("'what is adhd' is not a self-assessment", route("what is adhd") is None)
    check("fixed replies pass the reply validator",
          all(validate_reply(r)[0] for r, _ in FIXED.values() if r != MEDS_REPLY)
          and validate_reply(MEDS_REPLY)[1][5][1] is False)   # it names medication on purpose

    print()
    print("=" * 74)
    print("REPLY -- one call, guarded")
    print("=" * 74)
    good = ("Starting is the hard part, so hand it to a cue: 'if it's 4 pm and the "
            "doc isn't open, I open it and type one line.' Which task is it?")
    m = FakeModel([good])
    out = run_guide({"message": "I can never start my assignments",
                     "history": [{"role": "user", "text": "hi"},
                                 {"role": "assistant", "text": "Hey! What's up?"}]}, m)
    check("grounded answer, one call", out["kind"] == "answer" and m.calls == 1
          and out["reply"] == good)
    check("sources come from the index, not the model",
          out["sources"] and out["sources"][0]["id"] == "if_then_plans"
          and out["sources"][0]["url"] == GOLLWITZER_URL)
    check("prompt carries the sources and the conversation",
          "SOURCES:" in m.last_user and "Gollwitzer" in m.last_user
          and "CONVERSATION SO FAR" in m.last_user)
    for bad, rule in [
        ("Sounds like ADHD to me. Try one small step.", "never assesses the person"),
        ("You probably have ADHD, so start small.", "never assesses the person"),
        ("Ask about a stimulant like methylphenidate.", "nothing about medication"),
        ("Great question! Start with one line.", "no praise, pep talk or shame"),
        ("It's not about willpower, it's about cues.", "no praise, pep talk or shame"),
        ("Read more at https://www.nimh.nih.gov", "no links in the text"),
        ("- open it\n- type a line\n- repeat", "not a list"),
        ("Why? What? When?", "at most one question"),
    ]:
        ok, ch = validate_reply(bad)
        check(f"rejects: {bad[:40]!r}", not ok and rule in _reason(ch), _reason(ch))
    m = FakeModel(["You probably have ADHD.", good])
    out = run_guide({"message": "I can never start my assignments"}, m)
    check("bad reply repaired on the second call", out["kind"] == "answer" and m.calls == 2)
    m = FakeModel(["You probably have ADHD.", "Sounds like ADHD."])
    out = run_guide({"message": "I can never start my assignments"}, m)
    check("two bad replies -> honest fallback from the top source",
          out["kind"] == "fallback" and "Gollwitzer" in out["reply"], out["reply"])
    m = FakeModel([RuntimeError("throttled")])
    out = run_guide({"message": "what is adhd"}, m)
    check("model down -> fallback, still sourced",
          out["kind"] == "fallback" and out["sources"][0]["id"] == "what_is_adhd")
    out = run_guide({"message": "best pizza in bangalore"}, None)
    check("nothing found + no model -> says what it can help with",
          "trusted source" in out["reply"] and out["sources"] == [])
    m = FakeModel(["It works because the cue does the deciding for you."])
    out = run_guide({"message": "why?", "history": [
        {"role": "user", "text": "I can never start my assignments"},
        {"role": "assistant", "text": "Try an if-then plan."}]}, m)
    check("a bare 'why?' follow-up searches the previous question",
          out["sources"] and out["sources"][0]["id"] == "if_then_plans", out["sources"])
    check("JSON-wrapped reply is unwrapped", clean_reply('{"reply": "Hi there."}') == "Hi there.")
    check("empty message is a 400, not a model call",
          _raises(lambda: run_guide({"message": "  "}, FakeModel([]))))

    print()
    print(f"  {'ALL CHECKS PASS' if ok_all else '*** FAILURES ABOVE ***'}")
    print("=" * 74)
    return ok_all


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(0 if run_checks() else 1)
