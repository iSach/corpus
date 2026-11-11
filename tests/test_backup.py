import hashlib
import json
import os
import shutil
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


def test_group_readable_snapshots_for_a_pull_account(tmp_path):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    result = backup.run_backup(src, tmp_path / "backups", now=T0, group_readable=True)
    assert stat.S_IMODE(Path(result["backup"]).stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o750


def test_serve_lists_and_streams_only_finished_snapshots(tmp_path, monkeypatch, capsysbinary):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    dest = tmp_path / "backups"
    paths = [Path(backup.run_backup(src, dest, now=T0 + timedelta(days=d))["backup"]) for d in (1, 0)]
    (dest / "notes.txt").write_text("private")
    (dest / ".corpus-x.partial").write_text("half")

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "list")
    assert backup.main(["--serve", str(dest)]) == 0
    lines = capsysbinary.readouterr().out.decode().splitlines()
    assert lines == [f"{p.name} {p.stat().st_size} {hashlib.sha256(p.read_bytes()).hexdigest()}"
                     for p in sorted(paths)]

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"get {paths[0].name}")
    assert backup.main(["--serve", str(dest)]) == 0
    assert capsysbinary.readouterr().out == paths[0].read_bytes()

    for request in ["", "get notes.txt", "get ../corpus.sqlite3", "get .corpus-x.partial",
                    f"get {paths[0].name} extra", "cat /etc/passwd", "list; rm -rf ~"]:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", request)
        assert backup.main(["--serve", str(dest)]) == 2, request
        assert capsysbinary.readouterr().out == b""
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "get corpus-20990101T000000Z.sqlite3")
    assert backup.main(["--serve", str(dest)]) == 1


PULL = ROOT / "scripts" / "pull_backups_macos.zsh"


@pytest.mark.skipif(not (shutil.which("zsh") and shutil.which("shasum")), reason="needs zsh and shasum")
def test_mac_pull_verifies_downloads_and_keeps_its_own_retention(tmp_path):
    src = tmp_path / "corpus.sqlite3"
    make_db(src).close()
    server = tmp_path / "server"
    for day in range(4):
        backup.run_backup(src, server, now=T0 + timedelta(days=day))
    # Stand-in for ssh: run the real forced command for whatever follows the host.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "os.environ['SSH_ORIGINAL_COMMAND'] = ' '.join(args[args.index('corpus-test') + 1:])\n"
        "if os.environ.get('CORRUPT') and args[-2] == 'get':\n"
        "    sys.stdout.write('tampered'); sys.exit(0)\n"
        f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'corpus' / 'backup.py')!r}, '--serve', {str(server)!r}])\n"
    )
    (bin_dir / "ssh").chmod(0o755)
    mac = tmp_path / "icloud" / "Corpus Backups"
    mac.parent.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CORPUS_PULL_HOST="corpus-test",
               CORPUS_PULL_DEST=str(mac), CORPUS_PULL_KEEP="2", CORPUS_PULL_KEY=str(tmp_path / "key"))

    def pull(**extra):
        return subprocess.run(["zsh", str(PULL), "pull"], env={**env, **extra},
                              capture_output=True, text=True, timeout=60)

    roomy = tmp_path / "icloud" / "keep everything"
    run = pull(CORPUS_PULL_DEST=str(roomy), CORPUS_PULL_KEEP="30")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "fetched 4, pruned 0, keeping 4" in run.stdout
    assert sorted(p.name for p in roomy.iterdir()) == [stamp(d) for d in range(4)]

    run = pull()
    assert run.returncode == 0, run.stdout + run.stderr
    assert "fetched 2, pruned 0, keeping 2" in run.stdout
    assert sorted(p.name for p in mac.iterdir()) == [stamp(2), stamp(3)]
    assert all((mac / n).read_bytes() == (server / n).read_bytes() for n in (stamp(2), stamp(3)))
    assert stat.S_IMODE(mac.stat().st_mode) == 0o700
    assert stat.S_IMODE((mac / stamp(3)).stat().st_mode) == 0o600

    assert "fetched 0, pruned 0, keeping 2" in pull().stdout  # nothing is fetched twice

    backup.run_backup(src, server, now=T0 + timedelta(days=4))
    run = pull(CORRUPT="1")
    assert run.returncode == 1
    assert f"download of {stamp(4)} failed or did not match its checksum" in run.stdout
    assert sorted(p.name for p in mac.iterdir()) == [stamp(2), stamp(3)]

    run = pull()
    assert "fetched 1, pruned 1, keeping 2" in run.stdout
    assert sorted(p.name for p in mac.iterdir()) == [stamp(3), stamp(4)]

    run = pull(CORPUS_PULL_DEST=str(tmp_path / "unplugged" / "Corpus Backups"))
    assert run.returncode == 1 and "is not available (drive disconnected?)" in run.stdout
    assert not (tmp_path / "unplugged").exists()
