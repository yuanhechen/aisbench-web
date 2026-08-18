import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./app";

const ALICE = { id: "u1", username: "alice" };

describe("App", () => {
  it("renders the authenticated navigation without dashboard cards", () => {
    render(<App initialUser={ALICE} initialPath="/models" />);

    expect(screen.getByText("AISBench")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "新建评测" })).toBeInTheDocument();
    expect(screen.queryByText("团队管理")).not.toBeInTheDocument();
  });

  it("offers every equal-user destination and no administration entry", () => {
    render(<App initialUser={ALICE} initialPath="/models" />);

    for (const label of ["新建评测", "我的任务", "对比分析", "我的模型", "共享数据集"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    for (const forbidden of ["团队管理", "系统管理", "管理员", "邀请"]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });

  it("marks only the destination matching the current route", () => {
    render(<App initialUser={ALICE} initialPath="/jobs" />);

    expect(screen.getByRole("link", { name: "我的任务" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "新建评测" })).not.toHaveAttribute("aria-current");
  });

  it("lists the current user's recent jobs under the navigation", () => {
    render(
      <App
        initialUser={ALICE}
        initialPath="/models"
        recentJobs={[{ id: "j1", title: "GSM8K 精度" }]}
      />,
    );

    expect(screen.getByText("最近任务")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "GSM8K 精度" })).toHaveAttribute("href", "/jobs/j1");
  });

  it("shows the sign-in form and no navigation when signed out", () => {
    render(<App initialUser={null} />);

    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.getByLabelText("用户名")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
  });
});
