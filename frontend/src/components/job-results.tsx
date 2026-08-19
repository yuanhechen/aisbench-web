import { useI18n } from "../i18n/i18n-context";
import type { MessageKey } from "../i18n/messages";

export interface Artifact {
  id: string;
  kind: string;
  relative_path: string;
  content_type: string;
}

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

export function JobArtifacts({ jobId, artifacts }: { jobId: string; artifacts: Artifact[] }) {
  const { t } = useI18n();
  const found = artifacts;
  const visualizations = found.filter((artifact) => artifact.kind === "visualization");

  return (
    <>
      {visualizations.map((artifact) => (
        <section className="rail-section" key={artifact.id}>
          <h2 className="eyebrow">{t("results.visualization")}</h2>
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
        <section className="rail-section">
          <h2 className="eyebrow">{t("results.artifacts")}</h2>
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
