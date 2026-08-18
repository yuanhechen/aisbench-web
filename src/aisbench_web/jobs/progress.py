import re

# The worker only reports progress it can actually read. Anything else yields None so the UI
# shows "running" rather than a fabricated percentage.
EXPLICIT_PROGRESS = re.compile(r"\bPROGRESS\s+(\d+)\s*/\s*(\d+)\b")
BAR_PROGRESS = re.compile(r"\b(\d+)\s*%\s*\|.*?\|\s*(\d+)\s*/\s*(\d+)\b")


def parse_progress(line: str) -> tuple[int, int] | None:
    """Return (completed, total) when a line states both, otherwise None."""
    explicit = EXPLICIT_PROGRESS.search(line)
    if explicit is not None:
        completed, total = int(explicit.group(1)), int(explicit.group(2))
        return (completed, total) if total > 0 and completed <= total else None

    bar = BAR_PROGRESS.search(line)
    if bar is not None:
        completed, total = int(bar.group(2)), int(bar.group(3))
        return (completed, total) if total > 0 and completed <= total else None

    return None
