import type { ReactNode } from "react";
import {
  BarChart3,
  Database,
  GitCompare,
  ListChecks,
  PlusCircle,
} from "lucide-react";
import {
  BrowserRouter,
  MemoryRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";

import type { Job } from "./api/types";
import { useApiQuery } from "./api/use-query";
import { AuthProvider, useAuth } from "./auth/auth-context";
import type { CurrentUser } from "./auth/auth-context";
import { I18nProvider, useI18n } from "./i18n/i18n-context";
import type { MessageKey } from "./i18n/messages";
import { AuthPage } from "./pages/auth-page";
import { ComparisonPage } from "./pages/comparison-page";
import { DatasetsPage } from "./pages/datasets-page";
import { JobDetailRoute } from "./pages/job-detail-route";
import { JobsPage } from "./pages/jobs-page";
import { ModelsPage } from "./pages/models-page";
import { NewJobPage } from "./pages/new-job-page";

export type { CurrentUser };

export interface RecentJob {
  id: string;
  title: string;
}

interface NavigationItem {
  to: string;
  labelKey: MessageKey;
  icon: ReactNode;
}

// There is no team or system administration entry: every account has the same abilities.
export const NAVIGATION: NavigationItem[] = [
  { to: "/jobs/new", labelKey: "nav.newJob", icon: <PlusCircle size={16} aria-hidden /> },
  { to: "/jobs", labelKey: "nav.jobs", icon: <ListChecks size={16} aria-hidden /> },
  { to: "/comparison", labelKey: "nav.comparison", icon: <GitCompare size={16} aria-hidden /> },
  { to: "/models", labelKey: "nav.models", icon: <BarChart3 size={16} aria-hidden /> },
  { to: "/datasets", labelKey: "nav.datasets", icon: <Database size={16} aria-hidden /> },
];

export interface AppProps {
  /** Omit to resolve the session from /api/me; pass a value (or null) to skip that request. */
  initialUser?: CurrentUser | null;
  recentJobs?: RecentJob[];
  /** Set in tests to render one route directly; the browser uses real history. */
  initialPath?: string;
}

export function App({ initialUser, recentJobs = [], initialPath }: AppProps) {
  const content = (
    <I18nProvider>
      <AuthProvider initialUser={initialUser}>
        <AppContent recentJobs={recentJobs} />
      </AuthProvider>
    </I18nProvider>
  );

  return initialPath === undefined ? (
    <BrowserRouter>{content}</BrowserRouter>
  ) : (
    <MemoryRouter initialEntries={[initialPath]}>{content}</MemoryRouter>
  );
}

function AppContent({ recentJobs }: { recentJobs: RecentJob[] }) {
  const { t } = useI18n();
  const { user, loading } = useAuth();

  if (loading) {
    return <main className="workspace">{t("common.loading")}</main>;
  }
  if (user === null) {
    return <AuthPage />;
  }
  return <Shell user={user} recentJobs={recentJobs} />;
}

// Enough to get back to what you were just looking at, not a second jobs page.
const RECENT_SHOWN = 5;

function Shell({ user, recentJobs }: { user: CurrentUser; recentJobs: RecentJob[] }) {
  const { t, toggleLocale } = useI18n();
  const { logout } = useAuth();
  const jobs = useApiQuery<Job[]>("/api/jobs");
  const recent =
    recentJobs.length > 0
      ? recentJobs
      : (jobs.data ?? [])
          .slice(0, RECENT_SHOWN)
          .map((job) => ({ id: job.id, title: job.name === "" ? job.dataset.name : job.name }));

  return (
    <div className="app-shell">
      <nav className="sidebar" aria-label={t("app.name")}>
        <div className="sidebar-brand">{t("app.name")}</div>
        <div className="sidebar-nav">
          {NAVIGATION.map((item) => (
            <NavLink key={item.to} to={item.to} className="sidebar-link" end>
              {item.icon}
              <span>{t(item.labelKey)}</span>
            </NavLink>
          ))}
        </div>
        {recent.length > 0 && (
          <div>
            <div className="sidebar-section-title">{t("nav.recent")}</div>
            <div className="sidebar-recent">
              {recent.map((job) => (
                <NavLink key={job.id} className="sidebar-recent-item" to={`/jobs/${job.id}`}>
                  {job.title}
                </NavLink>
              ))}
            </div>
          </div>
        )}
        <div className="sidebar-footer">
          <span>{user.username}</span>
          <div className="sidebar-footer-actions">
            <button type="button" className="link-button" onClick={toggleLocale}>
              {t("common.language")}
            </button>
            <button type="button" className="link-button" onClick={() => void logout()}>
              {t("auth.signOut")}
            </button>
          </div>
        </div>
      </nav>
      <main className="workspace">
        <Routes>
          <Route path="/" element={<Navigate to="/jobs/new" replace />} />
          <Route path="/jobs/new" element={<NewJobPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:jobId" element={<JobDetailRoute />} />
          <Route path="/comparison" element={<ComparisonPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/datasets" element={<DatasetsPage />} />
          <Route path="*" element={<Navigate to="/jobs/new" replace />} />
        </Routes>
      </main>
    </div>
  );
}
