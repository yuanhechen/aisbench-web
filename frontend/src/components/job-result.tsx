import { useEffect, useState } from "react";

import { api } from "../api/client";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import type { Artifact } from "./job-results";

interface Metric {
  key: string;
  value: number | null;
  text_value: string | null;
  unit: string | null;
}

const EXTRA_PREFIX = "extra.";
// AISBench writes the summary three ways. The .txt concatenates all three with banner rules
// between them, and the .md needs rendering; the .csv is the table on its own.
const SUMMARY_SOURCE = ".csv";
// The column a summary row is about is the score; the rest say what the score is of.
const SCORE_COLUMNS = 1;

/** Split one CSV line, honouring the quoting the format allows even where AISBench omits it. */
export function splitRow(line: string): string[] {
  const cells: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const character = line[i];
    if (quoted) {
      if (character === '"' && line[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (character === '"') {
        quoted = false;
      } else {
        cell += character;
      }
    } else if (character === '"') {
      quoted = true;
    } else if (character === ",") {
      cells.push(cell);
      cell = "";
    } else {
      cell += character;
    }
  }
  cells.push(cell);
  return cells;
}

function parseCsv(text: string): string[][] {
  return text
    .split(/\r?\n/)
    .filter((line) => line.trim() !== "")
    .map(splitRow);
}

function isNumeric(value: string): boolean {
  return value.trim() !== "" && !Number.isNaN(Number(value));
}

/** `ARC-c.accuracy` under a heading that already says ARC_c is just `accuracy`. */
function metricName(key: string, datasetName: string): string {
  const prefix = `${datasetName.toLowerCase()}.`;
  return key.toLowerCase().startsWith(prefix) ? key.slice(prefix.length) : key;
}

/**
 * The summary columns that are not already on the page.
 *
 * The heading names the dataset and the number is labelled with its metric, so a context
 * line that opens "dataset gsm8k · metric accuracy" spends its first half saying what the
 * reader just read.
 */
function contextOf(
  header: string[],
  row: string[],
  datasetName: string,
  metrics: Metric[],
): string[] {
  const said = new Set(
    [datasetName, ...metrics.map((metric) => metricName(metric.key, datasetName))].map((value) =>
      value.toLowerCase(),
    ),
  );
  const shown: string[] = [];
  for (let column = 0; column < header.length - SCORE_COLUMNS; column++) {
    const value = row[column] ?? "";
    if (value === "" || said.has(value.toLowerCase())) {
      continue;
    }
    shown.push(`${header[column]} ${value}`);
  }
  return shown;
}

function summaryArtifact(artifacts: Artifact[]): Artifact | null {
  return (
    artifacts.find(
      (artifact) =>
        artifact.kind === "summary" && artifact.relative_path.endsWith(SUMMARY_SOURCE),
    ) ?? null
  );
}

function useSummary(jobId: string, artifacts: Artifact[]): string[][] {
  const [rows, setRows] = useState<string[][]>([]);
  const id = summaryArtifact(artifacts)?.id;

  useEffect(() => {
    if (id === undefined) {
      return;
    }
    let cancelled = false;
    void api
      .text(`/api/jobs/${jobId}/artifacts/${id}`)
      .then((body) => {
        if (!cancelled) {
          setRows(parseCsv(body));
        }
      })
      // A summary that will not load is not worth an error banner: the headline number and
      // the file list both still stand.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [jobId, id]);

  return rows;
}

/**
 * What the run produced, read as one thing.
 *
 * The number is the answer; the summary AISBench wrote says what the number is of. A single
 * evaluated dataset says that in a line, so it is a line — a two-row table with one row of
 * data is a table drawn for its own sake. Several datasets need the columns, and get them.
 */
export function JobResult({
  jobId,
  artifacts,
  datasetName,
}: {
  jobId: string;
  artifacts: Artifact[];
  datasetName: string;
}) {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const metrics = useApiQuery<Metric[]>(`/api/jobs/${jobId}/metrics`, {
    onFailure: reportFailure,
  });
  const [showExtra, setShowExtra] = useState(false);
  const rows = useSummary(jobId, artifacts);

  const all = metrics.data ?? [];
  const primary = all.filter((metric) => !metric.key.startsWith(EXTRA_PREFIX));
  const extra = all.filter((metric) => metric.key.startsWith(EXTRA_PREFIX));
  const [header, ...body] = rows.length > 1 ? rows : [[], []];

  if (primary.length === 0 && body.length === 0) {
    return null;
  }

  return (
    <section className="result">
      <div className={primary.length === 1 ? "metric-hero" : "metric-grid"}>
        {primary.map((metric) => (
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

      {body.length === 1 && (
        <p className="result-context">
          {contextOf(header, body[0], datasetName, primary).join(" · ")}
        </p>
      )}

      {body.length > 1 && (
        <div className="table-scroll result-table">
          <table className="data-table">
            <thead>
              <tr>
                {header.map((cell, column) => (
                  <th
                    key={cell}
                    scope="col"
                    className={
                      body.every((row) => isNumeric(row[column] ?? "")) ? "align-right" : ""
                    }
                  >
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row) => (
                <tr key={row.join(",")}>
                  {row.map((cell, column) => (
                    <td key={column} className={isNumeric(cell) ? "align-right mono" : ""}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {extra.length > 0 && (
        <div className="result-extra">
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
                    <td className="mono align-right">
                      {metric.value !== null ? metric.value : (metric.text_value ?? "")}
                      {metric.unit !== null && ` ${metric.unit}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}
