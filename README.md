# bacman

A small tool that takes a snapshot of a Postgres or MySQL database, uploads it
to AWS S3 and prunes old snapshots.

Requires Python 3.11+ and `pg_dump` / `mysqldump` on the `PATH`.

## Installation

```bash
pip install bacman
```

## Configuration

Settings are read from the environment (e.g. `/etc/environment`, a systemd
unit or your crontab):

| Variable           | Description                                   | Default                                 |
| ------------------ | --------------------------------------------- | --------------------------------------- |
| `DATABASE_URL`     | e.g. `postgres://user:pass@localhost:5432/db` | required                                |
| `BACMAN_DIRECTORY` | Where snapshots are written                   | `/tmp/bacman`                           |
| `BACMAN_PREFIX`    | Snapshot file name prefix                     | `pgdump` (Postgres), `mysqldump` (MySQL) |
| `BACMAN_BUCKET`    | S3 bucket to upload to                        | required for S3                         |
| `BACMAN_REGION`    | S3 region                                     | `eu-west-1`                             |

Special characters in the URL's password must be percent-encoded (`@` → `%40`).

AWS credentials are picked up by boto3 the usual way: `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, `~/.aws/credentials`, or an IAM role.

Snapshots are named `<prefix>-YYYYMMDD-HHMMSS.<bak|sql>`. Pruning only ever
touches files and S3 objects that match that pattern, so the directory and
bucket can safely hold other files.

## Command line

```bash
# Snapshot only
bacman postgres

# Snapshot, upload to S3, delete local snapshots older than 24 hours
# and S3 snapshots older than 30 days
bacman postgres --upload --keep-local 24 --keep-remote 720

bacman mysql --help
```

The command exits with a non-zero status if anything fails, so it plays well
with cron and monitoring. For example, to take a snapshot every 2 hours:

```
0 */2 * * * /path/to/venv/bin/bacman postgres --upload --keep-local 48 >> /var/log/bacman.log 2>&1
```

## Python

```python
from bacman import Postgres

Postgres().run(upload=True, keep_local=24, keep_remote=720)
```

Settings can also be passed explicitly instead of via the environment:

```python
from bacman import MySQL

backup = MySQL("mysql://user:pass@localhost/shop", directory="/backups", bucket="my-bucket")
path = backup.snapshot()  # just take a snapshot
backup.upload(path)  # upload a file to the bucket
backup.prune_local(hours=24)  # delete old local snapshots
backup.prune_remote(hours=720)  # delete old snapshots from the bucket
```

Passwords are handed to `pg_dump` / `mysqldump` via the environment or a
private temporary file, never on the command line.

## Upgrading from 0.x

1.0 requires Python 3.11+, replaces `boto` with `boto3` and drops
`dj-database-url`. The API is now explicit instead of doing everything in the
constructor:

| 0.x                                                        | 1.0                              |
| ---------------------------------------------------------- | -------------------------------- |
| `Postgres()`                                               | `Postgres().run()`               |
| `Postgres(to_remote=True)`                                 | `Postgres().run(upload=True)`    |
| `Postgres(cleanup_local_snapshots=True)`                   | `Postgres().run(keep_local=720)` |
| `Postgres(cleanup_local_snapshots=True, local_snapshot_timeout=24)`   | `Postgres().run(keep_local=24)`  |
| `Postgres(cleanup_remote_snapshots=True, remote_snapshot_timeout=24)` | `Postgres().run(keep_remote=24)` |

Other changes:

- Remote cleanup only deletes objects named like bacman snapshots instead of
  everything in the bucket, and compares timestamps in UTC.
- A failed dump raises an error (non-zero exit on the CLI) and leaves no
  partial file behind, instead of being silently ignored.
- Invalid ages raise `ValueError` instead of falling back to the default.
- Settings are read when the object is created rather than at import time,
  and importing bacman no longer configures logging.
- The MySQL port from `DATABASE_URL` is now honoured.

## Development

```bash
pip install -e . --group dev
ruff check . && ruff format --check . && pytest
```
