"""
Single Lambda entrypoint for every agent action, routed by body["action"].
One function, one deployment. Split later only if a path needs its own
scaling, concurrency or IAM.

Two ways in:
  - Function URL (browser): must carry a Cognito ACCESS token. Identity is
    the token's `sub`, never anything in the body.
  - EventBridge schedule (no token): runs the nightly analysis for every
    user who has logged sessions. Recognised by source == "aws.events" and
    no requestContext; a direct invoke needs lambda:InvokeFunction, so the
    public URL cannot reach this path.

DynamoDB layout (table RethreadData, PK userId, SK recordKey):
  SESSION#<ms>-<rand>   chat session object (updated each turn)
  EXP#<started_at ISO>  one finished session's measurements (log_session)
  NIGHTLY#latest        last nightly result for the user

Package alongside every module it imports. A missing one fails the whole
function at import, so EVERY action returns 500.

BUILD (use Linux wheels even when building on Mac/Windows -- pydantic-core
and cryptography are compiled):

    pip install --target package \
      --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 \
      strands-agents pydantic "PyJWT[crypto]"
    cd package && zip -rq ../deployment.zip . && cd ..
    zip -g deployment.zip lambda_handler.py model_provider.py seq_check.py \
      decompose_guardrail.py drift_probe.py reentry_probe.py reentry_validate.py \
      initiate_probe.py experiment_probe.py analyst_probe.py friction_probe.py \
      guide_probe.py

PyJWT without [crypto] cannot verify RS256. The auth `except` turns that
into a 401 on every request, which looks exactly like a bad token.

IAM (execution role), on this table only:
    dynamodb:PutItem, dynamodb:Query, dynamodb:Scan
"""

import json
import os
import time
import uuid
from datetime import datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
import jwt
from jwt import PyJWKClient

from pydantic import BaseModel, ConfigDict, Field

from model_provider import build_agent as _build_agent

from decompose_guardrail import SYSTEM_PROMPT as DECOMPOSE_PROMPT
from decompose_guardrail import safe_fallback
from drift_probe import DRIFT_PROMPT
from drift_probe import build_user as build_drift_user
from drift_probe import detect_churn
from drift_probe import parse_output as parse_drift_output
from reentry_probe import SYSTEM_PROMPT as REENTRY_PROMPT
from reentry_probe import build_user_message
from reentry_validate import validate_reentry
from experiment_probe import analyse, assign_condition
from initiate_probe import SESSION_PROMPT, SessionOutput, build_session_user
from initiate_probe import SessionClosed, advance_session, user_context
from initiate_probe import validate_session
from initiate_probe import ClarifyOutput, new_session
from decompose_guardrail import CLARIFY_STRICT, REAL_FILE_RE, filter_clarify_questions
from initiate_probe import session_fallback, ensure_action
from initiate_probe import validate_initiate
from analyst_probe import ANALYST_PROMPT, AnalystOutput
from analyst_probe import build_user as build_analyst_user
from analyst_probe import validate_analyst
from friction_probe import run_friction, run_heavy

# ---------------------------------------------------------------------------
# Cognito + DynamoDB
# ---------------------------------------------------------------------------
# The Function URL stays publicly reachable, so the Lambda verifies the
# Cognito access token itself. Never trust a userId supplied by the browser.
COGNITO_REGION = os.environ.get("COGNITO_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get(
    "COGNITO_USER_POOL_ID", "us-east-1_9Ea0gBsLs"
)
COGNITO_CLIENT_ID = os.environ.get(
    "COGNITO_CLIENT_ID", "7s5gpf5rdfuh25fhav2ha09ja"
)
COGNITO_ISSUER = (
    f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
    f"{COGNITO_USER_POOL_ID}"
)
JWKS_URL = f"{COGNITO_ISSUER}/.well-known/jwks.json"

