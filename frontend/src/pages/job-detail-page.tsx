import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { Job } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { JobResult } from "../components/job-result";
import { JobArtifacts } from "../components/job-results";
import type { Artifact } from "../components/job-results";
import { LogView } from "../components/log-view";
import { RunConfiguration } from "../components/run-configuration";
import { ACTIVE_STATUSES, StatusLabel } from "../components/status";
import { useI18n } from "../i18n/i18n-context";

interface LogChunk {
  offset: number;
  text: string;
}

// A socket only speeds this up. Polling is what guarantees a running job stays current when
// the socket never connects, which is what happens behind some proxies.
const ACTIVE_POLL_MS = 2000;

/** Live events only nudge the page; every value shown is fetched over REST. */
function eventSocketUrl(jobId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/jobs/${jobId}`;
}

export function JobDetailPage({ jobId }: { jobId: string }) {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const [job, setJob] = useState<Job | null>(null);
  const [log, setLog] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  // One fetch, two columns: the files list in the rail and the summary reads in the main.
  const artifacts = useApiQuery<Artifact[]>(`/api/jobs/${jobId}/artifacts`, {
    onFailure: reportFailure,
  });
  const [cancelling, setCancelling] = useState(false);
  // The last byte the server confirmed; a reconnect resumes from here, never from zero.
  const offsetRef = useRef(0);
  const logRef = useRef<HTMLPreElement>(null);
  const pinnedRef = useRef(true);

  const refreshJob = useCallback(async () => {
    try {
      setJob(await api.get<Job>(`/api/jobs/${jobId}`));
      setError(null);
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [jobId, reportFailure]);

  const pullLog = useCallback(async () => {
    try {
      const chunk = await api.get<LogChunk>(
        `/api/jobs/${jobId}/logs?offset=${offsetRef.current}`,
      );
      if (chunk.text !== "") {
        const view = logRef.current;
        // Follow the tail only while the reader is already at the bottom.
        pinnedRef.current =
          view === null || view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
        offsetRef.current = chunk.offset;
        setLog((current) => current + chunk.text);
      }
    } catch (failure) {
      reportFailure(failure);
    }
  }, [jobId, reportFailure]);

  useEffect(() => {
    offsetRef.current = 0;
    setLog("");
    void refreshJob();
    void pullLog();
  }, [jobId, refreshJob, pullLog]);

  const active = job !== null && ACTIVE_STATUSES.includes(job.status);

  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setInterval(() => {
      void refreshJob();
      void pullLog();
    }, ACTIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [active, refreshJob, pullLog]);

  useEffect(() => {
    const socket = new WebSocket(eventSocketUrl(jobId));
    const handleMessage = () => {
      // REST is authoritative: an event only says "something changed, go look".
      void refreshJob();
      void pullLog();
    };
    socket.addEventListener("message", handleMessage as EventListener);
    return () => {
      socket.removeEventListener("message", handleMessage as EventListener);
      socket.close();
    };
  }, [jobId, refreshJob, pullLog]);

  useEffect(() => {
    if (pinnedRef.current && logRef.current !== null) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [log]);

  async function cancel() {
    setCancelling(true);
    try {
      setJob(await api.post<Job>(`/api/jobs/${jobId}/cancel`));
      setConfirmingCancel(false);
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setCancelling(false);
    }
  }

  if (job === null) {
    return (
      <main>
        {error !== null ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : (
          <p className="empty-state">{t("common.loading")}</p>
        )}
      </main>
    );
  }

  const cancellable = active;
  const elapsed = durationBetween(job.started_at, job.finished_at);

  return (
    <div className="task-view">
      <header className="task-context">
        <div className="task-context-main">
          <h1 className="task-title">{job.name === "" ? job.dataset.name : job.name}</h1>
          <p className="task-subtitle">
            {/* An endpoint whose model was never detected has no name to print. */}
            {[
              job.mode === "accuracy" ? t("newJob.accuracy") : t("newJob.performance"),
              job.model.model_name,
            ]
              .filter((part) => part !== "")
              .join(" · ")}
          </p>
        </div>
        <div className="task-context-status">
          <StatusLabel status={job.status} />
          {cancellable &&
            (confirmingCancel ? (
              <span className="task-actions">
                <span className="task-confirm">{t("jobDetail.confirmCancel")}</span>
                <button
                  type="button"
                  className="button-danger"
                  disabled={cancelling}
                  onClick={() => void cancel()}
                >
                  {t("jobDetail.confirmStop")}
                </button>
                <button
                  type="button"
                  className="button-secondary"
                  onClick={() => setConfirmingCancel(false)}
                >
                  {t("common.cancel")}
                </button>
              </span>
            ) : (
              <button
                type="button"
                className="button-secondary"
                onClick={() => setConfirmingCancel(true)}
              >
                {t("jobDetail.stop")}
              </button>
            ))}
        </div>
      </header>

      {/* Main column carries what happened; the rail carries what it was run with. */}
      <div className="task-columns">
        <div className="task-main">
          {job.error_message !== null && (
            <p className="banner banner-danger" role="alert">
              {job.error_message}
            </p>
          )}

          {job.status === "queued" && job.queue_position !== null && (
            <p className="banner">
              {t("jobDetail.queuePosition")} {job.queue_position} · {t("jobDetail.ahead")}{" "}
              {job.queue_position - 1}
            </p>
          )}

          {/* A finished job has no progress left to report; the status badge already said so. */}
          {active && job.progress !== null && (
            <section>
              <div className="progress">
                <div className="progress-line">
                  {t("jobDetail.progress")} {job.progress.completed} / {job.progress.total}
                  {job.progress.total > 0 &&
                    ` · ${Math.round((job.progress.completed / job.progress.total) * 100)}%`}
                </div>
                {job.progress.total > 0 && (
                  <div className="progress-track">
                    <div
                      className="progress-fill"
                      style={{
                        width: `${Math.min(
                          100,
                          (job.progress.completed / job.progress.total) * 100,
                        )}%`,
                      }}
                    />
                  </div>
                )}
              </div>
            </section>
          )}

          {job.status === "succeeded" && (
            <JobResult
              jobId={jobId}
              artifacts={artifacts.data ?? []}
              datasetName={job.dataset.name}
            />
          )}

          {/* Open by default in every state. It is where the run says what it actually
              did, and its own scroll caps how much of the page it can take. */}
          <details className="card-log" open>
            <summary>
              {t("jobDetail.log")}
              {active && <span className="live-dot" aria-label={t("jobDetail.live")} />}
            </summary>
            <LogView ref={logRef} text={log} empty={t("jobDetail.noLogYet")} />
          </details>
        </div>

        <aside className="task-rail">
          <section className="rail-section">
            <h2 className="eyebrow">{t("jobDetail.runInfo")}</h2>
            <RunConfiguration job={job} elapsed={elapsed} />
          </section>
          {job.status === "succeeded" && (
            <JobArtifacts jobId={jobId} artifacts={artifacts.data ?? []} />
          )}
        </aside>
      </div>
    </div>
  );
}

/** Wall-clock time the run took, or how long it has been going. */
function durationBetween(started: string | null, finished: string | null): string | null {
  if (started === null) {
    return null;
  }
  const from = Date.parse(started);
  const to = finished === null ? Date.now() : Date.parse(finished);
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) {
    return null;
  }
  const seconds = Math.round((to - from) / 1000);
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
