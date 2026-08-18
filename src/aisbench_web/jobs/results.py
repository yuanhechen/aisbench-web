import csv
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SUMMARY_DIRNAME = "summary"
PERFORMANCES_DIRNAME = "performances"
PREDICTIONS_DIRNAME = "predictions"
# AISBench's DefaultSummarizer writes these identity columns; every other column is a model.
IDENTITY_COLUMNS = ("dataset", "version", "metric", "mode")
# Values arrive as "123.45 ms" or "10.2 token/s"; the number and unit are split back apart.
NUMBER_WITH_UNIT = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([^\s].*?)?\s*$")

# Names produced by DefaultPerfMetricCalculator, normalized so the UI and comparisons do not
# depend on AISBench's spelling. Anything absent from this map is preserved under extra.*.
PERFORMANCE_KEYS = {
    "E2EL": "latency.e2e",
    "TTFT": "latency.ttft",
    "TPOT": "latency.tpot",
    "ITL": "latency.itl",
    "Output Token Throughput": "throughput.output_tokens",
    "Input Token Throughput": "throughput.input_tokens",
    "Total Token Throughput": "throughput.total_tokens",
    "Request Throughput": "throughput.requests",
    "Success Requests": "requests.succeeded",
    "Failed Requests": "requests.failed",
    "Total Requests": "requests.total",
    "Benchmark Duration": "duration.benchmark",
}

CONTENT_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
    ".log": "text/plain",
}


@dataclass(frozen=True)
class Metric:
    key: str
    value: float | None
    text_value: str | None
    unit: str | None


@dataclass
class ParsedResults:
    metrics: dict[str, Metric] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, key: str, raw: object) -> None:
        value, text_value, unit = _split_value(raw)
        self.metrics[key] = Metric(key=key, value=value, text_value=text_value, unit=unit)


@dataclass(frozen=True)
class IndexedArtifact:
    kind: str
    relative_path: str
    content_type: str


def _split_value(raw: object) -> tuple[float | None, str | None, str | None]:
    if isinstance(raw, bool):
        return None, str(raw), None
    if isinstance(raw, (int, float)):
        return float(raw), None, None
    text = str(raw).strip()
    match = NUMBER_WITH_UNIT.match(text)
    if match is None:
        return None, text, None
    return float(match.group(1)), None, match.group(2) or None


def safe_artifact_path(job_dir: Path, relative_path: str) -> Path:
    """Resolve an artifact inside its job directory or refuse.

    Symlinks are resolved before the check, so a link planted in the output directory cannot
    be used to read a file the job does not own.
    """
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError(f"artifact escapes job directory: {relative_path!r}")
    root = job_dir.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"artifact escapes job directory: {relative_path!r}")
    return candidate


def _newest(paths: list[Path]) -> list[Path]:
    """Sort by modification time so the most recent run wins regardless of directory naming."""
    return sorted(paths, key=lambda path: (path.stat().st_mtime, path.name))


def parse_accuracy(output_dir: Path) -> ParsedResults:
    """Read the newest summary CSV; identity columns label the row, model columns carry values.

    AISBench writes into a timestamped run directory under the work dir, so the summary is
    searched at any depth rather than only directly beneath it.
    """
    results = ParsedResults()
    summaries = _newest(
        [
            *output_dir.glob(f"{SUMMARY_DIRNAME}/summary_*.csv"),
            *output_dir.glob(f"*/{SUMMARY_DIRNAME}/summary_*.csv"),
        ]
    )
    if not summaries:
        results.warnings.append("No accuracy summary was produced under summary/.")
        return results

    newest = summaries[-1]
    with newest.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            dataset = (row.get("dataset") or "").strip()
            metric = (row.get("metric") or "").strip()
            if not dataset or not metric:
                continue
            model_columns = [
                column
                for column in row
                if column and column not in IDENTITY_COLUMNS and row[column] is not None
            ]
            if not model_columns:
                continue
            results.add(f"{dataset}.{metric}", row[model_columns[0]])
    if not results.metrics:
        results.warnings.append(f"{newest.name} contained no dataset/metric rows.")
    return results


def parse_performance(output_dir: Path) -> ParsedResults:
    """Normalize recognized performance metrics and keep unknown ones under extra.*."""
    results = ParsedResults()
    documents = _newest(
        [
            *output_dir.glob(f"{PERFORMANCES_DIRNAME}/*/*.json"),
            *output_dir.glob(f"*/{PERFORMANCES_DIRNAME}/*/*.json"),
        ]
    )
    if not documents:
        results.warnings.append("No performance summary was produced under performances/.")
        return results

    for document in documents:
        try:
            payload = json.loads(document.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            results.warnings.append(f"Could not read {document.name}: {exc}")
            continue
        if not isinstance(payload, dict):
            results.warnings.append(f"{document.name} did not contain a metrics object.")
            continue
        for raw_key, raw_value in payload.items():
            normalized = PERFORMANCE_KEYS.get(raw_key, f"extra.{raw_key}")
            if isinstance(raw_value, dict):
                for stage, value in raw_value.items():
                    results.add(f"{normalized}.{stage}", value)
            else:
                results.add(normalized, raw_value)
    return results


def parse_results(mode: str, output_dir: Path) -> ParsedResults:
    return parse_accuracy(output_dir) if mode == "accuracy" else parse_performance(output_dir)


KIND_BY_DIRECTORY = {
    SUMMARY_DIRNAME: "summary",
    PREDICTIONS_DIRNAME: "prediction",
    PERFORMANCES_DIRNAME: "performance",
    "results": "result",
    "logs": "log",
    "configs": "config",
}


def _artifact_kind(relative: Path) -> str:
    """Classify by the first recognised directory at any depth.

    The run directory is named after a timestamp, so the meaningful component is rarely the
    first one.
    """
    if relative.suffix == ".html":
        return "visualization"
    for part in relative.parts[:-1]:
        kind = KIND_BY_DIRECTORY.get(part)
        if kind is not None:
            return kind
    return "other"


def index_artifacts(output_dir: Path) -> list[IndexedArtifact]:
    """List regular files under the job's output directory; symlinks are never indexed."""
    if not output_dir.is_dir():
        return []
    root = output_dir.resolve()
    indexed: list[IndexedArtifact] = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            relative = path.resolve().relative_to(root)
        except ValueError:
            continue
        content_type = CONTENT_TYPES.get(
            path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        indexed.append(
            IndexedArtifact(
                kind=_artifact_kind(relative),
                relative_path=relative.as_posix(),
                content_type=content_type,
            )
        )
    return indexed
