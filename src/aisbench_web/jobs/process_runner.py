import logging
import os
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TERMINATION_GRACE_SECONDS = 10.0
TERMINATION_POLL_SECONDS = 0.05
# AISBench needs a toolchain environment; the web service's own configuration is not part of it.
INHERITED_ENVIRONMENT_PREFIXES = ("CONDA", "PYTHON", "ASCEND", "LD_", "CUDA", "NPU", "HCCL")
INHERITED_ENVIRONMENT_NAMES = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "AISBENCH_DATASETS_DIR",
)


def sanitized_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Pass through what AISBench needs and drop this service's own settings."""
    source = os.environ if base is None else base
    kept = {
        name: value
        for name, value in source.items()
        if name in INHERITED_ENVIRONMENT_NAMES or name.startswith(INHERITED_ENVIRONMENT_PREFIXES)
    }
    # AISBENCH_WEB_* configure the service, not the benchmark, and may name private paths.
    return {name: value for name, value in kept.items() if not name.startswith("AISBENCH_WEB_")}


class ProcessRunner:
    """Launch and stop AISBench as its own process group."""

    def build_command(
        self,
        *,
        ais_bench_path: Path,
        config_path: Path,
        cli_mode: str,
        output_dir: Path,
    ) -> list[str]:
        return [
            str(ais_bench_path),
            str(config_path),
            "--mode",
            cli_mode,
            "--work-dir",
            str(output_dir),
        ]

    def launch(
        self,
        *,
        ais_bench_path: Path,
        config_path: Path,
        cli_mode: str,
        output_dir: Path,
        log_path: Path,
        job_dir: Path,
    ) -> subprocess.Popen:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = self.build_command(
            ais_bench_path=ais_bench_path,
            config_path=config_path,
            cli_mode=cli_mode,
            output_dir=output_dir,
        )
        log_file = log_path.open("ab")
        try:
            # start_new_session makes the child a process-group leader, so stopping the job
            # reaches every process AISBench spawns rather than only the CLI itself.
            # Fixed argv, never a shell: shell=True is prohibited for job execution.
            return subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(job_dir),
                env=sanitized_environment(),
            )
        finally:
            log_file.close()

    def terminate(
        self,
        process: subprocess.Popen,
        *,
        expected_pid: int | None = None,
    ) -> int | None:
        """Stop a process group with SIGTERM, then SIGKILL if it outlives the grace period."""
        if expected_pid is not None and process.pid != expected_pid:
            # Never signal a PID the database does not still attribute to this job: it may
            # have been recycled by an unrelated process.
            logger.warning(
                "Refusing to signal pid %s; the job records pid %s", process.pid, expected_pid
            )
            return process.poll()

        self._signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return process.returncode
            time.sleep(TERMINATION_POLL_SECONDS)

        self._signal_group(process, signal.SIGKILL)
        try:
            return process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error("Process group %s survived SIGKILL", process.pid)
            return None

    @staticmethod
    def _signal_group(process: subprocess.Popen, number: int) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), number)
        except (ProcessLookupError, PermissionError):
            logger.debug("Process group for pid %s already gone", process.pid)
