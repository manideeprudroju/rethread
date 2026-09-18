import { useState, useEffect } from "react";
import { Play, Pause } from "lucide-react";
import { getSnapshot, subscribe, pause, resume } from "./sessionStore";

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const shellStyle = {
  fontFamily: "'Inter', system-ui, sans-serif",
  background: "#F1EFE8",
  border: "1px solid #E4E1D7",
  borderRadius: 16,
  padding: "12px 16px",
  width: 280,
  boxSizing: "border-box",
  boxShadow: "0 1px 2px rgba(30, 42, 40, 0.04)",
};

export default function LiveBadge() {
  const [snap, setSnap] = useState(getSnapshot());

  useEffect(() => {
    setSnap(getSnapshot());
    return subscribe(setSnap);
  }, []);

  if (!snap.hasSession) {
    return (
      <div style={{ ...shellStyle, color: "#8A8578", fontSize: 13 }}>
        No active session
      </div>
    );
  }

  const dotColor = snap.running ? "#3F5D54" : "#B8874B";

  return (
    <div style={{ ...shellStyle, display: "flex", alignItems: "center", gap: 12 }}>
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: dotColor,
          flexShrink: 0,
          transition: "background 0.2s ease",
        }}
      />

      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 15,
            fontWeight: 500,
            color: "#1E2A28",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {formatElapsed(snap.elapsedSeconds)}
        </div>
        <div
          style={{
            fontSize: 12,
            color: "#8A8578",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            marginTop: 1,
          }}
        >
          {snap.firstAction}
        </div>
      </div>

      {/* No drift/distraction indicator here — showing that to the user
          is explicitly against the product's hard rules. That signal
          exists only for the backend's experiment loop. */}

      <button
        onClick={() => (snap.running ? pause() : resume())}
        style={{
          width: 30,
          height: 30,
          borderRadius: "50%",
          border: "none",
          background: "#3F5D54",
          color: "#F1EFE8",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
          flexShrink: 0,
        }}
        aria-label={snap.running ? "Pause session" : "Resume session"}
      >
        {snap.running ? <Pause size={13} fill="#F1EFE8" /> : <Play size={13} fill="#F1EFE8" />}
      </button>
    </div>
  );
}
