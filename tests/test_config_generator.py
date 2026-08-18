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
        "max_output_length": 512,
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
            return {
                keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords
            }
    raise AssertionError("generated config has no models[0].update(...) call")


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
    assert "test_range" in source
    assert "api_key" in source


def test_performance_config_uses_perf_dataset_and_stream_flag(tmp_path: Path) -> None:
    output = tmp_path / "generated.py"

    generate_config(
        output,
        mode="performance",
        dataset_import=GSM8K_PERFORMANCE_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"num_prompts": 32, "stream": True, "request_rate": 8},
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


def test_num_prompts_limits_the_dataset_and_is_omitted_when_unset() -> None:
    limited = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"num_prompts": 8},
    )
    unlimited = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={},
    )

    assert "'[0:8]'" in limited
    assert "test_range" not in unlimited


def test_worker_count_reaches_the_runner() -> None:
    source = render_config(
        mode="accuracy",
        dataset_import=GSM8K_ACCURACY_IMPORT,
        dataset_symbol="gsm8k_datasets",
        endpoint=endpoint_snapshot(),
        parameters={"max_num_workers": 4},
    )

    assert "max_num_workers=4" in source
    compile(source, "<generated>", "exec")
