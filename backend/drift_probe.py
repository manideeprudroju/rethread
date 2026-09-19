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



DRIFT_PROMPT = """\
Someone declared what they are working on. Judge whether ONE browser tab is \
part of that work.

You get the declared intent, the tab title, and the domain. You do NOT get \
page content. Judge from the title and domain only.

RELEVANT means the tab plausibly serves the declared work. It does not have \
to be the work itself - documentation, a reference, an error message, a tool, \
a data source and the artefact itself are all relevant.

NOT RELEVANT means the tab serves something else: messaging, social feeds, \
news, shopping, entertainment, or a different task entirely.

Judge the CONTENT, not the domain's reputation. A video explaining the exact \
technology they are working on is relevant. A video about something else on \
the same site is not. The same applies to Q&A sites, forums and wikis - the \
question is what this specific page is about.

One exception worth naming: researching HOW to do the task, rather than \
doing it, is NOT relevant. Reading about exam strategy is not studying. \
Reading about productivity systems is not working.

When you genuinely cannot tell, answer relevant. A wrong "not relevant" \
invents a lapse that did not happen; a wrong "relevant" only misses one.

Respond with ONLY a JSON object, no fences, no preamble:
{"relevant": true|false, "reason": "under 12 words"}
"""

# --------------------------------------------------------------------------
# Labelled set. `rel` is the correct answer. `hard` marks cases that exist
# specifically to punish domain-reputation shortcuts.
# --------------------------------------------------------------------------

JWT = "fix the JWT refresh bug in auth.py"
RPT = "write the Q3 summary report"
GATE = "study random variables for GATE DA"

CASES = [
    # --- clearly relevant ---
    (JWT, "auth.py - rethread - VS Code", "vscode.local", True, False),
    (JWT, "Token expiry not refreshing on 401 - Stack Overflow", "stackoverflow.com", True, False),
    (JWT, "PyJWT - decode options verify_exp", "pyjwt.readthedocs.io", True, False),
    (JWT, "jwt.io - JSON Web Tokens", "jwt.io", True, False),
    (RPT, "Q3 summary - Google Docs", "docs.google.com", True, False),
    (RPT, "Q3 revenue figures - Google Sheets", "docs.google.com", True, False),
    (GATE, "Probability notes - random variables.pdf", "drive.google.com", True, False),
    (GATE, "Random variables and probability distributions - Khan Academy", "khanacademy.org", True, False),

    # --- clearly not relevant ---
    (JWT, "(3) WhatsApp", "web.whatsapp.com", False, False),
    (JWT, "Amazon.in: mechanical keyboard", "amazon.in", False, False),
    (RPT, "Instagram", "instagram.com", False, False),
    (RPT, "Top headlines today", "timesofindia.indiatimes.com", False, False),
    (GATE, "India vs Australia live score", "cricbuzz.com", False, False),
    (GATE, "(12) Slack | general", "app.slack.com", False, False),

    # --- HARD: same domain, opposite verdicts ---
    (JWT, "JWT authentication explained in 10 minutes - YouTube", "youtube.com", True, True),
    (JWT, "I built a CLI in Rust and you won't believe - YouTube", "youtube.com", False, True),
    (JWT, "How to center a div - Stack Overflow", "stackoverflow.com", False, True),
    (JWT, "rethread/auth - GitHub", "github.com", True, True),
    (JWT, "GitHub Trending today", "github.com", False, True),
    (RPT, "Fiscal quarter - Wikipedia", "en.wikipedia.org", True, True),
    (RPT, "List of Marvel films - Wikipedia", "en.wikipedia.org", False, True),

    # --- HARD: researching HOW to do it, instead of doing it ---
    (GATE, "GATE DA 2027 preparation strategy - YouTube", "youtube.com", False, True),
    (RPT, "Best note-taking apps for 2026 - Medium", "medium.com", False, True),

    # --- HARD: genuinely ambiguous, bias says relevant ---
    (JWT, "ChatGPT", "chat.openai.com", True, True),
]


# --------------------------------------------------------------------------
# Session cache. In a real session you revisit the same handful of tabs
# constantly; only unseen contexts need a model call. This is the whole cost
# story for the highest-frequency path in the system.
# --------------------------------------------------------------------------

class RelevanceCache:
    def __init__(self):
        self.store = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(intent, title, domain):
        # Strip unread counters and volatile prefixes: "(3) WhatsApp" and
        # "(12) WhatsApp" are the same context.
        t = re.sub(r"^\(\d+\)\s*", "", title or "").strip().lower()
        return (intent.strip().lower(), domain.strip().lower(), t)

    def get(self, intent, title, domain):
        k = self.key(intent, title, domain)
        if k in self.store:
            self.hits += 1
            return self.store[k]
        self.misses += 1
        return None

    def put(self, intent, title, domain, verdict):
        self.store[self.key(intent, title, domain)] = verdict


# --------------------------------------------------------------------------
# CHURN - deterministic. No model. Staying on-intent but not progressing.
# Opposite shape to drift: drift leaves, churn stays and spins.
# --------------------------------------------------------------------------

