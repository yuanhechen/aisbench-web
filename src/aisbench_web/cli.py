import argparse
import ipaddress
import logging
import subprocess
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

import uvicorn

from aisbench_web.app import create_app
from aisbench_web.settings import Settings, discover_ais_bench

logger = logging.getLogger(__name__)
PROBE_DIAGNOSTIC_LIMIT = 500
PROBE_DIAGNOSTIC_OMISSION = "\n... diagnostic output omitted ...\n"


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AISBench Web service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".aisbench-web")
    parser.add_argument("--max-concurrent-jobs", type=int, default=1)
    return parser.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    ais_bench_path = discover_ais_bench()
    settings = Settings.create(
        data_dir=args.data_dir,
        ais_bench_path=ais_bench_path,
        max_concurrent_jobs=args.max_concurrent_jobs,
    )
    settings.ensure_layout()

    try:
        subprocess.run(
            [str(settings.ais_bench_path), "--help"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        stdout = exc.stdout.strip() if exc.stdout else ""
        diagnostic = stderr or stdout
        if len(diagnostic) > PROBE_DIAGNOSTIC_LIMIT:
            retained = PROBE_DIAGNOSTIC_LIMIT - len(PROBE_DIAGNOSTIC_OMISSION)
            beginning_length = retained // 2
            ending_length = retained - beginning_length
            diagnostic = (
                diagnostic[:beginning_length]
                + PROBE_DIAGNOSTIC_OMISSION
                + diagnostic[-ending_length:]
            )
        detail = f": {diagnostic}" if diagnostic else ""
        raise RuntimeError(
            f"AISBench probe exited with status {exc.returncode} "
            f"at {settings.ais_bench_path}{detail}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"AISBench probe timed out after {exc.timeout} seconds at {settings.ais_bench_path}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Could not launch AISBench executable at {settings.ais_bench_path}: {exc}"
        ) from exc

    logger.info("Using AISBench executable: %s", settings.ais_bench_path)
    try:
        ais_bench_version = metadata.version("ais_bench_benchmark")
    except metadata.PackageNotFoundError:
        logger.warning(
            "AISBench package version in the aisbench-web Python environment is unknown"
        )
    else:
        logger.info(
            "AISBench package version in the aisbench-web Python environment: %s",
            ais_bench_version,
        )

    if not _is_loopback_host(args.host):
        logger.warning(
            "Listening on non-loopback host %s; only expose AISBench Web on a trusted network",
            args.host,
        )

    app = create_app(settings=settings, start_worker=True)
    uvicorn.run(app, host=args.host, port=args.port)
