# Rethread — Frontend

A calm way back into your work after an interruption.

The `frontend` directory contains the user-facing React application and Chrome extension for **Rethread**, built for the **WeMakeDevs × AWS Bharat Builds Tour — First Commit (17–20 September 2026)**.

## Overview

Rethread helps a user return to unfinished work after an interruption by preserving lightweight session context and using browser activity to reconstruct where they left off.

The frontend provides:
- Intent declaration and first-move generation
- Live session timer
- Session chatbot
- Lightweight browser activity capture through a Chrome extension
- Reentry experience for returning to an unfinished session
- Session completion and archiving
- Nightly summary

The frontend communicates with the backend through an AWS Lambda Function URL.

## Architecture

```text
                    RETHREAD FRONTEND

        React + Vite
             |
     +-------+-----------------------------+
     |                                     |
 Dashboard                          IntentPanel
     |                                     |
     |                              First move + plans
     |                                     |
     +------------------> Active Session <--+
                            |
                  +---------+---------+
                  |                   |
             LiveBadge            AmendChat
              (timer)             (chatbot)
                  |
                  |  Check where I left off
                  v
             ReentryPanel
                  |
                  v
          Resume / Start New
                  |
                  v
             Active Session

 Chrome Extension
 (background + content script)
          |
          | tab title / domain / timestamp
          v
     React sessionStore
          |
          | API requests
          v
 AWS Lambda Function URL
          |
          v
      Backend agents
```

## Project Structure

```text
frontend/
│
├── extension/
│   ├── background.js
│   ├── content-script.js
│   └── manifest.json
│
├── public/
│   ├── favicon.svg
│   └── icons.svg
│
├── src/
│   ├── AmendChat.jsx
│   ├── App.jsx
│   ├── Dashboard.jsx
│   ├── IntentPanel.jsx
│   ├── LiveBadge.jsx
│   ├── NightlySummaryPanel.jsx
│   ├── ReentryPanel.jsx
│   ├── api.js
│   ├── index.css
│   ├── main.jsx
│   └── sessionStore.js
│
├── index.html
├── package.json
├── package-lock.json
├── vite.config.js
└── .oxlintrc.json
```

## Application Flow

### 1. Dashboard

The Dashboard is the initial screen. Users can:
- Start a new session
- Continue an existing session
- View the nightly summary

### 2. Intent Declaration

`IntentPanel.jsx` lets the user describe what they are working on.

The frontend sends the declared intent to the backend and receives:
- A concrete first action
- Optional if/then plans for situations where the user gets stuck

The user reviews the first move and starts working.

### 3. Active Session

The active session combines:

**LiveBadge**
- Displays the session timer
- Shows the current first action
- Provides pause/resume control

**AmendChat**
- Provides the conversational interface during the active session
- Lets the user communicate with the session agent while working

The timer and chatbot are displayed together as one active-session experience.

### 4. Chrome Extension

The extension captures lightweight browser activity:
- Tab title
- Domain
- Timestamp

It does **not** capture raw webpage content.

Browser events are passed to the React application and stored as part of the current session context.

### 5. Reentry

Reentry is a separate panel from the active Timer + Chatbot panel.

The user can select **Check where I left off**. The frontend sends the declared intent and current browser activity to the backend Reentry agent.

The Reentry panel can present:
- What the user was doing
- Why it matters
- Relevant activity
- A suggested next action

The user can then resume the existing session or start something new.

### 6. Session Completion

When the user completes the session, the session is ended and archived.

The session store records information such as:
- Intent
- First action
- Plans
- Browser activity
- Session start/end times
- Initiation latency
- Session duration
- Experiment condition

### 7. Nightly Summary

The Nightly Summary is based on completed and archived sessions rather than the currently active session.

```text
Reentry
→ unfinished/current session
→ helps the user resume

Nightly
→ completed sessions
→ summary and experiment analysis
```

## Key Frontend Files

### `App.jsx`

Controls navigation between:
- Dashboard
- Intent
- Active session
- Reentry
- Nightly summary

### `sessionStore.js`

Central client-side session state manager.

