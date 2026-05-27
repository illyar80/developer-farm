const { useState, useEffect, useRef } = React;

// ─── Initial state (will be populated from real API) ────────────────────────
const INITIAL_STATE = {
  workers: [],
  events: [],
  goals: [],
  metrics: {
    tasksToday: 0,
    tokensUsed: 0,
    tokensBudget: 500000,
    successRate: 0,
    avgTaskMin: 0,
  },
  threads: {},
  activeLayers: [], // Active LangGraph nodes
  systemInfo: { os: "loading...", vcpu: 0 },
};

// ─── Helpers ─────────────────────────────────────────────────────────────────
function timeAgo(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function useSSE(url) {
  const [state, setState] = useState(INITIAL_STATE);

  useEffect(() => {
    // Load initial status from API
    fetch("/api/status")
      .then((res) => res.json())
      .then((data) => {
        if (data.threads) {
          setState((prev) => ({ ...prev, threads: data.threads }));
          // Update metrics based on threads
          const completed = Object.values(data.threads).filter(
            (t) => t.status === "completed",
          ).length;
          const total = Object.keys(data.threads).length;
          if (total > 0) {
            setState((prev) => ({
              ...prev,
              metrics: {
                ...prev.metrics,
                tasksToday: completed,
                successRate: completed / total,
              },
            }));
          }
        }
      })
      .catch((err) => console.error("Failed to load status:", err));

    // Load system info
    fetch("/api/system")
      .then((res) => res.json())
      .then((data) => {
        setState((prev) => ({ ...prev, systemInfo: data }));
      })
      .catch((err) => console.error("Failed to load system info:", err));

    const es = new EventSource(url);

    es.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        if (event.type === "heartbeat") return;

        console.log("[SSE Event]", event);
        setState((prev) => {
          const newState = applyDashboardEvent(prev, event);
          console.log("[New State]", newState);
          return newState;
        });

        // Refresh threads status after each event
        fetch("/api/status")
          .then((res) => res.json())
          .then((data) => {
            if (data.threads) {
              setState((prev) => ({ ...prev, threads: data.threads }));
            }
          })
          .catch(() => {});
      } catch (err) {
        console.error("SSE parse error:", err);
      }
    };

    es.onerror = (err) => {
      console.error("SSE error:", err);
      console.warn("SSE disconnected, reconnecting in 3s...");
      es.close();
      setTimeout(() => window.location.reload(), 3000);
    };

    console.log("[SSE] Connected to:", url);

    return () => es.close();
  }, [url]);

  return state;
}

