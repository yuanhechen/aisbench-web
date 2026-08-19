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

const GROUPS = ["config_fields", "generation_kwargs", "cli"];

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

/**
 * What was run, in the shape the job was submitted in.
 *
 * The parameters arrive in the two groups AISBench has — fields of the model config file,
 * and the command line — so printing them as one flat list would undo that distinction.
 * Only what the user changed is stored, so an empty group means "the defaults stood".
 */
export function RunConfiguration({ job }: { job: Job }) {
  const { t } = useI18n();
  const parameters = job.parameters;
  const legacy = legacyParameters(parameters);
  const configFields = asRecord(parameters.config_fields);
  const generation = asRecord(parameters.generation_kwargs);
  const cli = cliWords(asRecord(parameters.cli));
  const changed = { ...configFields, ...generation };

  return (
    <dl className="config-row">
      <div>
        <dt>{t("newJob.modelEndpoint")}</dt>
        <dd>{job.model.name}</dd>
      </div>
      <div>
        <dt>Base URL</dt>
        <dd className="mono">{job.model.base_url}</dd>
      </div>
      {job.model.config_name !== "" && (
        <div>
          <dt>{t("newJob.modelConfig")}</dt>
          <dd className="mono">{job.model.config_name}</dd>
        </div>
      )}
      {job.dataset.config_name !== "" && (
        <div>
          <dt>{t("newJob.config")}</dt>
          <dd className="mono">{job.dataset.config_name}</dd>
        </div>
      )}
      {legacy !== null ? (
        <div className="config-row-wide">
          <dt>{t("jobDetail.storedParameters")}</dt>
          <dd className="mono">
            {Object.entries(legacy)
              .map(([key, value]) => `${key}=${String(value)}`)
              .join("  ")}
          </dd>
        </div>
      ) : (
        <>
          <div className="config-row-wide">
            <dt>{t("jobDetail.changedFields")}</dt>
            <dd className="mono">
              {Object.keys(changed).length === 0
                ? t("jobDetail.allDefaults")
                : Object.entries(changed)
                    .map(([key, value]) => `${key}=${String(value)}`)
                    .join("  ")}
            </dd>
          </div>
          <div className="config-row-wide">
            <dt>{t("newJob.groupCli")}</dt>
            <dd className="mono">
              {cli.length === 0 ? t("jobDetail.allDefaults") : cli.join(" ")}
            </dd>
          </div>
        </>
      )}
    </dl>
  );
}
