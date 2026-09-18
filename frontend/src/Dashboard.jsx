import { useState, useEffect } from "react";
import { ArrowRight } from "lucide-react";
import { getSnapshot, subscribe } from "./sessionStore";

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

export default function Dashboard({ onContinue, onStartNew, onNightly }) {
  const [snap, setSnap] = useState(getSnapshot());

  useEffect(() => {
    setSnap(getSnapshot());
    return subscribe(setSnap);
  }, []);

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

      {snap.hasSession ? (
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

      <button
        onClick={() => onNightly?.()}
        style={{
          width: "100%",
          marginTop: 10,
          boxSizing: "border-box",
          background: "transparent",
          color: "#3F5D54",
          border: "1px solid #A9A493",
          borderRadius: 12,
          padding: "13px 16px",
          fontSize: 14,
          fontWeight: 500,
          fontFamily: "inherit",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <span>View nightly summary</span>
        <ArrowRight size={17} />
      </button>
    </div>
  );
}