// Fold function: apply an event to the current state
function applyDashboardEvent(state, event) {
  const now = event.ts || Date.now();

  // Extract worker ID from event
  const workerId =
    event.worker ||
    (event.thread_id ? `wt-${event.thread_id.slice(-6)}` : "unknown");

  switch (event.type) {
    case "WorkerStarted": {
      let workers = [...state.workers];
      const existingIdx = workers.findIndex((w) => w.id === workerId);

      if (existingIdx >= 0) {
        workers[existingIdx] = {
          ...workers[existingIdx],
          status: "BUSY",
          task: event.msg || event.task || "Processing...",
          progress: event.progress || 10,
        };
      } else {
        workers.push({
          id: workerId,
          status: "BUSY",
          task: event.msg || event.task || "Processing...",
          progress: event.progress || 10,
          type: "code",
          tokens: 0,
          branch: event.thread_id || `agent/${workerId}`,
        });
      }

      const events = [
        {
          id: `e-${now}`,
          ts: now,
          type: "WorkerStarted",
          worker: workerId,
          msg: event.msg,
        },
        ...state.events,
      ].slice(0, 50);

      // Determine the active layer from the message
      let activeLayers = [...state.activeLayers];
      const msg = (event.msg || "").toLowerCase();
      if (msg.includes("planning")) {
        activeLayers = ["PLANNING"];
      } else if (msg.includes("execution") || msg.includes("generating code")) {
        activeLayers = ["EXECUTION"];
      } else if (msg.includes("verification") || msg.includes("reviewing")) {
        activeLayers = ["VERIFICATION"];
      }

      return { ...state, workers, events, activeLayers };
    }

    case "WorkerCompleted": {
      const workers = state.workers.map((w) =>
        w.id === workerId ? { ...w, status: "DONE", progress: 100 } : w,
      );
      const events = [
        {
          id: `e-${now}`,
          ts: now,
          type: "WorkerCompleted",
          worker: workerId,
          msg: event.msg,
        },
        ...state.events,
      ].slice(0, 50);
      const metrics = {
        ...state.metrics,
        tasksToday: state.metrics.tasksToday + 1,
      };

      // Clear the active layer on completion
      const activeLayers = [];

      return { ...state, workers, events, metrics, activeLayers };
    }

    case "WorkerFailed": {
      const workers = state.workers.map((w) =>
        w.id === workerId ? { ...w, status: "FAIL", progress: 0 } : w,
      );
      const events = [
        {
          id: `e-${now}`,
          ts: now,
          type: "WorkerFailed",
          worker: workerId,
          msg: event.msg,
        },
        ...state.events,
      ].slice(0, 50);

      // Clear the active layer on failure
      const activeLayers = [];

      return { ...state, workers, events, activeLayers };
    }

    case "NeedsApproval": {
      const workers = state.workers.map((w) =>
        w.id === workerId ? { ...w, status: "WAIT", progress: 100 } : w,
      );
      const events = [
        {
          id: `e-${now}`,
          ts: now,
          type: "NeedsApproval",
          worker: workerId,
          msg: event.msg,
        },
        ...state.events,
      ].slice(0, 50);
      return { ...state, workers, events };
    }

    default:
      // Log unknown events
      const events = [
        {
          id: `e-${now}`,
          ts: now,
          type: event.type || "Unknown",
          worker: workerId,
          msg: event.msg || JSON.stringify(event),
        },
        ...state.events,
      ].slice(0, 50);
      return { ...state, events };
  }
}

// APPROVE request
async function approveWorker(workerId) {
  await fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: workerId }),
  });
}

// ─── Status config ────────────────────────────────────────────────────────────
const STATUS = {
  BUSY: {
    color: "#00D4FF",
    bg: "rgba(0,212,255,0.08)",
    label: "BUSY",
    pulse: true,
  },
  IDLE: {
    color: "#4ADE80",
    bg: "rgba(74,222,128,0.08)",
    label: "IDLE",
    pulse: false,
  },
  FAIL: {
    color: "#FF4D6D",
    bg: "rgba(255,77,109,0.08)",
    label: "FAIL",
    pulse: false,
  },
  WAIT: {
    color: "#FFB800",
    bg: "rgba(255,184,0,0.08)",
    label: "WAIT",
    pulse: true,
  },
  DONE: {
    color: "#4ADE80",
    bg: "rgba(74,222,128,0.05)",
    label: "DONE",
    pulse: false,
  },
};

const TYPE_ICON = { code: "⬡", browser: "◎", review: "◈" };

const EVENT_COLOR = {
  WorkerCompleted: "#4ADE80",
  NeedsApproval: "#FFB800",
  WorkerFailed: "#FF4D6D",
  WorkerStarted: "#00D4FF",
};

