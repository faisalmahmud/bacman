import os
import subprocess
from pathlib import Path

from .bacman import BacMan


class Postgres(BacMan):
    """Take a snapshot of a Postgres database with ``pg_dump``."""

    prefix = "pgdump"
    suffix = "bak"

    def dump(self, path: Path) -> None:
        db = self.db
        cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl", f"--file={path}"]
        if db.host:
            cmd.append(f"--host={db.host}")
        if db.port:
            cmd.append(f"--port={db.port}")
        if db.user:
            cmd.append(f"--username={db.user}")
        cmd.append(f"--dbname={db.name}")
        # The password goes in the environment so it never shows up in `ps`.
        env = {**os.environ, "PGPASSWORD": db.password} if db.password else None
        subprocess.run(cmd, check=True, env=env)
