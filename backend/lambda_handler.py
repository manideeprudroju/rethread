"""
Single Lambda entrypoint for all three Strands-backed probes, routed by
event["action"]. One function, one deployment, three behaviours -- the
simplest thing that works for a demo. Split into separate functions later
only if you need independent scaling/concurrency/IAM per path.

Package alongside every module it imports (see the zip line below). A
missing one is not a per-action failure: the import is at module scope, so
the whole function fails to load and EVERY action returns 500.

DEPLOY (plain AWS CLI, no CDK/SAM required -- see deploy.sh for the full
scripted version of this):

    pip install --target package strands-agents pydantic
    cd package && zip -rq ../deployment.zip . && cd ..
    zip -g deployment.zip lambda_handler.py model_provider.py seq_check.py \
      decompose_guardrail.py drift_probe.py reentry_probe.py \
      initiate_probe.py experiment_probe.py analyst_probe.py

    aws lambda create-function \
      --function-name rethread-agents \
      --runtime python3.12 \
      --handler lambda_handler.handler \
      --zip-file fileb://deployment.zip \
      --role arn:aws:iam::<ACCOUNT_ID>:role/rethread-lambda-role \
      --timeout 30 --memory-size 512 --region us-east-1

TEST:
    aws lambda invoke --function-name rethread-agents \
      --payload '{"action":"decompose","goal":"fix the JWT refresh bug in auth.py"}' \
      --cli-binary-format raw-in-base64-out out.json && cat out.json
"""

import json
import os

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
    return _agents[role]


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
        return {"relevant": True, "reason": f"call failed ({type(e).__name__}), defaulted to relevant"}


def _handle_reentry(body):
    agent = _agent("reentry", REENTRY_PROMPT)
    user_msg = build_user_message(body)
    return _structured(agent, user_msg, ReentryOutput, parse_drift_output)


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
    idx = int(body.get("session_index", 0))
    # seed keys the shuffled assignment blocks per user. Without it every
    # user gets the same order of arms.
    seed = str(body.get("user_id") or body.get("seed") or "")
    return {"condition": assign_condition(idx, arms, seed=seed),
            "session_index": idx, "seed": seed}


def _handle_nightly(body):
    """The whole nightly loop, gate first.

    The gate is deterministic and runs ALWAYS. The analyst is a model call
    and runs ONLY on what the gate cleared. That ordering is the guarantee:
    there is no path to a user-facing finding that skips the gate.
    """
    result = analyse(
        body.get("sessions") or [],
        metric=body.get("metric", "initiation_latency_s"),
        min_per_arm=int(body.get("min_per_arm", 5)),
        lower_is_better=bool(body.get("lower_is_better", True)),
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


_HANDLERS = {
    "decompose": _handle_decompose,
    "churn": _handle_churn,
    "assign": _handle_assign,
    "initiate": _handle_initiate,
    "session": _handle_session,
    # Old name, same handler. The UI can move when it moves.
    "amend": _handle_session,
    "nightly": _handle_nightly,
    "drift": _handle_drift,
    "reentry": _handle_reentry,
}


def handler(event, context):
    # Direct `aws lambda invoke` with a raw JSON payload -> event IS the
    # payload. Behind API Gateway (proxy integration) -> the real payload
    # is JSON-encoded inside event["body"] instead. Handle both.
    body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

    action = body.get("action")
    fn = _HANDLERS.get(action)
    if fn is None:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"unknown action {action!r}, expected one of {list(_HANDLERS)}"}),
        }

    try:
        result = fn(body)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": f"{type(e).__name__}: {e}"})}
