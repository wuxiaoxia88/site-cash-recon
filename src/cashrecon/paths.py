"""Cross-platform locations of private runtime files.

Everything private (config, secrets, database, reports, logs) lives under one
home directory, ``~/.site-cash-recon`` by default on both macOS and Windows.
Override with the ``CASHRECON_HOME`` environment variable.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def default_home() -> Path:
    override = os.environ.get("CASHRECON_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".site-cash-recon"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def make_private_file(path: Path) -> None:
    if not IS_WINDOWS and path.exists():
        try:
            path.chmod(0o600)
        except OSError:
            pass


def atomic_write(path: Path, data: bytes | str, *, private: bool = True) -> None:
    """Write via a temp file + rename so readers never see a half-written file."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload = data.encode("utf-8") if isinstance(data, str) else data
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if private:
        make_private_file(tmp)
    os.replace(tmp, path)


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def resolve(cls, home: Path | None = None) -> Paths:
        return cls((home or default_home()).expanduser().resolve())

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets.env"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def database(self) -> Path:
        return self.data_dir / "cashrecon.db"

    @property
    def reports(self) -> Path:
        return self.home / "reports"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def lock(self) -> Path:
        return self.data_dir / ".run.lock"

    @property
    def leak_denylist(self) -> Path:
        return self.home / "leak-denylist.txt"

    def ensure(self) -> Paths:
        for path in (self.home, self.data_dir, self.reports, self.logs, self.backups):
            ensure_private_dir(path)
        return self
