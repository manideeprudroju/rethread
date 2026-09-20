"""
local_check.py -- run this before deploy.bat, from the folder that holds
lambda_handler.py and every probe file. No AWS, no model, no network.

    python local_check.py

What it does, in order:
  1. Every module deploy.bat will zip compiles.
  2. The guide checks and the friction checks pass, against YOUR copies of
     experiment_probe, decompose_guardrail and reentry_validate.
  3. Your real lambda_handler imports and routes friction, heavy and guide
     end to end, with the Cognito check and the model replaced by fakes.
     This also proves every agent call starts with an empty history. Then
     save_session and history against an in-memory table (no DynamoDB).
  4. Every local module the handler actually imported is in deploy.bat's
     MODULES list, so nothing is missing from the zip.

Step 3 needs the Lambda's own libraries installed locally:
    pip install boto3 "PyJWT[crypto]" pydantic
(strands is not needed: the model is faked.)

Delete-safe: it writes nothing except Python's own __pycache__.
"""

import io
import json
import os
import py_compile
import re
import sys
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

FALLBACK_MODULES = (
    "lambda_handler.py model_provider.py decompose_guardrail.py decompose_probe.py "
    "drift_probe.py reentry_probe.py experiment_probe.py analyst_probe.py "
    "reentry_validate.py initiate_probe.py seq_check.py friction_probe.py guide_probe.py"
).split()

results = []


def step(name, ok, detail=""):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not ok else ""))


def modules_from_bat():
    try:
        text = open("deploy.bat", encoding="utf-8", errors="replace").read()
    except OSError:
        return FALLBACK_MODULES, "deploy.bat not found here, using the expected list"
    m = re.search(r"(?im)^set MODULES=(.+)$", text)
    if not m:
        return FALLBACK_MODULES, "no 'set MODULES=' line in deploy.bat -- is it the updated one?"
    return m.group(1).split(), ""


# --------------------------------------------------------------------------
print("=" * 74)
print("1. EVERY MODULE IN THE ZIP COMPILES")
print("=" * 74)
MODULES, note = modules_from_bat()
if note:
    print(f"  note: {note}")
missing = [m for m in MODULES if not os.path.exists(m)]
step(f"all {len(MODULES)} modules are in this folder", not missing, f"missing: {missing}")
for mod in MODULES:
    if os.path.exists(mod):
        try:
            py_compile.compile(mod, doraise=True)
        except py_compile.PyCompileError as e:
            step(f"{mod} compiles", False, str(e).strip().splitlines()[-1])
step("no syntax errors", all(results))

# --------------------------------------------------------------------------
print()
print("=" * 74)
print("2. GUIDE AND FRICTION CHECKS (against your real shared modules)")
print("=" * 74)
for name in ("guide_probe", "friction_probe"):
    buf = io.StringIO()
    try:
        mod = __import__(name)
        with redirect_stdout(buf):
            ok = mod.run_checks()
        passed = buf.getvalue().count("PASS  ")
        step(f"{name}: {passed} checks", ok,
             "\n        ".join(l.strip() for l in buf.getvalue().splitlines() if "FAIL" in l))
    except Exception as e:
        step(f"{name} runs", False, f"{type(e).__name__}: {e}")
fp = sys.modules.get("friction_probe")
if fp is not None:
    step("friction is using the project's shared plan rules (not skipping them)",
         fp._plan_rules is not None and bool(fp.BANNED_PHRASES) and bool(fp.DRIFT_WORDS),
         "decompose_guardrail or reentry_validate didn't import next to friction_probe")

# --------------------------------------------------------------------------
print()
print("=" * 74)
print("3. YOUR REAL lambda_handler, END TO END (fake auth, fake model)")
print("=" * 74)
try:
    import lambda_handler as lh
except ModuleNotFoundError as e:
    lh = None
    print(f"  SKIP  can't import lambda_handler here: {e}")
    print('        pip install boto3 "PyJWT[crypto]" pydantic   then run this again')
    results.append(False)
