import subprocess
import tempfile
from pathlib import Path

from .bacman import BacMan


def _quote(value: str) -> str:
    """Quote a value for a MySQL option file."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MySQL(BacMan):
    """Take a snapshot of a MySQL/MariaDB database with ``mysqldump``."""

    prefix = "mysqldump"
    suffix = "sql"

    def dump(self, path: Path) -> None:
        db = self.db
        # The password goes in a private (0600) option file so it never shows up in `ps`.
        with tempfile.NamedTemporaryFile("w", prefix="bacman-", suffix=".cnf") as cnf:
            cnf.write("[client]\n")
            if db.password:
                cnf.write(f"password={_quote(db.password)}\n")
            cnf.flush()
            # --defaults-extra-file must come first.
            cmd = ["mysqldump", f"--defaults-extra-file={cnf.name}", f"--result-file={path}"]
            if db.host:
                cmd.append(f"--host={db.host}")
            if db.port:
                cmd.append(f"--port={db.port}")
            if db.user:
                cmd.append(f"--user={db.user}")
            cmd.append(db.name)
            subprocess.run(cmd, check=True)
