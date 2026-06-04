"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { StudentMetrics, UsersPayload } from "@/lib/types";

const TEN_THOUSAND = 10_000;

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatWindow(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed with HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export default function DashboardClient() {
  const [payload, setPayload] = useState<UsersPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [busyStudentId, setBusyStudentId] = useState<string | null>(null);
  const [tokenDeltas, setTokenDeltas] = useState<Record<string, string>>({});

  const fetchUsers = useCallback(async () => {
    try {
      const data = await requestJson<UsersPayload>("/api/users");
      setPayload(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to fetch gateway metrics.");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void fetchUsers(), 0);
    const timer = window.setInterval(() => void fetchUsers(), 4000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [fetchUsers]);

  const totals = useMemo(() => {
    const users = payload?.users ?? [];
    const consumed = users.reduce((sum, user) => sum + user.total_tokens_consumed, 0);
    const ceiling = users.reduce((sum, user) => sum + user.monthly_token_ceiling, 0);
    const active = users.filter((user) => user.is_active).length;
    return {
      active,
      consumed,
      ceiling,
      percentUsed: ceiling > 0 ? (consumed / ceiling) * 100 : 0,
    };
  }, [payload]);

  async function mutateStudent(studentId: string, path: string, body: unknown) {
    setBusyStudentId(studentId);
    try {
      await requestJson(`/api/users/${encodeURIComponent(studentId)}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await fetchUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Admin action failed.");
    } finally {
      setBusyStudentId(null);
    }
  }

  function tokenDeltaFor(user: StudentMetrics): number {
    const raw = tokenDeltas[user.student_id];
    if (!raw) {
      return -TEN_THOUSAND;
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? Math.trunc(parsed) : -TEN_THOUSAND;
  }

  return (
    <main className="dashboard-shell">
      <header className="top-panel">
        <div>
          <p className="eyebrow">PoiesisPathfinder / Governance Gateway</p>
          <h1>Club API Control Room</h1>
        </div>
        <div className="summary-strip" aria-live="polite">
          <div>
            <span>Active Keys</span>
            <strong>{payload ? `${totals.active}/${payload.users.length}` : "0/0"}</strong>
          </div>
          <div>
            <span>Total Spend</span>
            <strong>{formatNumber(totals.consumed)}</strong>
          </div>
          <div>
            <span>Fleet Used</span>
            <strong>{totals.percentUsed.toFixed(3)}%</strong>
          </div>
        </div>
      </header>

      <section className="status-bar">
        <span className={error ? "pulse error" : "pulse"} />
        <span>{error ? "Gateway alert" : isLoading ? "Loading metrics" : "Live metrics"}</span>
        {payload ? (
          <span>
            Window {formatWindow(payload.rate_window_seconds)} / Burst{" "}
            {formatWindow(payload.burst_window_seconds)}
          </span>
        ) : null}
      </section>

      {error ? <pre className="error-box">{error}</pre> : null}

      <section className="student-grid" aria-label="Student usage metrics">
        {(payload?.users ?? []).map((user, index) => (
          <article className="student-tile" key={user.student_id}>
            <div className="tile-index">{String(index + 1).padStart(2, "0")}</div>
            <div className="student-heading">
              <div>
                <h2>{user.student_name}</h2>
                <p>{user.key_preview}</p>
              </div>
              <span className={user.is_active ? "key-state active" : "key-state inactive"}>
                {user.is_active ? "ACTIVE" : "LOCKED"}
              </span>
            </div>

            <div className="meter-block">
              <div className="meter-label">
                <span>Monthly Tokens</span>
                <strong>{user.token_percent_used.toFixed(3)}%</strong>
              </div>
              <div
                className="token-meter"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.min(user.token_percent_used, 100)}
              >
                <span style={{ width: `${Math.min(user.token_percent_used, 100)}%` }} />
              </div>
              <div className="meter-numbers">
                <span>{formatNumber(user.total_tokens_consumed)} spent</span>
                <span>{formatNumber(user.tokens_remaining)} left</span>
              </div>
            </div>

            <div className="limit-grid">
              <div>
                <span>Standard</span>
                <strong>{formatNumber(user.standard_requests_remaining)}</strong>
                <small>{formatNumber(user.standard_window_count)} used</small>
              </div>
              <div>
                <span>High-Speed</span>
                <strong>{formatNumber(user.high_speed_requests_remaining)}</strong>
                <small>{formatNumber(user.high_speed_window_count)} used</small>
              </div>
              <div>
                <span>Std Burst</span>
                <strong>{formatNumber(user.standard_burst_remaining)}</strong>
                <small>{formatWindow(user.burst_window_seconds)}</small>
              </div>
              <div>
                <span>Fast Burst</span>
                <strong>{formatNumber(user.high_speed_burst_remaining)}</strong>
                <small>{formatWindow(user.burst_window_seconds)}</small>
              </div>
            </div>

            <div className="override-row">
              <button
                type="button"
                disabled={busyStudentId === user.student_id}
                onClick={() =>
                  void mutateStudent(user.student_id, "/reset-window", {
                    tier: "standard",
                    include_burst: true,
                  })
                }
              >
                Reset Std
              </button>
              <button
                type="button"
                disabled={busyStudentId === user.student_id}
                onClick={() =>
                  void mutateStudent(user.student_id, "/reset-window", {
                    tier: "all",
                    include_burst: true,
                  })
                }
              >
                Reset All
              </button>
              <button
                type="button"
                disabled={busyStudentId === user.student_id}
                onClick={() =>
                  void mutateStudent(user.student_id, "/active", {
                    is_active: !user.is_active,
                  })
                }
              >
                {user.is_active ? "Lock" : "Unlock"}
              </button>
            </div>

            <div className="token-adjust">
              <input
                aria-label={`${user.student_name} token delta`}
                inputMode="numeric"
                value={tokenDeltas[user.student_id] ?? `-${TEN_THOUSAND}`}
                onChange={(event) =>
                  setTokenDeltas((current) => ({
                    ...current,
                    [user.student_id]: event.target.value,
                  }))
                }
              />
              <button
                type="button"
                disabled={busyStudentId === user.student_id}
                onClick={() =>
                  void mutateStudent(user.student_id, "/adjust-tokens", {
                    delta_tokens: tokenDeltaFor(user),
                  })
                }
              >
                Apply Delta
              </button>
            </div>
          </article>
        ))}
      </section>

      {!isLoading && payload?.users.length === 0 ? (
        <section className="empty-state">
          <h2>No student keys found</h2>
          <p>Run the seed job, then this board will populate automatically.</p>
        </section>
      ) : null}
    </main>
  );
}
