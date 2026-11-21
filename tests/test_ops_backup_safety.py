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


# ---------------------------------------------------------------------------
# 4. 备份覆盖工坊/课堂资产：manifest 目录清单、题库版本、原样恢复
# ---------------------------------------------------------------------------

def _write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _asset_samples() -> dict[str, bytes]:
    """工坊页图/配图 + 课堂素材（含中文文件名与嵌套目录）+ 官方补采数据，覆盖真实数据目录结构。"""
    return {
        "attempts/answer-1.jpg": b"attempt-image-bytes",
        "paperlab/pages/page-1.jpg": b"paperlab-page-bytes",
        "paperlab/assets/裁切图-1.png": b"paperlab-asset-bytes",
        "classroom/assets/tts-旁白-1.mp3": b"classroom-tts-bytes",
        "classroom/video-work/job-7/frame-0001.png": b"classroom-video-frame-bytes",
        "official-sources/batch-lines.csv": b"official-batch-lines",
        "official-sources/raw/eol-fsx-index.html": b"official-raw-html",
    }


def _expected_directory_meta(samples: dict[str, bytes], folder: str) -> dict:
    matched = {name: data for name, data in samples.items() if name.startswith(folder + "/")}
    return {
        "file_count": len(matched),
        "bytes": sum(len(data) for data in matched.values()),
    }


def test_backup_covers_paperlab_and_classroom_assets(isolated_db, tmp_path):
    data_dir = db.DATA_DIR
    _create_sqlite(data_dir / "app.sqlite3")
    samples = _asset_samples()
    for relative, content in samples.items():
        _write_file(data_dir / relative, content)
    # 按设计排除：可重建的录取库、历史备份包本身。
    _write_file(data_dir / "admission.sqlite3", b"rebuildable-admission")
    _write_file(data_dir / "backups/old-backup.zip", b"old-zip")

    archive = tmp_path / "full.zip"
    info = ops.create_backup(archive)
    expected_directories = {
        "attempts": _expected_directory_meta(samples, "attempts"),
        "paperlab": _expected_directory_meta(samples, "paperlab"),
        "classroom": _expected_directory_meta(samples, "classroom"),
        "official-sources": _expected_directory_meta(samples, "official-sources"),
    }
    # 返回结构与 manifest 目录清单：工坊/课堂真实目录都在，数量与字节数可核对。
    assert info["directories"] == expected_directories
    assert isinstance(info["bank_version"], str)
    assert info["key_present"] is False
    assert "admission.sqlite3" in info["excluded_entries"]
    assert "backups" in info["excluded_entries"]

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    for relative in samples:
        assert relative in names, relative
    assert "admission.sqlite3" not in names
    assert not any(name.startswith("backups/") for name in names)
    assert manifest["format_version"] == 2
    assert manifest["directories"] == expected_directories
    assert "bank_version" in manifest and "bank_source" in manifest
    assert manifest["bank_version"] == info["bank_version"]
    assert "admission.sqlite3" in manifest["excluded_entries"]
    assert manifest["key_present"] is False

    verified = ops.verify_backup(archive)
    assert verified["ok"] is True
    assert verified["format_version"] == 2
    assert verified["directories"] == expected_directories
    assert verified["bank_version"] == manifest["bank_version"]

    restore_dir = tmp_path / "restore-drill"
    result = ops.restore_backup(archive, restore_dir)
    assert result["ok"] is True
    assert result["restored_files"] == len(manifest["files"])
    assert result["restored_directories"] == sorted(expected_directories)
    for relative, content in samples.items():
        restored = restore_dir / relative
        assert restored.is_file(), relative
        assert restored.read_bytes() == content
        assert hashlib.sha256(restored.read_bytes()).hexdigest() == manifest["files"][relative]["sha256"]
    # 数据库快照本身也原样恢复（清单哈希一致）。
    restored_db = restore_dir / "app.sqlite3"
    assert restored_db.is_file()
    assert hashlib.sha256(restored_db.read_bytes()).hexdigest() == manifest["files"]["app.sqlite3"]["sha256"]
    assert not (restore_dir / "admission.sqlite3").exists()
    assert not (restore_dir / "backups").exists()


