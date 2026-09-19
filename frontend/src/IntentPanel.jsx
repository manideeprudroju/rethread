import { useState } from "react";
import { ArrowRight, RotateCcw, ChevronDown, ChevronUp } from "lucide-react";
import { decompose, assignArm } from "./api";
import { createSession, beginTimer } from "./sessionStore";
import { EXPERIMENT_ARMS, PLANS_OPEN_ARM } from "./experiment";

const cardStyle = {
  fontFamily: "'Inter', system-ui, sans-serif",
  background: "#F1EFE8",
  color: "#1E2A28",
  maxWidth: 520,
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

const textareaStyle = {
  width: "100%",
  boxSizing: "border-box",
  resize: "none",
  border: "1px solid #E4E1D7",
  borderRadius: 12,
  padding: "14px 16px",
  fontSize: 15,
  fontFamily: "inherit",
  color: "#1E2A28",
  background: "#FFFFFF",
  outline: "none",
};

const inputStyle = {
  width: "100%",
  boxSizing: "border-box",
  border: "1px solid #E4E1D7",
  borderRadius: 12,
  padding: "14px 16px",
  fontSize: 15,
  fontFamily: "inherit",
  color: "#1E2A28",
  background: "#FFFFFF",
  outline: "none",
};

const labelStyle = {
  display: "block",
  fontSize: 12,
  color: "#8A8578",
  marginBottom: 7,
};

export default function IntentPanel({ onStarted }) {
  // "input" | "generating" | "preview" | "error"
  const [state, setState] = useState("input");

  const [goal, setGoal] = useState("");
  const [location, setLocation] = useState("");
  const [dueDate, setDueDate] = useState("");

  const [result, setResult] = useState(null);
  const [showPlans, setShowPlans] = useState(false);

  // Experiment arm for this session, from the server. null = not in the
  // experiment this time (assign failed); the session still works normally
  // and simply isn't counted. There is deliberately no local fallback: a
  // guessed arm (e.g. alternating) would line the arms up with the person's
  // routine, which the gate cannot tell apart from a real effect.
  const [condition, setCondition] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();

    if (!goal.trim()) return;

    setState("generating");

    try {
      const answers = [
        location.trim() ? `Location: ${location.trim()}` : "",
        dueDate.trim() ? `Due date: ${dueDate.trim()}` : "",
      ]
        .filter(Boolean)
        .join("\n");

      // Assign in parallel with decompose. The arm decides how the preview
      // below looks (plans open or collapsed), so it has to be known before
      // the preview renders. It also keeps "Start working" instant: no
      // network call on that click, so a double click can't start twice.
      // The server picks the arm (balanced shuffled blocks, seeded by the
      // user's Cognito id) and counts their sessions itself.
      const [res, arm] = await Promise.all([
        decompose({
          goal: goal.trim(),
          answers,
        }),
        assignArm({ arms: EXPERIMENT_ARMS }).catch((err) => {
          console.error("Assign failed, session runs outside the experiment:", err);
          return null;
        }),
      ]);

      const cond = arm?.condition ?? null;
      setCondition(cond);
      // The experiment's one manipulation: step_and_plans shows the if-then
      // plans open; first_step_only leaves them one click away.
      setShowPlans(cond === PLANS_OPEN_ARM);
      setResult(res);
      setState("preview");
    } catch (err) {
      console.error("Decompose failed:", err);
      setState("error");
    }
  }

  function handleStart() {
    createSession({
      goal: goal.trim(),
      // The store puts these into the backend session's notes, so the chat
      // can refer to where the work lives.
      location: location.trim(),
      dueDate: dueDate.trim(),
      firstAction: result.first_action,
      plans: result.plans,
      condition,
    });

    beginTimer();
    onStarted?.();
  }

  if (state === "generating") {
    return (
      <div style={{ ...cardStyle, color: "#8A8578", fontSize: 14 }}>
        Working out a first move…
      </div>
    );
  }

  if (state === "error") {
    return (
      <div style={cardStyle}>
        <p
          style={{
            fontSize: 14,
            color: "#5C5A4E",
            margin: "0 0 20px",
          }}
        >
          Couldn't put together a plan just now.
        </p>

        <button onClick={handleSubmit} style={retryButtonStyle}>
          <RotateCcw size={14} />
          Try again
        </button>
      </div>
    );
  }

  if (state === "preview" && result) {
    return (
      <div style={cardStyle}>
        <div
          style={{
            fontSize: 12,
            color: "#8A8578",
            marginBottom: 8,
          }}
        >
          Your first move
        </div>

        <h1
          style={{
            fontFamily: "'Fraunces', serif",
            fontWeight: 500,
            fontSize: 24,
            lineHeight: 1.35,
            margin: "0 0 24px",
          }}
        >
          {result.first_action}
        </h1>

        {result.plans && result.plans.length > 0 && (
          <div style={{ marginBottom: 24 }}>
            <button
              onClick={() => setShowPlans((s) => !s)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                background: "none",
                border: "none",
                padding: 0,
                marginBottom: showPlans ? 14 : 0,
                fontSize: 12,
                color: "#8A8578",
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              {showPlans ? "Hide" : "If things don't go smoothly"}
              {showPlans ? (
                <ChevronUp size={13} />
              ) : (
                <ChevronDown size={13} />
              )}
            </button>

            {showPlans && (
              <div
                style={{
                  background: "#FFFFFF",
                  border: "1px solid #E4E1D7",
                  borderRadius: 12,
                  padding: "4px 16px",
                  boxSizing: "border-box",
                }}
              >
                {result.plans.map((p, i) => (
                  <div
                    key={i}
                    style={{
                      padding: "12px 0",
                      borderBottom:
                        i < result.plans.length - 1
                          ? "1px solid #E4E1D7"
                          : "none",
                      fontSize: 13.5,
                      lineHeight: 1.5,
                    }}
                  >
                    <span style={{ color: "#8A8578" }}>If </span>
                    {p.if}
                    <span style={{ color: "#8A8578" }}> → </span>
                    {p.then}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <button onClick={handleStart} style={primaryButtonStyle}>
          <span>Start working</span>
          <ArrowRight size={18} />
        </button>
      </div>
    );
  }

  // state === "input"
  return (
    <div style={cardStyle}>
      <h1
        style={{
          fontFamily: "'Fraunces', serif",
          fontWeight: 500,
          fontSize: 28,
          lineHeight: 1.25,
          margin: "0 0 8px",
        }}
      >
        What are you working on?
      </h1>

      <p
        style={{
          fontSize: 14,
          color: "#5C5A4E",
          margin: "0 0 20px",
          lineHeight: 1.5,
        }}
      >
        Tell us what you're working on, where it is, and when it's due.
      </p>

      <form onSubmit={handleSubmit}>
        {/* 1. TASK */}
        <label style={labelStyle}>Task</label>

        <textarea
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="e.g. Fix the JWT refresh bug in auth.py"
          autoFocus
          rows={2}
          style={{
            ...textareaStyle,
            marginBottom: 16,
          }}
        />

        {/* 2. LOCATION */}
        <label style={labelStyle}>Where is it located?</label>

        <input
          type="text"
          value={location}
          onChange={(e) => setLocation(e.target.value)}
          placeholder="e.g. VS Code, YouTube, LeetCode, Google Docs"
          style={{
            ...inputStyle,
            marginBottom: 16,
          }}
        />

        {/* 3. DUE DATE */}
        <label style={labelStyle}>When do you want to have it done?</label>

        <textarea
          value={dueDate}
          onChange={(e) => setDueDate(e.target.value)}
          placeholder="e.g. Want to finish before I start practicing tonight"
          rows={2}
          style={{
            ...textareaStyle,
            marginBottom: 20,
          }}
        />

        <button
          type="submit"
          disabled={!goal.trim()}
          style={{
            ...primaryButtonStyle,
            opacity: goal.trim() ? 1 : 0.5,
            cursor: goal.trim() ? "pointer" : "default",
          }}
        >
          <span>Get a first move</span>
          <ArrowRight size={18} />
        </button>
      </form>
    </div>
  );
}

const retryButtonStyle = {
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
};