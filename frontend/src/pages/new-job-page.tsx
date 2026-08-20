import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { FormEvent } from "react";

import { api } from "../api/client";
import type { ConfigField, Dataset, Job, ModelConfigOption, ModelEndpoint } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

type Mode = "accuracy" | "performance";

interface FormState {
  name: string;
  modelEndpointId: string;
  modelConfigName: string;
  datasetIds: string[];
  mode: Mode;
  numPrompts: string;
  maxNumWorkers: string;
  maxWorkersPerGpu: string;
  numWarmups: string;
  // accuracy
  dumpEvalDetails: boolean;
  mergeDatasets: boolean;
  dumpExtractRate: boolean;
  // performance
  visualization: boolean;
  pressure: boolean;
  pressureTime: string;
  specDecode: boolean;
}

/** Fields the user changed, by the name the chosen config file gives them. */
type Overrides = Record<string, boolean | string>;

const INITIAL: FormState = {
  name: "",
  modelEndpointId: "",
  modelConfigName: "",
  datasetIds: [],
  mode: "accuracy",
  numPrompts: "8",
  maxNumWorkers: "1",
  maxWorkersPerGpu: "",
  numWarmups: "",
  dumpEvalDetails: false,
  mergeDatasets: false,
  dumpExtractRate: false,
  visualization: false,
  pressure: false,
  pressureTime: "",
  specDecode: false,
};

/** The most datasets one job may combine; the backend enforces the same ceiling. */
const MAX_DATASETS = 16;

function optionalNumber(value: string): number | undefined {
  const parsed = Number(value);
  return value.trim() === "" || Number.isNaN(parsed) ? undefined : parsed;
}

/**
 * The values the user actually changed, typed as the config file types them.
 *
 * A blank input means "leave the file alone", which is what not editing the file by hand
 * would have done; a checkbox has no blank, so it counts only when it differs.
 */
function changedFields(fields: ConfigField[], overrides: Overrides): Record<string, unknown> {
  const changed: Record<string, unknown> = {};
  for (const field of fields) {
    const value = overrides[field.name];
    if (value === undefined) continue;
    if (field.kind === "boolean") {
      if (value !== field.default) changed[field.name] = value;
      continue;
    }
    if (typeof value !== "string" || value.trim() === "") continue;
    if (field.kind === "text") {
      if (value !== field.default) changed[field.name] = value;
      continue;
    }
    const parsed = Number(value);
    if (!Number.isNaN(parsed) && parsed !== field.default) changed[field.name] = parsed;
  }
  return changed;
}