// ─── Components ──────────────────────────────────────────────────────────────
function WorkerCard({ worker, index }) {
  const st = STATUS[worker.status] || STATUS.IDLE;
  return (
    <div
      style={{
        background: st.bg,
        border: `1px solid ${st.color}22`,
        borderLeft: `3px solid ${st.color}`,
        borderRadius: "2px",
        padding: "14px 16px",
        position: "relative",
        overflow: "hidden",
        animation: `fadeSlideIn 0.4s ease both`,
        animationDelay: `${index * 60}ms`,
        transition: "border-color 0.3s",
      }}
    >
      {/* Scan line effect for BUSY */}
      {worker.status === "BUSY" && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: "1px",
            background: `linear-gradient(90deg, transparent, ${st.color}88, transparent)`,
            animation: "scanLine 2s linear infinite",
          }}
        />
      )}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginBottom: "10px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span
            style={{
              fontFamily: "monospace",
              fontSize: "11px",
              color: "#666",
              letterSpacing: "0.05em",
            }}
          >
            {TYPE_ICON[worker.type]} {worker.id}
          </span>
          <span
            style={{
              fontSize: "9px",
              fontFamily: "monospace",
              letterSpacing: "0.12em",
              color: st.color,
              padding: "2px 6px",
              border: `1px solid ${st.color}44`,
              borderRadius: "1px",
              display: "flex",
              alignItems: "center",
              gap: "4px",
            }}
          >
            {st.pulse && (
              <span
                style={{
                  width: "5px",
                  height: "5px",
                  borderRadius: "50%",
                  background: st.color,
                  display: "inline-block",
                  animation: "blink 1.2s ease infinite",
                }}
              />
            )}
            {st.label}
          </span>
        </div>
        {worker.tokens > 0 && (
          <span
            style={{ fontSize: "10px", color: "#444", fontFamily: "monospace" }}
          >
            {(worker.tokens / 1000).toFixed(1)}k tok
          </span>
        )}
      </div>

      <div
        style={{
          fontSize: "12px",
          color: worker.task ? "#ccc" : "#444",
          lineHeight: 1.4,
          marginBottom: "12px",
          minHeight: "32px",
          fontFamily: worker.task ? "'IBM Plex Mono', monospace" : "inherit",
          fontStyle: worker.task ? "normal" : "italic",
        }}
      >
        {worker.task || "awaiting task"}
      </div>

      {worker.branch && (
        <div
          style={{
            fontSize: "10px",
            color: "#555",
            fontFamily: "monospace",
            marginBottom: "10px",
          }}
        >
          ⎇ {worker.branch}
        </div>
      )}

      {/* Progress bar */}
      <div
        style={{
          height: "2px",
          background: "#1a1a1a",
          borderRadius: "1px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${worker.progress}%`,
            background:
              worker.status === "FAIL"
                ? "#FF4D6D"
                : `linear-gradient(90deg, ${st.color}88, ${st.color})`,
            borderRadius: "1px",
            transition: "width 0.8s cubic-bezier(0.4, 0, 0.2, 1)",
            boxShadow:
              worker.status === "BUSY" ? `0 0 8px ${st.color}66` : "none",
          }}
        />
      </div>
      {worker.progress > 0 && (
        <div
          style={{
            textAlign: "right",
            fontSize: "9px",
            color: "#444",
            marginTop: "3px",
            fontFamily: "monospace",
          }}
        >
          {worker.progress}%
        </div>
      )}

      {/* Approve button for WAIT */}
      {worker.status === "WAIT" && (
        <button
          style={{
            marginTop: "10px",
            width: "100%",
            padding: "6px",
            background: "transparent",
            border: `1px solid ${st.color}`,
            color: st.color,
            fontSize: "10px",
            fontFamily: "monospace",
            letterSpacing: "0.1em",
            cursor: "pointer",
            borderRadius: "1px",
            transition: "all 0.2s",
          }}
          onMouseEnter={(e) => {
            e.target.style.background = `${st.color}22`;
          }}
          onMouseLeave={(e) => {
            e.target.style.background = "transparent";
          }}
          onClick={() => approveWorker(worker.id)}
        >
          APPROVE →
        </button>
      )}
    </div>
  );
}

