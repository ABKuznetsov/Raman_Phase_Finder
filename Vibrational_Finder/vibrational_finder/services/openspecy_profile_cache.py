"""Local random access to profiles decoded from an OpenSpecy RDS archive."""

from contextlib import closing, suppress
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
import zlib

import numpy as np

from vibrational_finder.cancellation import checkpoint


_FLOAT = np.dtype("<f8")
_MAX_PROFILE_BYTES = 256 * 1024 * 1024


def profile_cache_path(archive: Path) -> Path:
    return archive.with_suffix(".profiles-v1.sqlite")


def archive_stamp(archive: Path) -> str:
    info = archive.stat()
    return f"{info.st_size}:{info.st_mtime_ns}"


def read_profile(archive: Path, stamp: str, sample: str) -> tuple[np.ndarray, np.ndarray] | None:
    cache = profile_cache_path(archive)
    if not cache.is_file():
        return None
    try:
        with closing(sqlite3.connect(cache.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as connection:
            row = connection.execute(
                "select axis, intensity from archive_info cross join profiles where stamp = ? and sample = ?",
                (stamp, sample),
            ).fetchone()
        if row is not None and row[0] and len(row[0]) % _FLOAT.itemsize == 0:
            axis, compressed = row
            decoder = zlib.decompressobj()
            intensities = decoder.decompress(compressed, len(axis) + 1)
            if decoder.eof and len(intensities) == len(axis):
                return np.frombuffer(axis, dtype=_FLOAT), np.frombuffer(intensities, dtype=_FLOAT)
    except (OSError, sqlite3.Error, TypeError, ValueError, zlib.error):
        pass  # The source archive remains usable if the derived cache is unavailable.
    return None


def write_profiles(archive: Path, stamp: str, coordinates, spectra) -> None:
    """Publish a complete cache for this archive revision, leaving the RDS untouched."""
    temporary: Path | None = None
    try:
        x = np.asarray(coordinates, dtype=_FLOAT).reshape(-1)
        if not x.size or x.nbytes * len(spectra.columns) > _MAX_PROFILE_BYTES:
            return
        with NamedTemporaryFile(dir=archive.parent, prefix=archive.name + ".", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.execute("create table archive_info (stamp text primary key, axis blob not null)")
            connection.execute("create table profiles (sample text primary key, intensity blob not null) without rowid")
            connection.execute("insert into archive_info values (?, ?)", (stamp, x.tobytes()))
            for sample, values in spectra.items():
                checkpoint()
                if isinstance(sample, str):
                    y = np.asarray(values, dtype=_FLOAT).reshape(-1)
                    if y.size == x.size:
                        connection.execute("insert into profiles values (?, ?)", (sample, zlib.compress(y.tobytes(), 1)))
        if archive_stamp(archive) == stamp:
            temporary.replace(profile_cache_path(archive))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