export function NewJobPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const models = useApiQuery<ModelEndpoint[]>("/api/models", { onFailure: reportFailure });
  const datasets = useApiQuery<Dataset[]>("/api/datasets", { onFailure: reportFailure });
  const modelConfigs = useApiQuery<ModelConfigOption[]>("/api/models/configs", {
    onFailure: reportFailure,
  });
  const [form, setForm] = useState<FormState>(INITIAL);
  const [overrides, setOverrides] = useState<Overrides>({});
  /** A config variant the user picked for a dataset, by dataset id. */
  const [configNames, setConfigNames] = useState<Record<string, string>>({});
  const [queued, setQueued] = useState<Job | null>(null);
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // A job can only run against a dataset that is actually on disk.
  const installed = useMemo(
    () => (datasets.data ?? []).filter((dataset) => dataset.status === "available"),
    [datasets.data],
  );
  const activeModels = (models.data ?? []).filter((model) => model.is_active);

  // Datasets chosen for this job, in the order they were picked.
  const selectedDatasets = useMemo(
    () =>
      form.datasetIds
        .map((id) => installed.find((dataset) => dataset.id === id))
        .filter((dataset): dataset is Dataset => dataset !== undefined),
    [form.datasetIds, installed],
  );
  // A dataset the installed AISBench has no config for in this mode cannot be picked.
  const configsFor = useCallback(
    (dataset: Dataset) => dataset.configs.filter((config) => config.mode === form.mode),
    [form.mode],
  );
  // Sorted flat: the family prefix already groups these visually, and a class grouping
  // repeated the prefix while splitting one serving stack across several headings.
  const sortedModelConfigs = useMemo(
    () => [...(modelConfigs.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [modelConfigs.data],
  );

  // The fields a job can change are the ones this file declares. They differ between files:
  // one has returns_tool_calls, another has do_sample, several have no api_key at all.
  // Picking none is not picking nothing: AISBench still runs a config, the one this mode
  // defaults to, so its fields are the ones to show.
  const selectedModelConfig =
    sortedModelConfigs.find((config) => config.name === form.modelConfigName) ??
    sortedModelConfigs.find((config) => config.default_for === form.mode) ??
    null;

  const atLimit = form.datasetIds.length >= MAX_DATASETS;

  const ready =
    form.modelEndpointId !== "" &&
    selectedDatasets.length > 0 &&
    selectedDatasets.every((dataset) => configsFor(dataset).length > 0) &&
    !submitting;

  function update<K extends keyof FormState>(field: K, value: FormState[K]) {
    setForm((current) => {
      const next = { ...current, [field]: value };
      // A variant belongs to one mode; switching it invalidates every dataset's choice.
      if (field === "mode") {
        setConfigNames({});
      }
      return next;
    });
    // Field names belong to one config file; another file may not have them at all, and
    // switching mode switches which file the default resolves to.
    if (field === "modelConfigName" || field === "mode") {
      setOverrides({});
    }
    setQueued(null);
  }

  function toggleDataset(id: string, checked: boolean) {
    setForm((current) => {
      const without = current.datasetIds.filter((dataset) => dataset !== id);
      return {
        ...current,
        datasetIds: checked ? [...without, id].slice(0, MAX_DATASETS) : without,
      };
    });
    if (!checked) {
      setConfigNames((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
    }
    setQueued(null);
  }

  function pickConfig(id: string, name: string) {
    setConfigNames((current) => ({ ...current, [id]: name }));
    setQueued(null);
  }

  function overrideField(name: string, value: boolean | string) {
    setOverrides((current) => ({ ...current, [name]: value }));
    setQueued(null);
  }

  function parameters(): Record<string, unknown> {
    // Two groups, because AISBench has two: the config file describes the endpoint, the
    // command line drives the run. Only what the user set travels.
    const common = {
      num_prompts: optionalNumber(form.numPrompts),
      max_num_workers: optionalNumber(form.maxNumWorkers),
      max_workers_per_gpu: optionalNumber(form.maxWorkersPerGpu),
      num_warmups: optionalNumber(form.numWarmups),
    };
    const specific =
      form.mode === "accuracy"
        ? {
            dump_eval_details: form.dumpEvalDetails,
            merge_datasets: form.mergeDatasets,
            dump_extract_rate: form.dumpExtractRate,
          }
        : {
            visualization: form.visualization,
            pressure: form.pressure,
            pressure_time: optionalNumber(form.pressureTime),
            spec_decode: form.specDecode,
          };
    return {
      cli: Object.fromEntries(
        Object.entries({ ...common, ...specific }).filter(([, value]) => value !== undefined),
      ),
      config_fields: changedFields(selectedModelConfig?.fields ?? [], overrides),
      generation_kwargs: changedFields(selectedModelConfig?.generation_fields ?? [], overrides),
    };
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const created = await api.post<Job>("/api/jobs", {
        name: form.name,
        model_endpoint_id: form.modelEndpointId,
        dataset_ids: form.datasetIds,
        mode: form.mode,
        config_names: configNames,
        model_config_name: form.modelConfigName === "" ? null : form.modelConfigName,
        parameters: parameters(),
      });
      setQueued(created);
      // The form's work is done; the job now lives in the list.
      navigate("/jobs");
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <PageHeader title={t("nav.newJob")}>
        <button type="submit" className="button-primary" disabled={!ready}>
          {t("newJob.submit")}
        </button>
      </PageHeader>

      {/* What to run on the left, how to run it on the right: one screen holds the
          whole form, and the two halves read as one decision each. */}
      <div className="job-columns">
      <div className="job-column">
      <section className="form-step">
        <h2 className="form-step-title"><span className="form-step-no">1</span>{t("newJob.stepModel")}</h2>
        {/* One row of choices a reader takes in at a glance; stacked fields read as a list
            of separate questions about the same thing. */}
        <div className="setup-row">
          <div>
            <label className="field" htmlFor="job-name">
              {t("newJob.name")}
            </label>
            <input
              id="job-name"
              className="input"
              type="text"
              placeholder={t("newJob.namePlaceholder")}
              value={form.name}
              onChange={(event) => update("name", event.target.value)}
            />
          </div>
          <div>
            <label className="field" htmlFor="job-model">
              {t("newJob.modelEndpoint")}
            </label>
            <select
              id="job-model"
              className="input"
              value={form.modelEndpointId}
              onChange={(event) => update("modelEndpointId", event.target.value)}
            >
              <option value="">{t("newJob.choose")}</option>
              {activeModels.map((model) => (
                <option key={model.id} value={model.id}>
                  {describeEndpoint(model)}
                </option>
              ))}
            </select>
          </div>
          {(modelConfigs.data ?? []).length > 0 && (
            <div>
              <label className="field" htmlFor="job-model-config">
                {t("newJob.modelConfig")}
              </label>
              <select
                id="job-model-config"
                className="input"
                value={form.modelConfigName}
                onChange={(event) => update("modelConfigName", event.target.value)}
              >
                <option value="">{t("newJob.modelConfigDefault")}</option>
                {sortedModelConfigs.map((config) => (
                  <option key={config.name} value={config.name}>
                    {config.name}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
        {(modelConfigs.data ?? []).length > 0 && (
          <p className="field-hint">{t("newJob.modelConfigHint")}</p>
        )}
        <div className="segmented" role="radiogroup" aria-label={t("newJob.accuracy")}>
          {(["accuracy", "performance"] as const).map((mode) => (
            <label key={mode} className="segmented-item">
              <input
                type="radio"
                name="mode"
                checked={form.mode === mode}
                onChange={() => update("mode", mode)}
              />
              <span>{mode === "accuracy" ? t("newJob.accuracy") : t("newJob.performance")}</span>
            </label>
          ))}
        </div>
      </section>

      <section className="form-step">
        <h2 className="form-step-title">
          {t("newJob.stepDataset")}
          {form.datasetIds.length > 0 && (
            <span className="form-step-count">
              {t("newJob.datasetCount", { count: String(form.datasetIds.length) })}
            </span>
          )}
        </h2>
        <p className="field-hint">{t("newJob.datasetHint")}</p>
        <DatasetPicker
          installed={installed}
          selectedIds={form.datasetIds}
          variantsOf={configsFor}
          atLimit={atLimit}
          onToggle={toggleDataset}
        />
        {/* A picked dataset the mode cannot run must say so where it was picked. */}
        {selectedDatasets.some((dataset) => configsFor(dataset).length === 0) && (
          <p className="form-error" role="alert">
            {t("newJob.noConfigForMode")}
          </p>
        )}

        {selectedDatasets.some((dataset) => configsFor(dataset).length > 1) && (
          <>
            <p className="field">{t("newJob.config")}</p>
            <div className="ds-variants">
              {selectedDatasets.map((dataset) => {
                const variants = configsFor(dataset);
                if (variants.length <= 1) {
                  return null;
                }
                const chosen =
                  variants.find((config) => config.name === configNames[dataset.id]) ??
                  variants.find((config) => config.name === dataset.default_config) ??
                  variants[0];
                return (
                  <label key={dataset.id} className="ds-variant">
                    <span className="ds-option-name mono">{dataset.name}</span>
                    <select
                      className="input"
                      value={chosen.name}
                      onChange={(event) => pickConfig(dataset.id, event.target.value)}
                    >
                      {variants.map((config) => (
                        <option key={config.name} value={config.name}>
                          {config.name}
                          {config.name === dataset.default_config
                            ? `\u2003${t("newJob.configDefault")}`
                            : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                );
              })}
            </div>
            <p className="field-hint">{t("newJob.configHint")}</p>
          </>
        )}
      </section>

      </div>
      <div className="job-column job-column-params">
      <section className="form-step">
        <h2 className="form-step-title"><span className="form-step-no">3</span>{t("newJob.stepParameters")}</h2>

        <div className="param-group">
        <p className="group-title">{t("newJob.groupModelConfig")}</p>
        {selectedModelConfig === null ? (
          <p className="field-hint">{t("newJob.pickConfigFirst")}</p>
        ) : (
          <>
            <p className="field-hint">
              {t("newJob.configFieldsHint")}
              <span className="mono"> {selectedModelConfig.name}.py</span>
            </p>
            <ConfigFields
              fields={selectedModelConfig.fields}
              overrides={overrides}
              hintFor={(name) =>
                name in FIELD_HINTS
                  ? t(FIELD_HINTS[name as keyof typeof FIELD_HINTS])
                  : undefined
              }
              onChange={overrideField}
            />
            {selectedModelConfig.generation_fields.length > 0 && (
              <details className="advanced">
                <summary>generation_kwargs</summary>
                <p className="field-hint">{t("newJob.samplingHint")}</p>
                <ConfigFields
                  fields={selectedModelConfig.generation_fields}
                  overrides={overrides}
                  onChange={overrideField}
                />
              </details>
            )}
          </>
        )}

        </div>

        <div className="param-group">
        <p className="group-title">{t("newJob.groupCli")}</p>
        <p className="field-hint">{t("newJob.cliHint")}</p>
        <div className="field-grid">
          <NumberField
            id="job-num-prompts"
            label="--num-prompts"
            hint={t("newJob.numPromptsHint")}
            value={form.numPrompts}
            onChange={(value) => update("numPrompts", value)}
          />
          <NumberField
            id="job-workers"
            label="--max-num-workers"
            hint={t("newJob.maxWorkersHint")}
            value={form.maxNumWorkers}
            onChange={(value) => update("maxNumWorkers", value)}
          />
          <NumberField
            id="job-workers-per-gpu"
            label="--max-workers-per-gpu"
            value={form.maxWorkersPerGpu}
            onChange={(value) => update("maxWorkersPerGpu", value)}
          />
          <NumberField
            id="job-warmups"
            label="--num-warmups"
            value={form.numWarmups}
            onChange={(value) => update("numWarmups", value)}
          />
          {form.mode === "performance" && (
            <>
              {form.pressure && (
                <NumberField
                  id="job-pressure-time"
                  label="--pressure-time"
                  value={form.pressureTime}
                  onChange={(value) => update("pressureTime", value)}
                />
              )}
            </>
          )}
        </div>

        {form.mode === "accuracy" ? (
            <>
              <CheckboxField
                label="--dump-eval-details"
                checked={form.dumpEvalDetails}
                onChange={(value) => update("dumpEvalDetails", value)}
              />
              <CheckboxField
                label="--merge-ds"
                checked={form.mergeDatasets}
                onChange={(value) => update("mergeDatasets", value)}
              />
              <CheckboxField
                label="--dump-extract-rate"
                checked={form.dumpExtractRate}
                onChange={(value) => update("dumpExtractRate", value)}
              />
            </>
          ) : (
            <>
              <CheckboxField
                label="--pressure"
                checked={form.pressure}
                onChange={(value) => update("pressure", value)}
              />
              <CheckboxField
                label="--spec-decode"
                checked={form.specDecode}
                onChange={(value) => update("specDecode", value)}
              />
              <CheckboxField
                label="--mode perf_viz"
                hint={t("newJob.visualizationHint")}
                checked={form.visualization}
                onChange={(value) => update("visualization", value)}
              />
            </>
          )}
        </div>
      </section>

      </div>
      </div>
      {(error !== null || queued !== null) && (
        <section className="form-step submit-step">
          {error !== null && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {queued !== null && (
            <p className="form-success" role="status">
              {t("newJob.queued")}
              {queued.queue_position !== null && (
                <span>
                  {" · "}
                  {t("newJob.queuePosition")} {queued.queue_position}
                </span>
              )}
            </p>
          )}
        </section>
      )}
    </form>
  );
}

/**
 * A searchable dropdown that collects several datasets: type to filter, click to pick,
 * and the picks sit beneath as removable tags. All of them laid flat was a list to walk;
 * this is a list to search.
 */
function DatasetPicker({
  installed,
  selectedIds,
  variantsOf,
  atLimit,
  onToggle,
}: {
  installed: Dataset[];
  selectedIds: string[];
  variantsOf: (dataset: Dataset) => { mode: string }[];
  atLimit: boolean;
  onToggle: (id: string, checked: boolean) => void;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const away = (event: MouseEvent) => {
      if (rootRef.current !== null && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  const needle = query.trim().toLowerCase();
  const listed = installed.filter(
    (dataset) =>
      needle === "" ||
      dataset.name.toLowerCase().includes(needle) ||
      dataset.id.toLowerCase().includes(needle) ||
      dataset.config_name.toLowerCase().includes(needle),
  );

  return (
    <div className="combobox" ref={rootRef}>
      <label className="field visually-hidden" htmlFor="job-dataset-search">
        {t("newJob.dataset")}
      </label>
      <div className="combobox-control">
        <input
          id="job-dataset-search"
          className="input"
          role="combobox"
          aria-expanded={open}
          placeholder={t("newJob.datasetSearchPlaceholder")}
          value={query}
          onFocus={() => setOpen(true)}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setOpen(false);
            }
          }}
        />
        {/* The arrow decorates the input; the input itself carries the label. */}
        <span className="combobox-toggle" aria-hidden>
          ▾
        </span>
      </div>
      {open && (
        <div className="combobox-panel" role="listbox" aria-label={t("newJob.dataset")}>
          {installed.length === 0 && <p className="combobox-empty">{t("newJob.noDatasets")}</p>}
          {installed.length > 0 && listed.length === 0 && (
            <p className="combobox-empty">{t("datasets.noMatch")}</p>
          )}
          {listed.map((dataset) => {
            const chosen = selectedIds.includes(dataset.id);
            const blocked = !chosen && (variantsOf(dataset).length === 0 || atLimit);
            return (
              <button
                type="button"
                key={dataset.id}
                role="option"
                aria-selected={chosen}
                disabled={blocked}
                className={`combobox-option${chosen ? " is-selected" : ""}${
                  blocked ? " is-blocked" : ""
                }`}
                onClick={() => onToggle(dataset.id, !chosen)}
              >
                <span className="ds-option-name mono">{dataset.name}</span>
                {dataset.task !== "" && (
                  <span className="ds-option-task">{dataset.task}</span>
                )}
                {chosen && (
                  <span className="combobox-check" aria-hidden>
                    ✓
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
      {selectedIds.length > 0 && (
        <div className="chip-row">
          {selectedIds.map((id) => {
            const dataset = installed.find((item) => item.id === id);
            return (
              <span key={id} className="chip-token mono">
                {dataset?.name ?? id}
                <button
                  type="button"
                  aria-label={`${dataset?.name ?? id} ✕`}
                  onClick={() => onToggle(id, false)}
                >
                  ✕
                </button>
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * The endpoint's own name, plus the model only when the name does not already say it.
 *
 * Naming an endpoint after its model is the obvious thing to do, and printing both then
 * repeats one long string twice. A served id often carries a path, so the comparison is
 * against its last segment.
 */
function describeEndpoint(model: ModelEndpoint): string {
  if (model.model_name === "") {
    return model.name;
  }
  const served = model.model_name.split("/").filter(Boolean).pop() ?? model.model_name;
  const name = model.name.trim().toLowerCase();
  if (name === served.toLowerCase() || name === model.model_name.toLowerCase()) {
    return model.name;
  }
  return `${model.name} · ${served}`;
}

// A few fields do something the name alone does not say. Everything else is left to speak
// for itself: the name in the file is the name AISBench uses.
const FIELD_HINTS = {
  batch_size: "newJob.batchSizeHint",
  request_rate: "newJob.requestRateHint",
} as const;

/**
 * The fields of one config file: boxes first, then the switches.
 *
 * A checkbox is a word and a tick, not a labelled box, so sharing a grid with the number
 * fields left it floating in a cell sized for something else.
 */
function ConfigFields({
  fields,
  overrides,
  hintFor,
  onChange,
}: {
  fields: ConfigField[];
  overrides: Overrides;
  hintFor?: (name: string) => string | undefined;
  onChange: (name: string, value: boolean | string) => void;
}) {
  const boxes = fields.filter((field) => field.kind !== "boolean");
  const switches = fields.filter((field) => field.kind === "boolean");
  return (
    <>
      {boxes.length > 0 && (
        <div className="field-grid">
          {boxes.map((field) => (
            <ConfigFieldInput
              key={field.name}
              field={field}
              value={overrides[field.name]}
              hint={hintFor?.(field.name)}
              onChange={(value) => onChange(field.name, value)}
            />
          ))}
        </div>
      )}
      {switches.map((field) => (
        <ConfigFieldInput
          key={field.name}
          field={field}
          value={overrides[field.name]}
          hint={hintFor?.(field.name)}
          onChange={(value) => onChange(field.name, value)}
        />
      ))}
    </>
  );
}

/**
 * One field of the chosen model config, typed and defaulted as that file has it.
 *
 * The default is the placeholder rather than the value, so an untouched field leaves the
 * file's own setting alone — the same as not editing that line by hand.
 */
function ConfigFieldInput({
  field,
  value,
  hint,
  onChange,
}: {
  field: ConfigField;
  value: boolean | string | undefined;
  hint?: string;
  onChange: (value: boolean | string) => void;
}) {
  const id = `job-field-${field.name}`;
  if (field.kind === "boolean") {
    const checked = typeof value === "boolean" ? value : field.default === true;
    return (
      <label className="checkbox-option" htmlFor={id}>
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="mono">{field.name}</span>
        {hint !== undefined && <span className="field-hint">{hint}</span>}
      </label>
    );
  }
  return (
    <div>
      <label className="field mono" htmlFor={id}>
        {field.name}
      </label>
      <input
        id={id}
        className="input"
        type={field.kind === "text" ? "text" : "number"}
        step={field.kind === "number" ? "any" : undefined}
        placeholder={String(field.default)}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint !== undefined && <p className="field-hint">{hint}</p>}
    </div>
  );
}

function CheckboxField({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="checkbox-option">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="mono">{label}</span>
      {hint !== undefined && <span className="field-hint">{hint}</span>}
    </label>
  );
}

function NumberField({
  id,
  label,
  hint,
  value,
  onChange,
}: {
  id: string;
  label: string;
  hint?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <label className="field mono" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="input"
        type="number"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint !== undefined && <p className="field-hint">{hint}</p>}
    </div>
  );
}
