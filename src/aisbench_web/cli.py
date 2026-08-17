import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from aisbench_web.app import create_app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AISBench Web service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".aisbench-web")
    parser.add_argument("--max-concurrent-jobs", type=int, default=1)
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    app = create_app(
        data_dir=args.data_dir,
        max_concurrent_jobs=args.max_concurrent_jobs,
        start_worker=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)
