import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from corpus import backup

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 1, 1, 3, 15, tzinfo=timezone.utc)


def stamp(days):
    return backup.backup_name(T0 + timedelta(days=days))


def make_db(path, rows=50):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
    db.executemany("INSERT INTO notes(body) VALUES(?)", [(f"note {i}",) for i in range(rows)])
    db.commit()
    return db


def read_only(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def corrupt(path):
    """Leave a database that opens and copies normally but fails integrity_check."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t(x, y)")
    db.execute("CREATE INDEX t_x ON t(x)")
    db.executemany("INSERT INTO t VALUES(?, ?)", [(i, -i) for i in range(50)])
    db.commit()
    db.execute("PRAGMA writable_schema=ON")
    db.execute("UPDATE sqlite_master SET sql='CREATE INDEX t_x ON t(y)' WHERE name='t_x'")
    db.commit()
    db.close()


def test_backup_of_live_wal_database_is_consistent_and_self_contained(tmp_path):
    src = tmp_path / "data" / "corpus.sqlite3"
    src.parent.mkdir()
    server = make_db(src)
    server.execute("PRAGMA wal_autocheckpoint=0")
    server.execute("INSERT INTO notes(body) VALUES('only in the wal')")
    server.commit()
    assert b"only in the wal" not in src.read_bytes()
    server.execute("BEGIN")
    server.execute("INSERT INTO notes(body) VALUES('uncommitted')")

    result = backup.run_backup(src, tmp_path / "backups", now=T0)

    final = Path(result["backup"])
    assert final.name == "corpus-20260101T031500Z.sqlite3"
    assert result == {"backup": str(final), "bytes": final.stat().st_size, "kept": 1, "pruned": []}
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert [p.name for p in final.parent.iterdir()] == [final.name]
    copy = read_only(final)
    bodies = [row[0] for row in copy.execute("SELECT body FROM notes")]
    assert len(bodies) == 51 and "only in the wal" in bodies and "uncommitted" not in bodies
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    assert [p.name for p in final.parent.iterdir()] == [final.name]

    server.commit()
    assert server.execute("SELECT count(*) FROM notes").fetchone()[0] == 52
    server.close()
    assert backup.main(["--verify", str(final)]) == 0


def test_keeps_newest_snapshots_and_never_touches_other_files(tmp_path):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    dest = tmp_path / "backups"
    dest.mkdir()
    others = ["corpus.sqlite3", "corpus-latest.sqlite3", "corpus-2026-01-01.sqlite3", "notes.txt"]
    for name in others:
        (dest / name).write_text("manual")

    results = [backup.run_backup(src, dest, keep=3, now=T0 + timedelta(days=d)) for d in range(5)]

    assert results[3]["pruned"] == [stamp(0)]
    assert results[4]["pruned"] == [stamp(1)] and results[4]["kept"] == 3
    assert sorted(p.name for p in dest.iterdir()) == sorted(others + [stamp(2), stamp(3), stamp(4)])
    assert all((dest / name).read_text() == "manual" for name in others)

    # A clock that went backwards must not prune the snapshot it just wrote.
    late = backup.run_backup(src, dest, keep=1, now=T0 - timedelta(days=1))
    assert Path(late["backup"]).exists()
    assert sorted(late["pruned"]) == [stamp(2), stamp(3)]


def test_failed_integrity_check_prunes_nothing_and_leaves_no_partial(tmp_path, capsys):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    dest = tmp_path / "backups"
    for day in range(3):
        backup.run_backup(src, dest, keep=10, now=T0 + timedelta(days=day))
    before = sorted(p.name for p in dest.iterdir())
    corrupt(src)

    assert backup.main(["--database", str(src), "--dest", str(dest), "--keep", "1"]) == 1

    err = capsys.readouterr().err
    assert "integrity_check failed on the copy:" in err and "missing from index t_x" in err
    assert sorted(p.name for p in dest.iterdir()) == before
    assert backup.main(["--verify", str(src)]) == 1


def test_interrupted_run_removes_its_partial_file(tmp_path, monkeypatch):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    dest = tmp_path / "backups"

    def killed(source, target):
        target.write_bytes(b"half a database")
        raise SystemExit(143)

    monkeypatch.setattr(backup, "snapshot", killed)
    with pytest.raises(SystemExit):
        backup.run_backup(src, dest)
    assert list(dest.iterdir()) == []


def test_refuses_missing_database_other_users_and_bad_retention(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "backups"
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(backup.BackupError, match="database not found"):
        backup.run_backup(missing, dest)
    assert not missing.exists()

    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    assert backup.main(["--database", str(src), "--dest", str(dest), "--keep", "0"]) == 1
    assert "--keep must be at least 1" in capsys.readouterr().err

    monkeypatch.setattr(backup.os, "geteuid", lambda: src.stat().st_uid + 1)
    with pytest.raises(backup.BackupError, match="database owner"):
        backup.run_backup(src, dest)
    # Nothing was opened, so no -wal/-shm files were created for another user.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus.sqlite3"]


def test_script_runs_from_any_directory_and_reports_json(tmp_path):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    dest = tmp_path / "backups"
    env = {k: v for k, v in os.environ.items() if k != "CORPUS_DATA_DIR"}
    env["CORPUS_DATA_DIR"] = str(tmp_path)

    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backup_db.py"), "--dest", str(dest)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )

    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert Path(result["backup"]).parent == dest and result["kept"] == 1
    assert backup.integrity_errors(Path(result["backup"])) == []
