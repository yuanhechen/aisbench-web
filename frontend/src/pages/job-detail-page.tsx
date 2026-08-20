import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

import { api } from "../api/client";
import type { Job } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { DatasetRows } from "../components/dataset-progress";
import { JobResult } from "../components/job-result";
import { JobArtifacts } from "../components/job-results";
import type { Artifact } from "../components/job-results";
import { RunConfiguration } from "../components/run-configuration";
import { ACTIVE_STATUSES, StatusLabel } from "../components/status";
import { useI18n } from "../i18n/i18n-context";

// A socket only speeds this up. Polling is what guarantees a running job stays current when
// the socket never connects, which is what happens behind some proxies.
const ACTIVE_POLL_MS = 2000;

const RAIL_STORAGE_KEY = "aisbench-web.railWidth";
const RAIL_MIN = 240;
const RAIL_MAX = 560;

/** The rail's width is a reader's preference; keep it across visits. */
function storedRailWidth(): number {
  const saved = Number(window.localStorage.getItem(RAIL_STORAGE_KEY));
  return Number.isFinite(saved) && saved >= RAIL_MIN && saved <= RAIL_MAX ? saved : 320;
}

function useWideLayout(): boolean {
  const [wide, setWide] = useState(() =>
    typeof window.matchMedia === "function"
      ? window.matchMedia("(min-width: 1001px)").matches
      : true,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia("(min-width: 1001px)");
    const listener = (event: MediaQueryListEvent) => setWide(event.matches);
    query.addEventListener("change", listener);
    return () => query.removeEventListener("change", listener);
  }, []);
  return wide;
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
  const [error, setError] = useState<string | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  // One fetch, two columns: the files list in the rail and the summary reads in the main.
  const artifacts = useApiQuery<Artifact[]>(`/api/jobs/${jobId}/artifacts`, {
    onFailure: reportFailure,
  });
  const [cancelling, setCancelling] = useState(false);
  const wide = useWideLayout();
  const [railWidth, setRailWidth] = useState(storedRailWidth);
  // The drag closure needs the latest width without re-binding on every pixel.
  const railWidthRef = useRef(railWidth);
  railWidthRef.current = railWidth;

  const dragRail = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = railWidthRef.current;
    const move = (moveEvent: PointerEvent) => {
      // Dragging the seam left gives the rail more room, and the run less.
      const width = Math.min(RAIL_MAX, Math.max(RAIL_MIN, startWidth + (startX - moveEvent.clientX)));
      railWidthRef.current = width;
      setRailWidth(width);
      window.localStorage.setItem(RAIL_STORAGE_KEY, String(Math.round(width)));
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  }, []);

  const refreshJob = useCallback(async () => {
    try {
      setJob(await api.get<Job>(`/api/jobs/${jobId}`));
      setError(null);
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [jobId, reportFailure]);

  useEffect(() => {
    void refreshJob();
  }, [refreshJob]);

  const active = job !== null && ACTIVE_STATUSES.includes(job.status);

  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setInterval(() => {
      void refreshJob();
    }, ACTIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [active, refreshJob]);

  // The files are written as the run ends, so a page opened while it was still going has
  // only ever seen an empty list. The job payload is polled and heals itself; this does not.
  const wasActive = useRef(false);
  const reloadArtifacts = artifacts.reload;
  useEffect(() => {
    if (wasActive.current && !active) {
      reloadArtifacts();
    }
    wasActive.current = active;
  }, [active, reloadArtifacts]);

  useEffect(() => {
    const socket = new WebSocket(eventSocketUrl(jobId));
    const handleMessage = () => {
      // REST is authoritative: an event only says "something changed, go look".
      void refreshJob();
    };
    socket.addEventListener("message", handleMessage as EventListener);
    return () => {
      socket.removeEventListener("message", handleMessage as EventListener);
      socket.close();
    };
  }, [jobId, refreshJob]);

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
  // While the run is live its clock ticks in the main column; the rail would only
  // duplicate it with a coarser rhythm. Finished, the rail keeps the final figure.
  const elapsed = active ? null : durationBetween(job.started_at, job.finished_at);

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

      {/* Main column carries what happened; the rail carries what it was run with. The seam
          between them is draggable: some runs are read for their parameters, some for rows. */}
      <div
        className="task-columns"
        style={wide ? { gridTemplateColumns: `minmax(0, 1fr) 40px ${railWidth}px` } : undefined}
      >
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

          {/* Per-dataset rows while any exist: progress while it runs, scores after. Rows
              only the tqdm pipeline can offer (no status_tmp, older jobs) fall back to it. */}
          {job.datasets.length > 0 ? (
            <DatasetRows job={job} jobId={jobId} />
          ) : (
            active &&
            job.progress !== null && (
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
            )
          )}

          {job.status === "succeeded" && job.datasets.length === 0 && (
            <JobResult
              jobId={jobId}
              artifacts={artifacts.data ?? []}
              datasetName={job.dataset.name}
            />
          )}
        </div>

        {wide && (
          <div
            className="rail-seam"
            role="separator"
            aria-orientation="vertical"
            aria-label={t("jobDetail.railResize")}
            onPointerDown={dragRail}
          />
        )}
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
