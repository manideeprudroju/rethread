# Rethread

**A calm way back into your work after you get pulled away.**

Built for the WeMakeDevs × AWS Bharat Builds Tour — First Commit (17–20 Sept 2026).

---

## The problem

Getting interrupted mid-task is normal. What isn't normal-sized is the cost of
coming back: re-reading five tabs, trying to remember what you were even
doing, and often just... not resuming at all. Existing productivity tools
track time or nag about focus — neither actually helps you *restart*.

Rethread does one thing: it watches which tabs you're actually using (title
and domain only, never page content), and when you come back after being
away, it tells you — plainly, without judging you — what you were doing and
what to do next.

## What it does

- **State your intent** in one sentence. An AWS Bedrock-backed agent turns it
  into one concrete first action plus a few if/then plans for when you get
  stuck — no vague "get started" advice.
- **Work normally.** A lightweight Chrome extension watches your tabs in the
  background. No manual logging, no button to click.
- **Come back whenever.** Hit "Continue session" and the app rebuilds what
  you were doing from your actual browsing — or tells you plainly there's
  nothing to hand back, which is a common and correct answer, not an error.

## What it deliberately doesn't do

Per our own design rules (enforced by validators on the backend, not just
described here): it never shows you a distraction count, never says "you
got distracted," never scores or characterises you, and never shows raw
page content. The signal that a tab was off-task exists only to inform the
agent — it is never surfaced as a stat or an accusation.

## Architecture

```
Chrome extension  --(tab title/domain/timestamp)-->  content script
                                                            |
                                                    window.postMessage
                                                            v
                                              React app (sessionStore.js)
                                                            |
                                                   fetch (Lambda Function URL)
                                                            v
                                          AWS Lambda "rethread-agents"
                                          (Strands + Bedrock, deepseek.v3.2)
                                          -> decompose / drift / churn / reentry
```

## Tech stack

- **Frontend:** React (Vite), no external UI library — custom components
- **Tab capture:** Chrome Extension (Manifest V3)
- **Backend:** AWS Lambda, single function, action-routed
- **Agents:** Strands Agents SDK + AWS Bedrock (Mantle endpoint, deepseek.v3.2)

## Running it locally

### 1. Frontend

```bash
npm install
cp .env.example .env.local
# edit .env.local and set VITE_LAMBDA_URL to the real Lambda Function URL
npm run dev
```

### 2. Chrome extension (real tab capture)

1. Go to `chrome://extensions`
2. Enable **Developer mode**
3. **Load unpacked** → select the `extension/` folder
4. Make sure the app is running at the URL listed in
   `extension/manifest.json`'s `host_permissions` (defaults to
   `http://localhost:5173`)

### 3. Backend

The Lambda function and its deployment steps live in the backend half of
this repo — see its own README for setup (AWS credentials, Bedrock model
access, deployment).

## Team

- Frontend / UI — [your name]
- Backend / agents — [your friend's name]

## Hackathon

WeMakeDevs × AWS Bharat Builds Tour, First Commit — 17–20 September 2026.
