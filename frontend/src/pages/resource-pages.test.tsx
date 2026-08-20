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
    method: "gen",
    shots: 4,
    chain_of_thought: true,
    chat_prompt: true,
    alias_of: "",
  },
  {
    name: "gsm8k_gen_0_shot_cot_str",
    mode: "accuracy",
    method: "gen",
    shots: 0,
    chain_of_thought: true,
    chat_prompt: false,
    alias_of: "",
  },
  // Same shots, same prompt style, different evaluation method.
  {
    name: "gsm8k_ppl_0_shot_str",
    mode: "accuracy",
    method: "ppl",
    shots: 0,
    chain_of_thought: false,
    chat_prompt: false,
    alias_of: "",
  },
  {
    name: "gsm8k_gen_0_shot_cot_str_perf",
    mode: "performance",
    method: "gen",
    shots: 0,
    chain_of_thought: true,
    chat_prompt: false,
    alias_of: "",
  },
];
const GSM8K = {
  id: "gsm8k",
  name: "gsm8k",
  description: "",
  config_name: "gsm8k_gen_4_shot_cot_chat_prompt",
  category: "llm",
  task: "数学推理",
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
  category: "llm",
  task: "多学科理解（英文）",
  config_name: "mmlu_gen_5_shot_chat_prompt",
  // AISBench ships no performance config for this dataset.
  configs: [
    {
      name: "mmlu_gen_5_shot_chat_prompt",
      mode: "accuracy",
      method: "gen",
      shots: 5,
      chain_of_thought: false,
      chat_prompt: true,
      alias_of: "",
    },
  ],
};

function renderAt(path: string) {
  return render(<App initialUser={ALICE} initialPath={path} />);
}

/** Datasets are picked from a dropdown: open it, click an option, keep it open for more. */
async function pickDatasets(user: ReturnType<typeof userEvent.setup>, names: string[]) {
  const box = await screen.findByLabelText("数据集");
  await user.click(box);
  for (const name of names) {
    await user.click(screen.getByRole("option", { name: new RegExp(name) }));
  }
  return box;
}

/** The variant select of one picked dataset, labelled by the dataset it belongs to. */
function variantSelect(dataset: string): HTMLElement {
  return screen.getByRole("combobox", { name: new RegExp(dataset) });
}

// The real config files do not declare the same fields, and neither do these.
const MODEL_CONFIGS = [
  {
    name: "vllm_api_general_chat",
    family: "vllm_api",
    class_name: "VLLMCustomAPIChat",
    stream: false,
    default_for: "accuracy",
    fields: [
      { name: "max_out_len", default: 512, kind: "integer" },
      { name: "batch_size", default: 1, kind: "integer" },
      { name: "returns_tool_calls", default: false, kind: "boolean" },
    ],
    generation_fields: [{ name: "temperature", default: 0.01, kind: "number" }],
  },
  {
    name: "vllm_api_stream_chat",
    family: "vllm_api",
    class_name: "VLLMCustomAPIChat",
    stream: true,
    default_for: "performance",
    fields: [
      { name: "max_out_len", default: 512, kind: "integer" },
      { name: "batch_size", default: 1, kind: "integer" },
      { name: "request_rate", default: 0, kind: "integer" },
    ],
    generation_fields: [
      { name: "temperature", default: 0.01, kind: "number" },
      { name: "ignore_eos", default: false, kind: "boolean" },
    ],
  },
];

