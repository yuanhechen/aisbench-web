import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./app";

describe("App", () => {
  it("renders the authenticated navigation without dashboard cards", () => {
    render(<App initialUser={{ id: "u1", username: "alice" }} />);

    expect(screen.getByText("AISBench")).toBeInTheDocument();
    expect(screen.getByText("新建评测")).toBeInTheDocument();
    expect(screen.queryByText("团队管理")).not.toBeInTheDocument();
  });

  it("offers every equal-user destination and no administration entry", () => {
    render(<App initialUser={{ id: "u1", username: "alice" }} />);

    for (const label of ["新建评测", "我的任务", "对比分析", "我的模型", "共享数据集"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    for (const forbidden of ["团队管理", "系统管理", "管理员", "邀请"]) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument();
    }
  });

  it("marks only the active destination", () => {
    render(<App initialUser={{ id: "u1", username: "alice" }} activeKey="jobs" />);

    expect(screen.getByRole("button", { name: "我的任务" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "新建评测" })).not.toHaveAttribute("aria-current");
  });

  it("lists the current user's recent jobs under the navigation", () => {
    render(
      <App
        initialUser={{ id: "u1", username: "alice" }}
        recentJobs={[{ id: "j1", title: "GSM8K 精度" }]}
      />,
    );

    expect(screen.getByText("最近任务")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "GSM8K 精度" })).toHaveAttribute("href", "#/jobs/j1");
  });

  it("shows no navigation at all when signed out", () => {
    render(<App initialUser={null} />);

    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.getByText("请登录后继续")).toBeInTheDocument();
  });
});