# The experiment's two arms. Same list as the frontend's experiment.js, so
# the scheduled run and the dashboard's on-demand run analyse the same
# experiment (a request can still pass its own "arms").
EXPERIMENT_ARMS = [a.strip() for a in os.environ.get(
    "EXPERIMENT_ARMS", "first_step_only,step_and_plans").split(",") if a.strip()]

_DDB_TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "RethreadData")
_dynamodb = boto3.resource(
    "dynamodb",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
_ddb_table = _dynamodb.Table(_DDB_TABLE_NAME)
_jwks_client = PyJWKClient(JWKS_URL)


def _get_bearer_token(event):
    headers = event.get("headers") or {}
    auth = next(
        (value for key, value in headers.items()
         if key.lower() == "authorization"),
        None,
    )
    if not auth or not auth.startswith("Bearer "):
        raise PermissionError("missing bearer token")
    return auth[7:].strip()


def _authenticate(event):
    token = _get_bearer_token(event)
    signing_key = _jwks_client.get_signing_key_from_jwt(token)

    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=COGNITO_ISSUER,
        options={"verify_aud": False},
    )

    if claims.get("token_use") != "access":
        raise PermissionError("expected a Cognito access token")
    if claims.get("client_id") != COGNITO_CLIENT_ID:
        raise PermissionError("token was issued for a different client")

    user_id = claims.get("sub")
    if not user_id:
        raise PermissionError("token has no subject")

    return user_id


def _json_default(o):
    """DynamoDB hands numbers back as Decimal, which json cannot encode."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def _to_ddb(obj):
    """boto3 rejects Python floats ('Float types are not supported'). Round-
    trip through JSON so every float becomes a Decimal. allow_nan=False:
    NaN/Infinity cannot be stored and should never have got this far."""
    return json.loads(json.dumps(obj, allow_nan=False, default=_json_default),
                      parse_float=Decimal)


def _from_ddb(obj):
    """Decimals back to int/float. analyse() ignores anything that is not
    an int or float, so un-converted Decimals would all count as missing."""
    return json.loads(json.dumps(obj, default=_json_default))


def _new_session_id():
    # Time-prefixed so SESSION# keys sort in creation order under a Query.
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _persist_session(user_id, session):
    """Persist server-produced session state under the Cognito subject.

    Best effort, on purpose: the user already has a good answer from the
    model, and a storage failure must not turn that into a 500.
    """
    if not isinstance(session, dict):
        return
    try:
        _ddb_table.put_item(
            Item={
                "userId": user_id,
                "recordKey": f"SESSION#{session['session_id']}",
                "recordType": "session",
                "updatedAt": int(time.time()),
                "session": _to_ddb(session),
            }
        )
    except Exception as e:
        print(f"[ddb] session persist failed: {type(e).__name__}: {e}")


def _query_all(**kwargs):
    items = []
    while True:
        resp = _ddb_table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _load_experiment_sessions(user_id):
    """This user's logged sessions, OLDEST FIRST (what analyse() requires).
    EXP# keys carry an ISO-8601 UTC start time, so key order is time order."""
    items = _query_all(
        KeyConditionExpression=(Key("userId").eq(user_id)
                                & Key("recordKey").begins_with("EXP#")),
        ScanIndexForward=True,
    )
    return _from_ddb(items)


def _count_experiment_sessions(user_id):
    n = 0
    kwargs = dict(
        KeyConditionExpression=(Key("userId").eq(user_id)
                                & Key("recordKey").begins_with("EXP#")),
        Select="COUNT",
    )
    while True:
        resp = _ddb_table.query(**kwargs)
        n += resp.get("Count", 0)
        if "LastEvaluatedKey" not in resp:
            return n
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# Provider, region and model ids all live in model_provider.py, overridable
# via env vars on the Lambda function itself (PROBE_PROVIDER, PROBE_REGION,
# DECOMPOSE_MODEL, DRIFT_MODEL, REENTRY_MODEL).

# Built once per cold start, reused across warm invocations. This is the
# whole point of module-level state in Lambda -- don't rebuild the agent
# on every call in a warm container.
_agents = {}


