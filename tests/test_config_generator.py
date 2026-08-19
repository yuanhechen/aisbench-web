import ast
import os
import stat
from pathlib import Path
from urllib.parse import urljoin

import pytest

from aisbench_web.jobs.config_generator import (
    REDACTED_API_KEY,
    EndpointSnapshot,
    aisbench_service_url,
    cli_arguments_for,
    cli_mode_for,
    generate_config,
    render_config,
)

GSM8K_ACCURACY_IMPORT = (
    "ais_bench.benchmark.configs.datasets.gsm8k.gsm8k_gen_4_shot_cot_chat_prompt"
)
GSM8K_PERFORMANCE_IMPORT = (
    "ais_bench.benchmark.configs.datasets.gsm8k.gsm8k_gen_0_shot_cot_str_perf"
)
# AISBench's chat model builds its request URL as urljoin(base_url, CHAT_ENDPOINT).
CHAT_ENDPOINT = "v1/chat/completions"


def endpoint_snapshot(**overrides) -> EndpointSnapshot:
    defaults = {
        "abbr": "job-model",
        "base_url": "http://127.0.0.1:8001/v1",
        "model_name": "Qwen3-32B",
        "api_key": "token-with-quote-'",
    }
    return EndpointSnapshot(**{**defaults, **overrides})


def model_update_kwargs(source: str) -> dict:
    """Read back the values written into models[0].update(...) without executing the config."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            return {keyword.arg: _value_of(keyword.value) for keyword in node.keywords}
    raise AssertionError("generated config has no models[0].update(...) call")


def _value_of(node: ast.expr):
    """Read a literal, or a dict(...) call, which is the idiom AISBench configs use."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
        return {keyword.arg: _value_of(keyword.value) for keyword in node.keywords}
    return ast.literal_eval(node)


# --- plan contracts ----------------------------------------------------------


def test_accuracy_config_uses_manifest_imports_and_escaped_values(tmp_path: Path) -> None:
    output = tmp_path / "generated.py"

    generate_config(
        output,
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"num_prompts": 8, "max_num_workers": 1},
    )

    source = output.read_text(encoding="utf-8")
    compile(source, str(output), "exec")
    assert "gsm8k_datasets" in source
    assert "api_key" in source


def test_performance_config_uses_perf_dataset_and_stream_flag(tmp_path: Path) -> None:
    output = tmp_path / "generated.py"

    generate_config(
        output,
        mode="performance",
        dataset_import=GSM8K_PERFORMANCE_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"cli": {"num_prompts": 32}, "config_fields": {"request_rate": 8}},
    )

    source = output.read_text(encoding="utf-8")
    compile(source, str(output), "exec")
    assert "stream=True" in source
    assert "request_rate=8" in source


def test_cli_mode_mapping_is_explicit() -> None:
    assert cli_mode_for("accuracy", {"visualization": False}) == "all"
    assert cli_mode_for("performance", {"visualization": False}) == "perf"
    assert cli_mode_for("performance", {"visualization": True}) == "perf_viz"
    with pytest.raises(ValueError, match="unknown job mode"):
        cli_mode_for("nonsense", {})


# --- the URL mapping AISBench actually implements ----------------------------


@pytest.mark.parametrize(
    ("base_url", "expected_service_root", "expected_request_url"),
    [
        (
            "http://127.0.0.1:8001/v1",
            "http://127.0.0.1:8001/",
            "http://127.0.0.1:8001/v1/chat/completions",
        ),
        (
            "https://api.example.com/v1",
            "https://api.example.com/",
            "https://api.example.com/v1/chat/completions",
        ),
        (
            "http://model-host:8001/openai/v1",
            "http://model-host:8001/openai/",
            "http://model-host:8001/openai/v1/chat/completions",
        ),
        (
            "http://127.0.0.1:8001",
            "http://127.0.0.1:8001/",
            "http://127.0.0.1:8001/v1/chat/completions",
        ),
        (
            "http://127.0.0.1:8001/v1/",
            "http://127.0.0.1:8001/",
            "http://127.0.0.1:8001/v1/chat/completions",
        ),
    ],
)
def test_base_url_maps_to_the_root_aisbench_appends_to(
    base_url: str,
    expected_service_root: str,
    expected_request_url: str,
) -> None:
    service_root = aisbench_service_url(base_url)

    assert service_root == expected_service_root
    # Reproduces AISBench's own _get_url(), so a wrong mapping fails here rather than on the server.
    assert urljoin(service_root, CHAT_ENDPOINT) == expected_request_url


