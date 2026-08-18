import { useMemo, useState } from "react";

import { api } from "../api/client";
import type { Job } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { EmptyState } from "../components/empty-state";
import { PageHeader } from "../components/page-header";
import { useI18n } from "../i18n/i18n-context";

const MIN_JOBS = 2;
const MAX_JOBS = 8;

interface ComparedJob {
  id: string;
  mode: string;
  model: string;
  dataset: string;
}

interface ComparisonRow {
  key: string;
  unit: string | null;
  values: Record<string, number | null>;
}

interface Comparison {
  jobs: ComparedJob[];
  rows: ComparisonRow[];
  warnings: string[];
}

function jobLabel(job: Job): string {
  return `${job.model.model_name} · ${job.dataset.name} · ${job.id}`;
}

export function ComparisonPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const jobs = useApiQuery<Job[]>("/api/jobs", { onFailure: reportFailure });
  const [selected, setSelected] = useState<string[]>([]);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);

  // Only a finished job has results to align; anything else would compare nothing.
  const comparable = useMemo(
    () => (jobs.data ?? []).filter((job) => job.status === "succeeded"),
    [jobs.data],
  );

  function toggle(jobId: string) {
    setComparison(null);
    setSelected((current) =>
      current.includes(jobId)
        ? current.filter((id) => id !== jobId)
        : current.length >= MAX_JOBS
          ? current
          : [...current, jobId],
    );
  }

  async function compare() {
    setError(null);
    setComparing(true);
    try {
      setComparison(await api.post<Comparison>("/api/comparisons", { job_ids: selected }));
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setComparing(false);
    }
  }

  const ready = selected.length >= MIN_JOBS && selected.length <= MAX_JOBS && !comparing;

  return (
    <>
      <PageHeader title={t("nav.comparison")} />

      {jobs.data !== null && comparable.length === 0 && (
        <EmptyState message={t("comparison.empty")} />
      )}

      {comparable.length > 0 && (
        <section className="form-step">
          <h2 className="form-step-title">{t("comparison.select")}</h2>
          <div className="checkbox-list">
            {comparable.map((job) => (
              <label key={job.id} className="checkbox-option">
                <input
                  type="checkbox"
                  checked={selected.includes(job.id)}
                  onChange={() => toggle(job.id)}
                />
                <span>{jobLabel(job)}</span>
              </label>
            ))}
          </div>
          <button
            type="button"
            className="button-primary"
            disabled={!ready}
            onClick={() => void compare()}
          >
            {t("comparison.compare")}
          </button>
        </section>
      )}

      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      {comparison !== null && <ComparisonTable comparison={comparison} />}
    </>
  );
}

function ComparisonTable({ comparison }: { comparison: Comparison }) {
  const { t } = useI18n();
  return (
    <section className="form-step">
      {comparison.warnings.map((warning) => (
        <p key={warning} className="comparison-warning">
          {warning}
        </p>
      ))}
      <table className="data-table">
        <thead>
          <tr>
            <th>{t("comparison.metric")}</th>
            {comparison.jobs.map((job) => (
              <th key={job.id}>
                {job.model}
                <div className="resource-meta">
                  {job.dataset} · {job.mode}
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {comparison.rows.map((row) => (
            <MetricRow key={row.key} row={row} jobs={comparison.jobs} />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function MetricRow({ row, jobs }: { row: ComparisonRow; jobs: ComparedJob[] }) {
  const values = jobs.map((job) => row.values[job.id] ?? null);
  // A bar invites comparison, so draw one only when every job actually has a number.
  const comparable = values.every((value) => typeof value === "number");
  const largest = comparable ? Math.max(...(values as number[]).map(Math.abs), 0) : 0;

  return (
    <tr>
      <th scope="row">
        {row.key}
        {row.unit !== null && <span className="resource-meta"> {row.unit}</span>}
      </th>
      {jobs.map((job, index) => {
        const value = values[index];
        return (
          <td key={job.id}>
            {value === null ? (
              ""
            ) : (
              <>
                <span className="mono">{value}</span>
                {comparable && largest > 0 && (
                  <span
                    className="metric-bar"
                    style={{ width: `${(Math.abs(value) / largest) * 100}%` }}
                  />
                )}
              </>
            )}
          </td>
        );
      })}
    </tr>
  );
}
