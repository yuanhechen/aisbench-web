import { useState } from "react";

import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import type { MessageKey } from "../i18n/messages";

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
// AISBench names the run directory after its start time, so every path repeats it.
const RUN_DIRECTORY = /^\d{8}_\d{6}\//;

// Most-wanted first: the summary is what a person opens, the config is what they check last.
const KIND_ORDER: Array<[string, MessageKey]> = [
  ["summary", "results.kindSummary"],
  ["result", "results.kindResult"],
  ["performance", "results.kindPerformance"],
  ["prediction", "results.kindPrediction"],
  ["log", "results.kindLog"],
  ["config", "results.kindConfig"],
  ["visualization", "results.kindVisualization"],
  ["other", "results.kindOther"],
];

function display(metric: Metric): string {
  const shown = metric.value !== null ? String(metric.value) : (metric.text_value ?? "");
  return metric.unit === null ? shown : `${shown} ${metric.unit}`;
}

/** The path without the run directory every artifact of a run shares. */
function shortPath(artifact: Artifact): string {
  return artifact.relative_path.replace(RUN_DIRECTORY, "");
}

/**
 * The numbers the job was run to produce.
 *
 * This is the answer the page exists to give, so it is the one thing set in a size you can
 * read from across the desk. Everything else on the page is how it was obtained.
 */
export function MetricHeadline({ metrics }: { metrics: Metric[] }) {
  return (
    <div className="metric-headline">
      {metrics.map((metric) => (
        <div key={metric.key}>
          <p className="metric-name">{metric.key}</p>
          <p className="metric-value">{display(metric)}</p>
        </div>
      ))}
    </div>
  );
}

export function JobMetrics({ jobId }: { jobId: string }) {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const metrics = useApiQuery<Metric[]>(`/api/jobs/${jobId}/metrics`, {
    onFailure: reportFailure,
  });
  const [showExtra, setShowExtra] = useState(false);

  const all = metrics.data ?? [];
  const primary = all.filter((metric) => !metric.key.startsWith(EXTRA_PREFIX));
  const extra = all.filter((metric) => metric.key.startsWith(EXTRA_PREFIX));

  return (
    <>
      {primary.length > 0 && (
        <section className="task-block">
          <MetricHeadline metrics={primary} />
          {extra.length > 0 && (
            <>
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
            </>
          )}
        </section>
      )}
    </>
  );
}

export function JobArtifacts({ jobId }: { jobId: string }) {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const artifacts = useApiQuery<Artifact[]>(`/api/jobs/${jobId}/artifacts`, {
    onFailure: reportFailure,
  });

  const found = artifacts.data ?? [];
  const visualizations = found.filter((artifact) => artifact.kind === "visualization");

  return (
    <>
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

      {found.length > 0 && (
        <details className="task-block task-fold">
          <summary>
            {t("results.artifacts")} <span className="fold-count">{found.length}</span>
          </summary>
          <div className="artifact-groups">
            {KIND_ORDER.map(([kind, label]) => {
              const group = found.filter((artifact) => artifact.kind === kind);
              if (group.length === 0) {
                return null;
              }
              return (
                <div key={kind}>
                  <p className="artifact-kind">{t(label)}</p>
                  <ul className="artifact-list">
                    {group.map((artifact) => (
                      <li key={artifact.id}>
                        {/* Addressed by ID: the stored path is never accepted from the browser. */}
                        <a href={`/api/jobs/${jobId}/artifacts/${artifact.id}`}>
                          {shortPath(artifact)}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        </details>
      )}
    </>
  );
}
