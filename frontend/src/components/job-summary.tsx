import { useEffect, useState } from "react";

import { api } from "../api/client";
import { useI18n } from "../i18n/i18n-context";
import type { Artifact } from "./job-results";

// AISBench writes the summary three ways. The .txt concatenates all three with banner rules
// between them, and the .md needs rendering; the .csv is the table on its own.
const SUMMARY_SOURCE = ".csv";

function summaryArtifact(artifacts: Artifact[]): Artifact | null {
  return (
    artifacts.find(
      (artifact) =>
        artifact.kind === "summary" && artifact.relative_path.endsWith(SUMMARY_SOURCE),
    ) ?? null
  );
}

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

/** A cell is right-aligned when it is a number, which is what makes a column scannable. */
function isNumeric(value: string): boolean {
  return value.trim() !== "" && !Number.isNaN(Number(value));
}

/**
 * The summary table AISBench itself produced.
 *
 * The headline number answers "how did it do". This answers "on what, at which version, by
 * which metric, in which mode" — the things a result is meaningless without, and which
 * otherwise cost a file download to find out.
 */
export function JobSummary({ jobId, artifacts }: { jobId: string; artifacts: Artifact[] }) {
  const { t } = useI18n();
  const [rows, setRows] = useState<string[][]>([]);
  const artifact = summaryArtifact(artifacts);
  const id = artifact?.id;

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

  if (rows.length < 2) {
    return null;
  }
  const [header, ...body] = rows;
  return (
    <section className="card">
      <h2 className="card-title">{t("results.summary")}</h2>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {header.map((cell, column) => (
                <th
                  key={cell}
                  scope="col"
                  className={body.every((row) => isNumeric(row[column] ?? "")) ? "align-right" : ""}
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
    </section>
  );
}
