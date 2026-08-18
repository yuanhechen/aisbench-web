"""Read the dataset catalog out of the AISBench installation itself.

A hand-written manifest can only ever describe the AISBench it was written against. The
configs that ship with the installed version are the authority on which datasets exist, which
variants each one offers, and where its data must live.
"""

import re
from dataclasses import dataclass
from pathlib import Path

CONFIGS_RELATIVE = Path("benchmark") / "configs" / "datasets"
MODELS_RELATIVE = Path("benchmark") / "configs" / "models"
DATASET_CONFIG_ROOT = "ais_bench.benchmark.configs.datasets"
MODEL_CONFIG_ROOT = "ais_bench.benchmark.configs.models"
# `path='ais_bench/datasets/gsm8k'` or `path="ais_bench/datasets/ceval/formal_ceval"`
DATA_PATH = re.compile(r"""path\s*=\s*['"]([^'"]*ais_bench/datasets/[^'"]*)['"]""")
DATASET_SYMBOL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*_datasets)\s*=", re.MULTILINE)
# `<name>_gen.py` files re-export one specific config: `from .x_gen_5_shot_str import x_datasets`.
ALIAS_IMPORT = re.compile(
    r"from\s+\.([A-Za-z_][A-Za-z0-9_]*)\s+import\s+([A-Za-z_][A-Za-z0-9_]*_datasets)"
)
DATASETS_PREFIX = "ais_bench/datasets/"
PERFORMANCE_SUFFIX = "_perf"
# Written for humans to read back: gsm8k_gen_4_shot_cot_chat_prompt, math_prm800k_500_5shot_cot_gen.
SHOTS = re.compile(r"_(\d+)_?shot")
# AISBench evaluates either by generating an answer or by scoring options with perplexity.
METHODS = ("gen", "ppl")


@dataclass(frozen=True)
class DatasetConfig:
    """One AISBench config file: a specific way of running a dataset.

    `name` is the identity. Everything else is read off that name for display and must never
    stand in for it: several configs in one dataset can share every derived attribute.
    """

    name: str
    package: str
    symbol: str
    is_performance: bool
    shots: int | None
    chain_of_thought: bool
    chat_prompt: bool
    #: "gen" generates an answer, "ppl" scores options by perplexity; empty when unstated.
    method: str = ""
    #: The config this one re-exports, for the `<name>_gen.py` shortcut files.
    alias_of: str = ""

    @property
    def import_path(self) -> str:
        return f"{DATASET_CONFIG_ROOT}.{self.package}.{self.name}"

    @property
    def mode(self) -> str:
        return "performance" if self.is_performance else "accuracy"


@dataclass(frozen=True)
class ScannedDataset:
    id: str
    #: Directory an install unpacks into, relative to the datasets root.
    install_path: str | None
    #: Full path a config actually reads, which can sit deeper than the install directory.
    required_path: str | None
    configs: tuple[DatasetConfig, ...]

    def configs_for(self, mode: str) -> tuple[DatasetConfig, ...]:
        return tuple(config for config in self.configs if config.mode == mode)


def _describe(name: str, package: str, symbol: str, alias_of: str = "") -> DatasetConfig:
    shots = SHOTS.search(name)
    parts = name.split("_")
    return DatasetConfig(
        name=name,
        package=package,
        symbol=symbol,
        is_performance=name.endswith(PERFORMANCE_SUFFIX),
        shots=int(shots.group(1)) if shots else None,
        # "noncot" must not be read as containing "cot".
        chain_of_thought="_cot" in name and "_noncot" not in name,
        chat_prompt="chat" in name,
        method=next((part for part in parts if part in METHODS), ""),
        alias_of=alias_of,
    )


def _declared_paths(source: str) -> tuple[str, str] | None:
    """Return (install directory, path the config reads), relative to the datasets root.

    A config can point deeper than the directory an archive unpacks into: C-Eval installs as
    `ceval/` but reads `ceval/formal_ceval`. Checking only the top directory would call a
    half-finished install available.
    """
    match = DATA_PATH.search(source)
    if match is None:
        return None
    tail = match.group(1).split(DATASETS_PREFIX, 1)[1]
    directory = tail.split("/")[0]
    if not directory:
        return None
    # Trim a trailing filename so the check looks at a directory.
    required = tail.rstrip("/")
    if "." in Path(required).name:
        required = str(Path(required).parent)
    return directory, required or directory


