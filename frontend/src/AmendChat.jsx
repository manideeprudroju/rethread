import { useEffect, useRef, useState } from "react";
import { RotateCcw, Send, Sparkles } from "lucide-react";
import { amend } from "./api";
import { getSessionForAmend, applyAmendResult, endSession } from "./sessionStore";

const cardStyle = {
  fontFamily: "'Inter', system-ui, sans-serif",
  background: "#F1EFE8",
  color: "#1E2A28",
  width: "100%",
  maxWidth: 600,
  margin: "0 auto",
  borderRadius: 20,
  padding: 20,
  border: "1px solid #E4E1D7",
  boxSizing: "border-box",
};

const messagesStyle = {
  display: "flex",
  flexDirection: "column",
  gap: 10,
  maxHeight: 360,
  overflowY: "auto",
  padding: "4px 2px 12px",
};

const inputRowStyle = {
  display: "flex",
  gap: 8,
  alignItems: "flex-end",
};

const textInputStyle = {
  flex: 1,
  minHeight: 42,
  maxHeight: 110,
  resize: "none",
  boxSizing: "border-box",
  border: "1px solid #E4E1D7",
  borderRadius: 12,
  padding: "11px 14px",
  fontSize: 14,
  lineHeight: 1.45,
  fontFamily: "inherit",
  color: "#1E2A28",
  background: "#FFFFFF",
  outline: "none",
};

const sendButtonStyle = {
  border: "none",
  borderRadius: 12,
  background: "#3F5D54",
  color: "#F1EFE8",
  width: 42,
  height: 42,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  cursor: "pointer",
  flexShrink: 0,
};

function Bubble({ role, children }) {
  const user = role === "user";
  return (
    <div
      style={{
        display: "flex",
        justifyContent: user ? "flex-end" : "flex-start",
      }}
    >
      <div
        style={{
          maxWidth: "82%",
          background: user ? "#3F5D54" : "#FFFFFF",
          color: user ? "#F1EFE8" : "#1E2A28",
          border: user ? "none" : "1px solid #E4E1D7",
          borderRadius: user ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
          padding: "10px 13px",
          fontSize: 13.5,
          lineHeight: 1.5,
          whiteSpace: "pre-wrap",
        }}
      >
        {children}
      </div>
    </div>
  );
}

export default function AmendChat({ onDone }) {
  const session = getSessionForAmend();
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      text: `You're working on: ${session.declared_intent || "your current task"}.\n\nCurrent next move: ${session.first_action || "Tell me what is happening."}`,
    },
  ]);
  const [message, setMessage] = useState("");
  const [state, setState] = useState("idle");
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, state]);

  async function send(textOverride) {
    const text = (textOverride ?? message).trim();
    if (!text || state === "sending") return;

    setMessages((prev) => [...prev, { role: "user", text }]);
    setMessage("");
    setState("sending");

    try {
      const result = await amend({
        session: getSessionForAmend(),
        message: text,
      });

      if (result.kind === "done") {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", text: result.note || "Session complete." },
        ]);
        endSession();
        onDone?.();
        return;
      }

      applyAmendResult({
        first_action: result.first_action,
        plans: result.plans,
      });

      const response = result.note
        ? `${result.note}\n\nNext move: ${result.first_action}`
        : `Next move: ${result.first_action}`;

      setMessages((prev) => [...prev, { role: "assistant", text: response }]);
      setState("idle");
    } catch (err) {
      console.error("Amend failed:", err);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: "I couldn't process that right now. Try sending it again." },
      ]);
      setState("error");
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div style={cardStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 14 }}>
        <div
          style={{
            width: 32,
            height: 32,
            borderRadius: 10,
            background: "#3F5D54",
            color: "#F1EFE8",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Sparkles size={16} />
        </div>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600 }}>Rethread</div>
          <div style={{ fontSize: 11.5, color: "#8A8578", marginTop: 1 }}>
            Talk through what happens next
          </div>
        </div>
      </div>

      <div style={messagesStyle}>
        {messages.map((item, index) => (
          <Bubble key={index} role={item.role}>
            {item.text}
          </Bubble>
        ))}

        {state === "sending" && (
          <Bubble role="assistant">Thinking about the next move…</Bubble>
        )}

        <div ref={bottomRef} />
      </div>

      {state === "error" && (
        <button
          onClick={() => setState("idle")}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            border: "1px solid #A9A493",
            background: "none",
            borderRadius: 8,
            padding: "7px 11px",
            marginBottom: 10,
            fontSize: 12,
            color: "#1E2A28",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          <RotateCcw size={12} /> Try again
        </button>
      )}

      <div style={{ display: "flex", gap: 7, flexWrap: "wrap", marginBottom: 10 }}>
        {["I'm stuck", "The scope changed", "I finished this step"].map((quick) => (
          <button
            key={quick}
            type="button"
            disabled={state === "sending"}
            onClick={() => send(quick)}
            style={{
              border: "1px solid #D4D0C4",
              background: "transparent",
              borderRadius: 999,
              padding: "6px 10px",
              fontSize: 11.5,
              color: "#5C5A4E",
              cursor: state === "sending" ? "default" : "pointer",
              fontFamily: "inherit",
              opacity: state === "sending" ? 0.5 : 1,
            }}
          >
            {quick}
          </button>
        ))}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
        style={inputRowStyle}
      >
        <textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Tell me what's happening…"
          disabled={state === "sending"}
          rows={1}
          style={textInputStyle}
        />
        <button
          type="submit"
          disabled={!message.trim() || state === "sending"}
          style={{
            ...sendButtonStyle,
            opacity: message.trim() && state !== "sending" ? 1 : 0.5,
            cursor: message.trim() && state !== "sending" ? "pointer" : "default",
          }}
          aria-label="Send message"
        >
          <Send size={15} />
        </button>
      </form>
    </div>
  );
}
