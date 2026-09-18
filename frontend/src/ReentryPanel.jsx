import { useState, useEffect, useCallback } from "react";
import { ArrowRight, Circle, RotateCcw } from "lucide-react";
import { reentry } from "./api";
import { getDeclaredIntent, getEventsPayload } from "./sessionStore";

const cardStyle = {
  fontFamily: "'Inter', system-ui, sans-serif",
  background: "#F1EFE8",
  color: "#1E2A28",
  maxWidth: 600,
  margin: "0 auto",
  borderRadius: 20,
  padding: "36px 32px 28px",
  border: "1px solid #E4E1D7",
  overflow: "hidden",
  boxSizing: "border-box",
};

const primaryButtonStyle = {
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

export default function ReentryPanel({ onResume, onStartFresh }) {
  // "loading" | "error" | "found" | "not_found"
  const [state, setState] = useState("loading");
  const [data, setData] = useState(null);
  const [resumed, setResumed] = useState(false);

  const load = useCallback(async () => {
    setState("loading");
    try {
      const result = await reentry({
        declared_intent: getDeclaredIntent(),
        events: getEventsPayload(),
      });
      setData(result);
      setState(result.session_found ? "found" : "not_found");
    } catch (err) {
      console.error("Reentry check failed:", err);
      setState("error");
    }
  }, []);

  useEffect(() => {
    const styleTag = document.createElement("style");
    styleTag.textContent = `
      @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500&family=Inter:wght@400;500;600&display=swap');
    `;
    document.head.appendChild(styleTag);
    load();
    return () => document.head.removeChild(styleTag);
  }, [load]);

  if (state === "loading") {
    return (
      <div style={{ ...cardStyle, color: "#8A8578", fontSize: 14 }}>
        Retracing your steps…
      </div>
    );
  }

  // A genuine backend/network failure — distinct from "nothing found,"
  // which is not an error and gets its own screen below.
  if (state === "error") {
    return (
      <div style={cardStyle}>
        <p style={{ fontSize: 14, color: "#5C5A4E", margin: "0 0 20px" }}>
          Couldn't check just now — a connection hiccup, not a lost session.
        </p>
        <button
          onClick={load}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            border: "1px solid #A9A493",
            background: "none",
            borderRadius: 10,
            padding: "10px 16px",
            fontSize: 14,
            color: "#1E2A28",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          <RotateCcw size={14} /> Try again
        </button>
      </div>
    );
  }

  // Common and correct — not an error. No apology, no "try again."
  if (state === "not_found") {
    return (
      <div style={cardStyle}>
        <h1
          style={{
            fontFamily: "'Fraunces', serif",
            fontWeight: 500,
            fontSize: 26,
            margin: "0 0 12px",
          }}
        >
          Nothing to hand back right now
        </h1>
        <p style={{ fontSize: 14, color: "#5C5A4E", margin: "0 0 28px", lineHeight: 1.5 }}>
          {data.evidence}
        </p>
        <button onClick={() => onStartFresh?.()} style={primaryButtonStyle}>
          <span>Start something new</span>
          <ArrowRight size={18} />
        </button>
      </div>
    );
  }

  // state === "found"
  return (
    <div style={cardStyle}>
      <h1
        style={{
          fontFamily: "'Fraunces', serif",
          fontWeight: 500,
          fontSize: 28,
          lineHeight: 1.25,
          margin: "0 0 4px",
        }}
      >
        Picking back up
      </h1>
      <p style={{ fontSize: 15, color: "#5C5A4E", margin: "0 0 24px", lineHeight: 1.5 }}>
        {data.doing}
      </p>

      {data.why && (
        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #E4E1D7",
            borderRadius: 12,
            padding: "16px 18px",
            marginBottom: 20,
            boxSizing: "border-box",
            fontSize: 14,
            color: "#5C5A4E",
            lineHeight: 1.5,
          }}
        >
          {data.why}
        </div>
      )}

      {data.key_tabs && data.key_tabs.length > 0 && (
        <div style={{ marginBottom: 28 }}>
          {data.key_tabs.map((tab, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
                fontSize: 13.5,
                color: "#5C5A4E",
                padding: "8px 0",
                borderBottom: i < data.key_tabs.length - 1 ? "1px solid #E4E1D7" : "none",
              }}
            >
              <Circle size={7} fill="#3F5D54" color="#3F5D54" style={{ marginTop: 5, flexShrink: 0 }} />
              {tab}
            </div>
          ))}
        </div>
      )}

      {!resumed ? (
        <button
          onClick={() => {
            setResumed(true);
            onResume?.(data.next_action);
          }}
          style={primaryButtonStyle}
        >
          <span>{data.next_action}</span>
          <ArrowRight size={18} />
        </button>
      ) : (
        <div
          style={{
            width: "100%",
            boxSizing: "border-box",
            border: "1px solid #3F5D54",
            color: "#3F5D54",
            borderRadius: 12,
            padding: "16px 20px",
            fontSize: 15,
            fontWeight: 500,
            textAlign: "center",
          }}
        >
          Resumed — back to it
        </div>
      )}
    </div>
  );
}
