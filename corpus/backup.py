"""Verified snapshots of the Corpus SQLite database.

The copy is made with SQLite's online backup API inside a single read
transaction, so it is consistent while the server keeps writing to its WAL
database. Each snapshot is written to a temporary file, converted to a
self-contained rollback-journal database, checked with ``PRAGMA
integrity_check`` and only then renamed into place. Old snapshots are pruned
after a successful run; a failed run deletes nothing.

``--serve`` answers one read-only request from an SSH forced command, so
another machine can pull finished snapshots without a shell on the server.

The module uses only the standard library and imports nothing else from
Corpus, so the file also runs on its own with any Python 3.10+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_KEEP = 14
BACKUP_NAME = re.compile(r"^corpus-\d{8}T\d{6}Z\.sqlite3$")


class BackupError(RuntimeError):
    """The snapshot could not be made, or it did not verify."""


def default_database() -> Path:
    """Mirror ``Settings.database``: ``$CORPUS_DATA_DIR`` or the checkout's ``data``."""
    data_dir = os.getenv("CORPUS_DATA_DIR") or Path(__file__).resolve().parents[1] / "data"
    return Path(data_dir) / "corpus.sqlite3"


def backup_name(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("corpus-%Y%m%dT%H%M%SZ.sqlite3")


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def integrity_errors(path: Path) -> list[str]:
    """Open ``path`` read-only and return its integrity problems (empty when sound)."""
    try:
        db = _read_only(path)
        try:
            rows = [row[0] for row in db.execute("PRAGMA integrity_check")]
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    return [] if rows == ["ok"] else rows


def snapshot(source: Path, target: Path) -> None:
    """Copy a consistent snapshot of ``source`` into the empty file ``target``."""
    src = _read_only(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            # The copy inherits WAL mode; a backup should be one self-contained file.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()


def prune(dest: Path, keep: int, protect: Path | None = None) -> list[str]:
    """Delete all but the newest ``keep`` timestamped snapshots in ``dest``.

    Only names produced by :func:`backup_name` are considered, so manual
    backups and other files in the directory are never touched.
    """
    backups = sorted(p for p in dest.iterdir() if BACKUP_NAME.match(p.name) and p.is_file())
    stale = [p for p in backups[:-keep] if p != protect]
    for path in stale:
        path.unlink()
    return [p.name for p in stale]


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_backup(database: Path, dest: Path, keep: int = DEFAULT_KEEP, now: datetime | None = None,
               group_readable: bool = False) -> dict:
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    database, dest = Path(database), Path(dest)
    if not database.is_file():
        raise BackupError(f"database not found: {database}")
    owner = database.stat().st_uid
    if owner != os.geteuid():
        # Reading a WAL database creates -wal/-shm files owned by the reader;
        # if they belonged to another user, the server could no longer write.
        raise BackupError(f"run this as the database owner (uid {owner}), not uid {os.geteuid()}")
    dest.mkdir(mode=0o750 if group_readable else 0o700, parents=True, exist_ok=True)

    final = dest / backup_name(now or datetime.now(timezone.utc))
    fd, name = tempfile.mkstemp(prefix=".corpus-", suffix=".partial", dir=dest)  # mode 0600
    os.close(fd)
    partial = Path(name)
    try:
        if group_readable:
            os.chmod(partial, 0o640)
        snapshot(database, partial)
        problems = integrity_errors(partial)
        if problems:
            raise BackupError("integrity_check failed on the copy: " + "; ".join(problems[:5]))
        _fsync(partial)
        os.replace(partial, final)
        _fsync(dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    pruned = prune(dest, keep, protect=final)
    kept = sum(1 for p in dest.iterdir() if BACKUP_NAME.match(p.name))
    return {"backup": str(final), "bytes": final.stat().st_size, "kept": kept, "pruned": pruned}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def serve(dest: Path, request: str, out=None) -> int:
    """Answer ``list`` or ``get NAME`` for finished snapshots in ``dest``.

    ``list`` prints ``name size sha256`` per snapshot, oldest first; ``get``
    streams one snapshot. Nothing else is readable and nothing is written.
    """
    out = out or sys.stdout.buffer
    words = request.split()
    if words == ["list"]:
        for path in sorted(p for p in Path(dest).iterdir() if BACKUP_NAME.match(p.name) and p.is_file()):
            out.write(f"{path.name} {path.stat().st_size} {_sha256(path)}\n".encode())
        return 0
    if len(words) == 2 and words[0] == "get" and BACKUP_NAME.match(words[1]):
        path = Path(dest) / words[1]
        if path.is_file():
            with open(path, "rb") as f:
                shutil.copyfileobj(f, out)
            return 0
        print(f"no such backup: {words[1]}", file=sys.stderr)
        return 1
    print("allowed requests: list | get corpus-YYYYMMDDTHHMMSSZ.sqlite3", file=sys.stderr)
    return 2


def _terminate(signum, _frame):
    # Turn systemd's SIGTERM into an exception so the partial file is removed.
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a verified snapshot of the Corpus database.")
    parser.add_argument("--database", type=Path, default=default_database(),
                        help="live database (default: $CORPUS_DATA_DIR/corpus.sqlite3)")
    parser.add_argument("--dest", type=Path, help="directory that receives the snapshots")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"timestamped snapshots to keep (default: {DEFAULT_KEEP})")
    parser.add_argument("--group-readable", action="store_true",
                        help="write snapshots as 0640 instead of 0600, for a pull account in the folder's group")
    parser.add_argument("--verify", type=Path, metavar="FILE",
                        help="only check an existing backup read-only and exit")
    parser.add_argument("--serve", type=Path, metavar="DIR",
                        help="answer $SSH_ORIGINAL_COMMAND (list | get NAME) for snapshots in DIR and exit")
    args = parser.parse_args(argv)

    if args.serve:
        return serve(args.serve, os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if args.verify:
        problems = integrity_errors(args.verify) if args.verify.is_file() else ["file not found"]
        print(f"{args.verify}: " + ("ok" if not problems else "; ".join(problems[:5])))
        return 1 if problems else 0
    if not args.dest:
        parser.error("--dest is required")

    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        result = run_backup(args.database, args.dest, args.keep, group_readable=args.group_readable)
    except (BackupError, sqlite3.Error, OSError) as exc:
        print(f"corpus backup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