def _agent(role, system_prompt):
    if role not in _agents:
        _agents[role] = _build_agent(role, system_prompt)
    agent = _agents[role]
    # A Strands Agent keeps its conversation between calls. Reused across
    # warm invocations, every call carried all the earlier ones: more tokens,
    # worse judgements, and one user's tab titles and goals inside another
    # user's model call. Every action is stateless (the client carries the
    # session), so each call starts clean.
    agent.messages.clear()
    return agent


class Plan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    if_: str = Field(alias="if")
    then: str


class DecomposeOutput(BaseModel):
    first_action: str
    plans: list[Plan]


class DriftOutput(BaseModel):
    relevant: bool
    reason: str


class ReentryOutput(BaseModel):
    session_found: bool
    evidence: str
    doing: str | None = None
    why: str | None = None
    key_tabs: list[str] | None = None
    next_action: str | None = None



def _structured(agent, msg, model_cls, parser, by_alias=False):
    """Get a dict back regardless of the Strands version.

    Newer Strands returns AgentResult.structured_output (a Pydantic object).
    Older versions ignore structured_output_model and hand back plain text.
    Seen in Lambda as: 'AgentResult' object has no attribute
    'structured_output' -- with perfectly good JSON in the text, thrown away
    on an attribute lookup.

    So: use the structured object when it is there, parse the text when it
    is not. `parser` is the probe's own parse_output, already hardened for
    think tags, fences and prose-wrapped JSON.
    """
    result = agent(msg, structured_output_model=model_cls)
    so = getattr(result, "structured_output", None)
    if so is not None:
        return so.model_dump(by_alias=by_alias)
    return parser(str(result))


def _handle_decompose(body):
    goal = body["goal"]
    answers = body.get("answers", "")
    context = goal + (" " + answers if answers else "")
    msg = f'Goal, in their words: "{goal}"'
    if answers:
        msg += "\n\nThey were asked and replied:\n" + answers

    def attempt(m):
        # Construction inside, so a config failure falls through to
        # safe_fallback like any other failure rather than raising a 500.
        agent = _agent("decompose", DECOMPOSE_PROMPT)
        return _structured(agent, m, DecomposeOutput,
                           parse_drift_output, by_alias=True)

    # Any failure here -- a bad Bedrock call as much as a validation miss --
    # falls through to safe_fallback(). This is user-facing output; a
    # general-but-honest first_action beats a 500 with nothing to act on.
    # The fallback is correct behaviour, but a SILENT fallback is
    # undebuggable -- you cannot tell a model failure from a validation
    # failure from the response. Log the reason; still return the fallback.
    # validate_initiate, not decompose_validate: it is the same rules plus
    # the grounding checks (invented filenames, artifacts, numbered
    # references). There is no reason this path should be the lax one.
    def _reason(checks):
        return "; ".join(r + (f" ({d})" if d else "")
                         for r, ok, d in checks if not ok)

    try:
        out = attempt(msg)
        passed, checks = validate_initiate(out, context)
        if passed:
            return out
        reason = _reason(checks)
        print(f"[decompose] attempt 1 failed validation: {reason}")
        repair_msg = (
            msg
            + "\n\nYour previous answer was REJECTED for: " + reason
            + "\nFix exactly those problems. Stay general rather than invent a "
              "replacement detail."
        )
        out2 = attempt(repair_msg)
        passed2, checks2 = validate_initiate(out2, context)
        if passed2:
            return out2
        print(f"[decompose] repair also failed validation: {_reason(checks2)}")
    except Exception as e:
        print(f"[decompose] model call raised {type(e).__name__}: {e}")
    print("[decompose] -> deterministic fallback")
    return safe_fallback(goal, context)


