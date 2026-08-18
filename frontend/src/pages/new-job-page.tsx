import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { FormEvent } from "react";

import { api } from "../api/client";
import type {
  Dataset,
  DatasetConfig,
  Job,
  ModelConfigOption,
  ModelEndpoint,
} from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

type Mode = "accuracy" | "performance";

interface FormState {
  name: string;
  modelEndpointId: string;
  modelConfigName: string;
  datasetId: string;
  configName: string;
  mode: Mode;
  numPrompts: string;
  maxNumWorkers: string;
  maxWorkersPerGpu: string;
  numWarmups: string;
  maxOutputLength: string;
  batchSize: string;
  retry: string;
  temperature: string;
  topP: string;
  topK: string;
  seed: string;
  repetitionPenalty: string;
  // accuracy
  dumpEvalDetails: boolean;
  mergeDatasets: boolean;
  dumpExtractRate: boolean;
  // performance
  requestRate: string;
  stream: boolean;
  visualization: boolean;
  pressure: boolean;
  pressureTime: string;
  specDecode: boolean;
}

const INITIAL: FormState = {
  name: "",
  modelEndpointId: "",
  modelConfigName: "",
  datasetId: "",
  configName: "",
  mode: "accuracy",
  numPrompts: "8",
  maxNumWorkers: "1",
  maxWorkersPerGpu: "",
  numWarmups: "",
  maxOutputLength: "512",
  batchSize: "",
  retry: "",
  temperature: "",
  topP: "",
  topK: "",
  seed: "",
  repetitionPenalty: "",
  dumpEvalDetails: false,
  mergeDatasets: false,
  dumpExtractRate: false,
  requestRate: "",
  stream: true,
  visualization: false,
  pressure: false,
  pressureTime: "",
  specDecode: false,
};