It handles:
- Current session state
- Session timer
- Declared intent
- First action
- Plans
- Browser events
- Chat history
- Session condition
- Session start/end times
- Activity history
- Nightly session archive

It also listens for browser activity messages from the Chrome extension.

### `api.js`

Provides the frontend API layer for communicating with the backend Lambda Function URL.

It is used for operations including:
- Decomposition / intent processing
- Reentry
- Amend
- Experiment arm assignment
- Nightly analysis

### `Dashboard.jsx`

Displays the current session state and navigation options.

### `IntentPanel.jsx`

Handles the new-session intent workflow:

```text
User goal
   ↓
Backend decomposition
   ↓
First move + plans
   ↓
Start working
```

### `LiveBadge.jsx`

Displays the live session timer and pause/resume control.

### `AmendChat.jsx`

Provides the conversational interface during an active work session.

### `ReentryPanel.jsx`

Provides the dedicated reentry experience for an unfinished session.

### `NightlySummaryPanel.jsx`

Displays the nightly analysis generated from archived session data.

## Chrome Extension

### `background.js`

Handles browser-level extension behavior and tab activity events.

### `content-script.js`

Bridges browser activity to the React application.

### `manifest.json`

Defines the Chrome Extension Manifest V3 configuration and permissions.

## Data and Privacy

Rethread intentionally uses lightweight browser activity signals.

The extension records only:
- Tab titles
- Domains
- Timestamps

It does **not** collect or display raw webpage content.

The captured activity is used as context for reconstructing a work session and helping the user return to unfinished work.

Rethread does not surface a distraction score or characterize the user based on their browsing activity.

## Tech Stack

### Frontend
- React
- Vite
- JavaScript
- CSS
- Lucide React

### Browser Integration
- Chrome Extension
- Manifest V3
- Browser tab activity events

### Backend Integration
- AWS Lambda Function URL
- Backend agents
- JSON API requests

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/manideeprudroju/rethread.git
```

### 2. Enter the frontend directory

```bash
cd rethread/frontend
```

### 3. Install dependencies

```bash
npm install
```

## Environment Variables

Create a local `.env.local` file:

```env
VITE_LAMBDA_URL=your_lambda_function_url
```

Never commit `.env.local` to GitHub.

For a new setup, use an `.env.example` file if one is provided and copy it to `.env.local`.

## Run the Frontend

From the `frontend` directory:

```bash
npm run dev
```

The Vite development server normally runs at:

```text
http://localhost:5173
```

## Chrome Extension Setup

1. Start the frontend development server.
2. Open Chrome.
3. Go to `chrome://extensions`.
4. Enable **Developer mode**.
5. Click **Load unpacked**.
6. Select:

```text
rethread/frontend/extension/
```

7. Make sure the extension's host permissions match the URL where the frontend is running.

For the default Vite development setup, this is normally:

```text
http://localhost:5173
```

## Frontend and Backend

The repository separates the application into two parts:

```text
rethread/
├── frontend/
│   └── React application + Chrome extension
│
└── backend/
    └── Lambda + agents
```

The frontend communicates with the backend through the Lambda Function URL configured in `VITE_LAMBDA_URL`.

Backend setup, AWS configuration, agent implementation, deployment, and model configuration are documented separately in:

```text
../backend/README.md
```

## Development Workflow

Frontend changes should be made inside the `frontend/` directory.

Check changes:

```bash
git status
```

Stage specific files:

```bash
git add frontend/<file>
```

Create a descriptive commit:

```bash
git commit -m "Describe the change"
```

Push:

```bash
git push origin main
```

Do not commit:

```text
.env.local
node_modules/
dist/
```

## Repository Structure

The complete project is organized as:

```text
rethread/
│
├── backend/
│   ├── agents
│   ├── Lambda handlers
│   └── README.md
│
└── frontend/
    ├── React application
    ├── Chrome extension
    └── README.md
```

The backend and frontend have separate READMEs so each part can document its own setup and implementation details.

## Team

- **Frontend / UI:** Mani Deep
- **Backend / Agents:** Rethread team

## Hackathon

Built for:

**WeMakeDevs × AWS Bharat Builds Tour — First Commit**

**17–20 September 2026**