def _handle_drift(body):
    user = build_drift_user(body["intent"], body["title"], body.get("domain", ""))
    try:
        # Agent construction is INSIDE the try. It can fail (bad credentials,
        # unresolved model config), and drift must never surface an error --
        # the bias says default to relevant rather than invent a lapse.
        agent = _agent("drift", DRIFT_PROMPT)
        # Plain text + parse_output, NOT structured output. One call per tab
        # instead of two -- drift is the highest-volume path in the system.
        out = parse_drift_output(str(agent(user)))
        return {"relevant": bool(out.get("relevant")), "reason": str(out.get("reason", ""))}
    except Exception as e:
        # Same bias as drift_probe.py / drift_strands.py: never invent a
        # lapse. A failed call defaults relevant, it doesn't fail closed.
        # _fallback tells the client this verdict is a default, not a
        # judgement, so it is never used as a measurement (initiation).
        return {"relevant": True, "_fallback": True,
                "reason": f"call failed ({type(e).__name__}), defaulted to relevant"}


# Shown in place of a reconstruction that could not be made safe to display.
# Honest in both cases: the not-found text claims no thread, and the found
# text claims only what the key tabs already show. The panel falls back to
# the session's own goal and step, which never depended on the model.
REENTRY_NEUTRAL_EVIDENCE = "These tabs don't point clearly to one piece of work."
REENTRY_FOUND_EVIDENCE = "Rebuilt from the tab titles in this session."


def _handle_reentry(body):
    """Reconstruct the thread from tab titles, then guard it like every other
    user-facing output.

    This was the one path with no guardrail: the model's text went straight
    to the re-entry panel. reentry_validate exists because a live run put
    "extended YouTube watching (drift)" and "a failed attempt to start work"
    into `evidence`, and the panel prints `evidence` verbatim.

    A genuine model failure on the first call still raises, so the UI shows
    its error state rather than a fake "nothing found" (API_CONTRACT). Only a
    reply that fails the rules is repaired, salvaged, or replaced.
    """
    user_msg = build_user_message(body)

    def attempt(m):
        agent = _agent("reentry", REENTRY_PROMPT)
        return _structured(agent, m, ReentryOutput, parse_drift_output)

    def _reason(checks):
        return "; ".join(r + (f" ({d})" if d else "")
                         for r, ok, d in checks if not ok)

    out = attempt(user_msg)
    passed, checks = validate_reentry(out, body)
    if passed:
        return out
    reason = _reason(checks)
    print(f"[reentry] attempt 1 failed: {reason}")

    out2 = None
    try:
        out2 = attempt(
            user_msg
            + "\n\nYour previous answer was REJECTED for: " + reason
            + "\nFix exactly those problems. Describe the work the tabs show, "
              "never the person. If no single thread is clear, set "
              "session_found to false.")
        if validate_reentry(out2, body)[0]:
            print("[reentry] repair succeeded")
            return out2
    except Exception as e:
        print(f"[reentry] repair call raised {type(e).__name__}: {e}")

    # Salvage before discarding. `evidence` is the free-text field where the
    # live failures happened; if replacing it alone makes a reply pass, the
    # reconstruction itself (doing, key tabs, next action) survives.
    for cand in (c for c in (out2, out) if isinstance(c, dict)):
        neutral = (REENTRY_FOUND_EVIDENCE if cand.get("session_found") is True
                   else REENTRY_NEUTRAL_EVIDENCE)
        fixed = {**cand, "evidence": neutral}
        if validate_reentry(fixed, body)[0]:
            print("[reentry] kept the reconstruction, replaced its evidence")
            return {**fixed, "_evidence_replaced": True}

    print("[reentry] repair failed -> neutral answer")
    return {"session_found": False, "evidence": REENTRY_NEUTRAL_EVIDENCE,
            "doing": None, "why": None, "key_tabs": None, "next_action": None,
            "_fallback": True}


def _handle_churn(body):
    """Pure Python, no model call, no cost, sub-millisecond. Safe to call
    on every timeline update."""
    return detect_churn(body.get("events") or [])


# The one question every later turn depends on: where the work lives.
CLARIFY_FALLBACK = "Which file, doc or app is this in?"


