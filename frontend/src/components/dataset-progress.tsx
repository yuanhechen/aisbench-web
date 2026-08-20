import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

import { api } from "../api/client";
import type { DatasetMetricValue, DatasetProgress, Job } from "../api/types";
import { LogView } from "../components/log-view";
import { useI18n } from "../i18n/i18n-context";

// Matches the page's own polling: the socket nudges, this rhythm guarantees freshness.
const LOG_POLL_MS = 2000;

interface LogChunk {
  offset: number;
  text: string;
}

interface SampleRecord {
  id: string;
  prompt: string | null;
  origin_prediction: string | null;
  prediction: string | null;
  reference: string | null;
  correct: boolean | null;
}

interface SamplesPage {
  source: string;
  total: number;
  samples: SampleRecord[];
}

/** One number, with no more precision than a benchmark ever means to report. */
function printNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, "");
}

/** "37/100" when both are known, "37" when only the done side is, nothing otherwise. */
function printCounts(completed: number | null, total: number | null): string {
  if (completed !== null && total !== null) {
    return `${completed}/${total}`;
  }
  return completed !== null ? String(completed) : "—";
}

function elapsedOf(started: string | null, now: number): string | null {
  if (started === null) {
    return null;
  }
  const from = Date.parse(started);
  if (Number.isNaN(from)) {
    return null;
  }
  const seconds = Math.max(0, Math.round((now - from) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

/**
 * One dataset per line of a table, one sheet of detail beneath: the overview stays a
 * table the eye can scan, and the depth — counters, samples, the task's own log — sits
 * behind tabs instead of stretching the page with every row opened at once.
 */
export function DatasetRows({ job, jobId }: { job: Job; jobId: string }) {
  const { t } = useI18n();
  const live = job.status !== "succeeded" && job.status !== "failed" && job.status !== "interrupted";
  const [selected, setSelected] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const aggregate = aggregateProgress(job.datasets);
  const settled = job.datasets.filter(
    (row) => row.phase === "finished" || row.phase === "failed",
  ).length;

  const INFER_PHASES = ["loading", "inferring", "writing_cache"];
  const inferActive = job.datasets.some((row) => INFER_PHASES.includes(row.phase));
  const allQueued = job.datasets.every((row) => row.phase === "queued");
  const evalActive =
    live && !inferActive && job.datasets.some((row) => row.phase === "evaluating");
  /* The samples bar means inference. Once inference is done the run still has work
     without a denominator — saying "100%" through the whole eval stage reads as done. */
  const barState: "none" | "fill" | "flowing" = !live
    ? "none"
    : allQueued || inferActive
      ? "fill"
      : "flowing";

  function liveSummary(): string {
    if (allQueued) {
      return t("jobDetail.datasetsDone", {
        done: String(settled),
        total: String(job.datasets.length),
      });
    }
    if (inferActive) {
      const parts = [
        t("jobDetail.datasetsDone", {
          done: String(settled),
          total: String(job.datasets.length),
        }),
      ];
      if (aggregate !== null && aggregate.total > 0) {
        parts.push(
          t("jobDetail.samplesDone", {
            completed: String(aggregate.completed),
            total: String(aggregate.total),
            percent: String(Math.round((aggregate.completed / aggregate.total) * 100)),
          }),
        );
      }
      return parts.join(" · ");
    }
    // Inference counts are the fact; the stage says what is happening now.
    const counted =
      aggregate !== null && aggregate.total > 0
        ? t("jobDetail.inferenceDone", {
            completed: String(aggregate.completed),
            total: String(aggregate.total),
          })
        : t("jobDetail.datasetsDone", {
            done: String(settled),
            total: String(job.datasets.length),
          });
    return evalActive
      ? `${counted} · ${t("jobDetail.evaluatingStage", {
          done: String(settled),
          total: String(job.datasets.length),
        })}`
      : `${counted} · ${t("jobDetail.summarizingStage")}`;
  }

  // A second hand: whatever stage the run is in — even a silent one, like the tens of
  // seconds AISBench's interpreter takes to boot — the page is visibly alive.
  useEffect(() => {
    if (!live) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);

  // One clock for the page: the run's own start, not each task's. Two timers that
  // disagree about when the run began read as a bug, not as detail.
  const elapsedText =
    live && job.started_at !== null
      ? ` · ${t("jobDetail.elapsed")} ${elapsedOf(job.started_at, now) ?? ""}`
      : "";

  // A finished run's page is its results: the first sheet is open on arrival — but a
  // collapse the reader chose is theirs, and no later poll undoes it.
  const arrivedLive = useRef(live);
  useEffect(() => {
    if (arrivedLive.current && !live && job.datasets.length > 0) {
      setSelected((current) => current ?? job.datasets[0].name);
    }
    arrivedLive.current = live;
  }, [live, job.datasets]);
  useEffect(() => {
    if (!live && !arrivedLive.current && selected === null && job.datasets.length > 0) {
      setSelected(job.datasets[0].name);
    }
    // Open once, on arrival; the reader's later choice stands.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedRow = job.datasets.find((row) => row.name === selected) ?? null;

  return (
    <section className="dataset-progress" aria-label={t("jobDetail.datasets")}>
      {!live ? (
        /* The page a finished run opens on is its results; the numbers lead. */
        <div className="ds-scoreboard">
          <p className="eyebrow">{t("jobDetail.results")}</p>
          <div className="ds-scoreboard-grid">
            {job.datasets.map((row) => {
              const headline = headlineMetric(row.metrics);
              return (
                <div key={row.name} className="ds-hero">
                  <span className="ds-hero-name" title={row.name}>
                    {row.name}
                  </span>
                  {headline === null ? (
                    <span className={`ds-hero-phase ds-phase-${row.phase}`}>
                      {t(`jobDetail.phase_${row.phase}`)}
                    </span>
                  ) : (
                    <span className="ds-hero-value mono">
                      {headline.value !== null
                        ? printNumber(headline.value)
                        : (headline.text_value ?? "—")}
                      {headline.unit !== null && headline.unit !== "" && (
                        <span className="ds-metric-unit"> {headline.unit}</span>
                      )}
                    </span>
                  )}
                  <span className="ds-hero-meta">
                    {headline !== null && `${headlineName(row.metrics)} · `}
                    {row.correct_count !== null && row.total_count !== null
                      ? `${row.correct_count}/${row.total_count}`
                      : printCounts(row.completed, row.total)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <>
          <div className="ds-summary">
            <span className="eyebrow">{t("jobDetail.datasets")}</span>
            <span className="ds-summary-counts">
              {liveSummary()}
              {elapsedText}
            </span>
          </div>
          {barState === "fill" && aggregate !== null && aggregate.total > 0 && (
            <div className="progress-track ds-aggregate" aria-hidden="true">
              <div
                className="progress-fill"
                style={{
                  width: `${Math.min(100, (aggregate.completed / aggregate.total) * 100)}%`,
                }}
              />
            </div>
          )}
          {barState === "flowing" && (
            /* Inference is through; the work that remains has no count of its own, so
               the bar says "moving" instead of pretending a percentage. */
            <div className="progress-track ds-aggregate is-indeterminate" aria-hidden="true">
              <div className="progress-fill" />
            </div>
          )}
        </>
      )}

      <table className="ds-table">
        <thead>
          <tr>
            <th scope="col">{t("jobDetail.datasets")}</th>
            <th scope="col">{t("jobDetail.phase")}</th>
            <th scope="col">{live ? t("jobDetail.progress") : t("jobDetail.results")}</th>
            <th scope="col" className="ds-col-actions" />
          </tr>
        </thead>
        <tbody>
          {job.datasets.map((row) => {
            const fraction =
              row.total !== null && row.total > 0
                ? Math.min(100, ((row.completed ?? 0) / row.total) * 100)
                : null;
            return (
              <tr key={row.name} className={selected === row.name ? "is-selected" : undefined}>
                <td className="mono ds-name" title={row.name}>
                  {row.name}
                </td>
                <td>
                  <span className={`ds-phase ds-phase-${row.phase}`}>
                    {t(`jobDetail.phase_${row.phase}`)}
                  </span>
                </td>
                <td>
                  {live ? (
                    <span className="ds-progress-cell">
                      <span className="mono ds-counts">{printCounts(row.completed, row.total)}</span>
                      <span className="ds-bar" aria-hidden="true">
                        {fraction !== null && (
                          <span className="ds-bar-fill" style={{ width: `${fraction}%` }} />
                        )}
                      </span>
                    </span>
                  ) : (
                    <span className="ds-score mono">
                      {Object.entries(row.metrics)
                        .slice(0, 3)
                        .map(([name, metric], index) => (
                          <span key={name} className="ds-score-item">
                            {index > 0 && <span className="ds-score-sep"> · </span>}
                            <span className="ds-score-name">{name}</span>{" "}
                            {metric.value !== null
                              ? printNumber(metric.value)
                              : (metric.text_value ?? "—")}
                            {metric.unit !== null && metric.unit !== "" && (
                              <span className="ds-metric-unit"> {metric.unit}</span>
                            )}
                          </span>
                        ))}
                      {Object.keys(row.metrics).length === 0 && "—"}
                      {row.correct_count !== null && row.total_count !== null && (
                        <span className="ds-score-counts">
                          {" "}
                          ({row.correct_count}/{row.total_count})
                        </span>
                      )}
                    </span>
                  )}
                </td>
                <td className="ds-col-actions">
                  <button
                    type="button"
                    className="link-button"
                    aria-expanded={selected === row.name}
                    onClick={() => setSelected(selected === row.name ? null : row.name)}
                  >
                    {selected === row.name
                      ? t("jobDetail.hideDetail")
                      : t("jobDetail.viewDetail")}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {selectedRow !== null && (
        <div className="ds-sheets">
          <div className="sheet-tabs" role="tablist" aria-label={t("jobDetail.datasets")}>
            {job.datasets.map((row) => (
              <button
                type="button"
                key={row.name}
                role="tab"
                aria-selected={row.name === selected}
                className={`sheet-tab mono${row.name === selected ? " is-active" : ""}`}
                onClick={() => setSelected(row.name)}
              >
                {row.name}
              </button>
            ))}
          </div>
          <DatasetDetailPanel row={selectedRow} jobId={jobId} live={live} />
        </div>
      )}
    </section>
  );
}

/** The sheet of one dataset: its counters, its scores, its samples, its own log. */
function DatasetDetailPanel({
  row,
  jobId,
  live,
}: {
  row: DatasetProgress;
  jobId: string;
  live: boolean;
}) {
  const { t } = useI18n();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!live) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);

  return (
    <div className="ds-detail">
      <p className="ds-detail-line">
        {t("jobDetail.phase")} {t(`jobDetail.phase_${row.phase}`)}
        {row.rate !== null && ` · ${row.rate}`}
        {row.started_at !== null &&
          ` · ${t("jobDetail.elapsed")} ${elapsedOf(row.started_at, now) ?? "—"}`}
      </p>
      {row.counters !== null && (
        <p className="ds-detail-line ds-counters mono">
          {Object.entries(row.counters).map(([name, value]) => (
            <span key={name}>
              {name} <span className="ds-counter-value">{value}</span>
            </span>
          ))}
        </p>
      )}
      {!live && Object.keys(row.metrics).length > 0 && (
        <dl className="ds-metrics">
          {Object.entries(row.metrics).map(([name, metric]) => (
            <div key={name} className="ds-metric">
              <dt className="eyebrow">{name}</dt>
              <dd className="mono">
                {metric.value !== null ? printNumber(metric.value) : (metric.text_value ?? "—")}
                {metric.unit !== null && metric.unit !== "" && (
                  <span className="ds-metric-unit"> {metric.unit}</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {!live && (
        <DatasetSamples
          jobId={jobId}
          dataset={row.name}
          correctCount={row.correct_count}
          totalCount={row.total_count}
        />
      )}
      <DatasetLogTail jobId={jobId} dataset={row.name} live={live} />
    </div>
  );
}

/** Sum the counts of the rows that know their totals; a row without one does not dilute. */
function aggregateProgress(
  rows: DatasetProgress[],
): { completed: number; total: number } | null {
  let completed = 0;
  let total = 0;
  let known = false;
  for (const row of rows) {
    if (row.total !== null && row.total > 0) {
      known = true;
      total += row.total;
      completed += row.completed ?? 0;
    }
  }
  return known ? { completed, total } : null;
}

/** The metric a row leads with: the first non-prefixed one, or just the first. */
function headlineMetric(metrics: Record<string, DatasetMetricValue>): DatasetMetricValue | null {
  const names = Object.keys(metrics);
  if (names.length === 0) {
    return null;
  }
  const plain = names.find((name) => !name.startsWith("extra."));
  return metrics[plain ?? names[0]];
}

/** The name that goes under the big number: the metric the headline came from. */
function headlineName(metrics: Record<string, DatasetMetricValue>): string {
  const names = Object.keys(metrics);
  const plain = names.find((name) => !name.startsWith("extra."));
  return plain ?? names[0] ?? "";
}

/**
 * The per-sample records of a finished dataset: what the model was asked, what it
 * answered, and whether that was right. One line per sample; a line opens to the whole
 * record — prompt, raw output, and both answers in full.
 */
function DatasetSamples({
  jobId,
  dataset,
  correctCount,
  totalCount,
}: {
  jobId: string;
  dataset: string;
  correctCount: number | null;
  totalCount: number | null;
}) {
  const { t } = useI18n();
  const [state, setState] = useState<{
    source: string;
    total: number;
    samples: SampleRecord[];
  } | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [onlyWrong, setOnlyWrong] = useState(false);
  // Column proportions of the four records, shared by every open sample of this dataset:
  // adjust once, read all.
  const [ratios, setRatios] = useState<number[]>([1.5, 1, 1, 1]);

  const load = useCallback(
    async (offset: number) => {
      try {
        const page = await api.get<SamplesPage>(
          `/api/jobs/${jobId}/datasets/${encodeURIComponent(dataset)}/samples` +
            `?offset=${offset}&limit=50`,
        );
        setState((current) =>
          current === null || offset === 0
            ? { source: page.source, total: page.total, samples: page.samples }
            : { ...current, samples: [...current.samples, ...page.samples] },
        );
      } catch {
        setState({ source: "none", total: 0, samples: [] });
      }
    },
    [jobId, dataset],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  if (state === null) {
    return <p className="ds-detail-line">{t("common.loading")}</p>;
  }
  if (state.total === 0) {
    return <p className="ds-detail-line">{t("jobDetail.noSamples")}</p>;
  }
  return (
    <section className="ds-samples" aria-label={t("jobDetail.samples")}>
      <p className="ds-samples-head">
        <span className="eyebrow">{t("jobDetail.samples")}</span>
        <span className="ds-samples-tools">
          {correctCount !== null && totalCount !== null && (
            <span className="ds-summary-counts">
              ✓ {correctCount} · ✗ {totalCount - correctCount}
            </span>
          )}
          <span className="ds-summary-counts">
            {t("jobDetail.sampleCount", { count: String(state.total) })}
          </span>
          {state.source !== "predictions" && (
            <button
              type="button"
              className={onlyWrong ? "chip chip-active" : "chip"}
              aria-pressed={onlyWrong}
              onClick={() => {
                setOnlyWrong((current) => !current);
                setOpenId(null);
              }}
            >
              {t("jobDetail.onlyWrong", {
                count: String(
                  correctCount !== null && totalCount !== null
                    ? totalCount - correctCount
                    : state.samples.filter((sample) => sample.correct === false).length,
                ),
              })}
            </button>
          )}
        </span>
      </p>
      {state.source === "predictions" && (
        <p className="ds-detail-line">{t("jobDetail.samplesFromPredictions")}</p>
      )}
      <table className="ds-sample-table">
        <thead>
          <tr>
            <th scope="col" className="ds-col-id">#</th>
            <th scope="col" className="ds-col-verdict">{t("jobDetail.sampleResult")}</th>
            <th scope="col">{t("jobDetail.sampleAnswer")}</th>
            <th scope="col" className="ds-col-ref">{t("jobDetail.sampleReference")}</th>
          </tr>
        </thead>
        <tbody>
          {state.samples
            .filter((sample) => !onlyWrong || sample.correct === false)
            .map((sample) => (
              <Fragment key={sample.id}>
                <tr
                  className={openId === sample.id ? "is-open" : undefined}
                  onClick={() => setOpenId(openId === sample.id ? null : sample.id)}
                >
                  <td className="mono">{sample.id}</td>
                  <td>
                    {sample.correct === null ? (
                      "—"
                    ) : sample.correct ? (
                      <span
                        className="ds-verdict ds-verdict-correct"
                        title={t("jobDetail.sampleCorrect")}
                      >
                        ✓
                      </span>
                    ) : (
                      <span
                        className="ds-verdict ds-verdict-wrong"
                        title={t("jobDetail.sampleWrong")}
                      >
                        ✗
                      </span>
                    )}
                  </td>
                  <td className="ds-sample-clamp" title={sample.prediction ?? ""}>
                    {sample.prediction ?? "—"}
                  </td>
                  <td className="ds-sample-clamp" title={sample.reference ?? ""}>
                    {sample.reference ?? "—"}
                  </td>
                </tr>
                {openId === sample.id && (
                  <tr className="ds-sample-detail-row">
                    <td colSpan={4}>
                      <SampleBlocks sample={sample} ratios={ratios} onRatios={setRatios} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
        </tbody>
      </table>
      {state.samples.length < state.total && (
        <button
          type="button"
          className="link-button"
          onClick={() => void load(state.samples.length)}
        >
          {t("jobDetail.loadMore")}
        </button>
      )}
    </section>
  );
}

/**
 * One sample's records side by side, with draggable seams between them.
 *
 * A long prompt wants more room than a short answer; the seams let the reader decide,
 * and the proportions apply to every open sample of the dataset.
 */
function SampleBlocks({
  sample,
  ratios,
  onRatios,
}: {
  sample: SampleRecord;
  ratios: number[];
  onRatios: (next: number[]) => void;
}) {
  const { t } = useI18n();
  const gridRef = useRef<HTMLDivElement>(null);
  const blocks = [
    { label: t("jobDetail.sampleInput"), text: sample.prompt },
    { label: t("jobDetail.sampleRaw"), text: sample.origin_prediction },
    { label: t("jobDetail.sampleAnswer"), text: sample.prediction },
    { label: t("jobDetail.sampleReference"), text: sample.reference },
  ].filter((block) => block.text !== null);

  function startResize(index: number, event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const grid = gridRef.current;
    if (grid === null) {
      return;
    }
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = grid.getBoundingClientRect().width;
    const start = [...ratios];
    const MIN = 0.3;

    const move = (moveEvent: PointerEvent) => {
      const delta =
        ((moveEvent.clientX - startX) / Math.max(1, startWidth)) *
        start.reduce((a, b) => a + b, 0);
      let left = start[index] + delta;
      let right = start[index + 1] - delta;
      if (left < MIN) {
        right -= MIN - left;
        left = MIN;
      }
      if (right < MIN) {
        left -= MIN - right;
        right = MIN;
      }
      if (left >= MIN && right >= MIN) {
        onRatios(
          start.map((value, position) =>
            position === index ? left : position === index + 1 ? right : value,
          ),
        );
      }
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  }

  return (
    <div
      className="ds-sample-grid"
      ref={gridRef}
      style={{ gridTemplateColumns: blocks.map((_, index) => `${ratios[index]}fr`).join(" ") }}
    >
      {blocks.map((block, index) => (
        <div key={block.label} className="ds-sample-block-wrap">
          <div className="ds-sample-block">
            <span className="eyebrow">{block.label}</span>
            <pre>{block.text}</pre>
          </div>
          {index < blocks.length - 1 && (
            <div
              className="col-seam"
              role="separator"
              aria-orientation="vertical"
              onPointerDown={(event) => startResize(index, event)}
            />
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * The tail of this dataset's own task log, fetched by byte offset like the page's log.
 *
 * Polling stops with the run; what arrived stays readable.
 */
function DatasetLogTail({
  jobId,
  dataset,
  live,
}: {
  jobId: string;
  dataset: string;
  live: boolean;
}) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const offsetRef = useRef(0);
  const pinnedRef = useRef(true);
  const logRef = useRef<HTMLPreElement>(null);

  const pull = useCallback(async () => {
    try {
      const chunk = await api.get<LogChunk>(
        `/api/jobs/${jobId}/datasets/${encodeURIComponent(dataset)}/logs?offset=${offsetRef.current}`,
      );
      if (chunk.text !== "") {
        const view = logRef.current;
        pinnedRef.current =
          view === null || view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
        offsetRef.current = chunk.offset;
        setText((current) => current + chunk.text);
      }
    } catch {
      // A dataset log is a courtesy, not a contract; the page log remains the record.
    }
  }, [jobId, dataset]);

  useEffect(() => {
    void pull();
    if (!live) {
      return;
    }
    const timer = setInterval(() => void pull(), LOG_POLL_MS);
    return () => clearInterval(timer);
  }, [pull, live]);

  useEffect(() => {
    if (pinnedRef.current && logRef.current !== null) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [text]);

  return (
    <details className="card-log ds-log" open={live}>
      <summary>{t("jobDetail.datasetLog")}</summary>
      <LogView ref={logRef} text={text} empty={t("jobDetail.noDatasetLog")} />
    </details>
  );
}
