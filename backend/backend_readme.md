# Rethread backend

One AWS Lambda function (`rethread-agents`, Python 3.12, handler `lambda_handler.handler`, 512 MB, 60 s timeout) behind a Lambda Function URL. Every agent is an action on this one function. Region `us-east-1`.

- **Browser path:** the dashboard POSTs `{"action": "...", ...}` to the Function URL with `Authorization: Bearer <Cognito access token>`.
- **Scheduled path:** an EventBridge rule invokes the function every night to run the experiment analysis for every user.

## Files

| File | What it is |
|---|---|
| `lambda_handler.py` | Entry point. Verifies the token, routes by `action`, reads and writes DynamoDB. |
| `model_provider.py` | The one place that decides which model each agent role uses and how it connects to Bedrock. |
| `decompose_probe.py`, `decompose_guardrail.py` | First move and if-then plans: prompt, and the validator every plan must pass (grounding, banned phrases). |
| `seq_check.py` | Rejects if-then plans that are just the next step in disguise ("if the file opens, then..."): triggers must be real sticking points. |
| `initiate_probe.py` | Opening turn (up to two clarifying questions) and the mid-session chat (`session`). |
| `drift_probe.py` | Drift prompt and parser (is this tab part of the declared work?) and the deterministic churn detector. |
| `reentry_probe.py`, `reentry_validate.py` | Re-entry reconstruction and its validator (no focus/drift words, nothing about the person, key tabs traced to real events). |
| `experiment_probe.py` | Condition assignment (balanced shuffled blocks) and the gate (exact permutation test, 8 sessions per condition). |
| `analyst_probe.py` | Writes up a result only after the gate clears; rejects trait claims, causes and overstatement. |
| `friction_probe.py` | Friction check: four deterministic detectors, 10-entry evidence base, one guarded plan. 60 built-in checks. |
| `guide_probe.py` | Guide chat: trusted-source index, BM25 search, safety routing before any model call. 44 built-in checks. Imported only when the guide is called. |
| `local_check.py` | Pre-deploy check, no AWS needed (see below). |
| `deploy.bat` | Windows deploy: checks, package, create or update role and function, smoke tests. |
| `cors.json` | CORS for the Function URL (Amplify site and `localhost:5173`). |
| `trust-policy.json`, `permissions-policy.json`, `dynamodb-policy.json` | IAM role trust and permissions. |

## Actions

| Action | What it does | Model calls |
|---|---|---|
| `decompose` | Goal (+ where and when) to one first action and if-then plans | 1, validated, safe fallback |
| `initiate` | Opening turn: up to two clarifying questions, then the session object | 1 per step |
| `session` (alias `amend`) | Mid-session chat: returns one next action; the session object carries the history | 1 per turn |
| `drift` | Is this tab part of the work? Used to measure time to start | 1, plain text; fails open (`relevant: true`, `_fallback: true`) |
| `churn` | On the work but spinning | 0, deterministic |
| `reentry` | Rebuilds goal, key tabs and next action after an interruption | 1, validated |
| `assign` | Experiment condition for a new session, seeded by the user's Cognito id | 0 |
| `log_session` | Stores one finished session's measurements (`EXP#`) | 0 |
| `nightly` | Runs the gate on this user's logged sessions; analyst only if it clears | 0, or 1 when the gate clears |
| `friction` | Weekly pattern check and one if-then plan for the next session | 0 on a quiet day, else 1 (+1 repair) |
| `heavy` | User tapped "this one feels heavy": one plan for now | 1 |
| `guide` | Chat answer from the trusted index | 0 for safety replies, else 1 |
| `save_session` | Saves the browser's session object (`SESSION#<session_id>`) when a session starts and when it ends. Rejects anything over 100 KB; a malformed id gets a fresh one | 0 |
| `history` | This user's sessions, newest first (`limit`, default 30, max 100): goal, step, start, end and the full session object, so the dashboard can list them and continue an unfinished one on any device | 0 |

An unknown action returns 400 with the list of valid actions.

## Auth

The Function URL is public (`AuthType NONE`), so the handler authenticates every request itself:

- The Cognito access token is verified against the user pool's JWKS (RS256), with `token_use == "access"` and the app client id checked.
- The user's identity is the token's `sub`. Any user id in the body is ignored.
- No token, or a bad one: 401.
- The EventBridge path is recognised by `source == "aws.events"` and no `requestContext`. The public URL cannot reach it, because a direct invoke needs `lambda:InvokeFunction`.

`PyJWT` must be installed with `[crypto]`, or RS256 can't be verified and every request returns 401.

## Models

Agents are built with the Strands Agents SDK. `model_provider.py` maps each role (`decompose`, `drift`, `reentry`, `analyst`, `amend`, `clarify`, `friction`, `guide`) to a model.

