import argparse
import json
import os
import re
import sys


def load_env(path=".env"):
    """Load .env into os.environ.

    Uses python-dotenv when available, otherwise parses the file itself so
    the script keeps its zero-dependency promise. Existing environment
    variables always win, which matches dotenv's default behaviour.
    """
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
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


load_env()

# --------------------------------------------------------------------------
# Grounding. Kept deliberately short - this is the whole "knowledge base".
# Barkley: externalise at the point of performance, don't exhort.
# Gollwitzer: contingent if-then format.
# Gross: reappraisal, never suppression. No shame framing.
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You reconstruct a lost work session for someone with ADHD who has just \
returned after being away.

You get their declared intent and a timeline of browser activity: tab titles, \
domains, timestamps, dwell times. You do NOT get page content. Work only from \
what is there. Never use knowledge about the topics in the titles - your only \
evidence is the timeline itself.

STEP 1 - DECIDE IF THERE IS A THREAD. Do this before anything else.

A work session leaves traces of a PROBLEM BEING WORKED, not a topic being \
read. Look for at least two of:
  - returning to the same resource more than once
  - a tab that is an editor, repo, terminal, doc being written, or form
  - error-shaped or question-shaped titles (a specific failure, a "how do I")
  - movement between a tool and a reference about that tool
  - the activity plainly matching the declared intent

These are NOT a thread, no matter how coherent they look:
  - browsing several pages on one topic (films, products, news, people)
  - reading, scrolling, watching, shopping
  - a single short errand that is finished
  - activity unrelated to the declared intent

If the evidence is not there, set "session_found": false and set every other \
field to null. An honest "there is no thread here" is a CORRECT and valuable \
answer. Inventing a plausible project from browsing is the worst thing you \
can do - this person cannot reliably check your version of their last hour \
against their own memory, so a confident wrong answer actively harms them. \
When in doubt, return false.

STEP 2 - ONLY IF session_found IS true, reconstruct:
  - doing: the specific problem being worked, not the topic
  - why: what made it a problem
  - key_tabs: the 2-4 tabs that carried the work. Drift tabs must not appear
  - next_action: ONE physical act, startable in under ten seconds. \
"Open auth.py and check the decode call" is right. "Continue debugging" is \
useless. It must require no decision.

Every claim must trace to specific tabs. If you cannot point at the tabs that \
support a statement, do not make it.

