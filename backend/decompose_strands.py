"""
decompose_strands.py — production Strands + Bedrock path with shared guardrail.

The production guardrail lives in decompose_guardrail.py. Strands uses the same
machine-checkable contract without importing the throwaway probe script.

What's NEW: the model-calling layer. structured_output() replaces
call_bedrock/call_deepinfra + parse_output(). Strands enforces the Pydantic
schema itself, so there's no raw text to regex JSON out of, and no
<think>-tag stripping to remember -- the exact gap found in
reentry_probe.py's parse_output() can't happen here, because there's no
hand-rolled parse_output() at all.

Run strands_smoke_test.py first. If that fails, this will too, for the
same reason.

Usage:
    python decompose_strands.py --case concrete
    python decompose_strands.py --case aversive --clarify
    python decompose_strands.py --goal "your own goal" --answers "where it lives, when it's due"
    python decompose_strands.py --all --clarify
"""

import argparse

from pydantic import BaseModel, ConfigDict, Field

from model_provider import build_agent as _build_agent
from model_provider import describe

# Production guardrail is shared and kept separate from the throwaway probe.
from decompose_guardrail import (
    CASES,
    CLARIFY_STRICT,
    filter_clarify_questions,
    SYSTEM_PROMPT,
    failure_summary,
    report,
    safe_fallback,
    validate,
)


class Plan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    if_: str = Field(alias="if")
    then: str


class DecomposeOutput(BaseModel):
    first_action: str
    plans: list[Plan]

    def as_dict(self):
        # by_alias=True turns if_ back into "if" -- matches the plain dict
        # shape decompose_probe.validate() already expects, unchanged.
        return self.model_dump(by_alias=True)


class ClarifyOutput(BaseModel):
    # `| None` matters. Models return {"questions": null} to mean "nothing to
    # ask", and a bare list[str] rejects that -- Strands then retries, up to
    # nine times on the starved case, to arrive at the same answer.
    questions: list[str] | None = Field(default=None)


def build_agent(system_prompt, region, model_id, role="decompose"):
    return _build_agent(role, system_prompt, model_id=model_id, region=region)


def run_one(goal, canned, args):
    context = goal
    qa_block = ""
    decompose_agent = build_agent(SYSTEM_PROMPT, args.region, args.model)

    if args.clarify:
        # CLARIFY_STRICT and role "clarify" are what lambda_handler runs. The
        # plain CLARIFY_PROMPT on the decompose role tested neither, so a
        # clean run here said nothing about production.
        clarify_agent = build_agent(CLARIFY_STRICT, args.region, args.model,
                                    role="clarify")
        out = clarify_agent(
            f'Goal, in their words: "{goal}"', structured_output_model=ClarifyOutput
        ).structured_output
        qs = filter_clarify_questions(goal, out.questions or [])
        if qs:
            print("  ASKED:")
            for q in qs:
                print("    ?", q)
            answers = canned
            print("  ANSWERED (canned):", answers)
            qa_block = "\n\nThey were asked and replied:\n" + answers
            context = goal + " " + answers
        else:
            print("  ASKED NOTHING - goal already had what it needed")
        print()

    user_msg = f'Goal, in their words: "{goal}"' + qa_block

    def attempt(msg):
        try:
            result = decompose_agent(msg, structured_output_model=DecomposeOutput)
            return result.structured_output.as_dict(), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    out, err = attempt(user_msg)
    checks = None
    if out is not None:
        passed, checks = validate(out, context)
        if passed or args.no_repair:
            return out, context

    # ONE repair turn, same policy as decompose_probe.py: never loop.
    reason = err if out is None else failure_summary(checks)
    print("  REPAIRING:", str(reason)[:110])
    repair_msg = (
        user_msg
        + "\n\nYour previous answer was REJECTED for: " + str(reason)
        + "\nFix exactly those problems. If the reason is an invented "
          "identifier or location, remove the invented detail and stay "
          "general - do not swap it for a different made-up one."
    )
    out2, err2 = attempt(repair_msg)
    if out2 is not None:
        passed2, _ = validate(out2, context)
        if passed2:
            print("  repair succeeded")
            return out2, context
        print("  repair failed validation too")
    else:
        print("  repair call failed:", err2)

    print("  -> deterministic fallback")
    fb = safe_fallback(goal, context)
    fb_ok, fb_checks = validate(fb, context, mode="fallback")
    if not fb_ok:
        raise RuntimeError(
            "deterministic fallback failed its own guardrail: "
            + failure_summary(fb_checks)
        )
    return fb, context


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=list(CASES), default="concrete")
    ap.add_argument("--goal", default=None, help="your own goal, overrides --case")
    ap.add_argument("--answers", default="", help="canned answers, used with --goal --clarify")
    ap.add_argument("--all", action="store_true", help="run every built-in case")
    ap.add_argument("--region", default=None)
    ap.add_argument("--model", default=None, help="override the configured model id")
    ap.add_argument("--clarify", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    args = ap.parse_args()

    if args.goal:
        goals = [("custom", {"goal": args.goal, "answers": args.answers})]
    elif args.all:
        goals = list(CASES.items())
    else:
        goals = [(args.case, CASES[args.case])]

    results = []
    for name, spec in goals:
        goal, canned = spec["goal"], spec.get("answers", "")
        print(f"\n### {name}")
        out, context = run_one(goal, canned, args)
        results.append((name, report(goal, out, context)))

    if len(results) > 1:
        print("\nSUMMARY")
        for name, ok in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")


if __name__ == "__main__":
    main()