beforeEach(() => {
  server.use(
    http.get("/api/models", () => HttpResponse.json([MODEL])),
    http.get("/api/models/configs", () => HttpResponse.json(MODEL_CONFIGS)),
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
    await pickDatasets(user, ["gsm8k"]);
    await user.clear(screen.getByLabelText("--num-prompts"));
    await user.type(screen.getByLabelText("--num-prompts"), "32");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted).not.toBeNull());
    expect(submitted).toMatchObject({
      model_endpoint_id: "model-1",
      dataset_ids: ["gsm8k"],
      mode: "performance",
      // No variant was chosen, so the mode's own default runs; the body says nothing.
      config_names: {},
      parameters: { cli: { num_prompts: 32 } },
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
    await pickDatasets(user, ["gsm8k"]);
    await user.clear(screen.getByLabelText("--max-num-workers"));
    await user.type(screen.getByLabelText("--max-num-workers"), "4");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted.mode).toBe("accuracy"));
    expect(submitted.parameters).toMatchObject({ cli: { max_num_workers: 4 } });
  });

  it("names a config once, without restating what the name already says", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);

    // "gen", "4_shot", "cot" and "chat" are in the file name; repeating them adds nothing.
    expect(
      within(variantSelect("gsm8k")).getByRole("option", {
        name: "gsm8k_gen_0_shot_cot_str",
      }),
    ).toBeInTheDocument();

    // Full names, sorted flat: the family prefix groups them without a heading repeating it.
    const models = screen.getByLabelText("模型配置");
    expect(models.querySelectorAll("optgroup")).toHaveLength(0);
    expect(
      within(models).getByRole("option", { name: "vllm_api_general_chat" }),
    ).toBeInTheDocument();
  });

  it("lets the model class be chosen, as the command line does", async () => {
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
    await user.selectOptions(screen.getByLabelText("模型配置"), "vllm_api_stream_chat");
    await pickDatasets(user, ["gsm8k"]);
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted.model_config_name).toBe("vllm_api_stream_chat"));
  });

  it("leaves the model class to the evaluation type when it is not chosen", async () => {
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
    await pickDatasets(user, ["gsm8k"]);
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted.model_config_name).toBeNull());
  });

  it("offers the config variants AISBench ships for the chosen dataset and mode", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);

    const configs = variantSelect("gsm8k");
    // The file name is the whole label: every attribute it could carry is already in it.
    expect(
      within(configs).getByRole("option", { name: "gsm8k_gen_4_shot_cot_chat_prompt" }),
    ).toBeInTheDocument();
    // Two configs that share every derived attribute must remain separately selectable.
    expect(
      within(configs).getByRole("option", { name: /^gsm8k_gen_0_shot_cot_str/ }),
    ).toBeInTheDocument();
    expect(
      within(configs).getByRole("option", { name: /^gsm8k_ppl_0_shot_str/ }),
    ).toBeInTheDocument();
    // The performance variant belongs to the other mode.
    expect(within(configs).queryAllByRole("option")).toHaveLength(3);

    await user.click(screen.getByRole("radio", { name: "性能评测" }));
    // One variant in this mode means there is nothing to choose; the select steps aside.
    expect(screen.queryByRole("combobox", { name: /gsm8k/ })).not.toBeInTheDocument();
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
    await pickDatasets(user, ["gsm8k"]);
    await user.selectOptions(variantSelect("gsm8k"), "gsm8k_gen_0_shot_cot_str");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() =>
      expect(submitted.config_names).toEqual({ gsm8k: "gsm8k_gen_0_shot_cot_str" }),
    );
  });

  it("refuses to submit a mode the chosen dataset has no configuration for", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["mmlu"]);
    await user.click(screen.getByRole("radio", { name: "性能评测" }));

    expect(screen.getByRole("button", { name: "提交评测" })).toBeDisabled();
    // The picked row itself explains what is missing, at the place it was picked.
    expect(screen.getByRole("alert")).toHaveTextContent("该数据集无此模式配置");
  });

  it("does not repeat the model when the endpoint is named after it", async () => {
    server.use(
      http.get("/api/models", () =>
        HttpResponse.json([
          { ...MODEL, id: "m1", name: "Qwen3-32B", model_name: "/models/Qwen3-32B" },
          { ...MODEL, id: "m2", name: "生产环境", model_name: "/models/Qwen3-32B" },
          { ...MODEL, id: "m3", name: "未探测", model_name: "" },
        ]),
      ),
    );
    renderAt("/jobs/new");

    const select = await screen.findByLabelText("模型端点");
    // Named after its model: printing both repeats one long string twice.
    expect(within(select).getByRole("option", { name: "Qwen3-32B" })).toBeInTheDocument();
    // Named something else: the model is the fact the name is missing.
    expect(within(select).getByRole("option", { name: "生产环境 · Qwen3-32B" })).toBeInTheDocument();
    // Nothing detected yet: there is nothing to append.
    expect(within(select).getByRole("option", { name: "未探测" })).toBeInTheDocument();
  });

  it("cannot be submitted before a model and dataset are chosen", async () => {
    renderAt("/jobs/new");

    expect(await screen.findByRole("button", { name: "提交评测" })).toBeDisabled();
  });

  it("offers only installed datasets", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([GSM8K, { ...MMLU, status: "not_installed" }]),
      ),
    );
    renderAt("/jobs/new");

    await user.click(await screen.findByLabelText("数据集"));
    expect(await screen.findByRole("option", { name: /gsm8k/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /mmlu/ })).not.toBeInTheDocument();
  });

  it("leaves the form for the job list once the job is queued", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/jobs", () =>
        HttpResponse.json({ id: "j", status: "queued", queue_position: 1 }, { status: 201 }),
      ),
      http.get("/api/jobs", () => HttpResponse.json([])),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    // The list is where a submitted job lives; staying on the form hides it.
    expect(await screen.findByText("还没有任务。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提交评测" })).not.toBeInTheDocument();
  });

  it("names the job when the user gives it one", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, unknown> = {};
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        submitted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ id: "j", status: "queued", queue_position: 1 }, { status: 201 });
      }),
    );
    renderAt("/jobs/new");

    await user.type(await screen.findByLabelText("任务名称"), "夜间基线");
    await user.selectOptions(screen.getByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted.name).toBe("夜间基线"));
  });

  it("offers exactly the fields of the chosen config file, and no others", async () => {
    // Editing that file is the CLI workflow this replaces, so the file decides the form.
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);
    await user.selectOptions(screen.getByLabelText("模型配置"), "vllm_api_general_chat");

    expect(screen.getByLabelText("max_out_len")).toBeInTheDocument();
    expect(screen.getByLabelText("returns_tool_calls")).toBeInTheDocument();
    expect(screen.queryByLabelText("request_rate")).not.toBeInTheDocument();

    // A different file declares different fields.
    await user.selectOptions(screen.getByLabelText("模型配置"), "vllm_api_stream_chat");
    expect(screen.getByLabelText("request_rate")).toBeInTheDocument();
    expect(screen.queryByLabelText("returns_tool_calls")).not.toBeInTheDocument();
    // batch_size is the concurrency knob, which its name does not say.
    expect(screen.getByText(/并发请求数/)).toBeInTheDocument();
  });

  it("never asks again for what the chosen model endpoint already supplies", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);
    await user.selectOptions(screen.getByLabelText("模型配置"), "vllm_api_stream_chat");

    for (const supplied of ["api_key", "host_ip", "host_port", "url"]) {
      expect(screen.queryByLabelText(supplied)).not.toBeInTheDocument();
    }
  });

  it("keeps the config file's fields apart from the command line arguments", async () => {
    const user = userEvent.setup();
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);

    // Picking no config still runs one, so its fields show rather than an empty group.
    expect(screen.getByLabelText("returns_tool_calls")).toBeInTheDocument();
    expect(screen.getByText("模型配置文件字段")).toBeInTheDocument();
    expect(screen.getByText("命令行参数")).toBeInTheDocument();
    expect(screen.getByLabelText("--num-prompts")).toBeInTheDocument();
    expect(screen.getByLabelText("--max-num-workers")).toBeInTheDocument();
    // --max-num-workers is task-level and does nothing for a single dataset, so it must not
    // read as the concurrency knob that batch_size is.
    expect(screen.getByText(/单数据集只切出一个任务/)).toBeInTheDocument();
  });

  it("sends a config field only when it differs from what the file already says", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, Record<string, unknown>> = {};
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        submitted = (await request.json()) as Record<string, Record<string, unknown>>;
        return HttpResponse.json({ id: "j", status: "queued", queue_position: 1 }, { status: 201 });
      }),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);
    await user.selectOptions(screen.getByLabelText("模型配置"), "vllm_api_stream_chat");
    await user.type(screen.getByLabelText("batch_size"), "16");
    await user.type(screen.getByLabelText("temperature"), "0.7");
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted.parameters).toBeDefined());
    expect(submitted.parameters.config_fields).toEqual({ batch_size: 16 });
    expect(submitted.parameters.generation_kwargs).toEqual({ temperature: 0.7 });
  });

  it("sends only the parameters the user filled in", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, Record<string, unknown>> = {};
    server.use(
      http.post("/api/jobs", async ({ request }) => {
        submitted = (await request.json()) as Record<string, Record<string, unknown>>;
        return HttpResponse.json({ id: "j", status: "queued", queue_position: 1 }, { status: 201 });
      }),
    );
    renderAt("/jobs/new");

    await user.selectOptions(await screen.findByLabelText("模型端点"), "model-1");
    await pickDatasets(user, ["gsm8k"]);
    await user.click(screen.getByRole("button", { name: "提交评测" }));

    await waitFor(() => expect(submitted.parameters).toBeDefined());
    // An untouched box must not send a value; the file's own value should stand.
    expect(submitted.parameters.config_fields).toEqual({});
    expect(submitted.parameters.generation_kwargs).toEqual({});
    expect(submitted.parameters.cli).not.toHaveProperty("pressure_time");
    expect(submitted.parameters.cli).toMatchObject({ num_prompts: 8, max_num_workers: 1 });
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
    await pickDatasets(user, ["gsm8k"]);
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

  it("narrows by a search that matches dataset or config IDs", async () => {
    const user = userEvent.setup();
    renderAt("/datasets");

    expect(await screen.findByText("gsm8k")).toBeInTheDocument();
    await user.type(screen.getByLabelText("搜索数据集"), "mmlu_gen_5");

    expect(screen.getByText("mmlu")).toBeInTheDocument();
    expect(screen.queryByText("gsm8k")).not.toBeInTheDocument();
  });

  it("narrows by the domain the AISBench documentation puts a dataset in", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([
          GSM8K,
          {
            ...MMLU,
            id: "textvqa",
            name: "textvqa",
            category: "multimodal",
            task: "多模态理解（图+文）",
          },
        ]),
      ),
    );
    renderAt("/datasets");

    expect(await screen.findByText("gsm8k")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("分类筛选"), "domain:multimodal");

    expect(screen.getByText("textvqa")).toBeInTheDocument();
    expect(screen.queryByText("gsm8k")).not.toBeInTheDocument();
  });

  it("shows the task type the documentation gives each dataset", async () => {
    renderAt("/datasets");

    // Scoped to the table: the same words are also options in the task filter.
    const table = await screen.findByRole("table");
    expect(within(table).getByText("数学推理")).toBeInTheDocument();
    expect(within(table).getByText("多学科理解（英文）")).toBeInTheDocument();
  });

  it("does not list a task that covers its whole domain", async () => {
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([
          { ...MMLU, id: "sharegpt", name: "sharegpt", category: "dialogue", task: "多轮对话" },
          { ...MMLU, id: "mtbench", name: "mtbench", category: "dialogue", task: "多轮对话" },
        ]),
      ),
    );
    renderAt("/datasets");

    const filter = await screen.findByLabelText("分类筛选");
    // Offering the domain and its only task would present the same set under two names.
    expect(within(filter).getAllByRole("option", { name: /多轮对话/ })).toHaveLength(1);
  });

  it("narrows to one task inside its domain", async () => {
    const user = userEvent.setup();
    renderAt("/datasets");

    await user.selectOptions(await screen.findByLabelText("分类筛选"), "task:数学推理");

    expect(screen.getByText("gsm8k")).toBeInTheDocument();
    expect(screen.queryByText("mmlu")).not.toBeInTheDocument();
  });

  it("groups tasks under the domain they belong to, with counts", async () => {
    server.use(
      http.get("/api/datasets", () =>
        HttpResponse.json([
          GSM8K,
          MMLU,
          {
            ...MMLU,
            id: "textvqa",
            name: "textvqa",
            category: "multimodal",
            task: "多模态理解（图+文）",
          },
        ]),
      ),
    );
    renderAt("/datasets");

    const filter = await screen.findByLabelText("分类筛选");
    // One control over one tree: a domain and the tasks inside it are not separate axes.
    const groups = [...filter.querySelectorAll("optgroup")].map((group) => group.label);
    expect(groups).toEqual(["LLM", "多模态"]);
    expect(within(filter).getByRole("option", { name: /^LLM（2）/ })).toBeInTheDocument();
    expect(within(filter).getByRole("option", { name: /数学推理（1）/ })).toBeInTheDocument();
    expect(within(filter).getByRole("option", { name: "全部数据集（3）" })).toBeInTheDocument();
  });

  it("finds a dataset by searching its task type", async () => {
    const user = userEvent.setup();
    renderAt("/datasets");

    await user.type(await screen.findByLabelText("搜索数据集"), "多学科");

    expect(screen.getByText("mmlu")).toBeInTheDocument();
    expect(screen.queryByText("gsm8k")).not.toBeInTheDocument();
  });

  it("offers only the domains something is actually in", async () => {
    server.use(http.get("/api/datasets", () => HttpResponse.json([GSM8K])));
    renderAt("/datasets");

    const filter = await screen.findByLabelText("分类筛选");
    expect([...filter.querySelectorAll("optgroup")].map((group) => group.label)).toEqual(["LLM"]);
  });

  it("says so when nothing matches instead of showing an empty table", async () => {
    const user = userEvent.setup();
    renderAt("/datasets");

    await user.type(await screen.findByLabelText("搜索数据集"), "nothing-matches-this");

    expect(await screen.findByText("没有匹配的数据集。")).toBeInTheDocument();
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
    // A dataset's state is read the same way a job's is: the same badge, the same colours.
    const badge = await screen.findByText("可用", {}, { timeout: 4000 });
    expect(badge).toHaveClass("status", "status-success");
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
