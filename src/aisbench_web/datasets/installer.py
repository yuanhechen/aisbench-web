import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from aisbench_web.datasets.catalog import CatalogEntry

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_SECONDS = 30.0
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
# An archive must be written and then unpacked beside itself, so require headroom for both.
DISK_HEADROOM_FACTOR = 3


def _validated_member_path(target: Path, name: str) -> Path:
    """Resolve an archive member lexically and refuse anything that escapes ``target``."""
    # A backslash never escapes on POSIX, but it is a separator elsewhere and no legitimate
    # dataset archive uses one, so refuse it rather than reason about the current platform.
    if not name or name.startswith("/") or "\\" in name or ":" in name.split("/")[0]:
        raise ValueError(f"unsafe archive member: {name!r}")
    resolved = os.path.normpath(os.path.join(str(target), name))
    prefix = str(target) + os.sep
    if resolved != str(target) and not resolved.startswith(prefix):
        raise ValueError(f"unsafe archive member: {name!r}")
    return Path(resolved)


def extract_archive(archive: Path, target: Path) -> None:
    """Extract a zip or tar archive, refusing any member that could escape ``target``."""
    target.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                _validated_member_path(target, name)
            bundle.extractall(target)
        return

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            for member in members:
                _validated_member_path(target, member.name)
                if member.issym() or member.islnk():
                    raise ValueError(f"unsafe archive member: {member.name!r} is a link")
                if not (member.isfile() or member.isdir()):
                    raise ValueError(
                        f"unsafe archive member: {member.name!r} is not a file or directory"
                    )
            bundle.extractall(target, members=members)
        return

    raise ValueError(f"Unsupported dataset archive format: {archive.name}")


class DatasetInstaller:
    """Download, verify, and atomically place one shared dataset directory."""

    def __init__(
        self,
        downloads_dir: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        self.downloads_dir = downloads_dir
        self._transport = transport
        self._timeout = timeout

    def install(self, entry: "CatalogEntry", target: Path) -> Path:
        if entry.download is None:
            raise ValueError(f"Dataset {entry.id!r} has no verified download source")
        if target.is_dir():
            return target

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._require_disk_space(entry)

        part = self.downloads_dir / f"{entry.id}.part"
        staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}.incoming-"))
        try:
            self._download(entry, part)
            extract_archive(part, staging)
            source = self._extracted_root(staging, target.name)
            # os.replace cannot merge into an existing directory, and a concurrent installer may
            # have finished first, so treat an occupied target as success.
            try:
                os.rename(source, target)
            except OSError:
                if not target.is_dir():
                    raise
        finally:
            part.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
        return target

    def _require_disk_space(self, entry: "CatalogEntry") -> None:
        if entry.download is None or entry.download.size_bytes is None:
            return
        required = entry.download.size_bytes * DISK_HEADROOM_FACTOR
        free = shutil.disk_usage(self.downloads_dir).free
        if free < required:
            raise ValueError(
                f"Not enough disk space for {entry.id!r}: "
                f"{required} bytes required, {free} bytes free"
            )

    def _download(self, entry: "CatalogEntry", part: Path) -> None:
        if entry.download is None:
            raise ValueError(f"Dataset {entry.id!r} has no verified download source")
        digest = hashlib.sha256()
        with (
            httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=True,
            ) as client,
            client.stream("GET", entry.download.url) as response,
            part.open("wb") as output,
        ):
            response.raise_for_status()
            for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                digest.update(chunk)
                output.write(chunk)

        if entry.download is not None and digest.hexdigest() != entry.download.sha256:
            raise ValueError(
                f"Dataset {entry.id!r} failed its checksum: "
                f"expected {entry.download.sha256}, downloaded {digest.hexdigest()}"
            )

    @staticmethod
    def _extracted_root(staging: Path, expected_name: str) -> Path:
        """Unwrap the common ``<name>/...`` archive layout so the target is not nested twice."""
        children = list(staging.iterdir())
        if len(children) == 1 and children[0].is_dir() and children[0].name == expected_name:
            return children[0]
        return staging
