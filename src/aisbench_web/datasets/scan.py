"""Read the dataset catalog out of the AISBench installation itself.

A hand-written manifest can only ever describe the AISBench it was written against. The
configs that ship with the installed version are the authority on which datasets exist, which
variants each one offers, and where its data must live.
"""

import ast
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
# `abbr='ARC-e'`: the name AISBench itself reports a dataset under, in tasks and results.
# Only whole literals count: some configs build the abbr (`abbr='GPQA_' + split`), and the
# fragment a loose match would catch names a dataset AISBench will never report.
DATASET_ABBR = re.compile(r"""abbr\s*=\s*['"]([^'"]+)['"]\s*[,)]""")
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
    #: The abbr AISBench reports this dataset under; empty when the config states none.
    abbr: str = ""
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


def _describe(
    name: str, package: str, symbol: str, alias_of: str = "", abbr: str = ""
) -> DatasetConfig:
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
        abbr=abbr,
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
                abbr = DATASET_ABBR.search(source)
                configs.append(
                    _describe(
                        config_file.stem,
                        package_dir.name,
                        symbol.group(1),
                        abbr="" if abbr is None else abbr.group(1),
                    )
                )
            elif alias is not None:
                # A shortcut file the CLI accepts by name; it runs the config it re-exports.
                configs.append(
                    _describe(config_file.stem, package_dir.name, alias.group(2), alias.group(1))
                )
            else:
                continue
            paths = paths or _declared_paths(source)
        if configs:
            _fill_alias_abbrs(configs)
            datasets.append(
                ScannedDataset(
                    id=package_dir.name,
                    install_path=None if paths is None else paths[0],
                    required_path=None if paths is None else paths[1],
                    configs=tuple(configs),
                )
            )
    return tuple(datasets)


def _fill_alias_abbrs(configs: list[DatasetConfig]) -> None:
    """An alias runs its target's config, so it reports the target's abbr too."""
    by_name = {config.name: config for config in configs}
    for config in configs:
        if config.abbr or not config.alias_of:
            continue
        target = by_name.get(config.alias_of)
        if target is not None and target.abbr:
            configs[configs.index(config)] = _describe(
                config.name,
                config.package,
                config.symbol,
                config.alias_of,
                target.abbr,
            )


# A model config declares the class that will drive the endpoint and how it will call it.
MODEL_SYMBOL = re.compile(r"^models\s*=", re.MULTILINE)
MODEL_ATTR = re.compile(r"""attr\s*=\s*['"]([^'"]+)['"]""")
MODEL_TYPE = re.compile(r"type\s*=\s*([A-Za-z_][A-Za-z0-9_]*)")
MODEL_ABBR = re.compile(r"""abbr\s*=\s*['"]([^'"]+)['"]""")
MODEL_STREAM = re.compile(r"stream\s*=\s*(True|False)")
# `attr` is AISBench's own discriminator: every family declares "service" or "local", and
# hf_model.py spells the choice out in a comment. Only a service config drives an endpoint.
SERVICE_ATTR = "service"
# Fields a job supplies from elsewhere, so asking for them again would be asking twice: the
# model endpoint carries the address and key, and the rest identify the config itself.
SUPPLIED_FIELDS = frozenset(
    {"attr", "type", "abbr", "path", "model", "model_name", "api_key", "host_ip", "host_port", "url"}
)
GENERATION_FIELD = "generation_kwargs"


@dataclass(frozen=True)
class ConfigField:
    """A field of a model config, with the value that file gives it."""

    name: str
    default: bool | int | float | str
    kind: str


@dataclass(frozen=True)
class ModelConfig:
    """One AISBench model config file: which class drives the endpoint, and how."""

    name: str
    family: str
    class_name: str
    abbr: str
    stream: bool
    is_service: bool
    #: Editable fields of this file, in the order it declares them.
    fields: tuple[ConfigField, ...] = ()
    #: Editable entries of its generation_kwargs.
    generation_fields: tuple[ConfigField, ...] = ()

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
            fields, generation = read_model_config_fields(source)
            found.append(
                ModelConfig(
                    name=config_file.stem,
                    family=family_dir.name,
                    class_name="" if model_type is None else model_type.group(1),
                    abbr="" if abbr is None else abbr.group(1),
                    stream=stream is not None and stream.group(1) == "True",
                    is_service=attr is not None and attr.group(1) == SERVICE_ATTR,
                    fields=fields,
                    generation_fields=generation,
                )
            )
    return tuple(found)


def _literal_field(name: str, node: ast.expr) -> ConfigField | None:
    """Read one keyword of a config dict, keeping only values that are plain literals."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, bool):
        return ConfigField(name=name, default=value, kind="boolean")
    if isinstance(value, int):
        return ConfigField(name=name, default=value, kind="integer")
    if isinstance(value, float):
        return ConfigField(name=name, default=value, kind="number")
    if isinstance(value, str):
        return ConfigField(name=name, default=value, kind="text")
    return None


def _model_dict(source: str) -> ast.Call | None:
    """Find the dict(...) call inside `models = [ ... ]`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "models" for t in node.targets):
            continue
        if isinstance(node.value, ast.List) and node.value.elts:
            first = node.value.elts[0]
            if isinstance(first, ast.Call) and getattr(first.func, "id", None) == "dict":
                return first
    return None


def read_model_config_fields(source: str) -> tuple[tuple[ConfigField, ...], tuple[ConfigField, ...]]:
    """Return this config's editable fields and its generation_kwargs entries.

    The file is the list of what can be set: config files differ from one another, so a fixed
    set of inputs would offer fields one config does not have and hide fields it does.
    """
    call = _model_dict(source)
    if call is None:
        return (), ()

    fields: list[ConfigField] = []
    generation: list[ConfigField] = []
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in SUPPLIED_FIELDS:
            continue
        if keyword.arg == GENERATION_FIELD:
            if isinstance(keyword.value, ast.Call):
                for inner in keyword.value.keywords:
                    if inner.arg is None:
                        continue
                    entry = _literal_field(inner.arg, inner.value)
                    if entry is not None:
                        generation.append(entry)
            continue
        entry = _literal_field(keyword.arg, keyword.value)
        if entry is not None:
            fields.append(entry)
    return tuple(fields), tuple(generation)
