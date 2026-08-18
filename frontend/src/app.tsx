import type { ReactNode } from "react";
import {
  BarChart3,
  Database,
  GitCompare,
  ListChecks,
  PlusCircle,
} from "lucide-react";

export interface CurrentUser {
  id: string;
  username: string;
}

export interface RecentJob {
  id: string;
  title: string;
}

export interface NavigationItem {
  key: string;
  label: string;
  icon: ReactNode;
}

// There is no team or system administration entry: every account has the same abilities.
export const NAVIGATION: NavigationItem[] = [
  { key: "new-job", label: "新建评测", icon: <PlusCircle size={16} aria-hidden /> },
  { key: "jobs", label: "我的任务", icon: <ListChecks size={16} aria-hidden /> },
  { key: "comparison", label: "对比分析", icon: <GitCompare size={16} aria-hidden /> },
  { key: "models", label: "我的模型", icon: <BarChart3 size={16} aria-hidden /> },
  { key: "datasets", label: "共享数据集", icon: <Database size={16} aria-hidden /> },
];

export interface AppProps {
  initialUser?: CurrentUser | null;
  recentJobs?: RecentJob[];
  activeKey?: string;
  children?: ReactNode;
}

export function App({
  initialUser = null,
  recentJobs = [],
  activeKey = "new-job",
  children,
}: AppProps) {
  if (initialUser === null) {
    return (
      <main className="workspace">
        <header className="workspace-header">
          <h1 className="workspace-title">AISBench</h1>
          <p className="workspace-subtitle">请登录后继续</p>
        </header>
        {children}
      </main>
    );
  }

  return (
    <div className="app-shell">
      <nav className="sidebar" aria-label="主导航">
        <div className="sidebar-brand">AISBench</div>
        <div className="sidebar-nav">
          {NAVIGATION.map((item) => (
            <button
              key={item.key}
              type="button"
              className="sidebar-link"
              aria-current={item.key === activeKey ? "page" : undefined}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </div>
        {recentJobs.length > 0 && (
          <div>
            <div className="sidebar-section-title">最近任务</div>
            <div className="sidebar-recent">
              {recentJobs.map((job) => (
                <a key={job.id} className="sidebar-recent-item" href={`#/jobs/${job.id}`}>
                  {job.title}
                </a>
              ))}
            </div>
          </div>
        )}
        <div className="sidebar-footer">{initialUser.username}</div>
      </nav>
      <main className="workspace">{children}</main>
    </div>
  );
}
