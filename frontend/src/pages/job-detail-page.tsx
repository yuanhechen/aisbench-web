import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { Job } from "../api/types";
import { useAuth } from "../auth/auth-context";
import { JobResults } from "../components/job-results";
import { ACTIVE_STATUSES, StatusLabel } from "../components/status";
import { useI18n } from "../i18n/i18n-context";

interface LogChunk {
  offset: number;
  text: string;
}

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
  const [cancelling, setCancelling] = useState(false);
  // The last byte the server confirmed; a reconnect resumes from here, never from zero.
  const offsetRef = useRef(0);

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

  const cancellable = ACTIVE_STATUSES.includes(job.status);

  return (
    <div className="task-view">
      <header className="task-context">
        <div className="task-context-main">
          <h1 className="workspace-title">{job.dataset.name}</h1>
          <p className="workspace-subtitle">
            {job.mode === "accuracy" ? t("newJob.accuracy") : t("newJob.performance")} ·{" "}
            {job.model.model_name}
          </p>
        </div>
        <StatusLabel status={job.status} />
      </header>

      <section className="task-block">
        <h2 className="form-step-title">{t("jobDetail.configuration")}</h2>
        <dl className="config-row">
          <div>
            <dt>{t("newJob.modelEndpoint")}</dt>
            <dd>{job.model.name}</dd>
          </div>
          <div>
            <dt>Base URL</dt>
            <dd className="mono">{job.model.base_url}</dd>
          </div>
          {Object.entries(job.parameters)
            .filter(([, value]) => value !== null)
            .map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd className="mono">{String(value)}</dd>
              </div>
            ))}
        </dl>
      </section>

      <section className="task-block">
        <h2 className="form-step-title">{t("jobDetail.state")}</h2>
        {job.status === "queued" && job.queue_position !== null && (
          <p>
            {t("jobDetail.queuePosition")} {job.queue_position} · {t("jobDetail.ahead")}{" "}
            {job.queue_position - 1}
          </p>
        )}
        {job.progress !== null && (
          <p className="progress-line">
            {job.progress.completed} / {job.progress.total}
          </p>
        )}
        {job.error_message !== null && (
          <p className="form-error" role="alert">
            {job.error_message}
          </p>
        )}
      </section>

      {job.status === "succeeded" && <JobResults jobId={jobId} />}

      <section className="task-block">
        <h2 className="form-step-title">{t("jobDetail.log")}</h2>
        <pre className="log-view">{log}</pre>
      </section>

      {cancellable && (
        <section className="task-actions">
          {confirmingCancel ? (
            <>
              <span>{t("jobDetail.confirmCancel")}</span>
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
            </>
          ) : (
            <button
              type="button"
              className="button-secondary"
              onClick={() => setConfirmingCancel(true)}
            >
              {t("jobDetail.stop")}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