def test_restore_drill_keeps_paperlab_images_and_publish_state(client, isolated_db, tmp_path):
    """恢复演练：工坊配图、题库数据与试卷发布状态在恢复后齐全。"""
    created = client.post("/api/paperlab/questions", json={
        "type": "SINGLE", "subject": "数学", "grade": "高中一年级", "difficulty": 0.3,
        "knowledge_tags": ["函数"], "status": "ready",
        "content": {
            "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于？",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            "answers": ["B"], "explanation": "代入得 4。",
        },
    })
    assert created.status_code == 200, created.text
    question_id = created.json()["question"]["id"]
    paper = client.post("/api/paperlab/papers", json={
        "title": "发布状态恢复测试", "kind": "manual", "subject": "数学",
        "grade": "高中一年级", "items": [{"ref": f"app:{question_id}", "score": 5}],
    })
    assert paper.status_code == 200, paper.text
    paper_id = paper.json()["paper"]["id"]
    published = client.post(f"/api/paperlab/papers/{paper_id}/publish", json={"published": True})
    assert published.status_code == 200, published.text
    assert published.json()["paper"]["status"] == "published"

    _write_file(db.DATA_DIR / "paperlab/pages/page-1.jpg", b"workshop-page-bytes")
    _write_file(db.DATA_DIR / "paperlab/assets/crop-1.png", b"workshop-crop-bytes")
    _write_file(db.DATA_DIR / "classroom/assets/tts-1.mp3", b"classroom-asset-bytes")
    archive = tmp_path / "state.zip"
    ops.create_backup(archive)
    restore_dir = tmp_path / "state-restore"
    result = ops.restore_backup(archive, restore_dir)
    assert result["ok"] is True
    assert {"paperlab", "classroom"}.issubset(set(result["restored_directories"]))
    assert (restore_dir / "paperlab/pages/page-1.jpg").read_bytes() == b"workshop-page-bytes"
    assert (restore_dir / "paperlab/assets/crop-1.png").read_bytes() == b"workshop-crop-bytes"
    assert (restore_dir / "classroom/assets/tts-1.mp3").read_bytes() == b"classroom-asset-bytes"

    con = sqlite3.connect(str(restore_dir / "app.sqlite3"))
    try:
        status = con.execute("SELECT status FROM pl_papers WHERE id=?", (paper_id,)).fetchone()[0]
        questions = con.execute("SELECT COUNT(*) FROM pl_questions").fetchone()[0]
        items = con.execute(
            "SELECT COUNT(*) FROM pl_paper_items WHERE paper_id=?", (paper_id,)
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "published"
    assert questions == 1 and items == 1


def test_legacy_manifest_without_new_fields_still_verifies_and_restores(isolated_db, tmp_path):
    """旧备份包（无 format_version/bank_version/directories）保持可 verify/restore。"""
    db_path = tmp_path / "legacy-app.sqlite3"
    _create_sqlite(db_path)
    members = {"app.sqlite3": db_path.read_bytes(), "issues/shot.jpg": b"legacy-shot"}
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(_manifest(members)))

    verified = ops.verify_backup(archive)
    assert verified["ok"] is True
    assert verified["format_version"] == 1
    assert verified["bank_version"] == ""
    assert verified["directories"] == {}
    assert verified["excluded_entries"] == []
    assert verified["key_included"] is False

    restore_dir = tmp_path / "legacy-restored"
    result = ops.restore_backup(archive, restore_dir)
    assert result["restored_files"] == 2
    assert result["restored_directories"] == []
    assert (restore_dir / "issues" / "shot.jpg").read_bytes() == b"legacy-shot"


def test_s3_bucket_setting_blocks_local_zip_backup(isolated_db, tmp_path, monkeypatch):
    """对象存储启用时本地 ZIP 无法覆盖 S3 资产：显式失败，而不是静默「备份成功」。"""
    _create_sqlite(db.DATA_DIR / "app.sqlite3")
    monkeypatch.setenv("S3_BUCKET", "zhixu-private-assets")
    with pytest.raises(ops.ExternalStorageBackupRequired):
        ops.create_backup(tmp_path / "blocked.zip")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    info = ops.create_backup(tmp_path / "local.zip")
    assert ops.verify_backup(Path(info["path"]))["ok"] is True


def test_paperlab_asset_s3_reference_blocks_local_zip_backup(client, isolated_db, tmp_path, monkeypatch):
    """快照里工坊页图引用 s3: 时，本地 ZIP 备份必须显式失败。"""
    monkeypatch.delenv("S3_BUCKET", raising=False)
    now = db.utcnow()
    with db.connection() as con:
        user_id = con.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO pl_imports(user_id,title,status,page_count,source_json,note,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, "迁移测试", "draft", 1, "[]", "", now, now),
        )
        import_id = con.execute(
            "SELECT id FROM pl_imports WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()[0]
        con.execute(
            "INSERT INTO pl_pages(user_id,import_id,page_no,width,height,storage_name,"
            "media_type,source_name,text_content,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (user_id, import_id, 1, 100, 100, "s3:paperlab/pages/page-1.jpg",
             "image/jpeg", "page-1.jpg", "", now, now),
        )
    with pytest.raises(ops.ExternalStorageBackupRequired):
        ops.create_backup(tmp_path / "blocked.zip", data_dir=db.DATA_DIR)
