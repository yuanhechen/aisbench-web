import { useState } from "react";
import type { FormEvent } from "react";

import { api } from "../api/client";
import type { ModelEndpoint, ProbeResult } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

interface Draft {
  name: string;
  base_url: string;
  model_name: string;
  api_key: string;
  request_timeout: string;
  max_output_length: string;
}

const EMPTY_DRAFT: Draft = {
  name: "",
  base_url: "",
  model_name: "",
  api_key: "",
  request_timeout: "60",
  max_output_length: "512",
};

export function ModelsPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const endpoints = useApiQuery<ModelEndpoint[]>("/api/models", { onFailure: reportFailure });
  const [creating, setCreating] = useState(false);
  const [probes, setProbes] = useState<Record<string, ProbeResult>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runProbe(endpoint: ModelEndpoint) {
    setBusy(endpoint.id);
    try {
      const result = await api.post<ProbeResult>(`/api/models/${endpoint.id}/test`);
      setProbes((current) => ({ ...current, [endpoint.id]: result }));
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(null);
    }
  }

  async function toggleActive(endpoint: ModelEndpoint) {
    setBusy(endpoint.id);
    try {
      await api.patch(`/api/models/${endpoint.id}`, { is_active: !endpoint.is_active });
      endpoints.reload();
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <PageHeader title={t("nav.models")} subtitle={t("models.subtitle")}>
        <button type="button" className="button-primary" onClick={() => setCreating(true)}>
          {t("models.create")}
        </button>
      </PageHeader>

      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      {endpoints.data !== null && endpoints.data.length === 0 && (
        <p className="empty-state">{t("models.empty")}</p>
      )}

      <div className="resource-list">
        {(endpoints.data ?? []).map((endpoint) => (
          <article key={endpoint.id} className="resource-row">
            <div className="resource-main">
              <div className="resource-title">
                {endpoint.name}
                {!endpoint.is_active && <span className="tag">{t("models.inactive")}</span>}
              </div>
              <div className="resource-meta">
                <span>{endpoint.model_name}</span>
                <span>{endpoint.base_url}</span>
                {/* The stored key is never returned by the API, so only its presence is shown. */}
                <span>{endpoint.has_api_key ? t("models.keySaved") : t("models.keyNone")}</span>
              </div>
              {probes[endpoint.id] !== undefined && (
                <p
                  className={probes[endpoint.id].ok ? "probe-ok" : "probe-failed"}
                  role="status"
                >
                  {probes[endpoint.id].message} ({probes[endpoint.id].latency_ms} ms)
                </p>
              )}
            </div>
            <div className="resource-actions">
              <button
                type="button"
                className="button-secondary"
                disabled={busy === endpoint.id}
                onClick={() => void runProbe(endpoint)}
              >
                {t("models.test")}
              </button>
              <button
                type="button"
                className="button-secondary"
                disabled={busy === endpoint.id}
                onClick={() => void toggleActive(endpoint)}
              >
                {endpoint.is_active ? t("models.deactivate") : t("models.activate")}
              </button>
            </div>
          </article>
        ))}
      </div>

      {creating && (
        <CreateEndpointDialog
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            endpoints.reload();
          }}
        />
      )}
    </>
  );
}

function CreateEndpointDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function update(field: keyof Draft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.post("/api/models", {
        name: draft.name,
        base_url: draft.base_url,
        model_name: draft.model_name,
        // An empty box means "no key", not an empty key.
        api_key: draft.api_key === "" ? null : draft.api_key,
        request_timeout: Number(draft.request_timeout),
        max_output_length: Number(draft.max_output_length),
      });
      onCreated();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <form className="modal" role="dialog" aria-label={t("models.create")} onSubmit={handleSubmit}>
        <h2 className="modal-title">{t("models.create")}</h2>
        {(
          [
            ["name", t("models.name"), "text"],
            ["base_url", "Base URL", "text"],
            ["model_name", t("models.modelName"), "text"],
            ["api_key", "API Key", "password"],
            ["request_timeout", t("models.timeout"), "number"],
            ["max_output_length", t("models.maxOutput"), "number"],
          ] as const
        ).map(([field, label, type]) => (
          <div key={field}>
            <label className="field" htmlFor={`model-${field}`}>
              {label}
            </label>
            <input
              id={`model-${field}`}
              className="input"
              type={type}
              value={draft[field]}
              onChange={(event) => update(field, event.target.value)}
            />
          </div>
        ))}
        {error !== null && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button type="button" className="button-secondary" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button-primary" disabled={saving}>
            {t("common.save")}
          </button>
        </div>
      </form>
    </div>
  );
}
