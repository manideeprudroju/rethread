import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, BarChart3, Clock3, Globe2, RefreshCw } from "lucide-react";
import { nightly } from "./api";
import {
  getActivityHistory,
  getNightlySessions,
  getSnapshot,
} from "./sessionStore";

const shell = {
  width: "min(860px, 100%)",
  fontFamily: "'Inter', system-ui, sans-serif",
  color: "#1E2A28",
};

const card = {
  background: "#F1EFE8",
  border: "1px solid #E4E1D7",
  borderRadius: 20,
  padding: "28px 30px",
  boxSizing: "border-box",
};

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatTime(ts) {
  if (!ts) return "";
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDate(ts) {
  return new Date(ts).toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function humanCondition(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function NightlySummaryPanel({ onBack }) {
  const [activities, setActivities] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [analysis, setAnalysis] = useState(null);
  const [state, setState] = useState("idle");
  const [error, setError] = useState("");

  function loadLocalData() {
    setActivities(getActivityHistory());
    setSessions(getNightlySessions());
  }

  useEffect(() => {
    loadLocalData();
    if (getNightlySessions().length > 0) {
      runNightly();
    }
  }, []);

  async function runNightly() {
    const stored = getNightlySessions();
    setSessions(stored);
    setState("loading");
    setError("");

    try {
      const result = await nightly({
        metric: "initiation_latency_s",
        metric_label: "seconds from session start to first action",
        lower_is_better: true,
        sessions: stored,
      });
      setAnalysis(result);
      setState("idle");
    } catch (err) {
      console.error("Nightly analysis failed:", err);
      setError(err.message || "Nightly analysis is unavailable right now.");
      setState("error");
    }
  }

  const stats = useMemo(() => {
    const domains = new Set(activities.map((e) => e.domain).filter(Boolean));
    const titles = new Map();
    for (const event of activities) {
      const key = event.title || event.domain || "Unknown activity";
      titles.set(key, (titles.get(key) || 0) + 1);
    }
    const top = [...titles.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5);
    const first = activities[0]?.ts;
    const last = activities[activities.length - 1]?.ts;
    const span = first && last ? Math.max(0, (new Date(last) - new Date(first)) / 1000) : 0;

    return {
      events: activities.length,
      domains: domains.size,
      span,
      top,
    };
  }, [activities]);

  return (
    <div style={shell}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18 }}>
        <button
          onClick={onBack}
          aria-label="Back"
          style={{
            width: 36,
            height: 36,
            borderRadius: 10,
            border: "1px solid #CFCBBF",
            background: "#F1EFE8",
            color: "#1E2A28",
            cursor: "pointer",
            display: "grid",
            placeItems: "center",
          }}
        >
          <ArrowLeft size={16} />
        </button>
        <div>
          <div style={{ fontFamily: "'Fraunces', serif", fontSize: 28, fontWeight: 500 }}>
            Nightly summary
          </div>
          <div style={{ color: "#8A8578", fontSize: 13, marginTop: 3 }}>
            A record of the activity captured during your sessions.
          </div>
        </div>
      </div>

      <div style={{ ...card, marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div>
            <div style={{ fontSize: 12, color: "#8A8578", marginBottom: 5 }}>Activity log</div>
            <div style={{ fontSize: 18, fontWeight: 600 }}>What happened during the captured sessions</div>
          </div>
          <button
            onClick={loadLocalData}
            title="Refresh activity"
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              border: "1px solid #CFCBBF",
              background: "#FFFFFF",
              cursor: "pointer",
              display: "grid",
              placeItems: "center",
              color: "#3F5D54",
            }}
          >
            <RefreshCw size={15} />
          </button>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginTop: 18 }}>
          {[
            ["Activities", stats.events, BarChart3],
            ["Domains", stats.domains, Globe2],
            ["Observed span", formatDuration(stats.span), Clock3],
          ].map(([label, value, Icon]) => (
            <div key={label} style={{ background: "#FFFFFF", border: "1px solid #E4E1D7", borderRadius: 12, padding: "14px 15px" }}>
              <Icon size={15} color="#3F5D54" />
              <div style={{ fontSize: 12, color: "#8A8578", marginTop: 9 }}>{label}</div>
              <div style={{ fontSize: 20, fontWeight: 600, marginTop: 2 }}>{value}</div>
            </div>
          ))}
        </div>

        {stats.top.length > 0 && (
          <div style={{ marginTop: 18 }}>
            <div style={{ fontSize: 12, color: "#8A8578", marginBottom: 8 }}>Most repeated contexts</div>
            {stats.top.map(([title, count]) => (
              <div key={title} style={{ display: "flex", justifyContent: "space-between", gap: 16, padding: "9px 0", borderTop: "1px solid #E4E1D7", fontSize: 13.5 }}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
                <span style={{ color: "#8A8578", flexShrink: 0 }}>{count}×</span>
              </div>
            ))}
          </div>
        )}

        {activities.length === 0 ? (
          <div style={{ marginTop: 18, padding: 18, borderRadius: 12, background: "#FFFFFF", color: "#8A8578", fontSize: 13.5 }}>
            No captured activity yet. The extension will add title, domain, and timestamp events as tabs change.
          </div>
        ) : (
          <div style={{ marginTop: 18, maxHeight: 280, overflowY: "auto", background: "#FFFFFF", border: "1px solid #E4E1D7", borderRadius: 12 }}>
            {[...activities].reverse().map((event, index) => (
              <div key={`${event.ts}-${index}`} style={{ display: "flex", gap: 14, padding: "12px 14px", borderBottom: index === activities.length - 1 ? "none" : "1px solid #E4E1D7" }}>
                <div style={{ color: "#8A8578", fontSize: 11, minWidth: 66 }}>{formatTime(event.ts)}</div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{event.title}</div>
                  <div style={{ fontSize: 11.5, color: "#8A8578", marginTop: 3 }}>{event.domain} · {formatDate(event.ts)}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={card}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div>
            <div style={{ fontSize: 12, color: "#8A8578", marginBottom: 5 }}>Nightly experiment</div>
            <div style={{ fontSize: 18, fontWeight: 600 }}>What the collected sessions show</div>
          </div>
          <button
            onClick={runNightly}
            disabled={state === "loading"}
            style={{
              border: "none",
              borderRadius: 10,
              background: "#3F5D54",
              color: "#F1EFE8",
              padding: "10px 14px",
              cursor: state === "loading" ? "default" : "pointer",
              opacity: state === "loading" ? 0.65 : 1,
              fontFamily: "inherit",
              fontSize: 13,
            }}
          >
            {state === "loading" ? "Analysing…" : "Run nightly analysis"}
          </button>
        </div>

        {sessions.length === 0 ? (
          <div style={{ marginTop: 18, padding: 16, borderRadius: 12, background: "#FFFFFF", border: "1px solid #E4E1D7", color: "#8A8578", fontSize: 13.5 }}>
            Session-level experiment data will appear here after completed sessions are recorded.
          </div>
        ) : (
          <div style={{ marginTop: 18 }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 10 }}>
              {Object.entries(analysis?.counts || sessions.reduce((acc, s) => {
                acc[s.condition] = (acc[s.condition] || 0) + 1;
                return acc;
              }, {})).map(([condition, count]) => (
                <div key={condition} style={{ background: "#FFFFFF", border: "1px solid #E4E1D7", borderRadius: 12, padding: "14px 15px" }}>
                  <div style={{ fontSize: 12, color: "#8A8578" }}>{humanCondition(condition)}</div>
                  <div style={{ fontSize: 22, fontWeight: 600, marginTop: 3 }}>{count}</div>
                  <div style={{ fontSize: 11, color: "#8A8578" }}>recorded sessions</div>
                </div>
              ))}
            </div>

            {analysis && (
              <div style={{ marginTop: 14, background: "#FFFFFF", border: "1px solid #E4E1D7", borderRadius: 12, padding: "16px 17px" }}>
                <div style={{ fontSize: 12, color: "#8A8578", marginBottom: 7 }}>Nightly result</div>
                <div style={{ fontSize: 16, fontWeight: 600 }}>{analysis.ui_text}</div>

                {analysis.writeup && (
                  <>
                    <div style={{ marginTop: 14, fontSize: 15, fontWeight: 600 }}>{analysis.writeup.headline}</div>
                    <div style={{ marginTop: 7, fontSize: 13.5, lineHeight: 1.55, color: "#5C5A4E" }}>{analysis.writeup.detail}</div>
                    <div style={{ marginTop: 12, padding: "10px 12px", borderRadius: 9, background: "#F1EFE8", fontSize: 13.5 }}>{analysis.writeup.suggestion}</div>
                  </>
                )}

                {analysis.status === "still_learning" && (
                  <div style={{ marginTop: 8, fontSize: 13, color: "#8A8578" }}>
                    The experiment gate has not collected enough observations yet.
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {state === "error" && <div style={{ marginTop: 12, color: "#8A5B4A", fontSize: 13 }}>{error}</div>}
      </div>
    </div>
  );
}
