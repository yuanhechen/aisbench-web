import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../app";
import { I18nProvider } from "../i18n/i18n-context";
import { AuthProvider } from "../auth/auth-context";
import { installFakeWebSocket } from "../test/fake-websocket";
import { server } from "../test/server";
import { JobDetailPage } from "./job-detail-page";

const ALICE = { id: "u1", username: "alice" };

const JOB = {
  id: "job-1",
  name: "GSM8K 精度基线",
  mode: "accuracy",
  status: "running",
  queue_position: null,
  progress: { completed: 512, total: 800 },
  model: {
    name: "Qwen3",
    model_name: "Qwen3-32B",
    base_url: "http://127.0.0.1:8001/v1",
    config_name: "vllm_api_general_chat",
  },
  dataset: { id: "gsm8k", name: "GSM8K", config_name: "gsm8k_gen_0_shot" },
  parameters: {
    config_fields: { batch_size: 16 },
    generation_kwargs: { temperature: 0.7 },
    cli: { num_prompts: 8, max_num_workers: 1, merge_datasets: true },
  },
  exit_code: null,
  error_code: null,
  error_message: null,
  created_at: "2026-08-18T12:00:00+00:00",
  started_at: "2026-08-18T12:00:01+00:00",
  finished_at: null,
};

/** The fold holding the run log, named by its summary rather than by its position. */
function logFold(container: HTMLElement): HTMLElement | null {
  return (
    [...container.querySelectorAll("details")].find((fold) =>
      fold.querySelector("summary")?.textContent?.includes("运行日志"),
    ) ?? null
  );
}

function renderDetail(jobId = "job-1") {
  return render(
    <I18nProvider>
      <AuthProvider initialUser={ALICE}>
        <JobDetailPage jobId={jobId} />
      </AuthProvider>
    </I18nProvider>,
  );
}

beforeEach(() => localStorage.clear());

