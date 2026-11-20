"""P0 备份安全回归：Windows Zip-Slip、默认备份目录、密钥排除与 S3 引用扫描。

对应《知序下一轮优化方案》§2.3-2：归档路径双重校验（POSIX + Windows）、
默认备份目录落在数据目录 backups/、api-key.key 默认排除、ai_uploads.extra_files_json
的外置存储引用必须阻止本地 ZIP 备份、恢复先暂存校验再原子替换。
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from app import db, ops


def _create_sqlite(path: Path, rows=("u",)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)")
        for index, name in enumerate(rows, start=1):
            con.execute("INSERT INTO users(id,username) VALUES (?,?)", (index, name))
        con.commit()
    finally:
        con.close()


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _manifest(members: dict[str, bytes]) -> dict:
    return {
        "created_at": db.utcnow(),
        "file_count": len(members),
        "files": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in members.items()
        },
    }


# ---------------------------------------------------------------------------
# 1. 归档路径校验：拒绝绝对路径 / .. / 驱动器号 / NTFS ADS / 保留设备名
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("member", [
    "../evil.txt",
    "/abs/evil.txt",
    "C:\\evil.txt",
    "a::b",
    "safe/../../evil.txt",
    "nested\\evil.txt",
    "CON.txt",
    "evil.txt.",
])
def test_unsafe_archive_members_are_rejected_and_never_written(tmp_path, member):
    archive = tmp_path / "bad.zip"
    _write_zip(archive, {member: b"evil"})
    with pytest.raises(ValueError):
        ops.verify_backup(archive)

    target = tmp_path / "restored"
    with pytest.raises(ValueError):
        ops.restore_backup(archive, target)
    # 危险成员不能落到目标目录外（也不应创建目标目录）。
    assert not (tmp_path / "evil.txt").exists()
    assert not (target / "evil.txt").exists()
    assert not (target.parent / "evil.txt").exists()


@pytest.mark.parametrize("member", [
    "app.sqlite3", "issues/shot.jpg", "ai-files/abc.png", "attempts/1.jpg",
])
def test_safe_archive_members_are_accepted(member):
    assert ops._validate_archive_member(member) == member


def test_unsafe_member_rejected_even_when_manifest_is_clean(tmp_path):
    """清单只收录正常文件、但压缩包另夹带穿越成员时同样拒绝（读 namelist 全部成员）。"""
    archive = tmp_path / "mixed.zip"
    safe_db = b"not-a-real-db"
    manifest = _manifest({"app.sqlite3": safe_db})
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app.sqlite3", safe_db)
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("../evil.txt", b"evil")
    with pytest.raises(ValueError):
        ops.verify_backup(archive)
    target = tmp_path / "restored"
    with pytest.raises(ValueError):
        ops.restore_backup(archive, target)
    assert not (tmp_path / "evil.txt").exists()
    assert not target.exists()


# ---------------------------------------------------------------------------
# 2. 默认备份目录 = 数据目录 backups/，且密钥默认排除
# ---------------------------------------------------------------------------

def test_default_backup_directory_is_data_dir_backups(isolated_db):
    data_dir = db.DATA_DIR
    _create_sqlite(data_dir / "app.sqlite3")
    (data_dir / "api-key.key").write_bytes(b"application-encryption-key")

    info = ops.create_backup()
    archive = Path(info["path"])
    assert archive.parent == (data_dir / "backups").resolve()
    assert archive.is_file()
    assert info["key_included"] is False
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "app.sqlite3" in names
    assert "manifest.json" in names
    assert "api-key.key" not in names
    verified = ops.verify_backup(archive)
    assert verified["ok"] is True
    assert verified["key_included"] is False

    included = ops.create_backup(include_key=True)
    assert included["key_included"] is True
    with zipfile.ZipFile(included["path"]) as zf:
        assert "api-key.key" in set(zf.namelist())


def test_restore_replaces_files_and_leaves_no_staging_dir(isolated_db, tmp_path):
    data_dir = db.DATA_DIR
    _create_sqlite(data_dir / "app.sqlite3")
    archive = tmp_path / "backup.zip"
    info = ops.create_backup(archive)
    assert info["key_included"] is False

    target = tmp_path / "restored"
    result = ops.restore_backup(archive, target)
    assert result["ok"] is True
    assert (target / "app.sqlite3").is_file()
    assert not list(target.glob(".zhixu-restore-*"))


def test_cli_backup_defaults_to_excluding_key(isolated_db, tmp_path, capsys):
    data_dir = db.DATA_DIR
    _create_sqlite(data_dir / "app.sqlite3")
    (data_dir / "api-key.key").write_bytes(b"application-encryption-key")

    excluded_out = tmp_path / "cli-default.zip"
    ops.main(["backup", "--out", str(excluded_out)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["key_included"] is False
    with zipfile.ZipFile(excluded_out) as zf:
        assert "api-key.key" not in set(zf.namelist())

    included_out = tmp_path / "cli-include.zip"
    ops.main(["backup", "--out", str(included_out), "--include-key"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["key_included"] is True
    with zipfile.ZipFile(included_out) as zf:
        assert "api-key.key" in set(zf.namelist())

    # --exclude-key 显式覆盖 --include-key（默认行为可重申）
    override_out = tmp_path / "cli-override.zip"
    ops.main(["backup", "--out", str(override_out), "--include-key", "--exclude-key"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["key_included"] is False


# ---------------------------------------------------------------------------
# 3. S3 引用扫描补齐 ai_uploads.extra_files_json
# ---------------------------------------------------------------------------

def test_extra_files_s3_reference_blocks_local_zip(client, isolated_db, tmp_path, monkeypatch):
    with db.connection() as con:
        user_id = con.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO ai_uploads(user_id,filename,storage_name,media_type,text_content,"
            "extra_files_json,status,created_at) VALUES (?,?,?,?,?,?,'uploaded',?)",
            (
                user_id, "题目.png", "q-main.png", "image/png", "",
                json.dumps([{"filename": "附加.png", "storage_name": "s3:ai-files/extra.png",
                             "media_type": "image/png"}], ensure_ascii=False),
                db.utcnow(),
            ),
        )
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(ops.ExternalStorageBackupRequired):
        ops.create_backup(tmp_path / "blocked.zip", data_dir=db.DATA_DIR)


def test_extra_files_local_reference_does_not_block_backup(client, isolated_db, tmp_path, monkeypatch):
    with db.connection() as con:
        user_id = con.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO ai_uploads(user_id,filename,storage_name,media_type,text_content,"
            "extra_files_json,status,created_at) VALUES (?,?,?,?,?,?,'uploaded',?)",
            (
                user_id, "题目.png", "q-main.png", "image/png", "",
                json.dumps([{"filename": "附加.png", "storage_name": "q-extra.png",
                             "media_type": "image/png"}], ensure_ascii=False),
                db.utcnow(),
            ),
        )
    monkeypatch.delenv("S3_BUCKET", raising=False)
    info = ops.create_backup(tmp_path / "ok.zip", data_dir=db.DATA_DIR)
    assert info["file_count"] >= 1
    assert ops.verify_backup(Path(info["path"]))["ok"] is True
