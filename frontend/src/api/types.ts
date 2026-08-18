export interface ModelEndpoint {
  id: string;
  name: string;
  base_url: string;
  model_name: string;
  has_api_key: boolean;
  is_active: boolean;
}

export interface DatasetConfig {
  name: string;
  mode: "accuracy" | "performance";
  method: string;
  shots: number | null;
  chain_of_thought: boolean;
  chat_prompt: boolean;
  alias_of: string;
}

export interface Dataset {
  id: string;
  name: string;
  description: string;
  config_name: string;
  default_config: string;
  category: string;
  task: string;
  configs: DatasetConfig[];
  status: "not_installed" | "installing" | "available" | "failed" | "detected";
  local_path: string | null;
  size_bytes: number | null;
  error_message: string | null;
  can_install: boolean;
}

export interface ProbeResult {
  ok: boolean;
  latency_ms: number;
  message: string;
  models: string[];
  request_url: string;
  runnable: boolean;
}

export interface Job {
  id: string;
  name: string;
  mode: string;
  status: string;
  queue_position: number | null;
  progress: { completed: number; total: number } | null;
  model: { name: string; model_name: string; base_url: string };
  dataset: { id: string; name: string };
  parameters: Record<string, unknown>;
  exit_code: number | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ModelConfigOption {
  name: string;
  family: string;
  class_name: string;
  stream: boolean;
}
