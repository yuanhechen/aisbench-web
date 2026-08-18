from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    # A restart leaves queued work queued (spec 9); only claimed work becomes interrupted.
    JobStatus.QUEUED: {JobStatus.STARTING, JobStatus.CANCELLED},
    JobStatus.STARTING: {
        JobStatus.RUNNING,
        JobStatus.STOPPING,
        JobStatus.FAILED,
        JobStatus.INTERRUPTED,
    },
    JobStatus.RUNNING: {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.STOPPING,
        JobStatus.INTERRUPTED,
    },
    JobStatus.STOPPING: {JobStatus.CANCELLED, JobStatus.INTERRUPTED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
    JobStatus.INTERRUPTED: set(),
}

ACTIVE_STATUSES = (JobStatus.STARTING, JobStatus.RUNNING, JobStatus.STOPPING)
TERMINAL_STATUSES = (
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.INTERRUPTED,
)


def require_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"illegal job transition: {current.value} -> {target.value}")
