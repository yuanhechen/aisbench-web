import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { AuthProvider } from "../auth/auth-context";
import { I18nProvider } from "../i18n/i18n-context";
import { server } from "../test/server";
import { ComparisonPage } from "./comparison-page";

const ALICE = { id: "u1", username: "alice" };

function job(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    mode: "accuracy",
    status: "succeeded",
    queue_position: null,
    progress: null,
    model: { name: "Qwen3", model_name: "Qwen3-32B", base_url: "http://h/v1" },
    dataset: { id: "gsm8k", name: "GSM8K" },
    parameters: {},
    exit_code: 0,
    error_code: null,
    error_message: null,
    created_at: "2026-08-18T12:00:00+00:00",
    started_at: null,
    finished_at: "2026-08-18T12:10:00+00:00",
    ...overrides,
  };
}

function renderPage() {
  return render(
    <I18nProvider>
      <AuthProvider initialUser={ALICE}>
        <ComparisonPage />
      </AuthProvider>
    </I18nProvider>,
  );
}

beforeEach(() => localStorage.clear());

describe("comparison", () => {
  it("shows aligned metrics and the API's incompatibility warnings", async () => {
    const user = userEvent.setup();
    let requested: Record<string, unknown> = {};
    server.use(
      http.get("/api/jobs", () =>
        HttpResponse.json([
          job("j1"),
          job("j2", { mode: "performance", dataset: { id: "gsm8k", name: "GSM8K" } }),
        ]),
      ),
      http.post("/api/comparisons", async ({ request }) => {
        requested = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          jobs: [
            { id: "j1", mode: "accuracy", model: "Qwen3-32B", dataset: "GSM8K" },
            { id: "j2", mode: "performance", model: "Qwen3-32B", dataset: "GSM8K" },
          ],
          rows: [{ key: "gsm8k.accuracy", unit: null, values: { j1: 82.5, j2: null } }],
          warnings: ["部分指标不可直接比较"],
        });
      }),
    );
    renderPage();

    await user.click(await screen.findByLabelText(/j1/));
    await user.click(screen.getByLabelText(/j2/));
    await user.click(screen.getByRole("button", { name: "开始对比" }));

    expect(await screen.findByText("gsm8k.accuracy")).toBeInTheDocument();
    expect(screen.getByText("部分指标不可直接比较")).toBeInTheDocument();
    expect(requested).toEqual({ job_ids: ["j1", "j2"] });
  });

  it("leaves a value blank rather than inventing one", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs", () => HttpResponse.json([job("j1"), job("j2")])),
      http.post("/api/comparisons", () =>
        HttpResponse.json({
          jobs: [
            { id: "j1", mode: "accuracy", model: "Qwen3-32B", dataset: "GSM8K" },
            { id: "j2", mode: "accuracy", model: "Qwen3-32B", dataset: "GSM8K" },
          ],
          rows: [{ key: "gsm8k.pass@1", unit: null, values: { j1: 64, j2: null } }],
          warnings: [],
        }),
      ),
    );
    renderPage();

    await user.click(await screen.findByLabelText(/j1/));
    await user.click(screen.getByLabelText(/j2/));
    await user.click(screen.getByRole("button", { name: "开始对比" }));

    const row = await screen.findByRole("row", { name: /gsm8k.pass@1/ });
    // The metric name is a row header, so the cells are the per-job values.
    const cells = within(row).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("64");
    expect(cells[1].textContent).toBe("");
  });

  it("requires between two and eight jobs before comparing", async () => {
    const user = userEvent.setup();
    server.use(http.get("/api/jobs", () => HttpResponse.json([job("j1"), job("j2")])));
    renderPage();

    expect(await screen.findByRole("button", { name: "开始对比" })).toBeDisabled();
    await user.click(screen.getByLabelText(/j1/));
    expect(screen.getByRole("button", { name: "开始对比" })).toBeDisabled();
    await user.click(screen.getByLabelText(/j2/));
    expect(screen.getByRole("button", { name: "开始对比" })).toBeEnabled();
  });

  it("offers only finished jobs, because nothing else has results", async () => {
    server.use(
      http.get("/api/jobs", () =>
        HttpResponse.json([job("j1"), job("j2", { status: "running" })]),
      ),
    );
    renderPage();

    expect(await screen.findByLabelText(/j1/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/j2/)).not.toBeInTheDocument();
  });

  it("shows the server's refusal instead of an empty table", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs", () => HttpResponse.json([job("j1"), job("j2")])),
      http.post("/api/comparisons", () =>
        HttpResponse.json({ detail: "job not found" }, { status: 404 }),
      ),
    );
    renderPage();

    await user.click(await screen.findByLabelText(/j1/));
    await user.click(screen.getByLabelText(/j2/));
    await user.click(screen.getByRole("button", { name: "开始对比" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("job not found");
  });

  it("draws a bar only when every value in the row is numeric", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs", () => HttpResponse.json([job("j1"), job("j2")])),
      http.post("/api/comparisons", () =>
        HttpResponse.json({
          jobs: [
            { id: "j1", mode: "accuracy", model: "Qwen3-32B", dataset: "GSM8K" },
            { id: "j2", mode: "accuracy", model: "Qwen3-32B", dataset: "GSM8K" },
          ],
          rows: [
            { key: "complete.metric", unit: null, values: { j1: 80, j2: 40 } },
            { key: "partial.metric", unit: null, values: { j1: 80, j2: null } },
          ],
          warnings: [],
        }),
      ),
    );
    renderPage();

    await user.click(await screen.findByLabelText(/j1/));
    await user.click(screen.getByLabelText(/j2/));
    await user.click(screen.getByRole("button", { name: "开始对比" }));

    const complete = await screen.findByRole("row", { name: /complete.metric/ });
    const partial = screen.getByRole("row", { name: /partial.metric/ });
    await waitFor(() =>
      expect(complete.querySelectorAll(".metric-bar").length).toBeGreaterThan(0),
    );
    expect(partial.querySelectorAll(".metric-bar")).toHaveLength(0);
  });
});
