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
const DOMAIN = "domain:";
const TASK = "task:";

// Domains as published in the AISBench documentation; "other" is what it does not list.
const DOMAIN_LABELS: Record<string, MessageKey> = {
  llm: "datasets.categoryLlm",
  multimodal: "datasets.categoryMultimodal",
  dialogue: "datasets.categoryDialogue",
  synthetic: "datasets.categorySynthetic",
  custom: "datasets.categoryCustom",
  other: "datasets.categoryOther",
};
const DOMAIN_ORDER = ["llm", "multimodal", "dialogue", "synthetic", "custom", "other"];

const STATUS_LABELS: Record<Dataset["status"], MessageKey> = {
  not_installed: "datasets.notInstalled",
  installing: "datasets.installing",
  available: "datasets.available",
  failed: "datasets.failed",
  detected: "datasets.detected",
};

interface Group {
  domain: string;
  label: string;
  count: number;
  tasks: { name: string; count: number }[];
}

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
  // One selection over one tree: a whole domain, or one task inside it.
  const [selection, setSelection] = useState(ALL);
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

  // A task belongs to exactly one domain, so the filter is one tree, not two axes.
  const groups = useMemo<Group[]>(() => {
    const byDomain = new Map<string, Map<string, number>>();
    const totals = new Map<string, number>();
    for (const dataset of all) {
      const domain = dataset.category;
      totals.set(domain, (totals.get(domain) ?? 0) + 1);
      const tasks = byDomain.get(domain) ?? new Map<string, number>();
      if (dataset.task !== "") {
        tasks.set(dataset.task, (tasks.get(dataset.task) ?? 0) + 1);
      }
      byDomain.set(domain, tasks);
    }
    return DOMAIN_ORDER.filter((domain) => totals.has(domain)).map((domain) => {
      const count = totals.get(domain) ?? 0;
      const tasks = [...(byDomain.get(domain) ?? new Map())]
        .map(([name, taskCount]) => ({ name, count: taskCount }))
        .sort((left, right) => left.name.localeCompare(right.name, "zh-Hans-CN"));
      return {
        domain,
        label: t(DOMAIN_LABELS[domain]),
        count,
        // One task covering the whole domain narrows nothing: listing it would offer the
        // same set twice under two names.
        tasks: tasks.length === 1 && tasks[0].count === count ? [] : tasks,
      };
    });
  }, [all, t]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return all.filter((dataset) => {
      if (selection.startsWith(DOMAIN) && dataset.category !== selection.slice(DOMAIN.length)) {
        return false;
      }
      if (selection.startsWith(TASK) && dataset.task !== selection.slice(TASK.length)) {
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
  }, [all, selection, query]);

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

      {error !== null && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <div className="filter-row">
        <input
          className="input search-input"
          type="search"
          aria-label={t("datasets.search")}
          placeholder={t("datasets.searchPlaceholder")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select
          className="input filter-select"
          aria-label={t("datasets.filter")}
          value={selection}
          onChange={(event) => setSelection(event.target.value)}
        >
          <option value={ALL}>
            {t("datasets.allDatasets")}（{all.length}）
          </option>
          {groups.map((group) => (
            <optgroup key={group.domain} label={group.label}>
              <option value={`${DOMAIN}${group.domain}`}>
                {group.label}（{group.count}）
              </option>
              {group.tasks.map((task) => (
                <option key={task.name} value={`${TASK}${task.name}`}>
                  {"\u2003"}
                  {task.name}（{task.count}）
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

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
