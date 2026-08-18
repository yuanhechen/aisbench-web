import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  BarChart3,
  Database,
  GitCompare,
  ListChecks,
  PlusCircle,
} from "lucide-react";

import { api } from "./api/client";
import { AuthProvider, useAuth } from "./auth/auth-context";
import type { CurrentUser } from "./auth/auth-context";
import { I18nProvider, useI18n } from "./i18n/i18n-context";
import type { MessageKey } from "./i18n/messages";
import { AuthPage } from "./pages/auth-page";

export type { CurrentUser };

export interface RecentJob {
  id: string;
  title: string;
}

interface NavigationItem {
  key: string;
  labelKey: MessageKey;
  icon: ReactNode;
}

// There is no team or system administration entry: every account has the same abilities.
export const NAVIGATION: NavigationItem[] = [
  { key: "new-job", labelKey: "nav.newJob", icon: <PlusCircle size={16} aria-hidden /> },
  { key: "jobs", labelKey: "nav.jobs", icon: <ListChecks size={16} aria-hidden /> },
  { key: "comparison", labelKey: "nav.comparison", icon: <GitCompare size={16} aria-hidden /> },
  { key: "models", labelKey: "nav.models", icon: <BarChart3 size={16} aria-hidden /> },
  { key: "datasets", labelKey: "nav.datasets", icon: <Database size={16} aria-hidden /> },
];

export interface AppProps {
  /** Omit to resolve the session from /api/me; pass a value (or null) to skip that request. */
  initialUser?: CurrentUser | null;
  recentJobs?: RecentJob[];
  activeKey?: string;
  children?: ReactNode;
}

export function App({ initialUser, recentJobs = [], activeKey = "new-job", children }: AppProps) {
  return (
    <I18nProvider>
      <AuthProvider initialUser={initialUser}>
        <AppContent recentJobs={recentJobs} activeKey={activeKey}>
          {children}
        </AppContent>
      </AuthProvider>
    </I18nProvider>
  );
}

function AppContent({
  recentJobs,
  activeKey,
  children,
}: {
  recentJobs: RecentJob[];
  activeKey: string;
  children?: ReactNode;
}) {
  const { t } = useI18n();
  const { user, loading } = useAuth();
  const [active, setActive] = useState(activeKey);

  useEffect(() => setActive(activeKey), [activeKey]);

  if (loading) {
    return <main className="workspace">{t("common.loading")}</main>;
  }
  if (user === null) {
    return <AuthPage />;
  }
  return (
    <Shell user={user} recentJobs={recentJobs} active={active} onNavigate={setActive}>
      {children}
    </Shell>
  );
}

function Shell({
  user,
  recentJobs,
  active,
  onNavigate,
  children,
}: {
  user: CurrentUser;
  recentJobs: RecentJob[];
  active: string;
  onNavigate: (key: string) => void;
  children?: ReactNode;
}) {
  const { t, toggleLocale } = useI18n();
  const { logout, reportFailure } = useAuth();

  async function handleNavigate(key: string) {
    onNavigate(key);
    if (key !== "jobs") {
      return;
    }
    try {
      // Any 401 here means the cookie expired; the session must not look alive.
      await api.get("/api/jobs");
    } catch (failure) {
      reportFailure(failure);
    }
  }

  return (
    <div className="app-shell">
      <nav className="sidebar" aria-label="AISBench">
        <div className="sidebar-brand">{t("app.name")}</div>
        <div className="sidebar-nav">
          {NAVIGATION.map((item) => (
            <button
              key={item.key}
              type="button"
              className="sidebar-link"
              aria-current={item.key === active ? "page" : undefined}
              onClick={() => void handleNavigate(item.key)}
            >
              {item.icon}
              <span>{t(item.labelKey)}</span>
            </button>
          ))}
        </div>
        {recentJobs.length > 0 && (
          <div>
            <div className="sidebar-section-title">{t("nav.recent")}</div>
            <div className="sidebar-recent">
              {recentJobs.map((job) => (
                <a key={job.id} className="sidebar-recent-item" href={`#/jobs/${job.id}`}>
                  {job.title}
                </a>
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
      <main className="workspace">{children}</main>
    </div>
  );
}
