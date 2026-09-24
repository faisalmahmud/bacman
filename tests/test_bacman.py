import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bacman import BacMan, Database, MySQL, Postgres
from bacman.cli import main

URL = "postgres://alice:s%40cret@db.example.com:5433/shop"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for var in ("BACMAN_PREFIX", "BACMAN_BUCKET", "BACMAN_REGION", "PGPASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("BACMAN_DIRECTORY", str(tmp_path))


@pytest.fixture
def calls(monkeypatch):
    """Record subprocess.run calls and write a fake dump instead of running anything."""
    recorded = []

    def fake_run(cmd, check, env=None):
        assert check
        cnf = next((a.split("=", 1)[1] for a in cmd if a.startswith("--defaults-extra-file=")), "")
        recorded.append({"cmd": cmd, "env": env, "cnf": Path(cnf).read_text() if cnf else None})
        out = next(a.split("=", 1)[1] for a in cmd if a.startswith(("--file=", "--result-file=")))
        with open(out, "w") as f:
            f.write("dump")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return recorded


class FakeS3:
    def __init__(self, objects=()):
        self.objects = dict(objects)  # key -> LastModified
        self.uploads = []
        self.errors = []

    def upload_file(self, filename, bucket, key):
        self.uploads.append((filename, bucket, key))

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        yield {"Contents": [{"Key": k, "LastModified": self.objects[k]} for k in keys]}

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            del self.objects[obj["Key"]]
        return {"Errors": self.errors}


# Configuration


def test_parse_url():
    db = Database.from_url(URL)
    assert db == Database("shop", "alice", "s@cret", "db.example.com", 5433)
    assert "s@cret" not in repr(db)


def test_parse_url_with_socket_and_no_credentials():
    db = Database.from_url("postgres://%2Fvar%2Frun%2Fpostgresql/shop")
    assert db == Database("shop", host="/var/run/postgresql")


def test_parse_url_without_name():
    with pytest.raises(ValueError, match="no database name"):
        Database.from_url("postgres://localhost/")


def test_settings_read_at_init_not_import(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Postgres()
    monkeypatch.setenv("DATABASE_URL", "mysql://u@h/other")
    monkeypatch.setenv("BACMAN_BUCKET", "b")
    b = MySQL()
    assert (b.db.name, b.directory, b.prefix, b.bucket, b.region) == (
        "other",
        tmp_path,
        "mysqldump",
        "b",
        "eu-west-1",
    )


def test_explicit_settings_win(monkeypatch):
    monkeypatch.setenv("BACMAN_PREFIX", "env")
    b = Postgres("postgres://h/x", directory="/d", prefix="p", bucket="b", region="r")
    assert (b.db.name, str(b.directory), b.prefix, b.bucket, b.region) == ("x", "/d", "p", "b", "r")


# Dumping


def test_postgres_dump(calls, tmp_path):
    path = Postgres().snapshot()
    assert path.parent == tmp_path.resolve()
    assert path.name.startswith("pgdump-") and path.name.endswith(".bak")
    assert path.read_text() == "dump"
    [call] = calls
    assert call["cmd"] == [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        f"--file={path}",
        "--host=db.example.com",
        "--port=5433",
        "--username=alice",
        "--dbname=shop",
    ]
    assert call["env"]["PGPASSWORD"] == "s@cret"
    assert "s@cret" not in " ".join(call["cmd"])


def test_postgres_dump_without_password_inherits_env(calls):
    Postgres("postgres://localhost/shop").snapshot()
    assert calls[0]["env"] is None


def test_mysql_dump(calls, tmp_path):
    path = MySQL('mysql://bob:p"w\\d@localhost:3307/shop').snapshot()
    assert path.name.startswith("mysqldump-") and path.name.endswith(".sql")
    [call] = calls
    cmd = call["cmd"]
    assert cmd[0] == "mysqldump"
    assert cmd[1].startswith("--defaults-extra-file=")
    assert cmd[2:] == [
        f"--result-file={path}",
        "--host=localhost",
        "--port=3307",
        "--user=bob",
        "shop",
    ]
    assert call["cnf"] == '[client]\npassword="p\\"w\\\\d"\n'
    assert not os.path.exists(cmd[1].split("=", 1)[1])  # option file is cleaned up


def test_failed_dump_removes_partial_file(monkeypatch, tmp_path):
    def fail(cmd, check, env=None):
        open(cmd[4].split("=", 1)[1], "w").close()
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        Postgres().snapshot()
    assert list(tmp_path.iterdir()) == []


# Pruning


def test_prune_local(tmp_path):
    old = time.time() - 25 * 3600
    for name in ("pgdump-old.bak", "pgdump-new.bak", "pgdump-old.txt", "other-old.bak"):
        (tmp_path / name).touch()
        if "old" in name:
            os.utime(tmp_path / name, (old, old))

    deleted = Postgres().prune_local(24)

    assert [p.name for p in deleted] == ["pgdump-old.bak"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "other-old.bak",
        "pgdump-new.bak",
        "pgdump-old.txt",
    ]


def test_prune_local_missing_directory(tmp_path):
    assert Postgres(directory=tmp_path / "nope").prune_local() == []


@pytest.mark.parametrize("hours", [0, -1, True, float("nan")])
def test_prune_rejects_bad_hours(hours):
    with pytest.raises(ValueError, match="positive number of hours"):
        Postgres().prune_local(hours)


def test_prune_remote():
    now = datetime.now(UTC)
    old = now - timedelta(hours=25)
    s3 = FakeS3(
        {
            "pgdump-old.bak": old,
            "pgdump-new.bak": now,
            "pgdump-old.txt": old,
            "other-old.bak": old,
        }
    )
    b = Postgres(bucket="b")
    b.s3 = s3

    assert b.prune_remote(24) == ["pgdump-old.bak"]
    assert sorted(s3.objects) == ["other-old.bak", "pgdump-new.bak", "pgdump-old.txt"]


def test_prune_remote_raises_on_errors():
    s3 = FakeS3({"pgdump-old.bak": datetime.now(UTC) - timedelta(days=60)})
    s3.errors = [{"Key": "pgdump-old.bak", "Code": "AccessDenied"}]
    b = Postgres(bucket="b")
    b.s3 = s3
    with pytest.raises(RuntimeError, match="AccessDenied"):
        b.prune_remote()


# Running


def test_run_uploads_and_prunes(calls, tmp_path):
    stale = tmp_path / "pgdump-stale.bak"
    stale.touch()
    os.utime(stale, (0, 0))
    b = Postgres(bucket="b")
    b.s3 = FakeS3({"pgdump-stale.bak": datetime(2000, 1, 1, tzinfo=UTC)})

    path = b.run(upload=True, keep_local=1, keep_remote=1)

    assert b.s3.uploads == [(str(path), "b", path.name)]
    assert b.s3.objects == {}
    assert list(tmp_path.iterdir()) == [path]


def test_run_validates_before_dumping(calls):
    with pytest.raises(ValueError, match="BACMAN_BUCKET"):
        Postgres().run(upload=True)
    with pytest.raises(ValueError, match="BACMAN_BUCKET"):
        Postgres().run(keep_remote=1)
    with pytest.raises(ValueError, match="hours"):
        Postgres().run(keep_local=0)
    assert calls == []


def test_base_class_requires_dump():
    with pytest.raises(NotImplementedError):
        BacMan().snapshot()


# CLI


def test_cli(monkeypatch, calls, tmp_path):
    seen = {}
    monkeypatch.setattr(Postgres, "prune_local", lambda self, hours: seen.setdefault("h", hours))
    assert main(["postgres", "--keep-local", "12.5", "--prefix", "nightly", "-q"]) == 0
    assert seen == {"h": 12.5}
    assert [p.name[:8] for p in tmp_path.iterdir()] == ["nightly-"]


def test_cli_reports_failures(caplog):
    assert main(["mysql", "--upload"]) == 1
    assert "BACMAN_BUCKET" in caplog.text


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_cli_rejects_bad_hours(value, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["postgres", "--keep-remote", value])
    assert exc.value.code == 2
    assert "positive number of hours" in capsys.readouterr().err