def _handle_initiate(body):
    """Open a session.

    No `answers` -> returns up to two clarifying questions and NO session.
    With `answers` -> returns the session, ready for `amend`.

    Two-step on purpose: the questions are where the work's location and
    deadline come from, and without them every later turn is guessing.
    """
    goal = body.get("goal", "")
    answers = body.get("answers", "")
    condition = body.get("condition")

    if not answers:
        try:
            agent = _agent("clarify", CLARIFY_STRICT)
            out = _structured(agent, f'Goal, in their words: "{goal}"',
                              ClarifyOutput, parse_drift_output)
            qs = filter_clarify_questions(goal, out.get("questions") or [])
        except Exception as e:
            print(f"[initiate] clarify failed {type(e).__name__}: {e}")
            qs = []
        # Deterministic backstop. A failed clarify call used to fall through
        # silently and the session was built anyway, so a goal like "study
        # for gate da" went straight to an invented document. If nothing was
        # asked and the goal names no artifact, ask the one question every
        # later turn depends on.
        if not qs and not REAL_FILE_RE.search(goal):
            qs = [CLARIFY_FALLBACK]
        if qs:
            return {"stage": "clarify", "questions": qs, "session": None}
        # Nothing to ask: fall through and build the session now.

    context = goal + (" " + answers if answers else "")
    msg = f'Goal, in their words: "{goal}"'
    if answers:
        msg += "\n\nThey were asked and replied:\n" + answers

    def attempt(m):
        agent = _agent("decompose", DECOMPOSE_PROMPT)
        return _structured(agent, m, DecomposeOutput, parse_drift_output,
                           by_alias=True)

    try:
        out = attempt(msg)
        passed, checks = validate_initiate(out, context)
        if not passed:
            reason = "; ".join(r + (f" ({d})" if d else "")
                               for r, ok, d in checks if not ok)
            print(f"[initiate] attempt 1 failed: {reason}")
            out2 = attempt(
                msg + "\n\nYour previous answer was REJECTED for: " + reason
                + "\nFix exactly those problems. Do not name any document, "
                  "file or resource they did not mention.")
            passed2, _ = validate_initiate(out2, context)
            out = out2 if passed2 else safe_fallback(goal, context)
            if not passed2:
                print("[initiate] repair failed -> fallback")
    except Exception as e:
        print(f"[initiate] model call raised {type(e).__name__}: {e}")
        out = safe_fallback(goal, context)

    session = new_session(goal, out.get("first_action"), out.get("plans", []),
                          notes=answers, condition=condition)
    return {"stage": "session", "questions": [], "session": session,
            "first_action": session["first_action"], "plans": session["plans"]}


