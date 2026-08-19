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

export interface Artifact {
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

/** In a narrow column the file name is what identifies it; the path is a tooltip. */
function baseName(artifact: Artifact): string {
  const name = shortPath(artifact).split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot <= 0 ? name : name.slice(0, dot);
}

/** The path minus the file name, or nothing when the file sits at the top. */
function directoryOf(artifact: Artifact): string | undefined {
  const path = shortPath(artifact);
  const slash = path.lastIndexOf("/");
  return slash < 0 ? undefined : path.slice(0, slash);
}

function extensionOf(artifact: Artifact): string {
  const name = shortPath(artifact).split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot <= 0 ? "" : name.slice(dot + 1);
}

/** `ARC-c.accuracy` under a heading that already says ARC_c is just `accuracy`. */
function metricName(key: string, datasetName: string): string {
  const prefix = `${datasetName.toLowerCase()}.`;
  return key.toLowerCase().startsWith(prefix) ? key.slice(prefix.length) : key;
}

/**
 * The numbers the job was run to produce.
 *
 * This is the answer the page exists to give. One number gets the whole stage; a
 * performance run reports a dozen, and twelve hero numbers are no hierarchy at all, so
 * those become a grid that can be scanned instead.
 */
export function MetricHeadline({
  metrics,
  datasetName,
}: {
  metrics: Metric[];
  datasetName: string;
}) {
  const single = metrics.length === 1;
  return (
    <div className={single ? "metric-hero" : "metric-grid"}>
      {metrics.map((metric) => (
        <div className="metric" key={metric.key}>
          <p className="metric-name" title={metric.key}>
            {metricName(metric.key, datasetName)}
          </p>
          <p className="metric-value">
            {metric.value !== null ? metric.value : (metric.text_value ?? "")}
            {metric.unit !== null && <span className="metric-unit">{metric.unit}</span>}
          </p>
        </div>
      ))}
    </div>
  );
}

export function JobMetrics({ jobId, datasetName }: { jobId: string; datasetName: string }) {
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
        <section className="card card-result">
          <MetricHeadline metrics={primary} datasetName={datasetName} />
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

export function JobArtifacts({ jobId, artifacts }: { jobId: string; artifacts: Artifact[] }) {
  const { t } = useI18n();
  const found = artifacts;
  const visualizations = found.filter((artifact) => artifact.kind === "visualization");

  return (
    <>
      {visualizations.map((artifact) => (
        <section className="card" key={artifact.id}>
          <h2 className="card-title">{t("results.visualization")}</h2>
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
        <section className="card">
          <h2 className="card-title">
            {t("results.artifacts")} <span className="card-count">{found.length}</span>
          </h2>
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
                        {/* The tooltip carries the directory; repeating the file name
                            the link already shows would say nothing. */}
                        <a
                          href={`/api/jobs/${jobId}/artifacts/${artifact.id}`}
                          title={directoryOf(artifact)}
                        >
                          <span className="artifact-name">{baseName(artifact)}</span>
                          <span className="artifact-ext">{extensionOf(artifact)}</span>
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </>
  );
}
