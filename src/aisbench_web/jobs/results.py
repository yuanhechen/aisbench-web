import csv
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

from aisbench_web.jobs.dataset_progress import CORRECT_TOTAL_PATTERN

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
    # The same values grouped the way the job's datasets are laid out, for the detail page's
    # per-dataset rows. The flat `metrics` keys stay untouched: comparisons read them.
    per_dataset: dict[str, dict[str, Metric]] = field(default_factory=dict)
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    def add(self, key: str, raw: object) -> None:
        value, text_value, unit = _split_value(raw)
        self.metrics[key] = Metric(key=key, value=value, text_value=text_value, unit=unit)

    def add_for_dataset(self, dataset: str, metric: str, raw: object) -> None:
        value, text_value, unit = _split_value(raw)
        self.per_dataset.setdefault(dataset, {})[metric] = Metric(
            key=metric, value=value, text_value=text_value, unit=unit
        )


@dataclass(frozen=True)
class IndexedArtifact:
    kind: str
    relative_path: str
    content_type: str


# --- per-sample preview --------------------------------------------------------

#: `--dump-eval-details` keeps per-sample records; predictions are written either way.
SAMPLES_SOURCE_DETAILS = "eval_details"
SAMPLES_SOURCE_PREDICTIONS = "predictions"
SAMPLES_SOURCE_NONE = "none"

# A preview, not a mirror: whole prompts and answers can run to thousands of tokens,
# and the raw files stay one click away in the artifacts rail.
PROMPT_PREVIEW_CHARS = 1200
ANSWER_PREVIEW_CHARS = 2000


@dataclass(frozen=True)
class DatasetSample:
    id: str
    prompt: str | None
    #: What the model printed, before the evaluator's postprocessor.
    origin_prediction: str | None
    #: The postprocessed answer the evaluator actually scored.
    prediction: str | None
    reference: str | None
    correct: bool | None


@dataclass(frozen=True)
class DatasetSamples:
    source: str
    total: int
    samples: list[DatasetSample]


def read_dataset_samples(output_dir: Path, model_abbr: str, dataset: str) -> DatasetSamples:
    """One sample per line of the run's two per-sample files, for a page preview.

    The evaluator's details are the richer record — they carry what was scored and whether
    it was right — but only `--dump-eval-details` writes them; the predictions file is
    always there and still shows what the model answered.
    """
    for document in _newest(
        [
            *output_dir.glob(f"results/{model_abbr}/{dataset}.json"),
            *output_dir.glob(f"*/results/{model_abbr}/{dataset}.json"),
        ]
    ):
        payload = _load_json(document)
        if payload is None:
            continue
        details = payload.get("details")
        if not isinstance(details, dict) or not details:
            continue
        samples = [
            DatasetSample(
                id=str(key),
                prompt=_preview(_flatten_prompt(entry.get("prompt")), PROMPT_PREVIEW_CHARS),
                origin_prediction=_preview(entry.get("origin_prediction")),
                prediction=_preview(entry.get("predictions")),
                reference=_preview(_stringify(entry.get("references"))),
                correct=_correct_flag(entry.get("correct")),
            )
            for key, entry in details.items()
            if isinstance(entry, dict)
        ]
        if samples:
            return DatasetSamples(SAMPLES_SOURCE_DETAILS, len(samples), _by_sample_id(samples))

    for document in _newest(
        [
            *output_dir.glob(f"predictions/{model_abbr}/{dataset}.jsonl"),
            *output_dir.glob(f"*/predictions/{model_abbr}/{dataset}.jsonl"),
        ]
    ):
        try:
            lines = document.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        samples = []
        for line in lines:
            record = _load_json_text(line)
            if not isinstance(record, dict):
                continue
            samples.append(
                DatasetSample(
                    id=str(record.get("id", len(samples))),
                    prompt=_preview(
                        _flatten_prompt(record.get("origin_prompt")), PROMPT_PREVIEW_CHARS
                    ),
                    origin_prediction=None,
                    prediction=_preview(record.get("prediction")),
                    reference=_preview(_stringify(record.get("gold"))),
                    correct=None,
                )
            )
        if samples:
            return DatasetSamples(SAMPLES_SOURCE_PREDICTIONS, len(samples), samples)

    return DatasetSamples(SAMPLES_SOURCE_NONE, 0, [])