def test_generated_config_carries_the_service_root_not_a_bare_path() -> None:
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(base_url="https://api.example.com/v1"),
        parameters={},
    )

    kwargs = model_update_kwargs(source)
    assert kwargs["url"] == "https://api.example.com/"
    assert kwargs["host_ip"] == "api.example.com"
    assert kwargs["enable_ssl"] is True
    # A bare path here would make AISBench build a hostless request URL.
    assert not kwargs["url"].startswith("/")


# --- secrets -----------------------------------------------------------------


def test_api_key_reaches_the_file_but_never_the_redacted_copy(tmp_path: Path) -> None:
    output = tmp_path / "generated.py"
    endpoint = endpoint_snapshot(api_key="super-secret-token")

    written = generate_config(
        output,
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint,
        parameters={},
    )
    redacted = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint,
        parameters={},
        redact_api_key=True,
    )

    assert model_update_kwargs(written)["api_key"] == "super-secret-token"
    assert "super-secret-token" in output.read_text(encoding="utf-8")
    assert "super-secret-token" not in redacted
    assert model_update_kwargs(redacted)["api_key"] == REDACTED_API_KEY


def test_config_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    output = tmp_path / "generated.py"

    generate_config(
        output,
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={},
    )

    assert stat.S_IMODE(os.stat(output).st_mode) == 0o600


def test_missing_api_key_becomes_the_aisbench_default() -> None:
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(api_key=None),
        parameters={},
    )

    assert model_update_kwargs(source)["api_key"] == ""


# --- injection ---------------------------------------------------------------


def test_hostile_user_strings_round_trip_as_data(tmp_path: Path) -> None:
    hostile_model = "x',\n    api_key='stolen"
    hostile_abbr = 'a"""b\\c\nd'
    output = tmp_path / "generated.py"

    generate_config(
        output,
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(
            abbr=hostile_abbr,
            model_name=hostile_model,
            api_key="real-key",
        ),
        parameters={},
    )

    source = output.read_text(encoding="utf-8")
    compile(source, str(output), "exec")
    kwargs = model_update_kwargs(source)
    assert kwargs["model"] == hostile_model
    assert kwargs["abbr"] == hostile_abbr
    assert kwargs["api_key"] == "real-key"


@pytest.mark.parametrize(
    "bad_import",
    [
        "os; import subprocess",
        "ais_bench.benchmark.configs.datasets.gsm8k.x import *",
        "../escape",
        "ais_bench..double",
        "",
    ],
)
def test_untrusted_import_paths_are_refused(bad_import: str) -> None:
    with pytest.raises(ValueError, match="unsafe config import"):
        render_config(
            mode="accuracy",
            dataset_import=bad_import,
            dataset_symbol="gsm8k_datasets",
            endpoint=endpoint_snapshot(),
            parameters={},
        )


def test_untrusted_dataset_symbols_are_refused() -> None:
    with pytest.raises(ValueError, match="unsafe config import"):
        render_config(
            mode="accuracy",
            dataset_import=GSM8K_ACCURACY_IMPORT,
            dataset_symbol="datasets, os.system('rm -rf /')",
            endpoint=endpoint_snapshot(),
            parameters={},
        )


# --- parameters --------------------------------------------------------------


def test_num_prompts_travels_on_the_command_line() -> None:
    """AISBench's --num-prompts sets exactly the dataset reader range this used to edit."""
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"num_prompts": 8},
    )

    assert "test_range" not in source
    assert cli_arguments_for({"num_prompts": 8}) == [
        "--max-num-workers",
        "1",
        "--num-prompts",
        "8",
    ]


def test_every_documented_option_reaches_the_command_line() -> None:
    arguments = cli_arguments_for(
        {
            "max_num_workers": 4,
            "max_workers_per_gpu": 2,
            "num_prompts": 8,
            "num_warmups": 0,
            "pressure": True,
            "pressure_time": 30,
            "spec_decode": True,
            "dump_eval_details": True,
            "merge_datasets": True,
            "dump_extract_rate": True,
        }
    )

    assert arguments == [
        "--max-num-workers", "4",
        "--max-workers-per-gpu", "2",
        "--num-prompts", "8",
        "--num-warmups", "0",
        "--pressure-time", "30",
        "--dump-eval-details",
        "--merge-ds",
        "--dump-extract-rate",
        "--pressure",
        "--spec-decode",
    ]
    # An option the user did not set stays at whatever AISBench defaults to.
    assert cli_arguments_for({}) == ["--max-num-workers", "1"]


