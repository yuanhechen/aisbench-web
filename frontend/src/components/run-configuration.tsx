import type { ReactNode } from "react";

import type { Job } from "../api/types";
import { useI18n } from "../i18n/i18n-context";

/**
 * Two of these are not `key=value`: AISBench spells one option differently from its field
 * name, and the visualizer is a mode rather than a flag.
 */
const CLI_FLAGS: Record<string, string> = {
  merge_datasets: "--merge-ds",
  visualization: "--mode perf_viz",
};

const GROUPS = ["config_fields", "generation_kwargs", "cli"];

function flagOf(key: string): string {
  return CLI_FLAGS[key] ?? `--${key.replaceAll("_", "-")}`;
}

/** A command line, written the way it would have been typed. */
function cliWords(cli: Record<string, unknown>): string[] {
  const words: string[] = [];
  for (const [key, value] of Object.entries(cli)) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    words.push(flagOf(key));
    if (value !== true) {
      words.push(String(value));
    }
  }
  return words;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * Jobs submitted before parameters were split stored one flat dict.
 *
 * Reading those with the new keys finds nothing and would report "all defaults", which is
 * a claim about a run that is simply untrue. Show what was actually stored instead.
 */
function legacyParameters(parameters: Record<string, unknown>): Record<string, unknown> | null {
  if (GROUPS.some((group) => group in parameters)) {
    return null;
  }
  const stored = Object.fromEntries(
    Object.entries(parameters).filter(([, value]) => value !== null && value !== false),
  );
  return Object.keys(stored).length === 0 ? null : stored;
}

function pairs(values: Record<string, unknown>): string {
  return Object.entries(values)
    .map(([key, value]) => `${key}=${String(value)}`)
    .join("  ");
}

function formatMoment(value: string, locale: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString(locale === "zh" ? "zh-CN" : "en-GB", {
        dateStyle: "short",
        timeStyle: "short",
      });
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="info-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * What the run was, in the shape it was submitted in.
 *
 * The parameters arrive in the two groups AISBench has — fields of the model config file,
 * and the command line — so printing them as one flat list would undo that distinction.
 * Only what the user changed is stored, so an empty group means "the defaults stood".
 */
export function RunConfiguration({ job, elapsed }: { job: Job; elapsed: string | null }) {
  const { t, locale } = useI18n();
  const parameters = job.parameters;
  const legacy = legacyParameters(parameters);
  const changed = {
    ...asRecord(parameters.config_fields),
    ...asRecord(parameters.generation_kwargs),
  };
  const cli = cliWords(asRecord(parameters.cli));

  return (
    <dl className="info-list">
      {elapsed !== null && <Row label={t("jobDetail.elapsed")}>{elapsed}</Row>}
      {job.finished_at !== null && (
        <Row label={t("jobDetail.finishedAt")}>{formatMoment(job.finished_at, locale)}</Row>
      )}
      <Row label={t("newJob.dataset")}>{job.dataset.name}</Row>
      {job.dataset.config_name !== "" && (
        <Row label={t("newJob.config")}>
          <span className="mono">{job.dataset.config_name}</span>
        </Row>
      )}
      <Row label={t("newJob.modelEndpoint")}>{job.model.name}</Row>
      <Row label="Base URL">
        <span className="mono">{job.model.base_url}</span>
      </Row>
      {job.model.config_name !== "" && (
        <Row label={t("newJob.modelConfig")}>
          <span className="mono">{job.model.config_name}</span>
        </Row>
      )}
      {legacy !== null ? (
        <Row label={t("jobDetail.storedParameters")}>
          <span className="mono">{pairs(legacy)}</span>
        </Row>
      ) : (
        <>
          <Row label={t("jobDetail.changedFields")}>
            {Object.keys(changed).length === 0 ? (
              <span className="info-muted">{t("jobDetail.allDefaults")}</span>
            ) : (
              <span className="mono">{pairs(changed)}</span>
            )}
          </Row>
          <Row label={t("newJob.groupCli")}>
            {cli.length === 0 ? (
              <span className="info-muted">{t("jobDetail.allDefaults")}</span>
            ) : (
              <span className="mono">{cli.join(" ")}</span>
            )}
          </Row>
        </>
      )}
    </dl>
  );
}
