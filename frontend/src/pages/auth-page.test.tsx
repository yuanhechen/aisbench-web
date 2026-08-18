import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../app";
import { server } from "../test/server";

const ALICE = { id: "u1", username: "alice", created_at: "2026-08-17", last_login_at: null };

function acceptRegistration() {
  server.use(http.post("/api/auth/register", () => HttpResponse.json(ALICE, { status: 201 })));
}

describe("authentication", () => {
  beforeEach(() => localStorage.clear());

  it("registers and enters the application", async () => {
    const user = userEvent.setup();
    acceptRegistration();
    render(<App />);

    await user.click(await screen.findByRole("tab", { name: "注册" }));
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "password1");
    await user.click(screen.getByRole("button", { name: "创建账号" }));

    expect(await screen.findByRole("link", { name: "新建评测" })).toBeInTheDocument();
  });

  it("signs in an existing account", async () => {
    const user = userEvent.setup();
    server.use(http.post("/api/auth/login", () => HttpResponse.json(ALICE)));
    render(<App />);

    await user.type(await screen.findByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "password1");
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("link", { name: "我的任务" })).toBeInTheDocument();
  });

  it("shows the server's reason inline instead of a generic failure", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/auth/register", () =>
        HttpResponse.json({ detail: "username already exists" }, { status: 409 }),
      ),
    );
    render(<App />);

    await user.click(await screen.findByRole("tab", { name: "注册" }));
    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "password1");
    await user.click(screen.getByRole("button", { name: "创建账号" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("username already exists");
    expect(screen.queryByRole("link", { name: "新建评测" })).not.toBeInTheDocument();
  });

  it("restores a session from /api/me without asking again", async () => {
    server.use(http.get("/api/me", () => HttpResponse.json(ALICE)));
    render(<App />);

    expect(await screen.findByRole("link", { name: "新建评测" })).toBeInTheDocument();
    expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
  });

  it("returns to the sign-in form after signing out", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/me", () => HttpResponse.json(ALICE)),
      http.post("/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
    );
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "退出登录" }));

    expect(await screen.findByLabelText("密码")).toBeInTheDocument();
  });

  it("drops the session when the API reports the cookie is no longer valid", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/me", () => HttpResponse.json(ALICE)),
      http.get("/api/jobs", () =>
        HttpResponse.json({ detail: "not authenticated" }, { status: 401 }),
      ),
    );
    render(<App />);

    await user.click(await screen.findByRole("link", { name: "我的任务" }));

    await waitFor(() => expect(screen.getByLabelText("密码")).toBeInTheDocument());
  });
});

describe("language", () => {
  beforeEach(() => localStorage.clear());

  it("starts in Chinese and switches to English", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/me", () => HttpResponse.json(ALICE)));
    render(<App />);

    expect(await screen.findByRole("link", { name: "新建评测" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "English" }));

    expect(screen.getByRole("link", { name: "New Evaluation" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "新建评测" })).not.toBeInTheDocument();
  });

  it("remembers the chosen language across reloads", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/me", () => HttpResponse.json(ALICE)));
    const first = render(<App />);

    await user.click(await first.findByRole("button", { name: "English" }));
    first.unmount();
    render(<App />);

    expect(await screen.findByRole("link", { name: "New Evaluation" })).toBeInTheDocument();
  });
});
