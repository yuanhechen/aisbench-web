import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children?: ReactNode;
}) {
  return (
    <header className="workspace-header">
      <div className="workspace-header-row">
        <div>
          <h1 className="workspace-title">{title}</h1>
          {subtitle !== undefined && <p className="workspace-subtitle">{subtitle}</p>}
        </div>
        {children}
      </div>
    </header>
  );
}