function optionalNumber(value: string): number | undefined {
  const parsed = Number(value);
  return value.trim() === "" || Number.isNaN(parsed) ? undefined : parsed;
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
  const [queued, setQueued] = useState<Job | null>(null);
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // A job can only run against a dataset that is actually on disk.
  const installed = useMemo(
    () => (datasets.data ?? []).filter((dataset) => dataset.status === "available"),
    [datasets.data],
  );
  const selectedDataset = installed.find((dataset) => dataset.id === form.datasetId) ?? null;
  const activeModels = (models.data ?? []).filter((model) => model.is_active);

  // The variants AISBench actually ships for this dataset and mode.
  const availableConfigs = useMemo(
    () => (selectedDataset?.configs ?? []).filter((config) => config.mode === form.mode),
    [selectedDataset, form.mode],
  );
  const modeUnsupported = selectedDataset !== null && availableConfigs.length === 0;
  const selectedConfig =
    availableConfigs.find((config) => config.name === form.configName) ?? availableConfigs[0];

  const ready =
    form.modelEndpointId !== "" && selectedDataset !== null && !modeUnsupported && !submitting;

  function update<K extends keyof FormState>(field: K, value: FormState[K]) {
    setForm((current) => {
      const next = { ...current, [field]: value };
      // A variant belongs to one dataset and one mode; changing either invalidates the choice.
      if (field === "datasetId" || field === "mode") {
        next.configName = "";
      }
      return next;
    });
    setQueued(null);
  }

  function parameters(): Record<string, unknown> {
    // Only what the user actually set; anything blank stays at AISBench's own default.
    const common = {
      num_prompts: optionalNumber(form.numPrompts),
      max_num_workers: optionalNumber(form.maxNumWorkers),
      max_workers_per_gpu: optionalNumber(form.maxWorkersPerGpu),
      num_warmups: optionalNumber(form.numWarmups),
      max_output_length: optionalNumber(form.maxOutputLength),
      batch_size: optionalNumber(form.batchSize),
      retry: optionalNumber(form.retry),
      temperature: optionalNumber(form.temperature),
      top_p: optionalNumber(form.topP),
      top_k: optionalNumber(form.topK),
      seed: optionalNumber(form.seed),
      repetition_penalty: optionalNumber(form.repetitionPenalty),
    };
    const specific =
      form.mode === "accuracy"
        ? {
            dump_eval_details: form.dumpEvalDetails,
            merge_datasets: form.mergeDatasets,
            dump_extract_rate: form.dumpExtractRate,
          }
        : {
            request_rate: optionalNumber(form.requestRate),
            stream: form.stream,
            visualization: form.visualization,
            pressure: form.pressure,
            pressure_time: optionalNumber(form.pressureTime),
            spec_decode: form.specDecode,
          };
    return Object.fromEntries(
      Object.entries({ ...common, ...specific }).filter(([, value]) => value !== undefined),
    );
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const created = await api.post<Job>("/api/jobs", {
        name: form.name,
        model_endpoint_id: form.modelEndpointId,
        dataset_id: form.datasetId,
        mode: form.mode,
        config_name: selectedConfig?.name ?? null,
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
      <PageHeader title={t("nav.newJob")} />

      <section className="form-step">
        <h2 className="form-step-title">{t("newJob.stepModel")}</h2>
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
              {model.model_name === "" ? model.name : `${model.name} · ${model.model_name}`}
            </option>
          ))}
        </select>

        {(modelConfigs.data ?? []).length > 0 && (
          <>
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
              {(modelConfigs.data ?? []).map((config) => (
                <option key={config.name} value={config.name}>
                  {config.name}
                  {"\u2003"}
                  {config.class_name}
                  {config.stream ? " · stream" : ""}
                </option>
              ))}
            </select>
            <p className="field-hint">{t("newJob.modelConfigHint")}</p>
          </>
        )}
      </section>

      <section className="form-step">
        <h2 className="form-step-title">{t("newJob.stepMode")}</h2>
        <div className="radio-row" role="radiogroup" aria-label={t("newJob.stepMode")}>
          {(["accuracy", "performance"] as const).map((mode) => (
            <label key={mode} className="radio-option">
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
        <h2 className="form-step-title">{t("newJob.stepDataset")}</h2>
        <label className="field" htmlFor="job-dataset">
          {t("newJob.dataset")}
        </label>
        <select
          id="job-dataset"
          className="input"
          value={form.datasetId}
          onChange={(event) => update("datasetId", event.target.value)}
        >
          <option value="">{t("newJob.choose")}</option>
          {installed.map((dataset) => (
            <option key={dataset.id} value={dataset.id}>
              {dataset.name}
            </option>
          ))}
        </select>
        {modeUnsupported && (
          <p className="form-error" role="alert">
            {form.mode === "performance"
              ? t("newJob.noPerformanceConfig")
              : t("newJob.noAccuracyConfig")}
          </p>
        )}

        {availableConfigs.length > 0 && (
          <>
            <label className="field" htmlFor="job-config">
              {t("newJob.config")}
            </label>
            <select
              id="job-config"
              className="input"
              value={selectedConfig?.name ?? ""}
              onChange={(event) => update("configName", event.target.value)}
            >
              {availableConfigs.map((config) => (
                <option key={config.name} value={config.name}>
                  {config.name}
                  {"\u2003"}
                  {describeConfig(config)}
                </option>
              ))}
            </select>
            {selectedConfig !== undefined && (
              <p className="field-hint">
                {selectedConfig.alias_of !== ""
                  ? t("newJob.configAlias").replace("{name}", selectedConfig.alias_of)
                  : t("newJob.configHint")}
              </p>
            )}
          </>
        )}
      </section>

      <section className="form-step">
        <h2 className="form-step-title">{t("newJob.stepParameters")}</h2>
        <div className="field-grid">
          <NumberField
            id="job-num-prompts"
            label={t("newJob.numPrompts")}
            hint={t("newJob.numPromptsHint")}
            value={form.numPrompts}
            onChange={(value) => update("numPrompts", value)}
          />
          <NumberField
            id="job-workers"
            label={t("newJob.maxWorkers")}
            value={form.maxNumWorkers}
            onChange={(value) => update("maxNumWorkers", value)}
          />
          <NumberField
            id="job-max-output"
            label={t("newJob.maxOutputLength")}
            value={form.maxOutputLength}
            onChange={(value) => update("maxOutputLength", value)}
          />
          <NumberField
            id="job-batch-size"
            label={t("newJob.batchSize")}
            value={form.batchSize}
            onChange={(value) => update("batchSize", value)}
          />
        </div>

        {form.mode === "accuracy" ? (
          <>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.dumpEvalDetails}
                onChange={(event) => update("dumpEvalDetails", event.target.checked)}
              />
              <span>{t("newJob.dumpEvalDetails")}</span>
            </label>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.mergeDatasets}
                onChange={(event) => update("mergeDatasets", event.target.checked)}
              />
              <span>{t("newJob.mergeDatasets")}</span>
            </label>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.dumpExtractRate}
                onChange={(event) => update("dumpExtractRate", event.target.checked)}
              />
              <span>{t("newJob.dumpExtractRate")}</span>
            </label>
          </>
        ) : (
          <>
            <div className="field-grid">
              <NumberField
                id="job-request-rate"
                label={t("newJob.requestRate")}
                hint={t("newJob.requestRateHint")}
                value={form.requestRate}
                onChange={(value) => update("requestRate", value)}
              />
              <NumberField
                id="job-warmups"
                label={t("newJob.numWarmups")}
                value={form.numWarmups}
                onChange={(value) => update("numWarmups", value)}
              />
              {form.pressure && (
                <NumberField
                  id="job-pressure-time"
                  label={t("newJob.pressureTime")}
                  value={form.pressureTime}
                  onChange={(value) => update("pressureTime", value)}
                />
              )}
            </div>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.stream}
                onChange={(event) => update("stream", event.target.checked)}
              />
              <span>{t("newJob.stream")}</span>
            </label>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.pressure}
                onChange={(event) => update("pressure", event.target.checked)}
              />
              <span>{t("newJob.pressure")}</span>
            </label>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.specDecode}
                onChange={(event) => update("specDecode", event.target.checked)}
              />
              <span>{t("newJob.specDecode")}</span>
            </label>
            <label className="checkbox-option">
              <input
                type="checkbox"
                checked={form.visualization}
                onChange={(event) => update("visualization", event.target.checked)}
              />
              <span>{t("newJob.visualization")}</span>
            </label>
          </>
        )}

        <details className="advanced">
          <summary>{t("newJob.sampling")}</summary>
          <p className="field-hint">{t("newJob.samplingHint")}</p>
          <div className="field-grid">
            <NumberField
              id="job-temperature"
              label="temperature"
              value={form.temperature}
              onChange={(value) => update("temperature", value)}
            />
            <NumberField
              id="job-top-p"
              label="top_p"
              value={form.topP}
              onChange={(value) => update("topP", value)}
            />
            <NumberField
              id="job-top-k"
              label="top_k"
              value={form.topK}
              onChange={(value) => update("topK", value)}
            />
            <NumberField
              id="job-seed"
              label="seed"
              value={form.seed}
              onChange={(value) => update("seed", value)}
            />
            <NumberField
              id="job-repetition-penalty"
              label="repetition_penalty"
              value={form.repetitionPenalty}
              onChange={(value) => update("repetitionPenalty", value)}
            />
            <NumberField
              id="job-retry"
              label={t("newJob.retry")}
              value={form.retry}
              onChange={(value) => update("retry", value)}
            />
            <NumberField
              id="job-workers-per-gpu"
              label={t("newJob.maxWorkersPerGpu")}
              value={form.maxWorkersPerGpu}
              onChange={(value) => update("maxWorkersPerGpu", value)}
            />
          </div>
        </details>
      </section>

      <section className="form-step">
        <h2 className="form-step-title">{t("newJob.stepReview")}</h2>
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
        <button type="submit" className="button-primary" disabled={!ready}>
          {t("newJob.submit")}
        </button>
      </section>
    </form>
  );
}

/**
 * Attributes read off a config file name, for display beside it.
 *
 * Never in place of it: several configs in one dataset can share every attribute, so the file
 * name is the only thing that identifies which one AISBench will run.
 */
function describeConfig(config: DatasetConfig): string {
  if (config.alias_of !== "") {
    return `= ${config.alias_of}`;
  }
  const parts: string[] = [];
  if (config.method !== "") {
    parts.push(config.method);
  }
  if (config.shots !== null) {
    parts.push(`${config.shots}-shot`);
  }
  if (config.chain_of_thought) {
    parts.push("CoT");
  }
  parts.push(config.chat_prompt ? "chat" : "completion");
  return parts.join(" · ");
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
      <label className="field" htmlFor={id}>
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
