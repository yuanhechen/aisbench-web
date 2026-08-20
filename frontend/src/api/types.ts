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

export type DatasetPhase =
  | "queued"
  | "loading"
  | "inferring"
  | "writing_cache"
  | "evaluating"
  | "finished"
  | "failed";

export interface DatasetMetricValue {
  value: number | null;
  text_value: string | null;
  unit: string | null;
}

/** One dataset's own progress through the run, and its scores once it has them. */
export interface DatasetProgress {
  name: string;
  phase: DatasetPhase;
  completed: number | null;
  total: number | null;
  rate: string | null;
  counters: Record<string, number> | null;
  log_available: boolean;
  metrics: Record<string, DatasetMetricValue>;
  correct_count: number | null;
  total_count: number | null;
  started_at: string | null;
}

export interface Job {
  id: string;
  name: string;
  mode: string;
  status: string;
  queue_position: number | null;
  progress: { completed: number; total: number } | null;
  model: { name: string; model_name: string; base_url: string; config_name: string };
  dataset: { id: string; name: string; config_name: string };
  datasets: DatasetProgress[];
  parameters: Record<string, unknown>;
  exit_code: number | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ConfigField {
  name: string;
  default: boolean | number | string;
  kind: "boolean" | "integer" | "number" | "text";
}

export interface ModelConfigOption {
  name: string;
  family: string;
  class_name: string;
  stream: boolean;
  /** The mode that falls back to this config when the user picks none. */
  default_for: "accuracy" | "performance" | null;
  /** What this config file lets a job change. Config files differ, so this list differs. */
  fields: ConfigField[];
  generation_fields: ConfigField[];
}