describe("job detail", () => {
  it("reconnects by fetching log bytes from the last offset", async () => {
    const requestedOffsets: number[] = [];
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json(JOB)),
      http.get("/api/jobs/job-1/logs", ({ request }) => {
        const offset = Number(new URL(request.url).searchParams.get("offset") ?? "0");
        requestedOffsets.push(offset);
        return HttpResponse.json({
          offset: offset === 0 ? 64 : 96,
          text: offset === 0 ? "startup\n" : "completed request batch\n",
        });
      }),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/512 \/ 800/)).toBeInTheDocument();
    await waitFor(() => expect(fake.sockets).toHaveLength(1));
    fake.sockets[0].open();
    fake.sockets[0].emitJson({ type: "log", offset: 120 });

    await waitFor(() => expect(requestedOffsets).toContain(64));
    expect(await screen.findByText(/completed request batch/)).toBeInTheDocument();
    // The first chunk is never refetched, so nothing is duplicated in the view.
    expect(screen.getByText(/startup/).textContent?.match(/startup/g)).toHaveLength(1);
    fake.restore();
  });

  it("restores everything from REST without any socket event", async () => {
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json(JOB)),
      http.get("/api/jobs/job-1/logs", () =>
        HttpResponse.json({ offset: 20, text: "restored line\n" }),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/512 \/ 800/)).toBeInTheDocument();
    expect(await screen.findByText(/restored line/)).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    fake.restore();
  });

  it("keeps a running job current without any socket event", async () => {
    let progress = { completed: 2, total: 8 };
    let logOffset = 0;
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, progress })),
      http.get("/api/jobs/job-1/logs", () => {
        const text = logOffset === 0 ? "first line\n" : "second line\n";
        logOffset += 10;
        return HttpResponse.json({ offset: logOffset, text });
      }),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/2 \/ 8/)).toBeInTheDocument();
    // No socket event is emitted; the page must still follow the job.
    progress = { completed: 8, total: 8 };

    expect(await screen.findByText(/8 \/ 8/, {}, { timeout: 6000 })).toBeInTheDocument();
    expect(await screen.findByText(/second line/, {}, { timeout: 6000 })).toBeInTheDocument();
    fake.restore();
  });

  it("stops polling once the job has finished", async () => {
    let requests = 0;
    server.use(
      http.get("/api/jobs/job-1", () => {
        requests += 1;
        return HttpResponse.json({ ...JOB, status: "succeeded" });
      }),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    await screen.findByText("已成功");
    const settled = requests;
    await new Promise((resolve) => setTimeout(resolve, 3000));

    // A finished job cannot change; polling it forever is pure noise.
    expect(requests).toBe(settled);
    fake.restore();
  });

  it("leaves out a model name the endpoint never detected", async () => {
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({ ...JOB, model: { ...JOB.model, model_name: "" } }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    const subtitle = await screen.findByText(/精度评测/);
    expect(subtitle).toHaveTextContent("精度评测 · GSM8K");
    // A trailing separator with nothing after it reads as a rendering fault.
    expect(subtitle.textContent?.trimEnd().endsWith("·")).toBe(false);
    fake.restore();
  });

  it("shows queue position and how many jobs are ahead", async () => {
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({ ...JOB, status: "queued", queue_position: 3, progress: null }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/队列位置 3/)).toBeInTheDocument();
    expect(screen.getByText(/前方任务数 2/)).toBeInTheDocument();
    fake.restore();
  });

  it("requires one confirmation before stopping a running job", async () => {
    const user = userEvent.setup();
    let cancelled = false;
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json(JOB)),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.post("/api/jobs/job-1/cancel", () => {
        cancelled = true;
        return HttpResponse.json({ ...JOB, status: "stopping" });
      }),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "停止任务" }));
    expect(cancelled).toBe(false);
    await user.click(screen.getByRole("button", { name: "确认停止" }));

    await waitFor(() => expect(cancelled).toBe(true));
    expect(await screen.findByText("停止中")).toBeInTheDocument();
    fake.restore();
  });

  it("offers no stop control once the job has finished", async () => {
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({ ...JOB, status: "succeeded", progress: { completed: 8, total: 8 } }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText("已成功")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "停止任务" })).not.toBeInTheDocument();
    fake.restore();
  });

  it("shows results, hides extra metrics until asked, and links artifacts by id", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({ ...JOB, status: "succeeded", progress: { completed: 8, total: 8 } }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () =>
        HttpResponse.json([
          { key: "gsm8k.accuracy", value: 82.5, text_value: null, unit: null },
          { key: "extra.SomeFutureMetric", value: 7, text_value: null, unit: null },
        ]),
      ),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json([
          {
            id: "artifact-1",
            kind: "summary",
            relative_path: "summary/summary_1.csv",
            content_type: "text/csv",
          },
        ]),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText("gsm8k.accuracy")).toBeInTheDocument();
    expect(screen.queryByText("SomeFutureMetric")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "展开其他指标" }));
    expect(screen.getByText("SomeFutureMetric")).toBeInTheDocument();

    expect(screen.getByRole("link", { name: "summary/summary_1.csv" })).toHaveAttribute(
      "href",
      "/api/jobs/job-1/artifacts/artifact-1",
    );
    fake.restore();
  });

  it("sandboxes an embedded visualization away from this origin", async () => {
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json([
          {
            id: "viz-1",
            kind: "visualization",
            relative_path: "report.html",
            content_type: "text/html",
          },
        ]),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    const frame = await screen.findByTitle("report.html");
    const sandbox = frame.getAttribute("sandbox") ?? "";
    expect(sandbox).toContain("allow-scripts");
    // allow-same-origin would hand the embedded page this origin's cookies and DOM.
    expect(sandbox).not.toContain("allow-same-origin");
    fake.restore();
  });

  it("prints the parameters instead of the word Object", async () => {
    // Parameters arrive in groups; stringifying a group renders "[object Object]", which
    // tells the reader nothing about what was run.
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    await user.click(await screen.findByText("配置快照"));
    expect(screen.getByText(/batch_size=16/)).toBeInTheDocument();
    expect(screen.getByText(/temperature=0.7/)).toBeInTheDocument();
    // A command line, written the way it would have been typed.
    expect(screen.getByText("--num-prompts 8 --max-num-workers 1 --merge-ds")).toBeInTheDocument();
    expect(screen.queryByText(/object Object/)).not.toBeInTheDocument();
    fake.restore();
  });

  it("drops the progress bar once there is nothing left to progress", async () => {
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({
          ...JOB,
          status: "succeeded",
          progress: { completed: 8, total: 8 },
          finished_at: "2026-08-18T12:00:15+00:00",
        }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const fake = installFakeWebSocket();
    const { container } = renderDetail();

    await screen.findByText("已成功");
    // The status label already says it finished; a full bar repeats it and says nothing.
    expect(container.querySelector(".progress-track")).toBeNull();
    expect(screen.getByText(/用时 14s/)).toBeInTheDocument();
    fake.restore();
  });

  it("opens the log when the log is the answer, and folds it when it is not", async () => {
    const failing = { ...JOB, status: "failed", error_message: "boom" };
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json(failing)),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 4, text: "trace" })),
    );
    const fake = installFakeWebSocket();
    const { container, unmount } = renderDetail();

    await screen.findByText("boom");
    expect(logFold(container)).toHaveAttribute("open");
    unmount();
    fake.restore();

    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const second = installFakeWebSocket();
    const done = renderDetail();

    await screen.findByText("已成功");
    expect(logFold(done.container)).not.toHaveAttribute("open");
    second.restore();
  });

  it("groups the output files and stops repeating the run directory", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json([
          {
            id: "a1",
            kind: "config",
            relative_path: "20260819_101550/configs/20260819_101550_1.py",
            content_type: "text/x-python",
          },
          {
            id: "a2",
            kind: "summary",
            relative_path: "20260819_101550/summary/summary_20260819_101550.md",
            content_type: "text/markdown",
          },
        ]),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    await user.click(await screen.findByText(/原始输出/));
    // Every path of a run starts with the same run directory, so printing it eight times
    // pushes the part that differs off to the right.
    expect(
      screen.getByRole("link", { name: "summary/summary_20260819_101550.md" }),
    ).toHaveAttribute("href", "/api/jobs/job-1/artifacts/a2");
    expect(screen.getByText("汇总")).toBeInTheDocument();
    expect(screen.getByText("生成的配置")).toBeInTheDocument();
    fake.restore();
  });

  it("shows an older job's parameters rather than claiming it used defaults", async () => {
    // Jobs submitted before parameters were split stored one flat dict. Reading it with the
    // new keys finds nothing, and "all defaults" would be a false claim about that run.
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({
          ...JOB,
          status: "succeeded",
          parameters: { num_prompts: 8, max_num_workers: 4 },
        }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    await user.click(await screen.findByText("配置快照"));
    expect(screen.getByText(/num_prompts=8/)).toBeInTheDocument();
    expect(screen.queryByText("全部沿用默认值")).not.toBeInTheDocument();
    fake.restore();
  });

  it("shows the failure reason a failed job carries", async () => {
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({
          ...JOB,
          status: "failed",
          error_code: "nonzero_exit",
          error_message: "AISBench exited with status 3",
        }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByRole("alert")).toHaveTextContent("status 3");
    fake.restore();
  });
});

