import { useState } from "react";

import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";

interface Metric {
  key: string;
  value: number | null;
  text_value: string | null;
  unit: string | null;
}

interface Artifact {
  id: string;
  kind: string;
  relative_path: string;
  content_type: string;
}

const EXTRA_PREFIX = "extra.";

function display(metric: Metric): string {
  const shown = metric.value !== null ? String(metric.value) : (metric.text_value ?? "");
  return metric.unit === null ? shown : `${shown} ${metric.unit}`;
}

export function JobResults({ jobId }: { jobId: string }) {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const metrics = useApiQuery<Metric[]>(`/api/jobs/${jobId}/metrics`, {
    onFailure: reportFailure,
  });
  const artifacts = useApiQuery<Artifact[]>(`/api/jobs/${jobId}/artifacts`, {
    onFailure: reportFailure,
  });
  const [showExtra, setShowExtra] = useState(false);

  const all = metrics.data ?? [];
  const primary = all.filter((metric) => !metric.key.startsWith(EXTRA_PREFIX));
  const extra = all.filter((metric) => metric.key.startsWith(EXTRA_PREFIX));
  const visualizations = (artifacts.data ?? []).filter(
    (artifact) => artifact.kind === "visualization",
  );

  return (
    <>
      {primary.length > 0 && (
        <section className="task-block">
          <h2 className="form-step-title">{t("results.summary")}</h2>
          <dl className="config-row">
            {primary.map((metric) => (
              <div key={metric.key}>
                <dt>{metric.key}</dt>
                <dd className="mono">{display(metric)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {extra.length > 0 && (
        <section className="task-block">
          <button
            type="button"
            className="link-button"
            onClick={() => setShowExtra((current) => !current)}
          >
            {showExtra ? t("results.hideExtra") : t("results.showExtra")}
          </button>
          {showExtra && (
            <table className="data-table">
              <tbody>
                {extra.map((metric) => (
                  <tr key={metric.key}>
                    <th scope="row">{metric.key.slice(EXTRA_PREFIX.length)}</th>
                    <td className="mono">{display(metric)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {(artifacts.data ?? []).length > 0 && (
        <section className="task-block">
          <h2 className="form-step-title">{t("results.artifacts")}</h2>
          <ul className="artifact-list">
            {(artifacts.data ?? []).map((artifact) => (
              <li key={artifact.id}>
                {/* Addressed by ID: the stored path is never accepted from the browser. */}
                <a href={`/api/jobs/${jobId}/artifacts/${artifact.id}`}>
                  {artifact.relative_path}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

      {visualizations.map((artifact) => (
        <section className="task-block" key={artifact.id}>
          <h2 className="form-step-title">{t("results.visualization")}</h2>
          {/*
            The artifact endpoint authorizes the owner before serving. allow-scripts lets the
            Plotly bundle run; allow-same-origin is deliberately omitted so the frame cannot
            reach this origin's cookies or DOM.
          */}
          <iframe
            className="visualization-frame"
            title={artifact.relative_path}
            src={`/api/jobs/${jobId}/artifacts/${artifact.id}`}
            sandbox="allow-scripts"
          />
        </section>
      ))}
    </>
  );
}
