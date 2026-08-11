"""Raw FIT files on disk.

Downloaded files are kept forever, in their original bytes. That costs about
150 kB per activity — a decade of triathlon training fits in well under a
gigabyte — and buys the ability to re-parse the entire history whenever the
parser learns a new field, without asking Garmin for anything.

It is also the only copy that survives the auth breaking, which given this
project's history is not a hypothetical.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from garmin_mcp.config import Settings, get_settings
from garmin_mcp.errors import FitParseError, IngestError
from garmin_mcp.ingest.fit_parser import hash_fit_bytes
from garmin_mcp.logging import get_logger

log = get_logger(__name__)

# A FIT file declares itself at byte 8 of its header.
_FIT_MAGIC = b".FIT"
_ZIP_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True, slots=True)
class StoredFile:
    """A FIT file that now exists at a known path with a known hash."""

    path: Path
    file_hash: str
    bytes_written: int
    already_present: bool


def looks_like_fit(data: bytes) -> bool:
    return len(data) >= 12 and data[8:12] == _FIT_MAGIC


def looks_like_zip(data: bytes) -> bool:
    return data[:4] == _ZIP_MAGIC


def extract_fit_bytes(data: bytes) -> bytes:
    """Return raw FIT bytes, unwrapping the ZIP that Garmin serves.

    `download_activity(..., ORIGINAL)` does not return a FIT file: it returns a
    ZIP archive containing one. Feeding that straight to the parser fails with
    a confusing "not a FIT file", so the unwrapping happens here, once.
    """
    if looks_like_fit(data):
        return data

    if not looks_like_zip(data):
        raise FitParseError(
            "downloaded payload is neither a FIT file nor a ZIP archive "
            f"(first bytes: {data[:8]!r})"
        )

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".fit")]
        if not members:
            raise FitParseError(f"ZIP archive contains no FIT file: {archive.namelist()}")
        if len(members) > 1:
            log.warning("store.multiple_fit_in_zip", members=members)
        extracted = archive.read(members[0])

    if not looks_like_fit(extracted):
        raise FitParseError(f"file inside the archive is not a FIT file: {members[0]}")
    return extracted


def raw_path_for(
    activity_id: int,
    started_at: datetime | None,
    settings: Settings | None = None,
) -> Path:
    """Where a given activity's raw file belongs.

    Sharded by year and month so the directory stays browsable after a few
    thousand activities. Files with no usable date land in `unknown/`.
    """
    settings = settings or get_settings()
    if started_at is None:
        return settings.raw_dir / "unknown" / f"{activity_id}.fit"
    return settings.raw_dir / f"{started_at:%Y}" / f"{started_at:%m}" / f"{activity_id}.fit"


def store_fit_bytes(
    data: bytes,
    *,
    activity_id: int | str,
    started_at: datetime | None = None,
    settings: Settings | None = None,
    overwrite: bool = False,
) -> StoredFile:
    """Write FIT bytes to their canonical location, unwrapping a ZIP if needed.

    Writing goes to a temporary file first and is then renamed, so an
    interrupted run can never leave a half-written FIT that would later be
    hashed and recorded as if it were complete.
    """
    settings = settings or get_settings()
    fit_bytes = extract_fit_bytes(data)
    file_hash = hash_fit_bytes(fit_bytes)

    destination = raw_path_for(activity_id, started_at, settings)  # type: ignore[arg-type]
    if destination.exists() and not overwrite:
        return StoredFile(destination, file_hash, len(fit_bytes), already_present=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".fit.part")
    try:
        temporary.write_bytes(fit_bytes)
        temporary.replace(destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise IngestError(f"could not write {destination}: {exc}") from exc

    log.info(
        "store.saved",
        path=str(destination.relative_to(settings.data_dir)),
        bytes=len(fit_bytes),
        file_hash=file_hash[:12],
    )
    return StoredFile(destination, file_hash, len(fit_bytes), already_present=False)


def adopt_local_file(
    source_path: Path,
    *,
    activity_id: int | str,
    started_at: datetime | None = None,
    settings: Settings | None = None,
) -> StoredFile:
    """Copy a file dropped in the inbox into the managed raw store."""
    return store_fit_bytes(
        source_path.read_bytes(),
        activity_id=activity_id,
        started_at=started_at,
        settings=settings,
    )


def iter_inbox(settings: Settings | None = None) -> list[Path]:
    """FIT files awaiting manual import, oldest first.

    The `_processed` and `_failed` subdirectories are skipped — they are where
    files go after a run, and re-reading them would loop forever.
    """
    settings = settings or get_settings()
    if not settings.inbox_dir.is_dir():
        return []
    return sorted(
        (
            path
            for path in settings.inbox_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".fit"
        ),
        key=lambda p: p.stat().st_mtime,
    )


def move_to(path: Path, target_dir: Path) -> Path:
    """Move a processed inbox file aside, without ever clobbering."""
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / path.name
    counter = 1
    while destination.exists():
        destination = target_dir / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    path.replace(destination)
    return destination