def scan_dataset_configs(ais_bench_package: Path) -> tuple[ScannedDataset, ...]:
    """List every dataset the installed AISBench ships a config for."""
    root = ais_bench_package / CONFIGS_RELATIVE
    if not root.is_dir():
        return ()

    datasets: list[ScannedDataset] = []
    for package_dir in sorted(root.iterdir()):
        if not package_dir.is_dir() or package_dir.name.startswith((".", "_")):
            continue
        configs: list[DatasetConfig] = []
        paths: tuple[str, str] | None = None
        for config_file in sorted(package_dir.glob("*.py")):
            if config_file.name.startswith("_"):
                continue
            try:
                source = config_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            symbol = DATASET_SYMBOL.search(source)
            alias = ALIAS_IMPORT.search(source)
            if symbol is not None:
                configs.append(_describe(config_file.stem, package_dir.name, symbol.group(1)))
            elif alias is not None:
                # A shortcut file the CLI accepts by name; it runs the config it re-exports.
                configs.append(
                    _describe(config_file.stem, package_dir.name, alias.group(2), alias.group(1))
                )
            else:
                continue
            paths = paths or _declared_paths(source)
        if configs:
            datasets.append(
                ScannedDataset(
                    id=package_dir.name,
                    install_path=None if paths is None else paths[0],
                    required_path=None if paths is None else paths[1],
                    configs=tuple(configs),
                )
            )
    return tuple(datasets)


# A model config declares the class that will drive the endpoint and how it will call it.
MODEL_SYMBOL = re.compile(r"^models\s*=", re.MULTILINE)
MODEL_ATTR = re.compile(r"""attr\s*=\s*['"]([^'"]+)['"]""")
MODEL_TYPE = re.compile(r"type\s*=\s*([A-Za-z_][A-Za-z0-9_]*)")
MODEL_ABBR = re.compile(r"""abbr\s*=\s*['"]([^'"]+)['"]""")
MODEL_STREAM = re.compile(r"stream\s*=\s*(True|False)")
# Only a service config drives an HTTP endpoint; the rest need a model file on disk.
SERVICE_ATTR = "service"
# Structural fallback for a config that does not declare `attr`: an HTTP endpoint has to say
# where the endpoint is, while an offline config points at a path on disk instead. Only the
# vllm_api family has been seen first hand, so recognising a family must not depend on it
# spelling itself the same way.
ENDPOINT_FIELDS = re.compile(r"\b(host_ip|host_port|url)\s*=")


@dataclass(frozen=True)
class ModelConfig:
    """One AISBench model config file: which class drives the endpoint, and how."""

    name: str
    family: str
    class_name: str
    abbr: str
    stream: bool
    is_service: bool

    @property
    def import_path(self) -> str:
        return f"{MODEL_CONFIG_ROOT}.{self.family}.{self.name}"


def scan_model_configs(ais_bench_package: Path) -> tuple[ModelConfig, ...]:
    """List the model configs the installed AISBench ships for API endpoints."""
    root = ais_bench_package / MODELS_RELATIVE
    if not root.is_dir():
        return ()

    found: list[ModelConfig] = []
    for family_dir in sorted(root.iterdir()):
        if not family_dir.is_dir() or family_dir.name.startswith((".", "_")):
            continue
        for config_file in sorted(family_dir.glob("*.py")):
            if config_file.name.startswith("_"):
                continue
            try:
                source = config_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if MODEL_SYMBOL.search(source) is None:
                continue
            attr = MODEL_ATTR.search(source)
            model_type = MODEL_TYPE.search(source)
            abbr = MODEL_ABBR.search(source)
            stream = MODEL_STREAM.search(source)
            if attr is not None:
                is_service = attr.group(1) == SERVICE_ATTR
            else:
                is_service = ENDPOINT_FIELDS.search(source) is not None
            found.append(
                ModelConfig(
                    name=config_file.stem,
                    family=family_dir.name,
                    class_name="" if model_type is None else model_type.group(1),
                    abbr="" if abbr is None else abbr.group(1),
                    stream=stream is not None and stream.group(1) == "True",
                    is_service=is_service,
                )
            )
    return tuple(found)
