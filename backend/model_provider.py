"""
One place that decides how every probe talks to a model.

Why this exists: BedrockModel (bedrock-runtime / Converse) is blocked on
this account at the account level -- every model, including Nova. Mantle
is a different endpoint with a different IAM namespace and it works.
Claude models are gated there, so the probes run on deepseek.v3.2, which
is what the 92% drift score was measured on anyway.

If Bedrock support later unblocks bedrock-runtime, flip PROVIDER to
"bedrock" and nothing else changes.

Env overrides (no code edit needed):
    PROBE_PROVIDER    bedrock_mantle | bedrock
    PROBE_REGION      default us-east-1
    DECOMPOSE_MODEL / DRIFT_MODEL / REENTRY_MODEL
"""

import os

PROVIDER = os.environ.get("PROBE_PROVIDER", "bedrock_mantle")
REGION = os.environ.get("PROBE_REGION", "us-east-1")

# Mantle ids. No us./global. prefix, no -v1:0 suffix.
MANTLE_MODELS = {
    # Grounding rules do real work here -- use the strong model.
    "decompose": os.environ.get("DECOMPOSE_MODEL", "deepseek.v3.2"),
    "reentry": os.environ.get("REENTRY_MODEL", "deepseek.v3.2"),
    # Highest call volume. The probe's original question was whether a SMALL
    # model is accurate enough here; deepseek.v3.2 is the known-92% baseline
    # to measure candidates against. Cheap ones worth trying:
    #   openai.gpt-oss-20b, qwen.qwen3-32b, mistral.ministral-3-8b-instruct,
    #   nvidia.nemotron-nano-9b-v2, zai.glm-4.7-flash
    "drift": os.environ.get("DRIFT_MODEL", "deepseek.v3.2"),
    # Nightly analyst. Lowest volume in the system (once a night) and the
    # only output that talks about the PERSON rather than the work, so this
    # is the last place to economise on model quality.
    "analyst": os.environ.get("ANALYST_MODEL", "deepseek.v3.2"),
    # Mid-session amend. Same grounding pressure as decompose -- it must not
    # invent a filename or test name from a two-word message.
    "amend": os.environ.get("AMEND_MODEL", "deepseek.v3.2"),
    # The two-question opening turn.
    "clarify": os.environ.get("CLARIFY_MODEL", "deepseek.v3.2"),
}

# Converse ids, for if/when bedrock-runtime is unblocked.
BEDROCK_MODELS = {
    "decompose": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "reentry": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "drift": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "analyst": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "amend": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "clarify": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


# Everything that has to be exact stays at 0. The chat ("amend") gets a
# little room: at 0, replies to similar messages came back with the same
# opening and the same shape every time, which reads as a script. The
# validator still checks every reply. Set AMEND_TEMPERATURE=0 on the Lambda
# to go back without a code change.
TEMPERATURES = {
    "amend": float(os.environ.get("AMEND_TEMPERATURE", "0.4")),
}


def build_model(role, model_id=None, region=None, max_tokens=None):
    """role: 'decompose' | 'drift' | 'reentry' | 'analyst' | 'amend' | 'clarify'."""
    region = region or REGION
    temperature = TEMPERATURES.get(role, 0)

    if PROVIDER == "bedrock":
        from strands.models import BedrockModel
        return BedrockModel(
            region_name=region,
            model_id=model_id or BEDROCK_MODELS[role],
            temperature=temperature,
        )

    from strands.models.openai import OpenAIModel
    params = {"temperature": temperature}
    if max_tokens:
        params["max_tokens"] = max_tokens
    mid = model_id or MANTLE_MODELS[role]

    try:
        return OpenAIModel(
            bedrock_mantle_config={"region": region},
            model_id=mid,
            params=params,
        )
    except Exception as e:
        # bedrock_mantle_config failed to resolve. Seen in Lambda as
        # "OpenAIError: The api_key client option must be set", which means
        # Strands did not mint a token and fell through to plain OpenAI
        # client construction. Mint it ourselves and pass the same two
        # values Strands would have set.
        print(f"[model_provider] mantle_config failed ({type(e).__name__}: {e});"
              " minting token explicitly")
        from aws_bedrock_token_generator import provide_token

        token = provide_token(region=region)
        # /openai/v1 for gpt-5, grok-4, gemma-4; /v1 for everything else.
        path = "/openai/v1" if mid.startswith(
            ("openai.gpt-5.", "xai.grok-4.", "google.gemma-4-")) else "/v1"
        return OpenAIModel(
            client_args={
                "base_url": f"https://bedrock-mantle.{region}.api.aws{path}",
                "api_key": token,
            },
            model_id=mid,
            params=params,
        )


def build_agent(role, system_prompt, model_id=None, region=None, max_tokens=None):
    from strands import Agent
    return Agent(
        model=build_model(role, model_id, region, max_tokens),
        system_prompt=system_prompt,
    )


def describe():
    models = BEDROCK_MODELS if PROVIDER == "bedrock" else MANTLE_MODELS
    return f"provider={PROVIDER} region={REGION} models={models}"


# Mantle needs: pip install "strands-agents[openai]"
# That pulls in aws-bedrock-token-generator, which mints the bearer token
# from your AWS credential chain. Plain `pip install openai` is not enough.
