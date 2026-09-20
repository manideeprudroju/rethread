import { useState, useEffect } from "react";
import { ArrowRight } from "lucide-react";
import { endSession, getSnapshot, subscribe } from "./sessionStore";
import { nightly } from "./api";
import { EXPERIMENT_ARMS, METRIC_LABEL } from "./experiment";

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function greetingForNow() {
  const h = new Date().getHours();
  if (h < 5) return "Still up";
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

const cardStyle = {
  fontFamily: "'Inter', system-ui, sans-serif",
  background: "#F1EFE8",
  color: "#1E2A28",
  maxWidth: 480,
  margin: "0 auto",
  borderRadius: 20,
  padding: "32px 32px 28px",
  border: "1px solid #E4E1D7",
  overflow: "hidden",
  boxSizing: "border-box",
};

const ctaStyle = {
  width: "100%",
  boxSizing: "border-box",
  background: "#3F5D54",
  color: "#F1EFE8",
  border: "none",
  borderRadius: 12,
  padding: "16px 20px",
  fontSize: 15,
  fontWeight: 500,
  fontFamily: "inherit",
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
};

/*
 * What the experiment has learned so far, from this user's sessions in
 * DynamoDB (the same analysis the nightly schedule runs). The wording comes
 * from the gate itself: "Still learning, 3 of 8 sessions" until both arms
 * have 8, then one decision. Neutral by construction: it describes the
 * conditions, never the person, and there is no failure state to show. If
 * the call fails, the card simply isn't there.
 */
function LearningCard() {
  const [result, setResult] = useState(null);

  useEffect(() => {
    let live = true;
    nightly({ arms: EXPERIMENT_ARMS, metric_label: METRIC_LABEL })
      .then((r) => {
        if (live) setResult(r);
      })
      .catch((err) => console.error("Nightly analysis unavailable:", err));
    return () => {
      live = false;
    };
  }, []);

  if (!result?.ui_text) return null;

  return (
    <div
      style={{
        marginTop: 24,
        paddingTop: 18,
        borderTop: "1px solid #E4E1D7",
      }}
    >
      <div style={{ fontSize: 11, color: "#8A8578", marginBottom: 6 }}>
        What your sessions are showing
      </div>
      <div style={{ fontSize: 14, color: "#1E2A28", lineHeight: 1.5 }}>
        {result.ui_text}
      </div>
      <div style={{ fontSize: 12, color: "#8A8578", marginTop: 6, lineHeight: 1.5 }}>
        Comparing a first step on its own with a first step plus if-then plans,
        by {METRIC_LABEL}.
      </div>
    </div>
  );
}

const endLinkStyle = {
  display: "block",
  margin: "12px auto 0",
  padding: "4px 2px",
  background: "none",
  border: "none",
  fontFamily: "inherit",
  fontSize: 13,
  color: "#8A8578",
  cursor: "pointer",
};

const endRowStyle = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  flexWrap: "wrap",
  gap: 10,
  marginTop: 12,
};

const endAskStyle = {
  fontSize: 13,
  color: "#5C5A4E",
};

const endConfirmStyle = {
  fontFamily: "inherit",
  fontSize: 13,
  padding: "6px 12px",
  borderRadius: 9,
  border: "1px solid #A9A493",
  background: "#F7F5EE",
  color: "#1E2A28",
  cursor: "pointer",
};

const endQuietStyle = {
  fontFamily: "inherit",
  fontSize: 13,
  padding: "6px 4px",
  background: "none",
  border: "none",
  color: "#8A8578",
  cursor: "pointer",
};

export default function Dashboard({ onContinue, onStartNew }) {
  const [snap, setSnap] = useState(getSnapshot());
  // Two taps to end, so a stray click can't close a live session.
  const [confirmEnd, setConfirmEnd] = useState(false);

  useEffect(() => {
    setSnap(getSnapshot());
    return subscribe(setSnap);
  }, []);

  // A new session is a clean slate for this control.
  useEffect(() => {
    setConfirmEnd(false);
  }, [snap.sessionStartedAt]);

  // A finished session has nothing to continue: offer a new one instead of
  // a "Continue" card with an empty first move. Starting it archives and
  // logs the finished one.
  const live = snap.hasSession && !snap.done;

  return (
    <div style={cardStyle}>
      <div
        style={{
          fontFamily: "'Fraunces', serif",
          fontSize: 26,
          fontWeight: 500,
          color: "#1E2A28",
          marginBottom: 4,
        }}
      >
        {greetingForNow()}
      </div>

      {live ? (
        <>
          <p style={{ fontSize: 14, color: "#5C5A4E", margin: "0 0 24px", lineHeight: 1.5 }}>
            {snap.intent}
          </p>

          <div
            style={{
              background: "#FFFFFF",
              border: "1px solid #E4E1D7",
              borderRadius: 12,
              padding: "16px 18px",
              marginBottom: 24,
              boxSizing: "border-box",
            }}
          >
            <div style={{ fontSize: 11, color: "#8A8578", marginBottom: 6 }}>
              First move
            </div>
            <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 14 }}>
              {snap.firstAction}
            </div>
            <div
              style={{
                fontSize: 20,
                fontWeight: 600,
                color: "#1E2A28",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {formatElapsed(snap.elapsedSeconds)}
            </div>
          </div>

          <button onClick={() => onContinue?.()} style={ctaStyle}>
            <span>Continue session</span>
            <ArrowRight size={18} />
          </button>

          {/* Ending a session here records it as stopped. Finishing the work
              is what you say in the chat, and that is what marks it done:
              one control, one meaning. Neutral wording either way -- a
              session that ends early is a data point, not a failure. */}
          {confirmEnd ? (
            <div style={endRowStyle}>
              <span style={endAskStyle}>End this session?</span>
              <button
                onClick={() => {
                  endSession("stopped");
                  setConfirmEnd(false);
                }}
                style={endConfirmStyle}
              >
                End it
              </button>
              <button onClick={() => setConfirmEnd(false)} style={endQuietStyle}>
                Keep going
              </button>
            </div>
          ) : (
            <button onClick={() => setConfirmEnd(true)} style={endLinkStyle}>
              End session
            </button>
          )}
        </>
      ) : (
        <>
          <p style={{ fontSize: 14, color: "#5C5A4E", margin: "0 0 24px" }}>
            Nothing going right now.
          </p>
          <button onClick={() => onStartNew?.()} style={ctaStyle}>
            <span>Start a session</span>
            <ArrowRight size={18} />
          </button>
        </>
      )}

      <LearningCard />
    </div>
  );
}