Hard rules:
- Never mention the drift, the lost time, or their focus. No judgement, no \
encouragement, no motivational language. They already know.
- Never diagnose, score, or characterise the person. Describe the work only.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{"session_found": true|false, "evidence": "which signals you found, or which \
were missing", "doing": "..."|null, "why": "..."|null, "key_tabs": [...]|null, \
"next_action": "..."|null}
"""

# Negative case: coherent topical browsing with no work thread.
# This is the shape that produced a confabulated "project" in testing.
SAMPLE_BROWSING = {
    "declared_intent": "",
    "events": [
        {"ts": "2026-09-13T21:02:00Z", "title": "The Avengers (2012) - IMDb",
         "domain": "imdb.com", "dwell_seconds": 240},
        {"ts": "2026-09-13T21:06:00Z",
         "title": "Captain America: The Winter Soldier (2014) - IMDb",
         "domain": "imdb.com", "dwell_seconds": 300},
        {"ts": "2026-09-13T21:11:00Z", "title": "Your ratings history - IMDb",
         "domain": "imdb.com", "dwell_seconds": 180},
        {"ts": "2026-09-13T21:14:00Z", "title": "MCU timeline explained",
         "domain": "reddit.com", "dwell_seconds": 420},
        {"ts": "2026-09-13T21:21:00Z", "title": "(2) WhatsApp",
         "domain": "web.whatsapp.com", "dwell_seconds": 300},
    ],
}

SAMPLE = {
    "declared_intent": "fix the JWT refresh bug",
    "events": [
        {"ts": "2026-09-13T09:02:00Z", "title": "auth.py - rethread - VS Code",
         "domain": "vscode.local", "dwell_seconds": 420},
        {"ts": "2026-09-13T09:09:00Z", "title": "jwt.io - JSON Web Tokens",
         "domain": "jwt.io", "dwell_seconds": 180},
        {"ts": "2026-09-13T09:12:00Z",
         "title": "Token expiry not refreshing on 401 - Stack Overflow",
         "domain": "stackoverflow.com", "dwell_seconds": 540},
        {"ts": "2026-09-13T09:21:00Z",
         "title": "PyJWT - decode options verify_exp",
         "domain": "pyjwt.readthedocs.io", "dwell_seconds": 300},
        {"ts": "2026-09-13T09:26:00Z", "title": "auth.py - rethread - VS Code",
         "domain": "vscode.local", "dwell_seconds": 240},
        {"ts": "2026-09-13T09:30:00Z", "title": "(1) WhatsApp",
         "domain": "web.whatsapp.com", "dwell_seconds": 600},
        {"ts": "2026-09-13T09:40:00Z",
         "title": "I built a CLI in Rust and you won't believe - YouTube",
         "domain": "youtube.com", "dwell_seconds": 1500},
        {"ts": "2026-09-13T10:05:00Z", "title": "r/programming",
         "domain": "reddit.com", "dwell_seconds": 900},
    ],
}


def build_user_message(payload):
    intent = payload.get("declared_intent") or "(none declared)"
    lines = []
    for ev in payload["events"]:
        lines.append(
            f"{ev['ts']} | {ev.get('dwell_seconds', 0):>5}s | "
            f"{ev.get('domain','')} | {ev.get('title','')}"
        )
    return (
        f"Declared intent: {intent}\n\n"
        f"Timeline (time | dwell | domain | title):\n" + "\n".join(lines)
    )


def call_bedrock(system, user, model_id, region):
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    resp = client.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"]


def call_deepinfra(system, user, model_id):
    """Fallback so you can run tonight even if Bedrock access is pending."""
    import urllib.request

    key = os.environ.get("DEEPINFRA_API_KEY")
    if not key:
        print("Set DEEPINFRA_API_KEY in your environment first.")
        sys.exit(1)

    body = json.dumps({
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 800,
        "temperature": 0,
    }).encode()

    req = urllib.request.Request(
        "https://api.deepinfra.com/v1/openai/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def parse_output(raw):
    cleaned = raw.strip()
    # Reasoning models (R1, V4 in reasoning mode) emit a think block first.
    # This was missing here while decompose and drift both had it.
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fallback: grab the outermost JSON object, ignoring prose either side.
    mm = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if mm:
        return json.loads(mm.group(0))
    raise ValueError("no JSON object found in model output")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="timeline.json from capture_history.py")
    ap.add_argument("--provider", choices=["bedrock", "deepinfra"], default="bedrock")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--model", default=None)
    ap.add_argument("--env", default=".env", help="path to your .env file")
    ap.add_argument(
        "--sample",
        choices=["work", "browsing"],
        default="work",
        help="which built-in timeline to use when --file is omitted. "
             "'browsing' is the negative case: it SHOULD return session_found=false",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the assembled prompt and exit, without calling any model",
    )
    args = ap.parse_args()

    if args.env != ".env":
        load_env(args.env)

    if args.file:
        if not os.path.exists(args.file):
            print(f"No such timeline file: {args.file}")
            print("Run capture_history.py first, or omit --file to use the sample.")
            sys.exit(1)
        try:
            with open(args.file, encoding="utf-8") as f:
                payload = json.load(f)
        except json.JSONDecodeError as e:
            print(f"{args.file} is not valid JSON: {e}")
            sys.exit(1)
    elif args.sample == "browsing":
        payload = SAMPLE_BROWSING
        print("(built-in NEGATIVE sample - correct answer is session_found=false)\n")
    else:
        payload = SAMPLE
        print("(built-in work sample - correct answer is session_found=true)\n")

    if not payload.get("events"):
        print("No events in that timeline.")
        sys.exit(1)

    user_msg = build_user_message(payload)

    if args.dry_run:
        print("--- SYSTEM ---")
        print(SYSTEM_PROMPT)
        print("--- USER ---")
        print(user_msg)
        print(f"\n({len(payload['events'])} events, no model called)")
        return

    try:
        if args.provider == "bedrock":
            model = args.model or "us.anthropic.claude-sonnet-4-20250514-v1:0"
            raw = call_bedrock(SYSTEM_PROMPT, user_msg, model, args.region)
        else:
            model = args.model or "meta-llama/Llama-3.3-70B-Instruct"
            raw = call_deepinfra(SYSTEM_PROMPT, user_msg, model)
    except ImportError:
        print("boto3 is not installed.  pip install boto3")
        print("Or run with --provider deepinfra, which needs no packages at all.")
        sys.exit(1)
    except Exception as e:
        name = type(e).__name__
        text = str(e)
        print(f"Model call failed ({name}): {text}\n")
        if "AccessDenied" in text or "AccessDenied" in name:
            print(f"That is model access, not credentials.")
            print(f"Enable '{model}' for region {args.region} in the Bedrock console,")
            print("and check the IAM policy allows bedrock:InvokeModel.")
        elif "ValidationException" in text or "ResourceNotFound" in text:
            print(f"'{model}' may not exist in {args.region}.")
            print("Check the model id, or try --region us-east-1.")
        elif "ExpiredToken" in text or "InvalidSignature" in text or "Unrecognized" in text:
            print("Credentials look stale. Re-run: aws configure")
        elif "NoCredential" in name or "NoCredential" in text:
            print("No AWS credentials found. Run: aws configure")
        elif "Throttling" in text or "TooManyRequests" in text:
            print("Rate limited. Wait a moment and retry.")
        elif "HTTP Error 401" in text or "HTTP Error 403" in text:
            print("DeepInfra rejected the key. Check DEEPINFRA_API_KEY in your .env")
        sys.exit(1)

    try:
        out = parse_output(raw)
    except Exception:
        print("--- could not parse as JSON, raw output below ---")
        print(raw)
        sys.exit(1)

    print("=" * 60)
    found = out.get("session_found")
    print("THREAD FOUND:", found)
    print("EVIDENCE    :", out.get("evidence"))
    if found:
        print("-" * 60)
        print("DOING       :", out.get("doing"))
        print("WHY         :", out.get("why"))
        print("KEY TABS    :")
        for t in (out.get("key_tabs") or []):
            print("              -", t)
        print("NEXT ACTION :", out.get("next_action"))
    else:
        print("-" * 60)
        print("No work thread to hand back. Correctly staying quiet.")
    print("=" * 60)
    print("\nThe only question that matters:")
    print("If you'd been away 40 minutes, would this actually get you back?")


if __name__ == "__main__":
    main()
