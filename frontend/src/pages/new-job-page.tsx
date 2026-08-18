import { useMemo, useState } from "react";
import type { FormEvent } from "react";

import { api } from "../api/client";
import type { Dataset, Job, ModelEndpoint } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

type Mode = "accuracy" | "performance";

interface FormState {
  modelEndpointId: string;
  datasetId: string;
  mode: Mode;
  numPrompts: string;
  maxNumWorkers: string;
  maxOutputLength: string;
  detailedScoring: boolean;
  concurrency: string;
  stream: boolean;
  visualization: boolean;
}

const INITIAL: FormState = {
  modelEndpointId: "",
  datasetId: "",
  mode: "accuracy",
  numPrompts: "8",
  maxNumWorkers: "1",
  maxOutputLength: "512",
  detailedScoring: false,
  concurrency: "1",
  stream: true,
  visualization: false,
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
  const [form, setForm] = useState<FormState>(INITIAL);
  const [queued, setQueued] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // A job can only run against a dataset that is actually on disk.
  const installed = useMemo(
    () => (datasets.data ?? []).filter((dataset) => dataset.status === "available"),
    [datasets.data],
  );
  const selectedDataset = installed.find((dataset) => dataset.id === form.datasetId) ?? null;
  const activeModels = (models.data ?? []).filter((model) => model.is_active);

  const modeUnsupported =
    selectedDataset !== null &&
    (form.mode === "accuracy"
      ? selectedDataset.accuracy_config === null
      : selectedDataset.performance_config === null);

  const ready =
    form.modelEndpointId !== "" && selectedDataset !== null && !modeUnsupported && !submitting;

  function update<K extends keyof FormState>(field: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [field]: value }));
    setQueued(null);
  }

  function parameters(): Record<string, unknown> {
    if (form.mode === "accuracy") {
      return {
        num_prompts: optionalNumber(form.numPrompts),
        max_num_workers: optionalNumber(form.maxNumWorkers),
        max_output_length: optionalNumber(form.maxOutputLength),
        detailed_scoring: form.detailedScoring,
      };
    }
    return {
      num_prompts: optionalNumber(form.numPrompts),
      concurrency: optionalNumber(form.concurrency),
      max_output_length: optionalNumber(form.maxOutputLength),
      stream: form.stream,
      visualization: form.visualization,
    };
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      setQueued(
        await api.post<Job>("/api/jobs", {
          model_endpoint_id: form.modelEndpointId,
          dataset_id: form.datasetId,
          mode: form.mode,
          parameters: parameters(),
        }),
      );
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <PageHeader title={t("nav.newJob")} subtitle={t("newJob.subtitle")} />

      <section className="form-step">
        <h2 className="form-step-title">{t("newJob.stepModel")}</h2>
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
              {model.name} · {model.model_name}
            </option>
          ))}
        </select>
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
      </section>

      <section className="form-step">
        <h2 className="form-step-title">{t("newJob.stepParameters")}</h2>
        <div className="field-grid">
          <NumberField
            id="job-num-prompts"
            label={form.mode === "accuracy" ? t("newJob.numPrompts") : t("newJob.requestCount")}
            value={form.numPrompts}
            onChange={(value) => update("numPrompts", value)}
          />
          {form.mode === "accuracy" ? (
            <NumberField
              id="job-workers"
              label={t("newJob.maxWorkers")}
              value={form.maxNumWorkers}
              onChange={(value) => update("maxNumWorkers", value)}
            />
          ) : (
            <NumberField
              id="job-concurrency"
              label={t("newJob.concurrency")}
              value={form.concurrency}
              onChange={(value) => update("concurrency", value)}
            />
          )}
          <NumberField
            id="job-max-output"
            label={t("newJob.maxOutputLength")}
            value={form.maxOutputLength}
            onChange={(value) => update("maxOutputLength", value)}
          />
        </div>
        {form.mode === "accuracy" ? (
          <label className="checkbox-option">
            <input
              type="checkbox"
              checked={form.detailedScoring}
              onChange={(event) => update("detailedScoring", event.target.checked)}
            />
            <span>{t("newJob.detailedScoring")}</span>
          </label>
        ) : (
          <>
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
                checked={form.visualization}
                onChange={(event) => update("visualization", event.target.checked)}
              />
              <span>{t("newJob.visualization")}</span>
            </label>
          </>
        )}
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

function NumberField({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
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
    </div>
  );
}
