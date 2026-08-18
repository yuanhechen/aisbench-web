"""Read the dataset catalog out of the AISBench installation itself.

A hand-written manifest can only ever describe the AISBench it was written against. The
configs that ship with the installed version are the authority on which datasets exist, which
variants each one offers, and where its data must live.
"""

import re
from dataclasses import dataclass
from pathlib import Path

CONFIGS_RELATIVE = Path("benchmark") / "configs" / "datasets"
DATASET_CONFIG_ROOT = "ais_bench.benchmark.configs.datasets"
# `path='ais_bench/datasets/gsm8k'` or `path="ais_bench/datasets/ceval/formal_ceval"`
DATA_PATH = re.compile(r"""path\s*=\s*['"]([^'"]*ais_bench/datasets/[^'"]*)['"]""")
DATASET_SYMBOL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*_datasets)\s*=", re.MULTILINE)
DATASETS_PREFIX = "ais_bench/datasets/"
PERFORMANCE_SUFFIX = "_perf"
# Written for humans to read back: gsm8k_gen_4_shot_cot_chat_prompt.
SHOTS = re.compile(r"_(\d+)_shot")


@dataclass(frozen=True)
class DatasetConfig:
    """One AISBench config file: a specific way of running a dataset."""

    name: str
    package: str
    symbol: str
    is_performance: bool
    shots: int | None
    chain_of_thought: bool
    chat_prompt: bool

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


def _describe(name: str, package: str, symbol: str) -> DatasetConfig:
    shots = SHOTS.search(name)
    return DatasetConfig(
        name=name,
        package=package,
        symbol=symbol,
        is_performance=name.endswith(PERFORMANCE_SUFFIX),
        shots=int(shots.group(1)) if shots else None,
        # "noncot" must not be read as containing "cot".
        chain_of_thought="_cot" in name and "_noncot" not in name,
        chat_prompt="chat" in name,
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
            if symbol is None:
                # Without an exported list there is nothing a generated config could import.
                continue
            configs.append(_describe(config_file.stem, package_dir.name, symbol.group(1)))
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
