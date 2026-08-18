import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../app";
import { server } from "../test/server";

const ALICE = { id: "u1", username: "alice" };
const MODEL = {
  id: "model-1",
  name: "Qwen3",
  host: "127.0.0.1",
  port: 8001,
  use_https: false,
  base_url: "http://127.0.0.1:8001/v1",
  model_name: "Qwen3-32B",
  has_api_key: true,
  request_timeout: 60,
  max_output_length: 512,
  is_active: true,
};
const GSM8K = {
  id: "gsm8k",
  name: "GSM8K",
  description: "math",
  config_name: "gsm8k_gen",
  accuracy_config: "gsm8k_accuracy",
  performance_config: "gsm8k_perf",
  status: "available",
  local_path: "/data/gsm8k",
  size_bytes: null,
  error_message: null,
  can_install: true,
};
const MMLU = {
  ...GSM8K,
  id: "mmlu",
  name: "MMLU",
  accuracy_config: "mmlu_accuracy",
  performance_config: null,
};

function renderAt(path: string) {
  return render(<App initialUser={ALICE} initialPath={path} />);
}

beforeEach(() => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL])),
    http.get("/api/datasets", () => HttpResponse.json([GSM8K, MMLU])),
    http.get("/api/jobs", () => HttpResponse.json([])),
  );
});

describe("new evaluation", () => {
  it("submits a performance job using a private model and shared dataset", async () => {
    const user = userEvent.setup();
    let submitted: unknown = null;
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        submitted = await request.json();
        return HttpResponse.json({ id: "job-1", status: "queued", queue_position: 1 }, { status: 201 });
      }),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await user.click(screen.getByRole("radio", { name: "性能评测" }));
    await user.selectOptions(screen.getByLabelText("数据集"), "gsm8k");
    await user.clear(screen.getByLabelText("请求数量"));
    await user.type(screen.getByLabelText("请求数量"), "32");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    expect(await screen.findByText("任务已进入队列")).toBeInTheDocument();
    expect(submitted).toMatchObject({
      model_endpoint_id: "model-1",
      dataset_id: "gsm8k",
      mode: "performance",
      parameters: { num_prompts: 32 },
    });
  });

  it("sends accuracy fields for an accuracy job", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, unknown> = {};
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        submitted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "job-1", status: "queued", queue_position: 3 }, { status: 201 });
      }),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await user.selectOptions(screen.getByLabelText("数据集"), "gsm8k");
    await user.clear(screen.getByLabelText("最大并行数"));
    await user.type(screen.getByLabelText("最大并行数"), "4");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await screen.findByText("任务已进入队列");
    expect(submitted.mode).toBe("accuracy");
    expect(submitted.parameters).toMatchObject({ max_num_workers: 4 });
    expect(screen.getByText(/队列位置/)).toHaveTextContent("3");
  });

  it("refuses to submit a mode the chosen dataset has no configuration for", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await user.selectOptions(screen.getByLabelText("数据集"), "mmlu");
    await user.click(screen.getByRole("radio", { name: "性能评测" }));

    expect(screen.getByRole("button", { name: "提交评测" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("性能");
  });

  it("cannot be submitted before a model and dataset are chosen", async () => {
    renderAt("/jobs/new");

    expect(await screen.findByRole("button", { name: "提交评测" })).toBeDisabled();
  });

  it("offers only installed datasets", async () => {
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([GSM8K, { ...MMLU, status: "not_installed" }]),
      ),
    );
    renderAt("/jobs/new");

    const select = await screen.findByLabelText("数据集");
    expect(within(select).getByRole("option", { name: /GSM8K/ })).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: /MMLU/ })).not.toBeInTheDocument();
  });

  it("shows the server's refusal instead of pretending the job queued", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/jobs", () =>
        HttpResponse.json({ detail: "this dataset is not installed yet" }, { status: 409 }),
      ),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await user.selectOptions(screen.getByLabelText("数据集"), "gsm8k");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not installed yet");
    expect(screen.queryByText("任务已进入队列")).not.toBeInTheDocument();
  });
});

