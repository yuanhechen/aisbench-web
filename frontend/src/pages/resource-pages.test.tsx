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
  base_url: "http://127.0.0.1:8001/v1",
  model_name: "Qwen3-32B",
  has_api_key: true,
  is_active: true,
};
const GSM8K_CONFIGS = [
  {
    name: "gsm8k_gen_4_shot_cot_chat_prompt",
    mode: "accuracy",
    shots: 4,
    chain_of_thought: true,
    chat_prompt: true,
  },
  {
    name: "gsm8k_gen_0_shot_cot_str",
    mode: "accuracy",
    shots: 0,
    chain_of_thought: true,
    chat_prompt: false,
  },
  {
    name: "gsm8k_gen_0_shot_cot_str_perf",
    mode: "performance",
    shots: 0,
    chain_of_thought: true,
    chat_prompt: false,
  },
];
const GSM8K = {
  id: "gsm8k",
  name: "gsm8k",
  description: "",
  config_name: "gsm8k_gen_4_shot_cot_chat_prompt",
  configs: GSM8K_CONFIGS,
  status: "available",
  local_path: "/data/gsm8k",
  size_bytes: null,
  error_message: null,
  can_install: true,
};
const MMLU = {
  ...GSM8K,
  id: "mmlu",
  name: "mmlu",
  config_name: "mmlu_gen_5_shot_chat_prompt",
  // AISBench ships no performance config for this dataset.
  configs: [
    {
      name: "mmlu_gen_5_shot_chat_prompt",
      mode: "accuracy",
      shots: 5,
      chain_of_thought: false,
      chat_prompt: true,
    },
  ],
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
      config_name: "gsm8k_gen_0_shot_cot_str_perf",
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

  it("offers the config variants AISBench ships for the chosen dataset and mode", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await user.selectOptions(screen.getByLabelText("数据集"), "gsm8k");

    const configs = screen.getByLabelText("评测配置");
    // Named for what the config actually does, with the file name kept underneath.
    expect(within(configs).getByRole("option", { name: "4-shot · CoT · chat" })).toBeInTheDocument();
    expect(
      within(configs).getByRole("option", { name: "0-shot · CoT · completion" }),
    ).toBeInTheDocument();
    // The performance variant belongs to the other mode.
    expect(within(configs).queryAllByRole("option")).toHaveLength(2);

    await user.click(screen.getByRole("radio", { name: "性能评测" }));
    expect(within(screen.getByLabelText("评测配置")).queryAllByRole("option")).toHaveLength(1);
  });

  it("submits the config the user picked, not just the first one", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, unknown> = {};
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        submitted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "j", status: "queued", queue_position: 1 }, { status: 201 });
      }),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await user.selectOptions(screen.getByLabelText("数据集"), "gsm8k");
    await user.selectOptions(screen.getByLabelText("评测配置"), "gsm8k_gen_0_shot_cot_str");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await screen.findByText("任务已进入队列");
    expect(submitted.config_name).toBe("gsm8k_gen_0_shot_cot_str");
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
    expect(within(select).getByRole("option", { name: /gsm8k/ })).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: /mmlu/ })).not.toBeInTheDocument();
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
  it("asks only for an address and a key", async () => {
    const user = userEvent.setup();
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));

    expect(screen.getByLabelText("服务地址")).toHaveAttribute(
      "placeholder",
      "http://127.0.0.1:8000/v1",
    );
    expect(screen.getByLabelText("API Key")).toBeInTheDocument();
    expect(screen.queryByLabelText("模型名")).not.toBeInTheDocument();
    // Per-run limits belong to the evaluation form, not to an endpoint.
    expect(screen.queryByLabelText("请求超时（秒）")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("默认最大输出长度")).not.toBeInTheDocument();
  });

  it("probes the typed address for connectivity and the model name", async () => {
    const user = userEvent.setup();
    let asked: Record<string, unknown> = {};
    server.use(
      http.post("/api/models/probe", async ({ request }) => {
        asked = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          ok: true,
          latency_ms: 14,
          message: "Model API reachable",
          models: ["Qwen3-32B"],
        });
      }),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));
    await user.type(screen.getByLabelText("服务地址"), "http://127.0.0.1:8000/v1");
    await user.type(screen.getByLabelText("API Key"), "top-secret");
    await user.click(screen.getByRole("button", { name: "探测" }));

    const result = await screen.findByRole("status");
    expect(result).toHaveTextContent("连接正常");
    expect(result).toHaveTextContent("14 ms");
    expect(result).toHaveTextContent("Qwen3-32B");
    expect(asked).toEqual({ base_url: "http://127.0.0.1:8000/v1", api_key: "top-secret" });
  });

  it("cannot probe before an address is typed", async () => {
    const user = userEvent.setup();
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));

    expect(screen.getByRole("button", { name: "探测" })).toBeDisabled();
  });

  it("reports an unreachable address inline and still allows saving", async () => {
    const user = userEvent.setup();
    let created = false;
    server.use(
      http.post("/api/models/probe", () =>
        HttpResponse.json({
          ok: false,
          latency_ms: 5,
          message: "Could not reach the model API: connection refused",
          models: [],
        }),
      ),
      http.post("/api/models", () => {
        created = true;
        return HttpResponse.json({ ...MODEL, model_name: "" }, { status: 201 });
      }),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));
    await user.type(screen.getByLabelText("服务地址"), "http://127.0.0.1:9999/v1");
    await user.click(screen.getByRole("button", { name: "探测" }));

    expect(await screen.findByRole("status")).toHaveTextContent("connection refused");
    // Design section 7.1: a temporarily unreachable endpoint must not block saving.
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(created).toBe(true));
  });

  it("drops a probe result once the address changes", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/models/probe", () =>
        HttpResponse.json({ ok: true, latency_ms: 9, message: "ok", models: ["Qwen3-32B"] }),
      ),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "新建模型端点" }));
    await user.type(screen.getByLabelText("服务地址"), "http://127.0.0.1:8000/v1");
    await user.click(screen.getByRole("button", { name: "探测" }));
    expect(await screen.findByRole("status")).toBeInTheDocument();

    await user.type(screen.getByLabelText("服务地址"), "2");

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
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
    expect(screen.getByText("http://127.0.0.1:8001/v1")).toBeInTheDocument();
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
    await user.type(screen.getByLabelText("服务地址"), "http://127.0.0.1:9000/v1");
    await user.type(screen.getByLabelText("API Key"), "top-secret");
    await user.type(screen.getByLabelText("显示名称"), "new");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(created.name).toBe("new"));
    // The address is what the user gives; the model name is never asked for.
    expect(created.base_url).toBe("http://127.0.0.1:9000/v1");
    expect(created.api_key).toBe("top-secret");
    expect(created).not.toHaveProperty("model_name");
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

  it("edits an existing endpoint and keeps the stored key when the box is left blank", async () => {
    const user = userEvent.setup();
    let patched: Record<string, unknown> = {};
    server.use(
      http.patch("/api/models/model-1", async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...MODEL, name: "renamed" });
      }),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "编辑" }));
    const address = screen.getByLabelText("服务地址");
    expect(address).toHaveValue("http://127.0.0.1:8001/v1");
    await user.clear(screen.getByLabelText("显示名称"));
    await user.type(screen.getByLabelText("显示名称"), "renamed");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(patched.name).toBe("renamed"));
    // A blank key box means "keep what is stored", not "clear it".
    expect(patched).not.toHaveProperty("api_key");
  });

  it("replaces the key when a new one is typed while editing", async () => {
    const user = userEvent.setup();
    let patched: Record<string, unknown> = {};
    server.use(
      http.patch("/api/models/model-1", async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(MODEL);
      }),
    );
    renderAt("/models");

    await user.click(await screen.findByRole("button", { name: "编辑" }));
    await user.type(screen.getByLabelText("API Key"), "rotated-key");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(patched.api_key).toBe("rotated-key"));
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

    const gsm8k = within(await screen.findByRole("row", { name: /gsm8k/ }));
    const mmlu = within(screen.getByRole("row", { name: /mmlu/ }));
    expect(gsm8k.getByRole("button", { name: "安装" })).toBeInTheDocument();
    expect(mmlu.queryByRole("button", { name: "安装" })).not.toBeInTheDocument();
  });

  it("never offers to delete a shared dataset", async () => {
    renderAt("/datasets");

    expect(await screen.findByText("gsm8k")).toBeInTheDocument();
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
