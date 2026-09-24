"""Command line interface: ``bacman postgres --upload --keep-local 24``."""

import argparse
import logging
import subprocess

from . import __version__
from .mysql import MySQL
from .postgres import Postgres

logger = logging.getLogger("bacman")

ENGINES = {"postgres": Postgres, "mysql": MySQL}


def _hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError:
        hours = 0
    if not hours > 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of hours, got {value!r}")
    return hours


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bacman",
        description="Take a database snapshot and optionally upload it to AWS S3. "
        "The database is read from $DATABASE_URL.",
    )
    parser.add_argument("engine", choices=ENGINES, help="database type")
    parser.add_argument("--upload", action="store_true", help="upload the snapshot to S3")
    parser.add_argument(
        "--keep-local", type=_hours, metavar="HOURS", help="delete local snapshots older than HOURS"
    )
    parser.add_argument(
        "--keep-remote", type=_hours, metavar="HOURS", help="delete S3 snapshots older than HOURS"
    )
    parser.add_argument("--directory", help="local snapshot directory [$BACMAN_DIRECTORY]")
    parser.add_argument("--prefix", help="snapshot file name prefix [$BACMAN_PREFIX]")
    parser.add_argument("--bucket", help="S3 bucket [$BACMAN_BUCKET]")
    parser.add_argument("--region", help="S3 region [$BACMAN_REGION]")
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        backup = ENGINES[args.engine](
            directory=args.directory, prefix=args.prefix, bucket=args.bucket, region=args.region
        )
        path = backup.run(
            upload=args.upload, keep_local=args.keep_local, keep_remote=args.keep_remote
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        logger.error("Backup failed: %s", exc)
        return 1
    logger.info("Done: %s", path)
    return 0