except Exception as e:
    lh = None
    step("lambda_handler imports", False, f"{type(e).__name__}: {e}")

if lh is not None:
    lh._authenticate = lambda event: "local-test-user"

    PLAN = {"if": "I'm back on Q3 summary for the third time in a few minutes",
            "then": "Write the one question I'm stuck on as a single line at the top",
            "why": "Putting the question on the page frees working memory for the answer."}
    HEAVY = {"if": "I sit down to write the Q3 summary report and it feels heavy",
             "then": "Shrink it to a version I could finish in ten minutes",
             "why": "Starting smaller lowers the cost of starting."}
    GUIDE = ("Starting is the hard part, so hand it to a cue: 'if it's 4 pm and the "
             "doc isn't open, I open it and type one line.' Which task is it?")

    class FakeAgent:
        def __init__(self, role, system):
            self.role, self.system, self.messages, self.history_at_call = role, system, [], []

        def __call__(self, user, **kwargs):
            self.history_at_call.append(len(self.messages))
            self.messages += [{"role": "user"}, {"role": "assistant"}]
            if self.role == "guide":
                return GUIDE
            if self.role == "friction":
                return json.dumps(HEAVY if "heavy" in user else PLAN)
            return json.dumps({"relevant": True, "reason": "fake"})

    agents = []

    def fake_build(role, system_prompt, *a, **k):
        agents.append(FakeAgent(role, system_prompt))
        return agents[-1]

    lh._build_agent = fake_build

    def call(action, **payload):
        event = {"headers": {"authorization": "Bearer local"}, "requestContext": {},
                 "body": json.dumps({"action": action, **payload})}
        with redirect_stdout(io.StringIO()):
            r = lh.handler(event, None)
        return r["statusCode"], json.loads(r["body"])

    code, out = call("friction", events=[], sessions=[])
    step("friction, empty archive -> 200 nothing_found, no model call",
         code == 200 and out.get("status") == "nothing_found" and not agents, (code, out))

    body = {"events": fp.CIRCLING, "sessions": [fp._session()], "tz_offset_min": -330}
    code, out = call("friction", **body)
    step("friction, a circling day -> 200 plan written by the model",
         code == 200 and out.get("status") == "plan" and out["plan"]["then"] == PLAN["then"],
         (code, str(out)[:200]))
    code, out2 = call("friction", **body, user_id="someone-else")
    step("the browser can't choose its experiment arm (Cognito id wins)",
         code == 200 and out2["plan"]["support"] == out["plan"]["support"])

    code, out = call("heavy", goal="write the Q3 summary report")
    step("heavy -> 200 plan + Tele-MANAS line",
         code == 200 and out.get("status") == "plan" and "14416" in out.get("care_line", ""),
         (code, str(out)[:200]))

    code, out = call("guide", message="Why is starting so hard?",
                     history=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hey!"}])
    step("guide -> 200 answer with sources from the index",
         code == 200 and out.get("kind") == "answer" and out.get("reply") == GUIDE
         and out.get("sources"), (code, str(out)[:200]))

    before = sum(len(a.history_at_call) for a in agents)
    code, out = call("guide", message="some days I want to die")
    step("guide, self-harm -> fixed Tele-MANAS / 112 reply, no model call",
         code == 200 and out.get("kind") == "crisis" and "112" in out.get("reply", "")
         and sum(len(a.history_at_call) for a in agents) == before, (code, out))

    code, out = call("guide", message="   ")
    step("guide, empty message -> 400", code == 400, (code, out))

    code, out = call("churn", events=[])
    step("an existing action still routes (churn -> 200)", code == 200, (code, out))

    code, out = call("definitely-not-an-action")
    step("unknown action -> 400 listing friction, heavy, guide",
         code == 400 and all(a in out.get("error", "") for a in ("friction", "heavy", "guide")),
         out)

    calls = [n for a in agents for n in a.history_at_call]
    step("every model call started with an empty history (messages.clear works)",
         calls and all(n == 0 for n in calls), calls)
    step("one agent per role, reused across calls",
         len({a.role for a in agents}) == len(agents), [a.role for a in agents])

    # ----------------------------------------------------------------------
    print()
    print("=" * 74)
    print("3b. SESSIONS ON THE ACCOUNT (save_session, history), in-memory table")
    print("=" * 74)

    class MemTable:
        """Just enough of the DynamoDB table for these actions."""
        def __init__(self):
            self.items = {}

        @staticmethod
        def _match(cond, item):
            ex = cond.get_expression()
            op, vals = ex["operator"], ex["values"]
            if op == "AND":
                return MemTable._match(vals[0], item) and MemTable._match(vals[1], item)
            if op == "=":
                return item.get(vals[0].name) == vals[1]
            if op == "begins_with":
                return str(item.get(vals[0].name, "")).startswith(vals[1])
            raise NotImplementedError(op)

        def put_item(self, Item):
            self.items[(Item["userId"], Item["recordKey"])] = json.loads(json.dumps(Item, default=str))

        def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None, **kw):
            rows = sorted((v for v in self.items.values() if self._match(KeyConditionExpression, v)),
                          key=lambda r: r["recordKey"], reverse=not ScanIndexForward)
            return {"Items": rows[:Limit] if Limit else rows}

    real_table, lh._ddb_table = lh._ddb_table, MemTable()
    who = {"id": "local-test-user"}
    lh._authenticate = lambda event: who["id"]
    try:
        sid = "1789900000000-0a1b2c3d"
        start = {"session_id": sid, "started_at": "2026-09-20T10:30:00.000Z", "ended_at": None,
                 "declared_intent": "write the Q3 summary report", "notes": "Where: Google Docs",
                 "first_action": "Open the Q3 summary doc", "plans": [], "history": [], "done": False}
        code, out = call("save_session", session=start)
        step("save_session stores a new session under the browser's id",
             code == 200 and out.get("saved") and out.get("session_id") == sid, (code, out))
        code, out = call("save_session", session={**start, "ended_at": "2026-09-20T11:00:00.000Z",
                                                  "end_reason": "stopped"})
        code, out = call("history")
        rows = out.get("sessions", []) if code == 200 else []
        step("history returns it, with its end, from the same item",
             len(rows) == 1 and rows[0]["session_id"] == sid and rows[0]["ended_at"]
             and rows[0]["goal"] == "write the Q3 summary report", (code, str(out)[:200]))
        who["id"] = "someone-else"
        code, out = call("history")
        step("another account sees none of it", code == 200 and out.get("sessions") == [], (code, out))
        code, out = call("save_session", session="nope")
        step("a malformed session is a 400", code == 400, (code, out))
    finally:
        lh._ddb_table = real_table
        lh._authenticate = lambda event: "local-test-user"

    # ----------------------------------------------------------------------
    print()
    print("=" * 74)
    print("4. NOTHING THE HANDLER IMPORTS IS MISSING FROM THE ZIP")
    print("=" * 74)
    local = sorted(
        os.path.basename(m.__file__) for m in list(sys.modules.values())
        if getattr(m, "__file__", None)
        and os.path.dirname(os.path.abspath(m.__file__)) == HERE
        and m.__file__.endswith(".py")
        and os.path.basename(m.__file__) != "local_check.py")
    not_zipped = [m for m in local if m not in MODULES]
    step(f"all {len(local)} local modules the handler loaded are in deploy.bat's list",
         not not_zipped, f"would be missing from the zip: {not_zipped}")

print()
print("=" * 74)
if all(results):
    print("  ALL LOCAL CHECKS PASS -- safe to run deploy.bat")
else:
    print(f"  {results.count(False)} FAILED -- fix these before deploy.bat")
print("=" * 74)
sys.exit(0 if all(results) else 1)