- **Default (`PROBE_PROVIDER=bedrock_mantle`):** Amazon Bedrock's OpenAI-compatible Mantle endpoint, every role on `deepseek.v3.2`. Bedrock Converse is blocked on this account. Tokens are minted at run time from the Lambda's IAM role, so there are no API keys.
- **`PROBE_PROVIDER=bedrock`:** Bedrock Converse, with Claude Haiku 4.5 for `drift`, `clarify` and `guide` and Claude Sonnet 4.5 for the rest. No code change needed.
- Any role can be overridden with its own variable (`DRIFT_MODEL`, `GUIDE_MODEL`, ...).
- Every agent call starts with an empty conversation (`agent.messages.clear()`): warm containers never carry one user's data into another's call, and prompts don't grow.

## Data

One DynamoDB table, `RethreadData`: partition key `userId`, sort key `recordKey`.

| `recordKey` | Holds |
|---|---|
| `SESSION#<ms>-<rand>` | One session: saved when it starts (`save_session`), after every chat turn, and when it ends. Holds goal, where and when, step, plans, chat turns, `started_at`, `ended_at`, `end_reason`. The browser makes the id, so all three saves update one item. |
| `EXP#<started_at>` | One finished session's measurements (condition, time to start, duration, ended how). No goal, location or due date. |
| `NIGHTLY#latest` | The latest nightly result for that user |

The tab timeline is never stored here. The friction check receives it for one call and discards it.

On sign-in the dashboard calls `history`, lists the sessions, and continues the newest one if it is unfinished, started in the last 12 hours, and wasn't ended in that browser. A session continued on a second device sends no experiment record from there: its start happened on the first device, and a second record would overwrite the one measured there.

## Environment variables

All have defaults, so none are required.

| Variable | Default |
|---|---|
| `COGNITO_REGION` | `us-east-1` |
| `COGNITO_USER_POOL_ID` | `us-east-1_9Ea0gBsLs` |
| `COGNITO_CLIENT_ID` | the dashboard's app client |
| `DDB_TABLE_NAME` | `RethreadData` |
| `EXPERIMENT_ARMS` | `first_step_only,step_and_plans` |
| `PROBE_PROVIDER` | `bedrock_mantle` (or `bedrock`) |
| `PROBE_REGION` | `us-east-1` |
| `<ROLE>_MODEL` | per role, see `model_provider.py` |
| `AMEND_TEMPERATURE`, `GUIDE_TEMPERATURE` | `0.4` (every other role runs at 0) |

## Check, then deploy

Needs Python 3.12 and the AWS CLI configured for the account. From this folder:

```
pip install boto3 "PyJWT[crypto]" pydantic
python local_check.py
deploy.bat
```

`local_check.py` needs no AWS and makes no model calls. It:
1. compiles every module in `deploy.bat`'s list
2. runs the friction (60) and guide (44) checks against the real shared validators
3. drives the real handler through `friction`, `heavy` and `guide` with fake auth and a fake model, and confirms every model call starts with an empty history
4. confirms every module the handler imports is in the deployment package

`deploy.bat`:
1. refuses to continue if a module is missing, and runs the friction and guide checks (`SKIP_CHECKS=1` skips them)
2. installs Linux wheels into `package/` and builds `deployment.zip`
3. creates the IAM role and function on the first run, or updates the code and policies after that
4. smoke-tests the live function: the auth gate (expects 401) and the nightly path with real DynamoDB access. With `ACCESS_TOKEN` set to a Cognito access token, it also tests `friction` and `guide`.

Overrides: `FUNCTION_NAME` (default `rethread-agents`), `REGION` (default `us-east-1`), `ROLE_NAME` (default `rethread-lambda-role`).

## CORS

Allowed origins are in `cors.json`, with no trailing slash. After changing it:

```
aws lambda update-function-url-config --function-name rethread-agents --region us-east-1 --cors file://cors.json
```

## Nightly schedule

EventBridge rule `rethread-nightly`, `cron(30 20 * * ? *)`, which is 02:00 IST. It was created with:

```
aws events put-rule --name rethread-nightly --schedule-expression "cron(30 20 * * ? *)" --region us-east-1
aws lambda add-permission --function-name rethread-agents --statement-id rethread-nightly --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn arn:aws:events:us-east-1:<account-id>:rule/rethread-nightly --region us-east-1
aws events put-targets --rule rethread-nightly --targets "Id=1,Arn=arn:aws:lambda:us-east-1:<account-id>:function:rethread-agents" --region us-east-1
```

## IAM

- `trust-policy.json`: lets Lambda assume the role.
- `permissions-policy.json`: CloudWatch Logs, Bedrock Mantle inference, `bedrock:InvokeModel` (for the Converse switch), and `GetItem`/`PutItem`/`UpdateItem`/`Query` on `RethreadData` only.
- `dynamodb-policy.json`: `PutItem`, `Query` and `Scan` on `RethreadData` only. Scan is used by the nightly run to find users with logged sessions.

## Logs

```
aws logs tail /aws/lambda/rethread-agents --follow --region us-east-1
```

## Never commit

`deployment.zip`, `package/`, `smoke.json` (it can contain an access token), `out.json`, `checks.log`, `__pycache__/`, `.env`.
