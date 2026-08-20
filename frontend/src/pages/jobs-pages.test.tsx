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
import { splitRow } from "../components/job-result";

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
  datasets: [],
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
  it(
    "resumes a dataset's own log from the last offset it was confirmed",
    { timeout: 12000 },
    async () => {
      const user = userEvent.setup();
      const requestedOffsets: number[] = [];
      let completed = 2;
      const jobAt = () => ({
        ...JOB,
        progress: { completed, total: 8 },
        datasets: [
          {
            name: "gsm8k",
            phase: "inferring",
            completed,
            total: 8,
            rate: null,
            counters: null,
            log_available: true,
            metrics: {},
            correct_count: null,
            total_count: null,
            started_at: "2026-08-18T12:00:01+00:00",
          },
        ],
      });
      server.use(
        http.get("/api/jobs/job-1", () => HttpResponse.json(jobAt())),
        http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
        http.get("/api/jobs/job-1/datasets/gsm8k/logs", ({ request }) => {
          const offset = Number(new URL(request.url).searchParams.get("offset") ?? "0");
          requestedOffsets.push(offset);
          return HttpResponse.json({
            offset: offset === 0 ? 24 : 40,
            text: offset === 0 ? "first line\n" : "second line\n",
          });
        }),
      );
    const fake = installFakeWebSocket();
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "查看详情" }));
    expect(await screen.findByText(/first line/)).toBeInTheDocument();

    // The next poll continues from the confirmed offset, so nothing is duplicated.
    expect(await screen.findByText(/second line/, {}, { timeout: 6000 })).toBeInTheDocument();
    await waitFor(() => expect(requestedOffsets).toContain(24));
    expect(screen.getByText(/first line/).textContent?.match(/first line/g)).toHaveLength(1);

    // A socket event only says "look again"; the job itself is re-read over REST.
    completed = 8;
    await waitFor(() => expect(fake.sockets).toHaveLength(1));
    fake.sockets[0].open();
    fake.sockets[0].emitJson({ type: "progress", completed: 8, total: 8 });
    expect((await screen.findAllByText(/8\/8/, {}, { timeout: 6000 })).length).toBeGreaterThan(0);
    fake.restore();
    },
  );

  it("restores everything from REST without any socket event", async () => {
    server.use(http.get("/api/jobs/job-1", () => HttpResponse.json(JOB)));
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/512 \/ 800/)).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    fake.restore();
  });

  it("keeps a running job current without any socket event", async () => {
    let progress = { completed: 2, total: 8 };
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, progress })),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/2 \/ 8/)).toBeInTheDocument();
    // No socket event is emitted; the page must still follow the job.
    progress = { completed: 8, total: 8 };

    expect(await screen.findByText(/8 \/ 8/, {}, { timeout: 6000 })).toBeInTheDocument();
    fake.restore();
  });

  it("shows each dataset's own progress, and its detail on demand", async () => {
    const user = userEvent.setup();
    const datasets = [
      {
        name: "ARC-e",
        phase: "finished",
        completed: 8,
        total: 8,
        rate: null,
        counters: null,
        log_available: true,
        metrics: {},
        correct_count: null,
        total_count: null,
        started_at: "2026-08-18T12:00:01+00:00",
      },
      {
        name: "math",
        phase: "inferring",
        completed: 37,
        total: 100,
        rate: "41.7 it/s",
        counters: { POST: 520, RECV: 510, FINISH: 500, FAIL: 2 },
        log_available: true,
        metrics: {},
        correct_count: null,
        total_count: null,
        started_at: "2026-08-18T12:00:01+00:00",
      },
    ];
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, datasets })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/datasets/:name/logs", ({ params }) =>
        HttpResponse.json({
          offset: params.name === "math" ? 15 : 0,
          text: params.name === "math" ? "inferring math\n" : "",
        }),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    // Two datasets, each with its own line: one through, one partway.
    expect(await screen.findByText("ARC-e")).toBeInTheDocument();
    expect(screen.getByText(/37\/100/)).toBeInTheDocument();
    expect(screen.getByText(/8\/8/)).toBeInTheDocument();
    expect(screen.getByText("推理中")).toBeInTheDocument();
    // The two denominators say which is which: datasets done, samples across them.
    expect(screen.getByText(/数据集 1\/2/)).toBeInTheDocument();
    expect(screen.getByText(/样本 45\/108/)).toBeInTheDocument();

    // The detail is one click away: rate, request counters, this dataset's own log.
    const rows = screen.getAllByRole("button", { name: "查看详情" });
    await user.click(rows[1]);
    expect(screen.getByText(/41.7 it\/s/)).toBeInTheDocument();
    expect(screen.getByText("POST")).toBeInTheDocument();
    expect(await screen.findByText(/inferring math/)).toBeInTheDocument();
    fake.restore();
  });

  it("says the stage instead of a full bar once inference is done and eval is not", async () => {
    // 100% through the whole evaluation stage reads as "finished"; the bar must not.
    const datasets = [
      {
        name: "gsm8k",
        phase: "evaluating",
        completed: 8,
        total: 8,
        rate: null,
        counters: null,
        log_available: true,
        metrics: {},
        correct_count: null,
        total_count: null,
        started_at: "2026-08-18T12:00:01+00:00",
      },
    ];
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, datasets })),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/datasets/:name/logs", () =>
        HttpResponse.json({ offset: 0, text: "" }),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/推理 8\/8/)).toBeInTheDocument();
    expect(screen.getByText(/评测中 0\/1/)).toBeInTheDocument();
    expect(screen.queryByText(/100%/)).not.toBeInTheDocument();
    fake.restore();
  });

  it("settles each dataset row into its score where its progress was", async () => {
    const datasets = [
      {
        name: "ARC-e",
        phase: "finished",
        completed: 8,
        total: 8,
        rate: null,
        counters: null,
        log_available: true,
        metrics: { accuracy: { value: 62.5, text_value: null, unit: null } },
        correct_count: 7,
        total_count: 8,
        started_at: "2026-08-18T12:00:01+00:00",
      },
      {
        name: "math",
        phase: "failed",
        completed: 37,
        total: 100,
        rate: null,
        counters: null,
        log_available: false,
        metrics: {},
        correct_count: null,
        total_count: null,
        started_at: "2026-08-18T12:00:01+00:00",
      },
    ];
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({ ...JOB, status: "succeeded", datasets }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    // The finished row leads with its named score; the failed row keeps where it stopped.
    // Auto-expanded, the score sits in the row and in the metrics block alike.
    expect(await screen.findAllByText("accuracy")).not.toHaveLength(0);
    expect((await screen.findAllByText("62.5")).length).toBeGreaterThan(0);
    expect(screen.getByText("(7/8)")).toBeInTheDocument();
    expect((await screen.findAllByText("失败")).length).toBeGreaterThan(0);
    fake.restore();
  });

  it("previews a finished dataset's samples, one verdict per answer", async () => {
    const user = userEvent.setup();
    const datasets = [
      {
        name: "gsm8k",
        phase: "finished",
        completed: 8,
        total: 8,
        rate: null,
        counters: null,
        log_available: true,
        metrics: { accuracy: { value: 50.0, text_value: null, unit: null } },
        correct_count: 4,
        total_count: 8,
        started_at: "2026-08-18T12:00:01+00:00",
      },
    ];
    server.use(
      http.get("/api/jobs/job-1", () =>
        HttpResponse.json({ ...JOB, status: "succeeded", datasets }),
      ),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/datasets/:name/samples", () =>
        HttpResponse.json({
          source: "eval_details",
          total: 2,
          samples: [
            {
              id: "0",
              prompt: "[HUMAN] Question 0",
              origin_prediction: "raw answer 0",
              prediction: "answer: 0",
              reference: "gold 0",
              correct: true,
            },
            {
              id: "1",
              prompt: "[HUMAN] Question 1",
              origin_prediction: "raw answer 1",
              prediction: "answer: 1",
              reference: "gold 1",
              correct: false,
            },
          ],
        }),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();
    await screen.findAllByText("50");
    expect(screen.getByText("(4/8)")).toBeInTheDocument();

    // A finished run's page is its results: the samples are already there, no clicking.
    // One line per sample: the scored answer, the reference, and the verdict.
    expect(await screen.findByText("answer: 0")).toBeInTheDocument();
    expect(screen.getByText("answer: 1")).toBeInTheDocument();
    expect(screen.getByTitle("正确").textContent).toBe("✓");
    expect(screen.getByTitle("错误").textContent).toBe("✗");

    // A sample row opens to the whole prompt and the raw output.
    await user.click(screen.getByText("answer: 0"));
    expect(await screen.findByText(/Question 0/)).toBeInTheDocument();
    expect(screen.getByText("raw answer 0")).toBeInTheDocument();

    // The wrong-only filter keeps the misses and drops the hits.
    await user.click(screen.getByRole("button", { name: /只看答错/ }));
    expect(screen.queryByText("answer: 0")).not.toBeInTheDocument();
    expect(screen.getByText("answer: 1")).toBeInTheDocument();
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
    expect(subtitle).toHaveTextContent("精度评测");
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

    expect(await screen.findByText("accuracy")).toBeInTheDocument();
    expect(screen.queryByText("SomeFutureMetric")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "展开其他指标" }));
    expect(screen.getByText("SomeFutureMetric")).toBeInTheDocument();

    // In a narrow rail the file name identifies the file; the path is the tooltip.
    expect(screen.getByRole("link", { name: /summary_1/ })).toHaveAttribute(
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
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () => HttpResponse.json([])),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/batch_size=16/)).toBeInTheDocument();
    expect(screen.getByText(/temperature=0.7/)).toBeInTheDocument();
    // A command line, one option per line: the flag and the value it ran with.
    const prompts = within(screen.getByText("--num-prompts").closest("li") ?? document.body);
    expect(prompts.getByText("8")).toBeInTheDocument();
    const workers = within(screen.getByText("--max-num-workers").closest("li") ?? document.body);
    expect(workers.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("--merge-ds")).toBeInTheDocument();
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
    expect(screen.getByText("14s")).toBeInTheDocument();
    fake.restore();
  });

  it("picks up the output files a run only writes as it ends", async () => {
    // A page opened while the job was still going has only ever seen an empty file list.
    // The job payload is polled and heals itself; this list was fetched once and never again,
    // so the files stayed missing until the reader reloaded the page by hand.
    let status = "running";
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () => HttpResponse.json([])),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json(
          status === "running"
            ? []
            : [
                {
                  id: "a1",
                  kind: "summary",
                  relative_path: "20260819_101550/summary/summary.csv",
                  content_type: "text/csv",
                },
              ],
        ),
      ),
      http.get("/api/jobs/job-1/artifacts/a1", () =>
        HttpResponse.text("dataset,metric,Qwen3-32B\ngsm8k,accuracy,50.00"),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    await screen.findByText("运行中");
    status = "succeeded";

    expect(await screen.findByText(/原始输出/, {}, { timeout: 6000 })).toBeInTheDocument();
    fake.restore();
  }, 20000);

  it("groups the output files and stops repeating the run directory", async () => {
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

    await screen.findByText(/原始输出/);
    // Every path of a run starts with the same run directory, so printing it eight times
    // pushes the part that differs off to the right.
    expect(screen.getByRole("link", { name: /summary_20260819_101550/ })).toHaveAttribute(
      "href",
      "/api/jobs/job-1/artifacts/a2",
    );
    expect(screen.getByText("汇总")).toBeInTheDocument();
    expect(screen.getByText("生成的配置")).toBeInTheDocument();
    fake.restore();
  });

  it("shows an older job's parameters rather than claiming it used defaults", async () => {
    // Jobs submitted before parameters were split stored one flat dict. Reading it with the
    // new keys finds nothing, and "all defaults" would be a false claim about that run.
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

    expect(await screen.findByText(/num_prompts=8/)).toBeInTheDocument();
    expect(screen.queryByText("全部沿用默认值")).not.toBeInTheDocument();
    fake.restore();
  });

  it("says what one evaluated dataset is in a line, not in a one-row table", async () => {
    // A table with a header and a single row of data is a table drawn for its own sake.
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () =>
        HttpResponse.json([
          { key: "gsm8k.accuracy", value: 82.5, text_value: null, unit: null },
        ]),
      ),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json([
          {
            id: "sum-txt",
            kind: "summary",
            relative_path: "20260819_101550/summary/summary.txt",
            content_type: "text/plain",
          },
          {
            id: "sum-csv",
            kind: "summary",
            relative_path: "20260819_101550/summary/summary.csv",
            content_type: "text/csv",
          },
        ]),
      ),
      // The .txt concatenates three formats with banner rules between them; the .csv is
      // the table on its own, so that is the one worth reading.
      http.get("/api/jobs/job-1/artifacts/sum-csv", () => HttpResponse.text("dataset,version,metric,mode,Qwen3-32B\ngsm8k,f588a9,accuracy,gen,82.50")),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    expect(await screen.findByText(/version f588a9/)).toBeInTheDocument();
    expect(screen.getByText(/mode gen/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    fake.restore();
  });

  it("gives several evaluated datasets the columns they need", async () => {
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () =>
        HttpResponse.json([
          { key: "gsm8k.accuracy", value: 82.5, text_value: null, unit: null },
        ]),
      ),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json([
          {
            id: "sum-txt",
            kind: "summary",
            relative_path: "20260819_101550/summary/summary.txt",
            content_type: "text/plain",
          },
          {
            id: "sum-csv",
            kind: "summary",
            relative_path: "20260819_101550/summary/summary.csv",
            content_type: "text/csv",
          },
        ]),
      ),
      // The .txt concatenates three formats with banner rules between them; the .csv is
      // the table on its own, so that is the one worth reading.
      http.get("/api/jobs/job-1/artifacts/sum-csv", () => HttpResponse.text("dataset,version,metric,mode,Qwen3-32B\ngsm8k,f588a9,accuracy,gen,82.50\nmath,aa11,accuracy,gen,41.00")),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    const summary = await screen.findByRole("table");
    expect(within(summary).getByRole("columnheader", { name: "Qwen3-32B" })).toBeInTheDocument();
    expect(within(summary).getByText("41.00")).toBeInTheDocument();
    fake.restore();
  });

  it("leaves the dataset and the metric out of the line that gives them context", async () => {
    // The heading names the dataset and the number is labelled with its metric, so
    // "dataset gsm8k · metric accuracy" spends its first half on what was just read.
    server.use(
      http.get("/api/jobs/job-1", () => HttpResponse.json({ ...JOB, status: "succeeded" })),
      http.get("/api/jobs/job-1/logs", () => HttpResponse.json({ offset: 0, text: "" })),
      http.get("/api/jobs/job-1/metrics", () =>
        HttpResponse.json([
          { key: "gsm8k.accuracy", value: 82.5, text_value: null, unit: null },
        ]),
      ),
      http.get("/api/jobs/job-1/artifacts", () =>
        HttpResponse.json([
          {
            id: "sum-csv",
            kind: "summary",
            relative_path: "20260819_101550/summary/summary.csv",
            content_type: "text/csv",
          },
        ]),
      ),
      http.get("/api/jobs/job-1/artifacts/sum-csv", () =>
        HttpResponse.text("dataset,version,metric,mode,Qwen3-32B\ngsm8k,f588a9,accuracy,gen,82.50"),
      ),
    );
    const fake = installFakeWebSocket();
    renderDetail();

    const context = await screen.findByText(/f588a9/);
    expect(context).toHaveTextContent("version f588a9 · mode gen");
    expect(context).not.toHaveTextContent("dataset");
    expect(context).not.toHaveTextContent("metric");
    fake.restore();
  });

  it("keeps a comma inside a quoted summary cell out of the columns", () => {
    // AISBench does not quote today, but a model name with a comma in it would silently
    // shift every column after it.
    expect(splitRow('a,"b,c",d')).toEqual(["a", "b,c", "d"]);
    expect(splitRow('"say ""hi""",x')).toEqual(['say "hi"', "x"]);
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

    // Scoped to the table: the sidebar links to these same jobs as recent work.
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("link", { name: "GSM8K 精度基线" })).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "MMLU 任务" })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("状态筛选"), "succeeded");

    expect(within(table).queryByRole("link", { name: "GSM8K 精度基线" })).not.toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "MMLU 任务" })).toBeInTheDocument();
  });

  it("offers the way back to recent work from every page", async () => {
    // The rail had markup for this and nothing ever filled it, so it sat empty on a
    // 244px column while the jobs it would list were one click away.
    server.use(http.get("/api/jobs", () => HttpResponse.json([JOB])));
    render(<App initialUser={ALICE} initialPath="/models" />);

    const rail = await screen.findByRole("navigation");
    expect(
      await within(rail).findByRole("link", { name: "GSM8K 精度基线" }),
    ).toHaveAttribute("href", "/jobs/job-1");
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
