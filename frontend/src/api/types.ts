export interface ModelEndpoint {
  id: string;
  name: string;
  base_url: string;
  model_name: string;
  has_api_key: boolean;
  request_timeout: number;
  max_output_length: number;
  is_active: boolean;
}

export interface Dataset {
  id: string;
  name: string;
  description: string;
  config_name: string;
  accuracy_config: string | null;
  performance_config: string | null;
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
}

export interface Job {
  id: string;
  mode: string;
  status: string;
  queue_position: number | null;
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