def test_config_fields_are_whatever_the_chosen_config_file_declares() -> None:
    """The editable fields differ per config file, so the job writes the ones it was given
    rather than a fixed list that would invent fields one file lacks and drop fields it has."""
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={
            "config_fields": {"batch_size": 8, "retry": 3, "returns_tool_calls": True},
            "generation_kwargs": {"temperature": 0.7, "do_sample": True},
        },
    )

    compile(source, "<generated>", "exec")
    kwargs = model_update_kwargs(source)
    assert kwargs["generation_kwargs"] == {"do_sample": True, "temperature": 0.7}
    assert kwargs["batch_size"] == 8
    assert kwargs["retry"] == 3
    assert kwargs["returns_tool_calls"] is True


def test_a_field_name_that_is_not_an_identifier_is_refused() -> None:
    """Field names reach a generated Python file, so anything but an identifier is injection."""
    with pytest.raises(ValueError):
        render_config(
            mode="accuracy",
            dataset_import=GSM8K_ACCURACY_IMPORT,
            dataset_symbol="gsm8k_datasets",
            endpoint=endpoint_snapshot(),
            parameters={"config_fields": {"batch_size=1)\nimport os\n#": 1}},
        )


def test_untouched_sampling_options_are_left_out_entirely() -> None:
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={},
    )

    assert "generation_kwargs" not in source
    assert "batch_size" not in source


def test_worker_count_travels_on_the_command_line_not_in_the_config() -> None:
    """AISBench builds its own runner when the config has no infer block, and reads the worker
    count from its CLI. Naming those internals in the config coupled us to module paths that
    do not exist in every release."""
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"max_num_workers": 4},
    )

    assert "infer" not in source
    assert cli_arguments_for({"max_num_workers": 4}) == ["--max-num-workers", "4"]
    assert cli_arguments_for({}) == ["--max-num-workers", "1"]


VERIFIED_IMPORT_ROOTS = (
    "mmengine.config",
    "ais_bench.benchmark.configs.datasets.",
    "ais_bench.benchmark.configs.models.",
    "ais_bench.benchmark.configs.summarizers.",
)


@pytest.mark.parametrize("mode", ["accuracy", "performance"])
def test_generated_config_imports_only_verified_modules(mode: str) -> None:
    """A config may only name modules checked against the installed AISBench.

    An unverified import fails at run time, not at load time: mmengine defers imports, so a
    config that loads cleanly can still name a module that does not exist.
    """
    source = render_config(
        mode=mode,
        dataset_import=GSM8K_ACCURACY_IMPORT if mode == "accuracy" else GSM8K_PERFORMANCE_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={},
    )

    imported = [
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert imported
    for module in imported:
        assert module.startswith(VERIFIED_IMPORT_ROOTS), module


def test_a_chosen_model_config_keeps_its_own_streaming_setting() -> None:
    """Two configs of one class can differ only by stream; overwriting it erases the choice."""
    chosen = render_config(
        mode="performance",
        dataset_import=GSM8K_PERFORMANCE_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={},
        model_import="ais_bench.benchmark.configs.models.vllm_api.vllm_api_general_chat",
    )

    assert "stream=" not in chosen
    assert "vllm_api_general_chat import models" in chosen


def test_the_default_model_config_still_streams_for_performance() -> None:
    source = render_config(
        mode="performance",
        dataset_import=GSM8K_PERFORMANCE_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={},
    )

    assert model_update_kwargs(source)["stream"] is True


def test_two_model_configs_of_one_class_still_produce_different_files() -> None:
    """They differ only by stream. Overwriting it made the choice change nothing at all."""
    base = {
        "mode": "performance",
        "dataset_import": GSM8K_PERFORMANCE_IMPORT,
        "dataset_symbol": "gsm8k_datasets",
        "endpoint": endpoint_snapshot(),
        "parameters": {},
    }
    general = render_config(
        **base,
        model_import="ais_bench.benchmark.configs.models.vllm_api.vllm_api_general_chat",
    )
    streaming = render_config(
        **base,
        model_import="ais_bench.benchmark.configs.models.vllm_api.vllm_api_stream_chat",
    )

    assert general != streaming
    assert "vllm_api_general_chat import models" in general
    assert "vllm_api_stream_chat import models" in streaming