describe("job list", () => {
  it("lists only the current user's jobs and filters them", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/jobs", () =>
        HttpResponse.json([
          JOB,
          {
            ...JOB,
            id: "job-2",
            name: "MMLU 任务",
            status: "succeeded",
            dataset: { id: "mmlu", name: "MMLU" },
          },
        ]),
      ),
    );
    render(<App initialUser={ALICE} initialPath="/jobs" />);

    expect(await screen.findByRole("link", { name: "GSM8K 精度基线" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "MMLU 任务" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("状态筛选"), "succeeded");

    expect(screen.queryByRole("link", { name: "GSM8K 精度基线" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "MMLU 任务" })).toBeInTheDocument();
  });

  it("uses a flat table with no metric cards", async () => {
    server.use(http.get("/api/jobs", () => HttpResponse.json([JOB])));
    render(<App initialUser={ALICE} initialPath="/jobs" />);

    const table = await screen.findByRole("table");
    expect(within(table).getByRole("link", { name: "GSM8K 精度基线" })).toBeInTheDocument();
    // Scoped to the table: the same word is also a filter option.
    expect(within(table).getByText("运行中")).toBeInTheDocument();
  });

  it("says so plainly when there is nothing to show", async () => {
    server.use(http.get("/api/jobs", () => HttpResponse.json([])));
    render(<App initialUser={ALICE} initialPath="/jobs" />);

    expect(await screen.findByText("还没有任务。")).toBeInTheDocument();
  });
});
