import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import type { Job } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { EmptyState } from "../components/empty-state";
import { JOB_STATUS_LABELS, StatusLabel } from "../components/status";
import { PageHeader } from "../components/page-header";
import { useI18n } from "../i18n/i18n-context";

const LIST_POLL_MS = 3000;

export function JobsPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const jobs = useApiQuery<Job[]>("/api/jobs", {
    pollMs: LIST_POLL_MS,
    onFailure: reportFailure,
  });
  const [status, setStatus] = useState("");
  const [mode, setMode] = useState("");

  const visible = useMemo(
    () =>
      (jobs.data ?? []).filter(
        (job) => (status === "" || job.status === status) && (mode === "" || job.mode === mode),
      ),
    [jobs.data, status, mode],
  );

  return (
    <>
      <PageHeader title={t("nav.jobs")} />

      <div className="filter-row">
        <label className="field" htmlFor="job-filter-status">
          {t("jobs.filterStatus")}
        </label>
        <select
          id="job-filter-status"
          className="input"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
        >
          <option value="">{t("jobs.filterAll")}</option>
          {Object.keys(JOB_STATUS_LABELS).map((value) => (
            <option key={value} value={value}>
              {t(JOB_STATUS_LABELS[value])}
            </option>
          ))}
        </select>
        <label className="field" htmlFor="job-filter-mode">
          {t("jobs.filterMode")}
        </label>
        <select
          id="job-filter-mode"
          className="input"
          value={mode}
          onChange={(event) => setMode(event.target.value)}
        >
          <option value="">{t("jobs.filterAll")}</option>
          <option value="accuracy">{t("newJob.accuracy")}</option>
          <option value="performance">{t("newJob.performance")}</option>
        </select>
      </div>

      {jobs.data !== null && visible.length === 0 && <EmptyState message={t("jobs.empty")} />}

      {visible.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>{t("jobs.name")}</th>
              <th>{t("jobs.dataset")}</th>
              <th>{t("jobs.mode")}</th>
              <th>{t("jobs.model")}</th>
              <th>{t("jobs.status")}</th>
              <th>{t("jobs.created")}</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((job) => (
              <tr key={job.id}>
                <td>
                  <Link to={`/jobs/${job.id}`}>
                    {job.name === "" ? job.dataset.name : job.name}
                  </Link>
                </td>
                <td className="mono">{job.dataset.name}</td>
                <td>{job.mode === "accuracy" ? t("newJob.accuracy") : t("newJob.performance")}</td>
                <td className="mono">{job.model.model_name}</td>
                <td>
                  <StatusLabel status={job.status} />
                </td>
                <td className="mono">{job.created_at.slice(0, 19).replace("T", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
