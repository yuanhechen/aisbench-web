import { useState } from "react";
import type { FormEvent } from "react";

import { api } from "../api/client";
import type { ModelEndpoint, ProbeResult } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

interface Draft {
  base_url: string;
  api_key: string;
  name: string;
  request_timeout: string;
  max_output_length: string;
}

const EMPTY_DRAFT: Draft = {
  base_url: "",
  api_key: "",
  name: "",
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
      // Testing is also how a renamed or replaced model is picked up.
      endpoints.reload();
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
                <span>{endpoint.base_url}</span>
                {/* Detected from the service, never typed in. */}
                <span>
                  {endpoint.model_name === ""
                    ? t("models.modelUnknown")
                    : endpoint.model_name}
                </span>
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
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function update(field: keyof Draft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    if (field === "base_url" || field === "api_key") {
      // The previous result described a different address; keep it from looking current.
      setProbe(null);
    }
  }

  async function runProbe() {
    setError(null);
    setProbing(true);
    try {
      setProbe(
        await api.post<ProbeResult>("/api/models/probe", {
          base_url: draft.base_url,
          api_key: draft.api_key === "" ? null : draft.api_key,
        }),
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setProbing(false);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      await api.post("/api/models", {
        name: draft.name,
        base_url: draft.base_url,
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
        <div>
          <label className="field" htmlFor="model-base_url">
            {t("models.baseUrl")}
          </label>
          <input
            id="model-base_url"
            className="input"
            type="text"
            placeholder="http://127.0.0.1:8000/v1"
            value={draft.base_url}
            onChange={(event) => update("base_url", event.target.value)}
          />
        </div>
        <div>
          <label className="field" htmlFor="model-api_key">
            API Key
          </label>
          <input
            id="model-api_key"
            className="input"
            type="password"
            value={draft.api_key}
            onChange={(event) => update("api_key", event.target.value)}
          />
        </div>

        <div className="probe-row">
          <button
            type="button"
            className="button-secondary"
            disabled={draft.base_url.trim() === "" || probing}
            onClick={() => void runProbe()}
          >
            {probing ? t("models.probing") : t("models.probe")}
          </button>
          {probe !== null && (
            <span className={probe.ok ? "probe-ok" : "probe-failed"} role="status">
              {probe.ok
                ? `${t("models.reachable")} · ${probe.latency_ms} ms · ${
                    probe.models[0] ?? t("models.modelUnknown")
                  }`
                : probe.message}
            </span>
          )}
        </div>
        <p className="field-hint">{t("models.detectHint")}</p>

        {(
          [
            ["name", t("models.name"), "text"],
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
