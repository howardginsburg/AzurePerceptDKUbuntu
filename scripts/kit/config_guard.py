"""Protect settings changed in the browser from replacement by a PC deploy."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def config_files(project: Path) -> dict[str, str]:
    root = project / "config"
    return {path.relative_to(project).as_posix(): digest(path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
            and path.suffix != ".pyc"}


def fingerprint(project: Path) -> str:
    files = config_files(project)
    return hashlib.sha256("\n".join(f"{key}:{value}" for key, value in files.items()).encode()).hexdigest()


def check_offer(project: Path, manifest: str) -> None:
    if not (project / ".settings-config-dirty").exists():
        return
    offered = {}
    for line in manifest.splitlines():
        if len(line) >= 67 and line[66:].startswith("config/"):
            offered[line[66:]] = line[:64]
    if offered != config_files(project):
        raise ValueError(
            "Device Settings changed config/. Deploy would overwrite those changes. "
            "Run './percept-kit.sh settings pull' to back up your PC config and retrieve the device's "
            "settings, merge any PC edits, then deploy again."
        )


if __name__ == "__main__":
    command, project_name, *extra = sys.argv[1:]
    project = Path(project_name)
    if command == "offer":
        try:
            check_offer(project, sys.stdin.read())
        except ValueError as error:
            sys.exit(str(error))
    elif command == "snapshot":
        Path(extra[0]).write_text(fingerprint(project), encoding="utf-8")
    elif command == "verify":
        if fingerprint(project) != Path(extra[0]).read_text(encoding="utf-8"):
            sys.exit("Device configuration changed after upload began. Run deploy again; nothing has been applied.")
    else:
        sys.exit("usage: config_guard.py offer|snapshot|verify PROJECT [SNAPSHOT]")