def _handle_session(body):
    """User came back mid-session: blocker, scope change, progress, or done.

    Returns ONE next action, never a fresh plan. On failure this echoes the
    action they already had rather than erroring -- mid-session, the worst
    outcome is leaving them with nothing to do.
    """
    session = body.get("session") or {}
    message = body.get("message", "")

    # A finished session does not reopen. Silently accepting more turns would
    # let a completed task sprout new work.
    if session.get("done"):
        return {"kind": "done", "first_action": None, "plans": [],
                "note": "", "session": session, "_closed": True}
    # USER text only. The previous first_action is model output, and feeding
    # it back in let an artifact the model invented count as something they
    # had said.
    context = user_context(session, message)
    user = build_session_user(session, message)

    def attempt(m):
        # "amend" is the registry key, not the prompt: role selects a model
        # id in model_provider, and SESSION_PROMPT is passed separately.
        # model_provider has no "session" entry, and a missing role raises
        # KeyError, which this path catches into the fallback - every turn
        # would answer without ever reaching the model.
        agent = _agent("amend", SESSION_PROMPT)
        return _structured(agent, m, SessionOutput, parse_drift_output, by_alias=True)

    # validate_amend NEEDS the session. Without it, the rules that depend on
    # the previous turn -- do not ask twice, do not leave two turns with no
    # action -- cannot fire, which is how two dead turns in a row reached a
    # real user. This path also had no repair turn, unlike decompose and
    # initiate, so one bad reply went straight to echoing the old action.
    try:
        out = attempt(user)
        passed, checks = validate_session(out, context, session)
        if not passed:
            reason = "; ".join(r + (f" ({d})" if d else "")
                               for r, ok, d in checks if not ok)
            print(f"[session] attempt 1 failed: {reason}")
            out2 = attempt(
                user + "\n\nYour previous answer was REJECTED for: " + reason
                + "\nFix exactly those problems. If the problem is a missing "
                  "action, give one they can do right now with what is already "
                  "on their screen. Do not invent a file, a number or a "
                  "document they did not mention.")
            if validate_session(out2, context, session)[0]:
                out = out2
            else:
                # If the only fault was the missing action, fill it and keep
                # the question. Only discard the turn if that still fails.
                for cand in (out2, out):
                    fixed = ensure_action(cand, session)
                    if fixed is not cand and validate_session(fixed, context, session)[0]:
                        print("[session] filled the missing action from the session")
                        out = fixed
                        break
                else:
                    print("[session] repair failed -> deterministic fallback")
                    out = session_fallback(session, message, context)
    except Exception as e:
        print(f"[session] model call raised {type(e).__name__}: {e}")
        out = session_fallback(session, message, context)

    # Return the UPDATED session alongside the answer. The client stores it
    # and passes it back next turn -- that is the whole persistence model.
    # Server-side stays stateless, so nothing is lost on a cold start.
    # Last line of defence on the response shape: while a session is open
    # there is always a next move, at minimum the one they already had. A
    # null reaches the UI as "Next move: null", which is what a chatty
    # message used to produce.
    out = ensure_action(out, session)
    return {**out, "session": advance_session(session, message, out)}


def _handle_assign(body):
    """Which experiment arm does this session get? Deterministic, no model."""
    arms = body.get("arms") or []
    if len(arms) < 2:
        raise ValueError("assign needs at least 2 arms")
    user_id = body["_authenticated_user_id"]
    # session_index comes from the server's count of this user's logged
    # sessions. The client's count lived in localStorage: a cleared browser
    # or a second device restarted it at 0 and broke the balanced blocks.
    # The client value is only a fallback if DynamoDB is unreachable.
    try:
        idx = _count_experiment_sessions(user_id)
        source = "server"
    except Exception as e:
        print(f"[assign] count failed, using client index: {type(e).__name__}: {e}")
        idx = int(body.get("session_index", 0))
        source = "client"
    # Seeded by the Cognito id only, so users get different arm orders and
    # nobody can choose theirs.
    return {"condition": assign_condition(idx, arms, seed=user_id),
            "session_index": idx, "index_source": source}