function EventLog({ events }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = 0;
  }, [events]);
  return (
    <div
      ref={ref}
      style={{
        height: "220px",
        overflowY: "auto",
        scrollbarWidth: "thin",
        scrollbarColor: "#222 transparent",
      }}
    >
      {events.map((ev, i) => (
        <div
          key={ev.id}
          style={{
            display: "flex",
            gap: "10px",
            alignItems: "flex-start",
            padding: "8px 0",
            borderBottom: "1px solid #111",
            animation: `fadeSlideIn 0.3s ease both`,
            animationDelay: `${i * 30}ms`,
          }}
        >
          <span
            style={{
              width: "6px",
              height: "6px",
              borderRadius: "50%",
              marginTop: "5px",
              flexShrink: 0,
              background: EVENT_COLOR[ev.type] || "#555",
              boxShadow: `0 0 6px ${EVENT_COLOR[ev.type] || "#555"}66`,
            }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontSize: "11px",
                color: "#bbb",
                lineHeight: 1.4,
                fontFamily: "'IBM Plex Mono', monospace",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {ev.msg}
            </div>
            <div style={{ display: "flex", gap: "8px", marginTop: "2px" }}>
              <span
                style={{
                  fontSize: "9px",
                  color: "#444",
                  fontFamily: "monospace",
                }}
              >
                {ev.worker}
              </span>
              <span
                style={{
                  fontSize: "9px",
                  color: "#333",
                  fontFamily: "monospace",
                }}
              >
                {timeAgo(ev.ts)}
              </span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function GoalBar({ goal }) {
  const pct = Math.round((goal.done / goal.tasks) * 100);
  const st = STATUS[goal.status] || STATUS.IDLE;
  return (
    <div style={{ marginBottom: "14px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: "5px",
        }}
      >
        <span
          style={{
            fontSize: "11px",
            color: "#aaa",
            fontFamily: "'IBM Plex Mono', monospace",
          }}
        >
          {goal.title}
        </span>
        <span
          style={{ fontSize: "10px", color: "#555", fontFamily: "monospace" }}
        >
          {goal.done}/{goal.tasks}
        </span>
      </div>
      <div
        style={{
          height: "3px",
          background: "#111",
          borderRadius: "2px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${pct}%`,
            background:
              pct === 100
                ? "#4ADE80"
                : `linear-gradient(90deg, ${st.color}66, ${st.color})`,
            borderRadius: "2px",
            transition: "width 1s cubic-bezier(0.4, 0, 0.2, 1)",
          }}
        />
      </div>
    </div>
  );
}

function MetricTile({ label, value, sub, accent }) {
  return (
    <div
      style={{
        padding: "14px",
        border: "1px solid #1a1a1a",
        borderRadius: "2px",
        background: "#0a0a0a",
      }}
    >
      <div
        style={{
          fontSize: "22px",
          fontFamily: "'DM Mono', monospace",
          color: accent || "#e0e0e0",
          letterSpacing: "-0.02em",
          lineHeight: 1,
        }}
      >
        {value}
      </div>
      <div
        style={{
          fontSize: "9px",
          color: "#444",
          marginTop: "5px",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
        }}
      >
        {label}
      </div>
      {sub && (
        <div
          style={{
            fontSize: "10px",
            color: "#333",
            marginTop: "3px",
            fontFamily: "monospace",
          }}
        >
          {sub}
        </div>
      )}
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────
function FarmDashboard() {
  const state = useSSE("/api/events");
  const [tick, setTick] = useState(0);

  // Clock
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  if (!state)
    return (
      <div
        style={{
          background: "#050505",
          height: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#333",
          fontFamily: "monospace",
        }}
      >
        connecting...
      </div>
    );

  const {
    workers,
    events,
    goals,
    metrics,
    activeLayers = [],
    threads = {},
    systemInfo = { os: "unknown", vcpu: 0 },
  } = state;
  const busyCount = workers.filter((w) => w.status === "BUSY").length;
  const failCount = workers.filter((w) => w.status === "FAIL").length;
  const waitCount = workers.filter((w) => w.status === "WAIT").length;
  const tokenPct = Math.round(
    (metrics.tokensUsed / metrics.tokensBudget) * 100,
  );
  const now = new Date();

  return (
    <div
      style={{
        background: "#050505",
        minHeight: "100vh",
        color: "#e0e0e0",
        fontFamily: "'IBM Plex Sans', sans-serif",
        padding: "24px",
        boxSizing: "border-box",
      }}
    >
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400&family=DM+Mono:wght@300;400;500&display=swap');
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #222; border-radius: 2px; }
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(6px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50%      { opacity: 0.2; }
        }
        @keyframes scanLine {
          0%   { transform: translateX(-100%); }
          100% { transform: translateX(100%); }
        }
      `}</style>

      {/* ── Header ── */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginBottom: "28px",
          animation: "fadeSlideIn 0.5s ease both",
        }}
      >
        <div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "10px",
              marginBottom: "4px",
            }}
          >
            <div
              style={{
                width: "8px",
                height: "8px",
                borderRadius: "50%",
                background: busyCount > 0 ? "#00D4FF" : "#4ADE80",
                boxShadow: `0 0 12px ${busyCount > 0 ? "#00D4FF" : "#4ADE80"}`,
                animation: busyCount > 0 ? "blink 1.5s ease infinite" : "none",
              }}
            />
            <h1
              style={{
                margin: 0,
                fontSize: "14px",
                fontFamily: "'DM Mono', monospace",
                fontWeight: 400,
                letterSpacing: "0.2em",
                color: "#e0e0e0",
                textTransform: "uppercase",
              }}
            >
              Developer Farm
            </h1>
          </div>
          <div
            style={{
              fontSize: "11px",
              color: "#333",
              fontFamily: "monospace",
              paddingLeft: "18px",
            }}
          >
            {busyCount} working ·{" "}
            {waitCount > 0 ? `${waitCount} waiting approval · ` : ""}
            {failCount > 0 ? `${failCount} failed · ` : ""}
            {now.toLocaleTimeString("en-GB", { hour12: false })}
          </div>
        </div>

        {/* Token budget */}
        <div style={{ textAlign: "right" }}>
          <div
            style={{
              fontSize: "10px",
              color: "#444",
              letterSpacing: "0.1em",
              marginBottom: "4px",
              fontFamily: "monospace",
            }}
          >
            TOKEN BUDGET
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <div
              style={{
                width: "80px",
                height: "3px",
                background: "#111",
                borderRadius: "2px",
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${tokenPct}%`,
                  background:
                    tokenPct > 80
                      ? "#FF4D6D"
                      : tokenPct > 60
                        ? "#FFB800"
                        : "#00D4FF",
                  borderRadius: "2px",
                  transition: "width 1s ease",
                }}
              />
            </div>
            <span
              style={{
                fontSize: "11px",
                color: "#555",
                fontFamily: "monospace",
              }}
            >
              {tokenPct}%
            </span>
          </div>
        </div>
      </div>

      {/* ── Metrics row ── */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(4, 1fr)",
          gap: "8px",
          marginBottom: "24px",
          animation: "fadeSlideIn 0.5s ease 0.1s both",
        }}
      >
        <MetricTile
          label="Tasks Today"
          value={metrics.tasksToday}
          accent="#e0e0e0"
        />
        <MetricTile
          label="Success Rate"
          value={`${Math.round(metrics.successRate * 100)}%`}
          accent="#4ADE80"
        />
        <MetricTile
          label="Avg Task Time"
          value={`${metrics.avgTaskMin}m`}
          accent="#00D4FF"
        />
        <MetricTile
          label="Tokens Used"
          value={`${(metrics.tokensUsed / 1000).toFixed(0)}k`}
          sub={`of ${(metrics.tokensBudget / 1000).toFixed(0)}k`}
          accent={tokenPct > 80 ? "#FF4D6D" : "#e0e0e0"}
        />
      </div>

      {/* ── Main grid ── */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 320px",
          gap: "16px",
          animation: "fadeSlideIn 0.5s ease 0.2s both",
        }}
      >
        {/* Left: workers */}
        <div>
          <div
            style={{
              fontSize: "9px",
              letterSpacing: "0.2em",
              color: "#333",
              textTransform: "uppercase",
              fontFamily: "monospace",
              marginBottom: "10px",
            }}
          >
            Workers · {workers.length} total
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: "8px",
            }}
          >
            {workers.map((w, i) => (
              <WorkerCard key={w.id} worker={w} index={i} />
            ))}
          </div>
        </div>

        {/* Right: sidebar */}
        <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
          {/* Goals */}
          <div
            style={{
              border: "1px solid #111",
              padding: "16px",
              borderRadius: "2px",
            }}
          >
            <div
              style={{
                fontSize: "9px",
                letterSpacing: "0.2em",
                color: "#333",
                textTransform: "uppercase",
                fontFamily: "monospace",
                marginBottom: "14px",
              }}
            >
              Active Goals
            </div>
            {goals.map((g) => (
              <GoalBar key={g.id} goal={g} />
            ))}
          </div>

          {/* Event log */}
          <div
            style={{
              border: "1px solid #111",
              padding: "16px",
              borderRadius: "2px",
              flex: 1,
            }}
          >
            <div
              style={{
                fontSize: "9px",
                letterSpacing: "0.2em",
                color: "#333",
                textTransform: "uppercase",
                fontFamily: "monospace",
                marginBottom: "12px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <span>Event Stream</span>
              <span style={{ color: "#1a1a1a" }}>live</span>
            </div>
            <EventLog events={events} />
          </div>

          {/* Layer status */}
          <div
            style={{
              border: "1px solid #111",
              padding: "16px",
              borderRadius: "2px",
            }}
          >
            <div
              style={{
                fontSize: "9px",
                letterSpacing: "0.2em",
                color: "#333",
                textTransform: "uppercase",
                fontFamily: "monospace",
                marginBottom: "12px",
              }}
            >
              System Layers
            </div>
            {[
              { name: "PLANNING", ok: activeLayers.includes("PLANNING") },
              { name: "EXECUTION", ok: activeLayers.includes("EXECUTION") },
              {
                name: "VERIFICATION",
                ok: activeLayers.includes("VERIFICATION"),
              },
              { name: "CHECKPOINTER", ok: activeLayers.length > 0 }, // Active when any node is running
            ].map((layer) => (
              <div
                key={layer.name}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "5px 0",
                  borderBottom: "1px solid #0d0d0d",
                }}
              >
                <span
                  style={{
                    fontSize: "10px",
                    color: "#555",
                    fontFamily: "monospace",
                    letterSpacing: "0.05em",
                  }}
                >
                  {layer.name}
                </span>
                <span
                  style={{
                    fontSize: "9px",
                    fontFamily: "monospace",
                    color: layer.ok ? "#4ADE80" : "#333",
                  }}
                >
                  {layer.ok ? "● online" : "○ idle"}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── Footer ── */}
      <div
        style={{
          marginTop: "20px",
          paddingTop: "16px",
          borderTop: "1px solid #0d0d0d",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          animation: "fadeSlideIn 0.5s ease 0.4s both",
        }}
      >
        <span
          style={{
            fontSize: "9px",
            color: "#222",
            fontFamily: "monospace",
            letterSpacing: "0.1em",
          }}
        >
          {`SSE · /api/events · ${events.length} events · ${Object.keys(threads).length} threads`}
        </span>
        <span
          style={{ fontSize: "9px", color: "#222", fontFamily: "monospace" }}
        >
          {`${now.toLocaleDateString("en-GB")} · ${systemInfo.os.toLowerCase()} · ${systemInfo.vcpu} vcpu`}
        </span>
      </div>
    </div>
  );
}
