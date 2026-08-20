import { useMemo } from "react";
import type { ReactNode } from "react";

import type { Job, ModelConfigOption } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
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

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * Jobs submitted before parameters were split stored one flat dict.
 *
 * Reading those with the new keys finds nothing and would report "all defaults", which is a
 * claim about a run that is simply untrue. Show what was actually stored instead.
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

/** The config file the run used: the one named in the snapshot, or the mode's default. */
function resolveConfig(job: Job, configs: ModelConfigOption[]): ModelConfigOption | null {
  if (job.model.config_name !== "") {
    const named = configs.find((config) => config.name === job.model.config_name);
    if (named !== undefined) {
      return named;
    }
  }
  return configs.find((config) => config.default_for === job.mode) ?? null;
}

function Row({
  label,
  wide = false,
  children,
}: {
  label: string;
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <>
      <dt className={wide ? "info-wide" : ""}>{label}</dt>
      <dd className={wide ? "info-wide" : ""}>{children}</dd>
    </>
  );
}

/** Where one kind of fact ends and the next begins. */
function Break() {
  return <div className="info-break" />;
}

/** One `name  value` line of a parameter list. */
function Param({
  name,
  value,
  changed = false,
  unset = false,
}: {
  name: string;
  value: string;
  changed?: boolean;
  /** An option the run left at its own default: present in the list, quiet in the eye. */
  unset?: boolean;
}) {
  return (
    <li className="param-row">
      <span className="mono param-name" title={name}>
        {name}
      </span>
      <span
        className={`mono param-value${changed ? " is-changed" : ""}${unset ? " is-unset" : ""}`}
        title={value}
      >
        {value}
      </span>
    </li>
  );
}

/**
 * What the run was, in the shape it ran in.
 *
 * The rail answers "what exactly did this run use": every field of the config file with the
 * value that actually applied — the file's own default, or the value submitted in its place
 * (marked) — and every command-line option that was in force, not only the unusual ones.
 */
export function RunConfiguration({ job, elapsed }: { job: Job; elapsed: string | null }) {
  const { t, locale } = useI18n();
  const { reportFailure } = useAuth();
  const modelConfigs = useApiQuery<ModelConfigOption[]>("/api/models/configs", {
    onFailure: reportFailure,
  });
  const parameters = job.parameters;
  const legacy = legacyParameters(parameters);
  const changed = {
    ...asRecord(parameters.config_fields),
    ...asRecord(parameters.generation_kwargs),
  };
  const config = useMemo(
    () => resolveConfig(job, modelConfigs.data ?? []),
    [job, modelConfigs.data],
  );
  const extraDatasets = job.datasets.length > 1 ? job.datasets.length - 1 : 0;

  return (
    <>
      <dl className="info-grid">
        {elapsed !== null && <Row label={t("jobDetail.elapsed")}>{elapsed}</Row>}
        {job.finished_at !== null && (
          <Row label={t("jobDetail.finishedAt")}>{formatMoment(job.finished_at, locale)}</Row>
        )}
        <Break />
        <Row label={t("newJob.dataset")}>
          {job.dataset.name}
          {extraDatasets > 0 && <span className="info-muted"> +{extraDatasets}</span>}
        </Row>
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
      </dl>

      {legacy !== null ? (
        <dl className="info-grid">
          <Break />
          <Row label={t("jobDetail.storedParameters")} wide>
            <span className="mono">{pairs(legacy)}</span>
          </Row>
        </dl>
      ) : (
        <>
          <Break />
          <p className="eyebrow">{t("jobDetail.configInfo")}</p>
          {config === null ? (
            /* The catalog no longer carries the file this run used; the submitted values
               still are the truth of the run, and pretending at defaults would not be. */
            <dl className="info-grid">
              <Row label={t("jobDetail.changedFields")} wide>
                {Object.keys(changed).length === 0 ? (
                  <span className="info-muted">{t("jobDetail.allDefaults")}</span>
                ) : (
                  <span className="mono">{pairs(changed)}</span>
                )}
              </Row>
            </dl>
          ) : (
            <>
              <p className="field-hint mono">{config.name}.py</p>
              <ul className="param-list">
                <Param name="model" value={job.model.model_name} />
                {[...config.fields, ...config.generation_fields].map((field) => (
                  <Param
                    key={field.name}
                    name={field.name}
                    value={String(field.name in changed ? changed[field.name] : field.default)}
                    changed={field.name in changed}
                  />
                ))}
              </ul>
              <p className="field-hint">{t("jobDetail.changedHint")}</p>
            </>
          )}

          <p className="eyebrow">{t("newJob.groupCli")}</p>
          <ul className="param-list">
            {Object.entries(asRecord(parameters.cli)).map(([key, value]) => (
              <Param
                key={key}
                name={flagOf(key)}
                value={
                  value === true
                    ? "✓"
                    : value === false
                      ? "✗"
                      : value === null || value === undefined
                        ? "—"
                        : String(value)
                }
                unset={value === null || value === undefined}
              />
            ))}
          </ul>
        </>
      )}
    </>
  );
}
