import { useState } from "react";
import { useAuth } from "react-oidc-context";

import Dashboard from "./Dashboard";
import IntentPanel from "./IntentPanel";
import LiveBadge from "./LiveBadge";
import ReentryPanel from "./ReentryPanel";
import AmendChat from "./AmendChat";
import { bindUser } from "./sessionStore";
// DriftPopup intentionally not mounted — on hold, see team discussion.
// It's still built (./DriftPopup.jsx) and ready to re-enable, but it
// violates the "never show a drift verdict to the user" rule and needs
// a redesign, not just a reactivation.

// view: "dashboard" -> first screen.
// "intent"          -> declaring a new session (no re-entry check needed —
//                       there's nothing to reconstruct yet).
// "session_fresh"   -> just started; Live badge only.
// "session_return"  -> arrived via "Continue session"; Live badge +
//                       Re-entry check, since this is the moment that
//                       check actually matters.

export default function App() {
  const auth = useAuth();
  const [view, setView] = useState("dashboard");

  // =====================================================
  // COGNITO AUTHENTICATION
  // =====================================================

  if (auth.isLoading) {
    return (
      <div style={authPageStyle}>
        <div style={authCardStyle}>
          <div style={brandMarkStyle}>R</div>

          <h1 style={authTitleStyle}>Rethread</h1>

          <p style={authSubtitleStyle}>
            Your focus, untangled.
          </p>

          <p style={authLoadingStyle}>
            Checking your session...
          </p>
        </div>
      </div>
    );
  }

  if (auth.error) {
    return (
      <div style={authPageStyle}>
        <div style={authCardStyle}>
          <div style={brandMarkStyle}>R</div>

          <h1 style={authTitleStyle}>Rethread</h1>

          <p style={authSubtitleStyle}>
            Your focus, untangled.
          </p>

          <div style={errorBoxStyle}>
            {auth.error.message}
          </div>

          <button
            onClick={() => auth.signinRedirect()}
            style={primaryButtonStyle}
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    return (
      <div style={authPageStyle}>
        <div style={authCardStyle}>
          <div style={brandMarkStyle}>R</div>

          <h1 style={authTitleStyle}>Rethread</h1>

          <p style={authSubtitleStyle}>
            Your focus, untangled.
          </p>

          <p style={authDescriptionStyle}>
            Pick up where you left off and get back to what matters.
          </p>

          <button
            onClick={() => auth.signinRedirect()}
            style={primaryButtonStyle}
          >
            Sign in
          </button>

          <p style={authFooterStyle}>
            Your workspace stays private to your account.
          </p>
        </div>
      </div>
    );
  }

  // =====================================================
  // EXISTING RETHREAD APPLICATION
  // =====================================================

  // Point the store at this user's saved data before any panel reads it.
  // Idempotent: a no-op on every render after the first. Runs before the
  // children below mount, so no subscriber is notified mid-render.
  bindUser(auth.user?.profile?.sub);

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
      {view === "dashboard" && (
        <Dashboard
          onContinue={() => setView("session_return")}
          onStartNew={() => setView("intent")}
        />
      )}

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
        </>
      )}

      {view === "session_return" && (
        <>
          <button
            onClick={() => setView("dashboard")}
            style={backButtonStyle}
          >
            ← Back to dashboard
          </button>

          <LiveBadge />

          <ReentryPanel
            onResume={() => setView("session_fresh")}
            onStartFresh={() => setView("intent")}
          />

          <AmendChat
            onDone={() => setView("dashboard")}
          />
        </>
      )}

      {/* <DriftPopup /> — see note above imports */}
    </div>
  );
}

// =====================================================
// AUTHENTICATION SCREEN STYLES
// =====================================================

const authPageStyle = {
  fontFamily: "'Inter', system-ui, sans-serif",
  background: "#E4E1D7",
  minHeight: "100vh",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 24,
  boxSizing: "border-box",
};

const authCardStyle = {
  width: "100%",
  maxWidth: 430,
  background: "#F7F5EE",
  border: "1px solid #C9C5B8",
  borderRadius: 20,
  padding: "42px 38px 34px",
  boxSizing: "border-box",
  textAlign: "center",
  boxShadow: "0 12px 35px rgba(60, 57, 45, 0.08)",
};

const brandMarkStyle = {
  width: 46,
  height: 46,
  margin: "0 auto 20px",
  borderRadius: 14,
  background: "#5C5A4E",
  color: "#F7F5EE",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 20,
  fontWeight: 700,
  letterSpacing: "-0.02em",
};

const authTitleStyle = {
  margin: 0,
  color: "#343329",
  fontSize: 30,
  fontWeight: 700,
  letterSpacing: "-0.03em",
};

const authSubtitleStyle = {
  margin: "8px 0 0",
  color: "#777362",
  fontSize: 15,
  fontWeight: 500,
};

const authDescriptionStyle = {
  margin: "28px auto 26px",
  maxWidth: 310,
  color: "#5C5A4E",
  fontSize: 14,
  lineHeight: 1.7,
};

const authLoadingStyle = {
  margin: "28px 0 0",
  color: "#777362",
  fontSize: 13,
};

const errorBoxStyle = {
  margin: "24px 0",
  padding: "12px 14px",
  borderRadius: 10,
  background: "#F3E6E2",
  border: "1px solid #DFC7C0",
  color: "#8B3A3A",
  fontSize: 13,
  lineHeight: 1.5,
  textAlign: "left",
};

const authFooterStyle = {
  margin: "20px 0 0",
  color: "#989382",
  fontSize: 11,
  lineHeight: 1.5,
};

const primaryButtonStyle = {
  width: "100%",
  fontFamily: "inherit",
  fontSize: 14,
  fontWeight: 600,
  padding: "12px 18px",
  borderRadius: 10,
  border: "1px solid #5C5A4E",
  background: "#5C5A4E",
  color: "#FFFFFF",
  cursor: "pointer",
  boxSizing: "border-box",
};

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