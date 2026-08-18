import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import type { Dataset } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import type { MessageKey } from "../i18n/messages";
import { PageHeader } from "../components/page-header";

const POLL_MS = 1500;
const ALL = "";

// Domains as published in the AISBench documentation; "other" is what it does not list.
const CATEGORY_LABELS: Record<string, MessageKey> = {
  llm: "datasets.categoryLlm",
  multimodal: "datasets.categoryMultimodal",
  dialogue: "datasets.categoryDialogue",
  synthetic: "datasets.categorySynthetic",
  custom: "datasets.categoryCustom",
  other: "datasets.categoryOther",
};
const CATEGORY_ORDER = ["llm", "multimodal", "dialogue", "synthetic", "custom", "other"];

const STATUS_LABELS: Record<Dataset["status"], MessageKey> = {
  not_installed: "datasets.notInstalled",
  installing: "datasets.installing",
  available: "datasets.available",
  failed: "datasets.failed",
  detected: "datasets.detected",
};

/** Summarise the variants AISBench ships, in the reader's language. */
function useConfigSummary() {
  const { t } = useI18n();
  return (dataset: Dataset): string => {
    const accuracy = dataset.configs.filter((c) => c.mode === "accuracy").length;
    const performance = dataset.configs.filter((c) => c.mode === "performance").length;
    const parts: string[] = [];
    if (accuracy > 0) {
      parts.push(`${accuracy} ${t("datasets.accuracyConfigs")}`);
    }
    if (performance > 0) {
      parts.push(`${performance} ${t("datasets.performanceConfigs")}`);
    }
    return parts.join(" · ");
  };
}

export function DatasetsPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const [installing, setInstalling] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [watching, setWatching] = useState(false);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState(ALL);
  const [task, setTask] = useState(ALL);
  const describeConfigs = useConfigSummary();
  // Installs run in the background on the server, so the shared rows are polled -- but only
  // while one is actually running. A settled catalog does not change on its own.
  const datasets = useApiQuery<Dataset[]>("/api/datasets", {
    pollMs: watching ? POLL_MS : undefined,
    onFailure: reportFailure,
  });

  async function install(dataset: Dataset) {
    setInstalling((current) => ({ ...current, [dataset.id]: true }));
    setWatching(true);
    setError(null);
    try {
      await api.post(`/api/datasets/${dataset.id}/install`);
    } catch (failure) {
      reportFailure(failure);
      setError(failure instanceof Error ? failure.message : String(failure));
      setInstalling((current) => ({ ...current, [dataset.id]: false }));
      setWatching(false);
    }
  }

  useEffect(() => {
    const running = (datasets.data ?? []).some((dataset) => dataset.status === "installing");
    const claimed = Object.values(installing).some(Boolean);
    if (watching && !running && !claimed) {
      setWatching(false);
    }
  }, [datasets.data, installing, watching]);

  const all = datasets.data ?? [];
  // Only offer a domain that something is actually in.
  const presentCategories = useMemo(
    () => CATEGORY_ORDER.filter((name) => all.some((dataset) => dataset.category === name)),
    [all],
  );
  // Only offer a task something is actually in, in the order the table shows them.
  const presentTasks = useMemo(() => {
    const seen = new Set<string>();
    for (const dataset of all) {
      if (dataset.task !== "" && (category === ALL || dataset.category === category)) {
        seen.add(dataset.task);
      }
    }
    return [...seen].sort((left, right) => left.localeCompare(right, "zh-Hans-CN"));
  }, [all, category]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return all.filter((dataset) => {
      if (category !== ALL && dataset.category !== category) {
        return false;
      }
      if (task !== ALL && dataset.task !== task) {
        return false;
      }
      if (needle === "") {
        return true;
      }
      // Search what is on screen: the dataset, its task, and the config IDs it offers.
      return (
        dataset.id.toLowerCase().includes(needle) ||
        dataset.task.toLowerCase().includes(needle) ||
        dataset.config_name.toLowerCase().includes(needle) ||
        dataset.configs.some((config) => config.name.toLowerCase().includes(needle))
      );
    });
  }, [all, category, task, query]);

  function statusOf(dataset: Dataset): Dataset["status"] {
    if (dataset.status === "available" || dataset.status === "failed") {
      return dataset.status;
    }
    return installing[dataset.id] ? "installing" : dataset.status;
  }

  return (
    <>
      <PageHeader title={t("nav.datasets")}>
        <span className="resource-meta">
          {visible.length === all.length
            ? `${all.length}`
            : `${visible.length} / ${all.length}`}
        </span>
      </PageHeader>

      <div className="filter-row">
        <input
          className="input search-input"
          type="search"
          aria-label={t("datasets.search")}
          placeholder={t("datasets.searchPlaceholder")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="chip-row" role="group" aria-label={t("datasets.category")}>
          <button
            type="button"
            className="chip"
            aria-pressed={category === ALL}
            onClick={() => {
              setCategory(ALL);
              setTask(ALL);
            }}
          >
            {t("jobs.filterAll")}
          </button>
          {presentCategories.map((name) => (
            <button
              key={name}
              type="button"
              className="chip"
              aria-pressed={category === name}
              onClick={() => {
                setCategory(name);
                // A task belongs to one domain; switching domains invalidates the choice.
                setTask(ALL);
              }}
            >
              {t(CATEGORY_LABELS[name])}
            </button>
          ))}
        </div>
        {presentTasks.length > 1 && (
          <select
            className="input task-select"
            aria-label={t("datasets.task")}
            value={task}
            onChange={(event) => setTask(event.target.value)}
          >
            <option value={ALL}>{t("datasets.allTasks")}</option>
            {presentTasks.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        )}
      </div>
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
          {visible.map((dataset) => {
            const status = statusOf(dataset);
            return (
              <tr key={dataset.id}>
                <td>
                  <div className="resource-title">{dataset.name}</div>
                  <div className="resource-meta">
                    {dataset.task !== "" && <span className="task-tag">{dataset.task}</span>}
                    <span>{describeConfigs(dataset)}</span>
                  </div>
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
      {all.length > 0 && visible.length === 0 && (
        <p className="empty-state">{t("datasets.noMatch")}</p>
      )}
    </>
  );
}