def _num(v):
    """A finite number as Decimal, else None (NaN/Infinity/bool/strings)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return Decimal(str(v))


def _handle_log_session(body):
    """Store one finished session's measurements for the nightly analysis.

    Keyed EXP#<started_at>, so a retried call overwrites the same item
    rather than double-counting a session. Only the fields analyse() and
    the dashboard need are kept; the goal text never leaves the browser.
    """
    user_id = body["_authenticated_user_id"]
    rec = body.get("record") or {}

    started_at = str(rec.get("started_at") or "")
    try:
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("record.started_at must be an ISO-8601 timestamp")

    condition = rec.get("condition")
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError("record.condition is required")

    item = {
        "userId": user_id,
        "recordKey": f"EXP#{started_at}",
        "recordType": "experiment_session",
        "condition": condition.strip()[:64],
        "started_at": started_at,
        "ended_at": str(rec.get("ended_at") or "")[:40],
        "initiation_latency_s": _num(rec.get("initiation_latency_s")),
        "session_duration_s": _num(rec.get("session_duration_s")),
        "event_count": _num(rec.get("event_count")),
        "completed": bool(rec.get("completed")),
        "end_reason": str(rec.get("end_reason") or "")[:40],
        "initiation_capped": bool(rec.get("initiation_capped")),
        "loggedAt": int(time.time()),
    }
    _ddb_table.put_item(Item=item)
    return {"logged": True, "recordKey": item["recordKey"]}


def _handle_nightly(body):
    """This user's analysis, on demand, from DynamoDB. Sessions in the body
    are ignored: the browser no longer gets to supply the data it is judged
    on. The EventBridge schedule runs the same thing for every user."""
    user_id = body["_authenticated_user_id"]
    return _nightly_for_user(user_id, body)


def _nightly_for_user(user_id, opts):
    sessions = _load_experiment_sessions(user_id)
    result = _run_nightly(sessions, opts)
    try:
        _ddb_table.put_item(Item={
            "userId": user_id,
            "recordKey": "NIGHTLY#latest",
            "recordType": "nightly",
            "ranAt": int(time.time()),
            "result": _to_ddb(result),
        })
    except Exception as e:
        print(f"[nightly] result not stored: {type(e).__name__}: {e}")
    return result


def _run_nightly_all_users():
    """EventBridge entry point. Scan is fine at this scale (a handful of
    users, one run a night); at real scale this becomes a sparse GSI on
    users with EXP# records, or a per-user fan-out from Step Functions."""
    users, kwargs = set(), {
        "ProjectionExpression": "userId",
        "FilterExpression": Attr("recordKey").begins_with("EXP#"),
    }
    while True:
        resp = _ddb_table.scan(**kwargs)
        users.update(i["userId"] for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    statuses = {}
    for uid in sorted(users):
        try:
            statuses[uid] = _nightly_for_user(uid, {}).get("status")
        except Exception as e:
            statuses[uid] = f"error: {type(e).__name__}"
            print(f"[nightly] {uid[:8]}... failed: {type(e).__name__}: {e}")
    print(f"[nightly] ran for {len(users)} users: {sorted(statuses.values())}")
    return {"users": len(users), "statuses": statuses}


def _run_nightly(sessions, opts):
    """The whole nightly loop, gate first.

    The gate is deterministic and runs ALWAYS. The analyst is a model call
    and runs ONLY on what the gate cleared. That ordering is the guarantee:
    there is no path to a user-facing finding that skips the gate.
    """
    body = opts
    result = analyse(
        sessions,
        metric=body.get("metric", "initiation_latency_s"),
        lower_is_better=bool(body.get("lower_is_better", True)),
        arms=body.get("arms") or EXPERIMENT_ARMS or None,
    )

    # still_learning, or cleared but the arms are indistinguishable.
    # Both are honest answers and neither needs a model.
    if not result["reportable"] or result["finding"] is None:
        return {**result, "writeup": None}

    arms = sorted(result["counts"])
    a, b = arms[0], arms[-1]
    c = {
        "metric": body.get("metric_label", body.get("metric", "initiation_latency_s")),
        "arm_a": a, "a_n": result["counts"][a], "a_mean": result["means"][a],
        "arm_b": b, "b_n": result["counts"][b], "b_mean": result["means"][b],
        "effect_size": result["effect_size"],
        "lower_is_better": bool(body.get("lower_is_better", True)),
    }

    try:
        agent = _agent("analyst", ANALYST_PROMPT)
        out = _structured(agent, build_analyst_user(c), AnalystOutput,
                          parse_drift_output)
    except Exception as e:
        # The finding is real -- the gate said so. Only the prose failed.
        # Hand back the numbers and let the UI render its own plain version
        # rather than losing a result that was honestly earned.
        return {**result, "writeup": None,
                "writeup_error": f"{type(e).__name__}: {e}"}

    passed, checks = validate_analyst(out)
    if not passed:
        # Guardrail rejected the wording. Never ship unvalidated prose about
        # a person -- drop the writeup, keep the numbers.
        failed = [r for r, ok, _ in checks if not ok]
        return {**result, "writeup": None, "writeup_rejected": failed}

    return {**result, "writeup": out}


def _friction_model(system, user):
    """The one model call friction makes. Plain text, parsed and validated
    inside friction_probe, which falls back to its own template on any error."""
    return str(_agent("friction", system)(user))


def _handle_friction(body):
    """Nightly friction check, run on the first dashboard open of the day.

    Detectors, protocol and gate are pure Python; at most one model call
    (plus one repair). Quiet day = zero calls. Stores nothing: the tab
    timeline is read and discarded, never written to DynamoDB. It can't run
    from the EventBridge path for the same reason -- only the browser has it.
    """
    # Seed the arm order with the Cognito id, like assign does, so nobody
    # can choose theirs. Whatever the browser sent is ignored.
    body["user_id"] = body["_authenticated_user_id"]
    return run_friction(body, _friction_model)


def _handle_heavy(body):
    """The user tapped 'this one feels heavy'. User-stated, never inferred."""
    body["user_id"] = body["_authenticated_user_id"]
    return run_heavy(body, _friction_model)


def _guide_model(system, user):
    return str(_agent("guide", system)(user))


def _handle_guide(body):
    """The guide chat in the Friction panel. Answers only from the trusted
    index in guide_probe; self-harm, medication and "do I have ADHD?" get
    fixed replies before any model call. One call per message at most (plus
    one repair). Stores nothing."""
    # Imported here, not at the top: a problem in the guide can only break
    # the guide, never the other actions.
    from guide_probe import run_guide
    return run_guide(body, _guide_model)


_HANDLERS = {
    "decompose": _handle_decompose,
    "churn": _handle_churn,
    "assign": _handle_assign,
    "initiate": _handle_initiate,
    "session": _handle_session,
    # Old name, same handler. The UI can move when it moves.
    "amend": _handle_session,
    "nightly": _handle_nightly,
    "log_session": _handle_log_session,
    "drift": _handle_drift,
    "reentry": _handle_reentry,
    "friction": _handle_friction,
    "heavy": _handle_heavy,
    "guide": _handle_guide,
}


def _response(status, payload):
    return {"statusCode": status,
            "body": json.dumps(payload, default=_json_default)}


def handler(event, context):
    # Scheduled nightly run. EventBridge invokes directly with no token, so
    # this has to come before authentication. A Function URL request always
    # has requestContext, so the public URL cannot take this path.
    if event.get("source") == "aws.events" and "requestContext" not in event:
        return _run_nightly_all_users()

    # Function URL -> the JSON payload is in event["body"].
    try:
        user_id = _authenticate(event)
    except Exception as e:
        print(f"[auth] rejected request: {type(e).__name__}: {e}")
        return _response(401, {"error": "unauthorized"})

    try:
        raw = event.get("body")
        body = json.loads(raw) if isinstance(raw, str) else dict(event)
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except ValueError as e:
        return _response(400, {"error": f"bad request body: {e}"})

    # Server-derived identity always wins over anything supplied by the
    # browser. Handlers read only this field for identity.
    body["_authenticated_user_id"] = user_id

    action = body.get("action")
    fn = _HANDLERS.get(action)
    if fn is None:
        return _response(400, {
            "error": f"unknown action {action!r}, expected one of {list(_HANDLERS)}"
        })

    try:
        result = fn(body)

        # Give every backend-created session a stable, time-sortable id. It
        # rides inside the session object on later amend calls, so updates
        # replace the same DynamoDB item rather than adding one per turn.
        if action in {"initiate", "session", "amend"} and isinstance(result, dict):
            session = result.get("session")
            if isinstance(session, dict):
                session.setdefault("session_id", _new_session_id())
                _persist_session(user_id, session)

        return _response(200, result)

    except PermissionError as e:
        print(f"[auth] permission error: {e}")
        return _response(403, {"error": "forbidden"})

    except ValueError as e:
        # Our own input checks (log_session, assign). Safe to show.
        return _response(400, {"error": str(e)})

    except Exception as e:
        # Details go to CloudWatch, not to the browser.
        print(f"[handler] {action}: {type(e).__name__}: {e}")
        return _response(500, {"error": f"{action} failed ({type(e).__name__})"})