describe("my models", () => {
  it("never asks for a model name in the form", async () => {
    const user = userEvent.setup();
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));

    expect(screen.queryByLabelText("模型名")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
    expect(screen.getByLabelText("IP 或主机名")).toBeInTheDocument();
    expect(screen.getByLabelText("端口")).toBeInTheDocument();
  });

  it("shows a not-yet-detected model instead of an empty gap", async () => {
    server.use(
      http.get("/api/models", () => HttpResponse.json([{ ...MODEL, model_name: "" }])),
    );
    renderAt("/models");

    expect(await screen.findByText("模型待探测")).toBeInTheDocument();
  });

  it("lists endpoints and never shows the stored key", async () => {
    renderAt("/models");

    expect(await screen.findByText("Qwen3")).toBeInTheDocument();
    expect(screen.getByText("Qwen3-32B")).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1:8001")).toBeInTheDocument();
    expect(screen.getByText("已保存")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret");
  });

  it("creates an endpoint through one modal", async () => {
    const user = userEvent.setup();
    let created: Record<string, unknown> = {};
    server.use(
      http.post("/api/models", async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...MODEL, id: "model-2", name: "new" }, { status: 201 });
      }),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));
    await user.type(screen.getByLabelText("IP 或主机名"), "127.0.0.1");
    await user.clear(screen.getByLabelText("端口"));
    await user.type(screen.getByLabelText("端口"), "9000");
    await user.type(screen.getByLabelText("API Key"), "top-secret");
    await user.type(screen.getByLabelText("显示名称"), "new");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(created.name).toBe("new"));
    // The address is what the user gives; the model name is never asked for.
    expect(created.host).toBe("127.0.0.1");
    expect(created.port).toBe(9000);
    expect(created.api_key).toBe("top-secret");
    expect(created).not.toHaveProperty("model_name");
    expect(created).not.toHaveProperty("base_url");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(document.body.textContent).not.toContain("top-secret");
  });

  it("reports a connectivity test as a diagnostic, not a page failure", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/models/model-1/test", () =>
        HttpResponse.json({
          ok: false,
          latency_ms: 12,
          message: "connection refused",
          models: [],
        }),
      ),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "测试连接" }));

    expect(await screen.findByText(/connection refused/)).toBeInTheDocument();
    expect(screen.getByText("Qwen3")).toBeInTheDocument();
  });

  it("deactivates an endpoint without deleting it", async () => {
    const user = userEvent.setup();
    let active = true;
    server.use(
      http.get("/api/models", () => HttpResponse.json([{ ...MODEL, is_active: active }])),
      http.patch("/api/models/model-1", () => {
        active = false;
        return HttpResponse.json({ ...MODEL, is_active: false });
      }),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "停用" }));

    expect(await screen.findByRole("button", { name: "启用" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  });
});

describe("shared datasets", () => {
  it("offers install only for a missing dataset with a verified source", async () => {
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([
          { ...GSM8K, status: "not_installed", local_path: null },
          { ...MMLU, status: "not_installed", local_path: null, can_install: false },
        ]),
      ),
    );
    renderAt("/datasets");

    const gsm8k = within(await screen.findByRole("row", { name: /GSM8K/ }));
    const mmlu = within(screen.getByRole("row", { name: /MMLU/ }));
    expect(gsm8k.getByRole("button", { name: "安装" })).toBeInTheDocument();
    expect(mmlu.queryByRole("button", { name: "安装" })).not.toBeInTheDocument();
  });

  it("never offers to delete a shared dataset", async () => {
    renderAt("/datasets");

    expect(await screen.findByText("GSM8K")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  });

  it("follows an install through to available", async () => {
    const user = userEvent.setup();
    let installed = false;
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([
          installed
            ? { ...GSM8K, status: "available" }
            : { ...GSM8K, status: "not_installed", local_path: null },
        ]),
      ),
      http.post("/api/datasets/gsm8k/install", () => {
        installed = true;
        return HttpResponse.json({ ...GSM8K, status: "installing" }, { status: 202 });
      }),
    );
    renderAt("/datasets");

    await user.click(await screen.findByRole("button", { name: "安装" }));

    expect(await screen.findByText("安装中")).toBeInTheDocument();
    expect(await screen.findByText("可用", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("shows why an install failed and allows another attempt", async () => {
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([
          { ...GSM8K, status: "failed", local_path: null, error_message: "checksum mismatch" },
        ]),
      ),
    );
    renderAt("/datasets");

    expect(await screen.findByText(/checksum mismatch/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});