def _by_sample_id(samples: list[DatasetSample]) -> list[DatasetSample]:
    def order(sample: DatasetSample):
        return (0, int(sample.id)) if sample.id.isdigit() else (1, sample.id)

    return sorted(samples, key=order)


def _flatten_prompt(prompt: object) -> str | None:
    """A chat prompt arrives as [{role, prompt}, ...]; one paragraph reads better in a table."""
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, list):
        return None
    parts = []
    for turn in prompt:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip()
        content = turn.get("prompt")
        if content is None:
            continue
        parts.append(f"[{role}] {content}" if role else str(content))
    return "\n".join(parts) or None


def _stringify(value: object) -> str | None:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value) or None
    if value is None:
        return None
    return str(value)


def _preview(value: object, limit: int = ANSWER_PREVIEW_CHARS) -> str | None:
    text = _stringify(value)
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + " …"


def _correct_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, list) and value:
        return any(item is True for item in value)
    return None


def _load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_json_text(text: str) -> object | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


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
            # A dash row is AISBench's "this dataset produced nothing" marker, written when
            # the dataset's task failed; it is the absence of a result, not one.
            if metric == "-":
                continue
            model_columns = [
                column
                for column in row
                if column and column not in IDENTITY_COLUMNS and row[column] is not None
            ]
            if not model_columns:
                continue
            results.add(f"{dataset}.{metric}", row[model_columns[0]])
            results.add_for_dataset(dataset, metric, row[model_columns[0]])
    if not results.metrics:
        results.warnings.append(f"{newest.name} contained no dataset/metric rows.")
        return results
    results.counts = _correct_totals(output_dir, results.per_dataset)
    return results


def _correct_totals(
    output_dir: Path, per_dataset: dict[str, dict[str, Metric]]
) -> dict[str, tuple[int, int]]:
    """Correct/total counts, from the evaluator's own results files when they are there.

    The summary cell carries them as a "(1250/2000)" suffix, which is the fallback; the
    results/<model>/<dataset>.json the evaluator wrote is the source of truth.
    """
    documents: dict[str, Path] = {}
    for document in [
        *output_dir.glob("results/*/*.json"),
        *output_dir.glob("*/results/*/*.json"),
    ]:
        documents.setdefault(document.stem, document)
    counts: dict[str, tuple[int, int]] = {}
    for dataset, dataset_metrics in per_dataset.items():
        correct, total = None, None
        document = documents.get(dataset)
        if document is not None:
            try:
                payload = json.loads(document.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                correct = _as_int(payload.get("correct_count"))
                total = _as_int(payload.get("total_count"))
                if correct is None or total is None:
                    # Most evaluators only write accuracy plus a per-sample `details`
                    # dict; the counts are then still right there, one flag per sample.
                    counted = _counts_from_details(payload.get("details"))
                    if counted is not None:
                        correct, total = counted
        if correct is None or total is None:
            for metric in dataset_metrics.values():
                if metric.unit:
                    match = CORRECT_TOTAL_PATTERN.search(metric.unit)
                    if match:
                        correct = int(match.group(1))
                        total = int(match.group(2))
                        break
        if correct is not None and total is not None:
            counts[dataset] = (correct, total)
    return counts


def _counts_from_details(details: object) -> tuple[int, int] | None:
    """Correct/total from per-sample `correct` flags — only where the evaluator set one.

    Some evaluators leave every flag null (ARC's does); counting those as wrong invents a
    0/N that contradicts the accuracy right beside it. Unflagged samples simply do not
    count, and an evaluator that flagged nothing reports no pair at all.
    """
    if not isinstance(details, dict) or not details:
        return None
    correct = 0
    total = 0
    for entry in details.values():
        if not isinstance(entry, dict):
            continue
        flag = entry.get("correct")
        if flag is None:
            continue
        total += 1
        if flag is True or (isinstance(flag, list) and any(item is True for item in flag)):
            correct += 1
    return (correct, total) if total else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


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
        # performances/<model>/<dataset>.json: the file's stem is the dataset it measured.
        dataset = document.stem
        for raw_key, raw_value in payload.items():
            normalized = PERFORMANCE_KEYS.get(raw_key, f"extra.{raw_key}")
            if isinstance(raw_value, dict):
                for stage, value in raw_value.items():
                    results.add(f"{normalized}.{stage}", value)
                    results.add_for_dataset(dataset, f"{normalized}.{stage}", value)
            else:
                results.add(normalized, raw_value)
                results.add_for_dataset(dataset, normalized, raw_value)
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
