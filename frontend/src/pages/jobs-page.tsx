import { Link } from "react-router-dom";

import type { Job } from "../api/types";
import { useApiQuery } from "../api/use-query";
import { useAuth } from "../auth/auth-context";
import { useI18n } from "../i18n/i18n-context";
import { PageHeader } from "../components/page-header";

export function JobsPage() {
  const { t } = useI18n();
  const { reportFailure } = useAuth();
  const jobs = useApiQuery<Job[]>("/api/jobs", { onFailure: reportFailure });

  return (
    <>
      <PageHeader title={t("nav.jobs")} subtitle={t("jobs.subtitle")} />
      {jobs.data !== null && jobs.data.length === 0 && (
        <p className="empty-state">{t("jobs.empty")}</p>
      )}
      <div className="resource-list">
        {(jobs.data ?? []).map((job) => (
          <article key={job.id} className="resource-row">
            <div className="resource-main">
              <div className="resource-title">
                <Link to={`/jobs/${job.id}`}>{job.dataset.name}</Link>
              </div>
              <div className="resource-meta">
                <span>{job.mode}</span>
                <span>{job.model.model_name}</span>
                <span>{job.status}</span>
              </div>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}
