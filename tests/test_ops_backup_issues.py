"""P0 备份回归：报错截图目录 issues/ 进入 ZIP、S3 引用核查与恢复演练。"""
import zipfile

import pytest

from app import db, ops


def _add_issue_screenshot(data_dir, name="shot.jpg", content=b"fake-jpeg-bytes"):
    folder = data_dir / "issues"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content)
    return path


def test_backup_includes_issue_screenshots_and_restores(client, isolated_db, tmp_path, monkeypatch):
    data_dir = db.DATA_DIR
    screenshot = _add_issue_screenshot(data_dir)

    # 1) content_issues 引用对象存储文件时必须拒绝本地 ZIP 备份（与 attempts/ai-files 同口径）。
    with db.connection() as con:
        con.execute(
            "INSERT INTO content_issues(qid,status,risk,created_at,updated_at,screenshot_path) "
            "VALUES (?,?,?,?,?,?)",
            ("unit:s3", "pending_check", "high", db.utcnow(), db.utcnow(), "s3:issues/shot.jpg"),
        )
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(ops.ExternalStorageBackupRequired):
        ops.create_backup(tmp_path / "s3.zip", data_dir=data_dir)
    with db.connection() as con:
        con.execute("DELETE FROM content_issues")

    # 2) 本地 issues/ 文件必须进包，verify 通过。
    archive = tmp_path / "backup.zip"
    info = ops.create_backup(archive, data_dir=data_dir)
    assert info["file_count"] >= 2
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "issues/shot.jpg" in names, sorted(names)
    verified = ops.verify_backup(archive)
    assert verified["ok"] is True
    assert verified["integrity_check"] == "ok"

    # 3) 恢复演练能把截图目录一起还原。
    restore_dir = tmp_path / "restored"
    result = ops.restore_backup(archive, restore_dir)
    assert result["ok"] is True
    restored = restore_dir / "issues" / "shot.jpg"
    assert restored.is_file()
    assert restored.read_bytes() == screenshot.read_bytes()
