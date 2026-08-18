import { useState } from "react";

import { api } from "../api/client";
import type { Dataset } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import type { MessageKey } from "../i18n/messages";
import { PageHeader } from "../components/page-header";

const POLL_MS = 1000;

const STATUS_LABELS: Record<Dataset["status"], MessageKey> = {
  not_installed: "datasets.notInstalled",
  installing: "datasets.installing",
  available: "datasets.available",
  failed: "datasets.failed",
  detected: "datasets.detected",
};

export function DatasetsPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  // Installs run in the background on the server, so the shared rows are polled.
  const datasets = useApiQuery<Dataset[]>("/api/datasets", {
    pollMs: POLL_MS,
    onFailure: reportFailure,
  });
  const [installing, setInstalling] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  async function install(dataset: Dataset) {
    setInstalling((current) => ({ ...current, [dataset.id]: true }));
    setError(null);
    try {
      await api.post(`/api/datasets/${dataset.id}/install`);
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
      setInstalling((current) => ({ ...current, [dataset.id]: false }));
    }
  }

  function statusOf(dataset: Dataset): Dataset["status"] {
    return installing[dataset.id] && dataset.status === "not_installed"
      ? "installing"
      : dataset.status;
  }

  return (
    <>
      <PageHeader title={t("nav.datasets")} subtitle={t("datasets.subtitle")} />
      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <table className="data-table">
        <thead>
          <tr>
            <th>{t("datasets.name")}</th>
            <th>{t("datasets.configId")}</th>
            <th>{t("datasets.status")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(datasets.data ?? []).map((dataset) => {
            const status = statusOf(dataset);
            return (
              <tr key={dataset.id}>
                <td>
                  <div className="resource-title">{dataset.name}</div>
                  <div className="resource-meta">{dataset.description}</div>
                  {dataset.error_message !== null && (
                    <div className="probe-failed">{dataset.error_message}</div>
                  )}
                </td>
                <td className="mono">{dataset.config_name}</td>
                <td>{t(STATUS_LABELS[status])}</td>
                <td className="align-right">
                  {/* Shared datasets are never deletable from the web UI. */}
                  {dataset.can_install && status === "not_installed" && (
                    <button
                      type="button"
                      className="button-secondary"
                      onClick={() => void install(dataset)}
                    >
                      {t("datasets.install")}
                    </button>
                  )}
                  {dataset.can_install && status === "failed" && (
                    <button
                      type="button"
                      className="button-secondary"
                      onClick={() => void install(dataset)}
                    >
                      {t("datasets.retry")}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
