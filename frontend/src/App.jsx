import { useState } from "react";
import Dashboard from "./Dashboard";
import IntentPanel from "./IntentPanel";
import LiveBadge from "./LiveBadge";
import ReentryPanel from "./ReentryPanel";
import AmendChat from "./AmendChat";
import NightlySummaryPanel from "./NightlySummaryPanel";

// view:
// "dashboard"      -> first screen
// "intent"         -> declaring a new session
// "session_fresh"  -> Timer + Chatbot
// "session_return" -> Reentry panel
// "nightly"        -> nightly summary

export default function App() {
  const [view, setView] = useState("dashboard");

  return (
    <div
      style={{
        fontFamily: "'Inter', system-ui, sans-serif",
        background: "#E4E1D7",
        minHeight: "100vh",
        padding: 40,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 32,
      }}
    >

      {/* DASHBOARD */}
      {view === "dashboard" && (
        <Dashboard
          onContinue={() => setView("session_return")}
          onStartNew={() => setView("intent")}
          onNightly={() => setView("nightly")}
        />
      )}

      {/* NIGHTLY SUMMARY */}
      {view === "nightly" && (
        <NightlySummaryPanel
          onBack={() => setView("dashboard")}
        />
      )}

      {/* NEW SESSION / INTENT */}
      {view === "intent" && (
        <>
          <button
            onClick={() => setView("dashboard")}
            style={backButtonStyle}
          >
            ← Back to dashboard
          </button>

          <IntentPanel
            onStarted={() => setView("session_fresh")}
          />
        </>
      )}

      {/* TIMER + CHATBOT */}
      {view === "session_fresh" && (
        <>
          <button
            onClick={() => setView("dashboard")}
            style={backButtonStyle}
          >
            ← Back to dashboard
          </button>

          <LiveBadge />

          <AmendChat
            onDone={() => setView("dashboard")}
          />

          {/* GO TO REENTRY */}
          <button
            onClick={() => setView("session_return")}
            style={reentryButtonStyle}
          >
            Check where I left off →
          </button>
        </>
      )}

      {/* REENTRY PANEL */}
      {view === "session_return" && (
        <>
          <button
            onClick={() => setView("session_fresh")}
            style={backButtonStyle}
          >
            ← Back to session
          </button>

          <LiveBadge />

          <ReentryPanel
            onResume={() => setView("session_fresh")}
            onStartFresh={() => setView("intent")}
          />
        </>
      )}
    </div>
  );
}

const backButtonStyle = {
  fontFamily: "inherit",
  fontSize: 13,
  padding: "6px 12px",
  borderRadius: 8,
  border: "1px solid #A9A493",
  background: "none",
  color: "#5C5A4E",
  cursor: "pointer",
  alignSelf: "flex-start",
};

const reentryButtonStyle = {
  width: 520,
  maxWidth: "100%",
  boxSizing: "border-box",
  fontFamily: "inherit",
  fontSize: 14,
  fontWeight: 500,
  padding: "13px 16px",
  borderRadius: 12,
  border: "1px solid #A9A493",
  background: "#F1EFE8",
  color: "#3F5D54",
  cursor: "pointer",
};