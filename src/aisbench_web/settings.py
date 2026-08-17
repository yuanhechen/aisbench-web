import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def discover_ais_bench() -> Path:
    override = os.environ.get("AISBENCH_WEB_AIS_BENCH_PATH")
    if override is not None:
        executable = Path(override).expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("AISBENCH_WEB_AIS_BENCH_PATH is not an executable file")
        return executable

    discovered = shutil.which("ais_bench")
    if discovered is None:
        raise RuntimeError(
            "Could not find ais_bench; activate or install AISBench before starting AISBench Web"
        )
    return Path(discovered).resolve()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ais_bench_path: Path
    max_concurrent_jobs: int

    @classmethod
    def create(
        cls,
        data_dir: Path,
        ais_bench_path: Path,
        max_concurrent_jobs: int,
    ) -> "Settings":
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        return cls(
            data_dir=data_dir.expanduser().resolve(),
            ais_bench_path=ais_bench_path.expanduser().resolve(),
            max_concurrent_jobs=max_concurrent_jobs,
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aisbench-web.db"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    def ensure_layout(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.jobs_dir.mkdir(exist_ok=True)
        self.downloads_dir.mkdir(exist_ok=True)