CHURN_MIN_RETURNS = 3        # times back to the same context
CHURN_MAX_DWELL = 90         # seconds - each visit is short
CHURN_MIN_ELAPSED = 300      # seconds - but a lot of time has gone
CHURN_MAX_UNIQUE_DOMAINS = 2


def detect_churn(events):
    """events: [{title, domain, dwell_seconds}, ...] for a recent window.

    Returns {"churning": bool, "context": str|None, "returns": int,
             "elapsed": int, "evidence": str}
    """
    if not events:
        return {"churning": False, "context": None, "returns": 0,
                "elapsed": 0, "evidence": "no events"}

    counts, dwells = {}, {}
    for ev in events:
        k = RelevanceCache.key("", ev.get("title", ""), ev.get("domain", ""))
        counts[k] = counts.get(k, 0) + 1
        dwells.setdefault(k, []).append(int(ev.get("dwell_seconds", 0)))

    elapsed = sum(int(e.get("dwell_seconds", 0)) for e in events)
    domains = {(e.get("domain") or "").lower() for e in events}

    top = max(counts, key=lambda k: counts[k])
    returns = counts[top]
    short = all(d <= CHURN_MAX_DWELL for d in dwells[top])

    churning = (returns >= CHURN_MIN_RETURNS and short
                and elapsed >= CHURN_MIN_ELAPSED
                and len(domains) <= CHURN_MAX_UNIQUE_DOMAINS)

    if churning:
        ev_txt = (f"{returns} returns to the same context, "
                  f"all under {CHURN_MAX_DWELL}s, {elapsed}s elapsed, "
                  f"{len(domains)} domain(s)")
    else:
        why = []
        if returns < CHURN_MIN_RETURNS:
            why.append(f"only {returns} return(s)")
        if not short:
            why.append("at least one long dwell")
        if elapsed < CHURN_MIN_ELAPSED:
            why.append(f"only {elapsed}s elapsed")
        if len(domains) > CHURN_MAX_UNIQUE_DOMAINS:
            why.append(f"{len(domains)} domains - moving around")
        ev_txt = "; ".join(why)

    return {"churning": churning, "context": top[2] or None,
            "returns": returns, "elapsed": elapsed, "evidence": ev_txt}


