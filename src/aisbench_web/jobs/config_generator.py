import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REDACTED_API_KEY = "***"
# AISBench's chat model class appends exactly this to the service root.
CHAT_ENDPOINT = "v1/chat/completions"
DOTTED_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ACCURACY = "accuracy"
PERFORMANCE = "performance"

MODEL_IMPORTS = {
    ACCURACY: "ais_bench.benchmark.configs.models.vllm_api.vllm_api_general_chat",
    PERFORMANCE: "ais_bench.benchmark.configs.models.vllm_api.vllm_api_stream_chat",
}
SUMMARIZER_IMPORTS = {
    ACCURACY: "ais_bench.benchmark.configs.summarizers.example",
    PERFORMANCE: "ais_bench.benchmark.configs.summarizers.perf.default_perf",
}
CLI_MODES = {ACCURACY: "all", PERFORMANCE: "perf"}
PERFORMANCE_VISUALIZATION_MODE = "perf_viz"

DEFAULT_MAX_NUM_WORKERS = 1

# Options AISBench reads from its own command line. Passing them here rather than editing the
# config keeps the generated file to the three imports and one update it needs.
COUNTED_CLI_OPTIONS = (
    ("max_num_workers", "--max-num-workers"),
    ("max_workers_per_gpu", "--max-workers-per-gpu"),
    ("num_prompts", "--num-prompts"),
    ("num_warmups", "--num-warmups"),
    ("pressure_time", "--pressure-time"),
)
FLAG_CLI_OPTIONS = (
    ("dump_eval_details", "--dump-eval-details"),
    ("merge_datasets", "--merge-ds"),
    ("dump_extract_rate", "--dump-extract-rate"),
    ("pressure", "--pressure"),
    ("spec_decode", "--spec-decode"),
)
def cli_arguments_for(parameters: dict) -> list[str]:
    """Options AISBench reads from its own command line rather than from the config."""
    cli = parameters.get("cli") if "cli" in parameters else parameters
    arguments: list[str] = [
        "--max-num-workers",
        str(int(cli.get("max_num_workers") or DEFAULT_MAX_NUM_WORKERS)),
    ]
    for key, option in COUNTED_CLI_OPTIONS:
        if key == "max_num_workers":
            continue
        value = cli.get(key)
        if value is not None:
            arguments += [option, str(int(value))]
    for key, option in FLAG_CLI_OPTIONS:
        if cli.get(key):
            arguments.append(option)
    return arguments


def _rendered_fields(values: dict) -> list[str]:
    """Render config fields, refusing any name that is not a plain identifier."""
    lines = []
    for name in sorted(values):
        _checked_import(name, pattern=IDENTIFIER, label="field")
        lines.append(f"    {name}={values[name]!r},")
    return lines


@dataclass(frozen=True)
class EndpointSnapshot:
    abbr: str
    base_url: str
    model_name: str
    api_key: str | None


def cli_mode_for(mode: str, parameters: dict) -> str:
    if mode == PERFORMANCE and parameters.get("visualization"):
        return PERFORMANCE_VISUALIZATION_MODE
    try:
        return CLI_MODES[mode]
    except KeyError as exc:
        raise ValueError(f"unknown job mode: {mode!r}") from exc


def aisbench_service_url(base_url: str) -> str:
    """Return the service root AISBench appends its own ``v1/...`` endpoint to.

    AISBench builds requests as ``urljoin(base_url, "v1/chat/completions")``, so the value it
    needs is the server root, not the OpenAI-style ``/v1`` prefix users paste in. A trailing
    slash is required or urljoin drops the last path segment.
    """
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    path = path.removesuffix("/v1")
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/", "", ""))


def _checked_import(value: str, *, pattern: re.Pattern, label: str) -> str:
    """Import paths come from the packaged catalog; refuse anything that is not a plain name."""
    if not pattern.match(value):
        raise ValueError(f"unsafe config import: {label} {value!r}")
    return value


