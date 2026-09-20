@echo off
REM Windows deploy for lambda_handler.py. No WSL, no Git Bash needed.
REM Run from the folder holding lambda_handler.py, the probe files and the
REM three policy JSON files (trust, permissions, dynamodb).
REM
REM First run creates the IAM role, then the function.
REM Safe to re-run: after that it just pushes updated code and policies.
REM
REM Before packaging it checks that every module is in the folder and runs
REM the friction and guide checks (no model, no AWS). SKIP_CHECKS=1 skips
REM the checks. Set ACCESS_TOKEN to a Cognito access token to also smoke-test
REM friction and the guide after deploy.

setlocal enabledelayedexpansion

if "%FUNCTION_NAME%"=="" set FUNCTION_NAME=rethread-agents
if "%REGION%"=="" set REGION=us-east-1
if "%ROLE_NAME%"=="" set ROLE_NAME=rethread-lambda-role

echo == account ==
for /f "delims=" %%i in ('aws sts get-caller-identity --query Account --output text') do set ACCOUNT_ID=%%i
if "%ACCOUNT_ID%"=="" (
  echo Could not read AWS account. Is 'aws configure' done?
  exit /b 1
)
set ROLE_ARN=arn:aws:iam::%ACCOUNT_ID%:role/%ROLE_NAME%
echo account %ACCOUNT_ID%

REM Every local module lambda_handler needs, transitively. ONE list: the
REM check below and the zip step both read it. guide_probe is imported only
REM when the guide is called, but it still has to be in the zip.
set MODULES=lambda_handler.py model_provider.py decompose_guardrail.py decompose_probe.py drift_probe.py reentry_probe.py experiment_probe.py analyst_probe.py reentry_validate.py initiate_probe.py seq_check.py friction_probe.py guide_probe.py

echo.
echo == modules ==
REM A missing module deploys fine and then fails EVERY action at invoke time
REM with Runtime.ImportModuleError. Catch it here instead.
set MISSING=
for %%F in (%MODULES%) do if not exist "%%F" set MISSING=!MISSING! %%F
if not "!MISSING!"=="" (
  echo Missing from this folder:!MISSING!
  echo Copy them next to lambda_handler.py and run again.
  exit /b 1
)
echo all 13 modules present

echo.
echo == checks: friction and guide, no model, no AWS ==
if "%SKIP_CHECKS%"=="1" (
  echo skipped because SKIP_CHECKS=1
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo python is not on PATH. Set SKIP_CHECKS=1 to deploy without the checks.
    exit /b 1
  )
  set PYTHONIOENCODING=utf-8
  python guide_probe.py > checks.log 2>&1
  if errorlevel 1 (
    type checks.log
    echo guide checks failed - not deploying
    exit /b 1
  )
  python friction_probe.py > checks.log 2>&1
  if errorlevel 1 (
    type checks.log
    echo friction checks failed - not deploying
    exit /b 1
  )
  del checks.log
  echo guide and friction checks pass
)

echo.
echo == packaging ==
if exist package rmdir /s /q package
if exist deployment.zip del deployment.zip

REM Lambda runs LINUX and PYTHON 3.12. A plain `pip install` on Windows
REM fetches Windows binaries (.pyd) for compiled packages like pydantic_core
REM and cryptography, which fail at import inside Lambda. These flags force
REM Linux wheels for the right Python version. --only-binary is required
REM with --platform. Both platform tags are safe on the python3.12 runtime
REM (Amazon Linux 2023); the second lets pip use newer wheels.
REM
REM PyJWT[crypto]: lambda_handler verifies Cognito tokens with RS256, which
REM needs `cryptography`. Plain PyJWT imports fine and then rejects every
REM token, so every request returns 401.
pip install --target package ^
  --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 ^
  --python-version 3.12 --only-binary=:all: --implementation cp ^
  "strands-agents[openai]" pydantic aws-bedrock-token-generator "PyJWT[crypto]" -q
if errorlevel 1 (
  echo pip install failed
  exit /b 1
)

REM aws-bedrock-token-generator is named explicitly above, not left to the
REM [openai] extra. It went missing once when pip hit a version conflict,
REM and its absence shows up only at runtime as ModuleNotFoundError.

REM boto3/botocore ship with the Lambda Python runtime already.
REM Dropping them takes the zip from ~30MB to ~14MB.
if exist package\boto3 rmdir /s /q package\boto3
if exist package\botocore rmdir /s /q package\botocore

powershell -NoProfile -Command "Compress-Archive -Path 'package\*' -DestinationPath 'deployment.zip' -Force"
REM The local modules, from the MODULES list above: 'a.py','b.py',...
set PSLIST=
for %%F in (%MODULES%) do set PSLIST=!PSLIST!,'%%F'
set PSLIST=!PSLIST:~1!
powershell -NoProfile -Command "Compress-Archive -Path !PSLIST! -DestinationPath 'deployment.zip' -Update"
if errorlevel 1 (
  echo adding the modules to deployment.zip failed
  exit /b 1
)

for %%A in (deployment.zip) do echo package size: %%~zA bytes

