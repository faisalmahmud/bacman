"""Base class for taking database snapshots and shipping them to AWS S3."""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

DEFAULT_KEEP_HOURS = 720  # 30 days


@dataclass(frozen=True)
class Database:
    """Connection details parsed from a database URL."""

    name: str
    user: str = ""
    password: str = field(default="", repr=False)
    host: str = ""
    port: int | None = None

    @classmethod
    def from_url(cls, url: str) -> "Database":
        """Parse ``scheme://user:password@host:port/name``.

        Percent-encoded parts are decoded, so a Unix socket directory can be
        given as the host, e.g. ``postgres://%2Fvar%2Frun%2Fpostgresql/db``.
        """
        parts = urlsplit(url)
        name = unquote(parts.path.lstrip("/"))
        if not name:
            raise ValueError("Database URL has no database name")
        return cls(
            name=name,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            host=unquote(parts.hostname or ""),
            port=parts.port,
        )


def _max_age(hours: float) -> timedelta:
    if isinstance(hours, bool) or not hours > 0:
        raise ValueError(f"Expected a positive number of hours, got {hours!r}")
    return timedelta(hours=hours)


class BacMan:
    """Take a snapshot of a database, upload it to S3 and prune old snapshots.

    Settings not passed explicitly are read from the environment when the
    instance is created: ``DATABASE_URL``, ``BACMAN_DIRECTORY``,
    ``BACMAN_PREFIX``, ``BACMAN_BUCKET`` and ``BACMAN_REGION``. AWS
    credentials are resolved by boto3 (environment, config files or IAM role).

    Subclasses set :attr:`prefix` and :attr:`suffix` and implement :meth:`dump`.
    """

    prefix = "sqldump"
    suffix = "sql"

    def __init__(
        self,
        database_url: str | None = None,
        *,
        directory: str | os.PathLike | None = None,
        prefix: str | None = None,
        bucket: str | None = None,
        region: str | None = None,
    ) -> None:
        url = database_url or os.environ.get("DATABASE_URL")
        if not url:
            raise ValueError("No database URL given and DATABASE_URL is not set")
        self.db = Database.from_url(url)
        self.directory = Path(directory or os.environ.get("BACMAN_DIRECTORY", "/tmp/bacman"))
        self.prefix = prefix or os.environ.get("BACMAN_PREFIX") or self.prefix
        self.bucket = bucket or os.environ.get("BACMAN_BUCKET")
        self.region = region or os.environ.get("BACMAN_REGION", "eu-west-1")

    def run(
        self,
        *,
        upload: bool = False,
        keep_local: float | None = None,
        keep_remote: float | None = None,
    ) -> Path:
        """Take a snapshot, then optionally upload it and prune old snapshots.

        ``keep_local`` / ``keep_remote`` are ages in hours: older snapshots are
        deleted locally / from the bucket. ``None`` leaves them alone.
        Returns the path of the new snapshot.
        """
        # Validate everything up front rather than failing after a long dump.
        for hours in (keep_local, keep_remote):
            if hours is not None:
                _max_age(hours)
        if upload or keep_remote is not None:
            self._require_bucket()

        path = self.snapshot()
        if upload:
            self.upload(path)
        if keep_remote is not None:
            self.prune_remote(keep_remote)
        if keep_local is not None:
            self.prune_local(keep_local)
        return path

    def dump(self, path: Path) -> None:
        """Write a dump of the database to ``path``."""
        raise NotImplementedError

    def snapshot(self) -> Path:
        """Dump the database into a new timestamped file and return its path."""
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = (self.directory / f"{self.prefix}-{stamp}.{self.suffix}").resolve()
        logger.info("Dumping database %r to %s", self.db.name, path)
        try:
            self.dump(path)
        except BaseException:
            path.unlink(missing_ok=True)  # don't leave a partial dump behind
            raise
        return path

    def is_snapshot(self, name: str) -> bool:
        """Whether a file or object name looks like one of our snapshots."""
        return name.startswith(f"{self.prefix}-") and name.endswith(f".{self.suffix}")

    def prune_local(self, hours: float = DEFAULT_KEEP_HOURS) -> list[Path]:
        """Delete local snapshots older than ``hours``. Returns deleted paths."""
        cutoff = time.time() - _max_age(hours).total_seconds()
        if not self.directory.is_dir():
            return []
        deleted = []
        for path in self.directory.iterdir():
            if self.is_snapshot(path.name) and path.is_file() and path.stat().st_mtime < cutoff:
                logger.info("Deleting local snapshot %s", path)
                path.unlink()
                deleted.append(path)
        return deleted

    @cached_property
    def s3(self):
        """boto3 S3 client, created on first use."""
        import boto3  # imported lazily so local-only backups stay fast

        return boto3.client("s3", region_name=self.region)

    def _require_bucket(self) -> str:
        if not self.bucket:
            raise ValueError("No S3 bucket given and BACMAN_BUCKET is not set")
        return self.bucket

    def upload(self, path: str | os.PathLike) -> str:
        """Upload a snapshot to the bucket under its file name. Returns the key."""
        bucket = self._require_bucket()
        key = Path(path).name
        logger.info("Uploading %s to s3://%s/%s", path, bucket, key)
        self.s3.upload_file(os.fspath(path), bucket, key)
        return key

    def prune_remote(self, hours: float = DEFAULT_KEEP_HOURS) -> list[str]:
        """Delete snapshots older than ``hours`` from the bucket. Returns deleted keys.

        Only objects named like our snapshots (``<prefix>-*.<suffix>``) are
        touched, so the bucket can safely hold other files.
        """
        cutoff = datetime.now(UTC) - _max_age(hours)
        bucket = self._require_bucket()
        deleted = []
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{self.prefix}-"
        )
        for page in pages:
            keys = [
                obj["Key"]
                for obj in page.get("Contents", [])
                if self.is_snapshot(obj["Key"]) and obj["LastModified"] < cutoff
            ]
            if not keys:
                continue
            logger.info("Deleting %d snapshot(s) from s3://%s: %s", len(keys), bucket, keys)
            response = self.s3.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True}
            )
            if errors := response.get("Errors"):
                raise RuntimeError(f"Failed to delete from s3://{bucket}: {errors}")
            deleted += keys
        return deleted