def render_config(
    *,
    mode: str,
    datasets: list[tuple[str, str]],
    endpoint: EndpointSnapshot,
    parameters: dict,
    model_import: str | None = None,
    redact_api_key: bool = False,
) -> str:
    """Render the job config.

    `datasets` holds one (import path, symbol) pair per chosen dataset config; a single
    dataset renders exactly as it always did, only through the same list path.
    `parameters` separates what the CLI takes from what the model config file holds, because
    those are different things in AISBench and editing one is not editing the other.
    """
    if mode not in MODEL_IMPORTS:
        raise ValueError(f"unknown job mode: {mode!r}")
    if not datasets:
        raise ValueError("a job config needs at least one dataset")

    checked = []
    for position, (import_path, symbol) in enumerate(datasets):
        module = _checked_import(import_path, pattern=DOTTED_PATH, label="module")
        checked_symbol = _checked_import(symbol, pattern=IDENTIFIER, label="symbol")
        checked.append((module, checked_symbol, f"ds_{position}"))
    chosen_import = _checked_import(
        model_import or MODEL_IMPORTS[mode], pattern=DOTTED_PATH, label="module"
    )

    service_url = aisbench_service_url(endpoint.base_url)
    parsed = urlsplit(service_url)
    api_key = REDACTED_API_KEY if redact_api_key else (endpoint.api_key or "")

    lines = [
        "# Generated by AISBench Web. Do not edit: submitting the job again replaces this file.",
        "from mmengine.config import read_base",
        "",
        "with read_base():",
    ]
    for module, symbol, alias in checked:
        lines.append(f"    from {module} import {symbol} as {alias}")
    lines += [
        f"    from {chosen_import} import models",
        f"    from {SUMMARIZER_IMPORTS[mode]} import summarizer",
        "",
        "datasets = [" + ", ".join(f"*{alias}" for _, _, alias in checked) + "]",
        "",
        "models[0].update(",
        f"    abbr={endpoint.abbr!r},",
        f"    model={endpoint.model_name!r},",
        f"    api_key={api_key!r},",
        f"    host_ip={parsed.hostname or ''!r},",
        f"    host_port={parsed.port or (443 if parsed.scheme == 'https' else 80)!r},",
        f"    url={service_url!r},",
        f"    enable_ssl={(parsed.scheme == 'https')!r},",
    ]
    # Only fields the user actually changed. Anything untouched keeps whatever the chosen
    # model config file already says, which is the file they would have edited by hand.
    lines += _rendered_fields(parameters.get("config_fields") or {})
    generation = parameters.get("generation_kwargs") or {}
    if generation:
        for name in generation:
            _checked_import(name, pattern=IDENTIFIER, label="field")
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(generation.items()))
        lines.append(f"    generation_kwargs=dict({rendered}),")
    # Without a chosen config the mode picks the default one, which decides streaming.
    if model_import is None and "stream" not in (parameters.get("config_fields") or {}):
        lines.append(f"    stream={mode == PERFORMANCE!r},")
    lines += [")", ""]
    # num_prompts is not written here: AISBench's own --num-prompts sets exactly this
    # dataset reader range, and the CLI is the documented way to ask for it.

    # No `infer` block on purpose. Without one AISBench builds the partitioner, runner, and
    # task itself, and picks the API inference task for a service model. Naming those internals
    # here coupled the config to module paths that do not exist in every release.
    return "\n".join(lines)


def generate_config(
    output: Path,
    *,
    mode: str,
    datasets: list[tuple[str, str]],
    endpoint: EndpointSnapshot,
    parameters: dict,
    model_import: str | None = None,
) -> str:
    """Write the job config with owner-only permissions and return its source.

    The returned source contains the decrypted API key, so it must never be logged; use
    ``render_config(..., redact_api_key=True)`` for anything a user or log can see.
    """
    source = render_config(
        mode=mode,
        datasets=datasets,
        endpoint=endpoint,
        parameters=parameters,
        model_import=model_import,
        redact_api_key=False,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        config_file.write(source)
    output.chmod(0o600)
    return source