echo.
echo == IAM role ==
REM Two inline policies, each re-applied on every run. DynamoDB has its own
REM policy on purpose: put-role-policy REPLACES a policy wholesale, so any
REM permission added by hand to rethread-bedrock-and-logs is wiped the next
REM time this runs. Everything the function needs lives in these files.
aws iam get-role --role-name %ROLE_NAME% >nul 2>&1
if errorlevel 1 (
  echo creating role %ROLE_NAME%
  aws iam create-role --role-name %ROLE_NAME% --assume-role-policy-document file://trust-policy.json >nul
  aws iam put-role-policy --role-name %ROLE_NAME% --policy-name rethread-bedrock-and-logs --policy-document file://permissions-policy.json
  aws iam put-role-policy --role-name %ROLE_NAME% --policy-name rethread-dynamodb --policy-document file://dynamodb-policy.json
  echo waiting for IAM propagation...
  timeout /t 12 /nobreak >nul
) else (
  echo role exists - refreshing policies
  aws iam put-role-policy --role-name %ROLE_NAME% --policy-name rethread-bedrock-and-logs --policy-document file://permissions-policy.json
  aws iam put-role-policy --role-name %ROLE_NAME% --policy-name rethread-dynamodb --policy-document file://dynamodb-policy.json
)

echo.
echo == function ==
aws lambda get-function --function-name %FUNCTION_NAME% --region %REGION% >nul 2>&1
if errorlevel 1 (
  echo creating %FUNCTION_NAME%
  aws lambda create-function --function-name %FUNCTION_NAME% ^
    --runtime python3.12 --handler lambda_handler.handler ^
    --zip-file fileb://deployment.zip --role %ROLE_ARN% ^
    --timeout 60 --memory-size 512 --region %REGION% >nul
  echo waiting for function to go Active...
  aws lambda wait function-active --function-name %FUNCTION_NAME% --region %REGION%
) else (
  echo updating %FUNCTION_NAME% code
  aws lambda update-function-code --function-name %FUNCTION_NAME% ^
    --zip-file fileb://deployment.zip --region %REGION% >nul
  REM Wait for the new code to be live. A fixed sleep could smoke-test
  REM the previous version and report it as this one.
  echo waiting for the update to finish...
  aws lambda wait function-updated --function-name %FUNCTION_NAME% --region %REGION%
)

REM Every browser request needs a Cognito token, so these smoke tests do not
REM call the agent actions directly (a direct invoke has no token). They
REM check the two things a deploy can break: the auth gate, and DynamoDB
REM access through the scheduled nightly path. The model path is checked
REM by signing in to the dashboard.

echo.
echo == smoke test 1: auth gate (expect statusCode 401) ==
aws lambda invoke --function-name %FUNCTION_NAME% --region %REGION% ^
  --payload "{\"action\":\"churn\",\"events\":[]}" ^
  --cli-binary-format raw-in-base64-out out.json >nul
type out.json
echo.
echo   401 = the function loaded and is rejecting requests with no token.
echo   An errorMessage mentioning ImportModuleError or 'jwt' = packaging problem.

echo.
echo == smoke test 2: nightly run, the EventBridge path (expect "users") ==
aws lambda invoke --function-name %FUNCTION_NAME% --region %REGION% ^
  --payload "{\"source\":\"aws.events\",\"detail-type\":\"Scheduled Event\"}" ^
  --cli-binary-format raw-in-base64-out out.json >nul
type out.json
echo.
echo   {"users": N, ...} = DynamoDB reads and writes work (N is 0 until
echo   someone finishes a session). AccessDeniedException = dynamodb-policy.json
echo   is missing or names a different table.

echo.
if "%ACCESS_TOKEN%"=="" (
  echo == smoke test 3 skipped: set ACCESS_TOKEN to test friction and the guide ==
  echo   Sign in to the dashboard, open DevTools ^> Application ^> Session Storage,
  echo   key oidc.user:..., copy its access_token. Then: set ACCESS_TOKEN=^<token^>
) else (
  echo == smoke test 3: friction and the guide, with your token ==
  > smoke.json echo {"headers":{"authorization":"Bearer %ACCESS_TOKEN%"},"requestContext":{},"body":"{\"action\":\"friction\",\"events\":[],\"sessions\":[]}"}
  aws lambda invoke --function-name %FUNCTION_NAME% --region %REGION% --payload fileb://smoke.json out.json >nul
  type out.json
  echo.
  echo   expect statusCode 200 and nothing_found. A 500 on every action = a module missing from the zip.
  echo.
  > smoke.json echo {"headers":{"authorization":"Bearer %ACCESS_TOKEN%"},"requestContext":{},"body":"{\"action\":\"guide\",\"message\":\"do I have adhd\"}"}
  aws lambda invoke --function-name %FUNCTION_NAME% --region %REGION% --payload fileb://smoke.json out.json >nul
  type out.json
  echo.
  echo   expect kind self_assessment: the fixed reply, no model call
  echo.
  > smoke.json echo {"headers":{"authorization":"Bearer %ACCESS_TOKEN%"},"requestContext":{},"body":"{\"action\":\"guide\",\"message\":\"why is starting so hard\"}"}
  aws lambda invoke --function-name %FUNCTION_NAME% --region %REGION% --payload fileb://smoke.json out.json >nul
  type out.json
  echo.
  echo   expect kind answer. kind fallback = the model call failed: look for [guide] in the logs.
  echo   A 401 on all three = the token expired or is an ID token, not an access token.
  del smoke.json
)

echo.
echo Logs: aws logs tail /aws/lambda/%FUNCTION_NAME% --follow --region %REGION%
endlocal