CHURN_CASES = [
    ("rewriting the same intro", [
        {"title": "Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 70},
        {"title": "Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 45},
        {"title": "Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 80},
        {"title": "Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 60},
        {"title": "Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 75},
    ], True),
    ("working steadily in one doc", [
        {"title": "Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 900},
    ], False),
    ("real progress, moving between resources", [
        {"title": "auth.py - VS Code", "domain": "vscode.local", "dwell_seconds": 300},
        {"title": "PyJWT docs", "domain": "pyjwt.readthedocs.io", "dwell_seconds": 200},
        {"title": "auth.py - VS Code", "domain": "vscode.local", "dwell_seconds": 400},
    ], False),
    ("drift, not churn - many domains", [
        {"title": "reddit", "domain": "reddit.com", "dwell_seconds": 60},
        {"title": "YouTube", "domain": "youtube.com", "dwell_seconds": 80},
        {"title": "Instagram", "domain": "instagram.com", "dwell_seconds": 70},
        {"title": "Twitter", "domain": "x.com", "dwell_seconds": 90},
        {"title": "reddit", "domain": "reddit.com", "dwell_seconds": 60},
    ], False),
    ("three quick returns but barely any time", [
        {"title": "auth.py - VS Code", "domain": "vscode.local", "dwell_seconds": 20},
        {"title": "auth.py - VS Code", "domain": "vscode.local", "dwell_seconds": 25},
        {"title": "auth.py - VS Code", "domain": "vscode.local", "dwell_seconds": 30},
    ], False),
    ("unread counters should not split the context", [
        {"title": "(1) Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 70},
        {"title": "(2) Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 80},
        {"title": "(3) Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 75},
        {"title": "(4) Q3 summary - Google Docs", "domain": "docs.google.com", "dwell_seconds": 90},
    ], True),
]


# --------------------------------------------------------------------------

def call_bedrock(system, user, model_id, region):
    import boto3
    c = boto3.client("bedrock-runtime", region_name=region)
    r = c.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": 120, "temperature": 0},
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
        "max_tokens": 120,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepinfra.com/v1/openai/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def parse_output(raw):
    s = raw.strip()
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
    mm = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if mm:
        return json.loads(mm.group(0))
    raise ValueError("no JSON object found")


def build_user(intent, title, domain):
    return (f"Declared intent: {intent}\n"
            f"Tab domain: {domain}\n"
            f"Tab title: {title}")


def judge(intent, title, domain, args, cache):
    if cache is not None:
        hit = cache.get(intent, title, domain)
        if hit is not None:
            return hit, True

    user = build_user(intent, title, domain)
    try:
        if args.provider == "bedrock":
            model = args.model or "us.anthropic.claude-haiku-4-5-20251001-v1:0"
            raw = call_bedrock(DRIFT_PROMPT, user, model, args.region)
        else:
            model = args.model or "meta-llama/Llama-3.3-70B-Instruct"
            raw = call_deepinfra(DRIFT_PROMPT, user, model)
    except ImportError:
        print("boto3 not installed.  pip install boto3   (or --provider deepinfra)")
        sys.exit(1)
    except Exception as e:
        text = str(e)
        print(f"Model call failed ({type(e).__name__}): {text}")
        if "429" in text or "Throttling" in text:
            print("-> rate limited. Wait a moment and retry.")
        elif "AccessDenied" in text:
            print("-> model access, not credentials. Enable it in Bedrock.")
        sys.exit(1)

    try:
        out = parse_output(raw)
        verdict = bool(out.get("relevant"))
        reason = str(out.get("reason", ""))[:60]
    except Exception:
        # Unparseable: apply the bias. Never invent a lapse.
        verdict, reason = True, "unparseable, defaulted to relevant"

    if cache is not None:
        cache.put(intent, title, domain, (verdict, reason))
    return (verdict, reason), False


def score(results):
    """results: [(expected, got, hard)]"""
    tp = sum(1 for e, g, _ in results if e and g)
    tn = sum(1 for e, g, _ in results if not e and not g)
    fp = sum(1 for e, g, _ in results if not e and g)      # missed a drift
    fn = sum(1 for e, g, _ in results if e and not g)      # invented a lapse
    n = len(results)
    acc = (tp + tn) / n if n else 0
    # "Positive" = drift detected, since that is the costly call.
    d_prec = tn / (tn + fn) if (tn + fn) else 0
    d_rec = tn / (tn + fp) if (tn + fp) else 0
    return dict(tp=tp, tn=tn, fp=fp, fn=fn, n=n, acc=acc,
                drift_precision=d_prec, drift_recall=d_rec)


def run_churn():
    print("=" * 70)
    print("CHURN DETECTOR (deterministic - no model)")
    print("=" * 70)
    ok = True
    for name, events, expect in CHURN_CASES:
        r = detect_churn(events)
        good = (r["churning"] == expect)
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {name:<44} "
              f"churn={str(r['churning']):<5} {r['evidence']}")
    print(f"\n  {'ALL CHURN CASES CORRECT' if ok else '*** CHURN FAILURES ***'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="run the labelled set")
    ap.add_argument("--churn-only", action="store_true")
    ap.add_argument("--intent", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--domain", default="")
    ap.add_argument("--provider", choices=["bedrock", "deepinfra"], default="bedrock")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--model", default=None)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.env != ".env":
        load_env(args.env)

    if args.churn_only:
        sys.exit(0 if run_churn() else 1)

    if args.dry_run:
        print("--- SYSTEM ---")
        print(DRIFT_PROMPT)
        print("--- USER (first case) ---")
        print(build_user(*CASES[0][:3]))
        print(f"\n({len(CASES)} labelled cases, {len(CHURN_CASES)} churn cases, "
              "no model called)")
        run_churn()
        return

    cache = None if args.no_cache else RelevanceCache()

    if args.intent and args.title:
        (v, reason), _ = judge(args.intent, args.title, args.domain, args, cache)
        print("RELEVANT:", v, "|", reason)
        return

    if not args.all:
        print("Nothing to do. Use --all, --churn-only, or --intent/--title.")
        return

    results = []
    print("=" * 70)
    print("DRIFT RELEVANCE - labelled set")
    print("=" * 70)
    for intent, title, domain, expect, hard in CASES:
        (got, reason), cached = judge(intent, title, domain, args, cache)
        mark = "ok  " if got == expect else "MISS"
        tag = "hard" if hard else "    "
        print(f"  {mark} {tag} exp={str(expect):<5} got={str(got):<5} "
              f"{title[:44]:<44} {reason[:32]}")
        results.append((expect, got, hard))

    s = score(results)
    print("-" * 70)
    print(f"  accuracy        {s['acc']:.0%}  ({s['tp'] + s['tn']}/{s['n']})")
    print(f"  drift precision {s['drift_precision']:.0%}  "
          f"(of tabs called drift, how many really were)")
    print(f"  drift recall    {s['drift_recall']:.0%}  "
          f"(of real drift, how much was caught)")
    print(f"  invented lapses {s['fn']}   <- the costly error")
    print(f"  missed drift    {s['fp']}   <- the cheap error")

    hard_res = [(e, g) for e, g, h in results if h]
    if hard_res:
        hc = sum(1 for e, g in hard_res if e == g)
        print(f"  hard cases      {hc}/{len(hard_res)}")

    if cache:
        print(f"  cache           {cache.hits} hit / {cache.misses} miss "
              f"({len(cache.store)} contexts)")
    print("=" * 70)
    print("\nThe number that matters: invented lapses. Every one of those is a")
    print("phantom pattern in the experiment data that nobody can trace back.")

    run_churn()


if __name__ == "__main__":
    main()
