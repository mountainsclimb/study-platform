"""Online SQLite WAL hot backup, SHA-256 manifest verification, and restore drill CLI.

Usage:
  python -m app.ops backup [--out backups/zhixu-YYYYMMDD-HHMMSS.zip] [--include-key]
  python -m app.ops verify <backup.zip>
  python -m app.ops restore <backup.zip> --target <data_dir>

安全约定：
  * 备份包默认**不含** ``api-key.key``（应用级加密密钥）。需要完整离线恢复时显式加
    ``--include-key``，并把 ZIP 与密钥分别保存到离机介质；ZIP 本身不加密，但包含
    SHA-256 清单，可校验完整性与篡改。
  * 归档成员路径在解压前经 ``PureWindowsPath`` / ``PurePosixPath`` 双重校验：
    拒绝绝对路径、``..`` 穿越、驱动器号、NTFS 备用数据流（``a::b``）与 Windows
    保留设备名；解压先落到临时目录，全部校验通过后才逐个原子替换目标文件。
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import tempfile
import zipfile

from . import db


class ExternalStorageBackupRequired(RuntimeError):
    """The SQLite ZIP cannot contain assets stored by an external service."""


class UnsafeArchivePath(ValueError):
    """备份包成员路径不安全（绝对路径 / 目录穿越 / 驱动器号 / NTFS ADS / 保留设备名）。"""


# Windows 保留设备名：即使带扩展名（CON.txt）在 Win32 命名空间仍会命中设备。
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

# 快照可能引用对象存储的列；旧库缺少列/表时跳过。extra_files_json 是 JSON 数组，
# 用子串匹配才能发现其中的 "s3:..." 引用（storage_name 则是完整引用）。
_S3_REFERENCE_SCANS = (
    ("attempts", "image_path", "s3:%"),
    ("ai_documents", "storage_name", "s3:%"),
    ("ai_uploads", "storage_name", "s3:%"),
    ("ai_uploads", "extra_files_json", "%s3:%"),
    ("content_issues", "screenshot_path", "s3:%"),
    # 工坊页图/配图与课堂素材：storage_name 是本地内容名，只有对象存储部署才会存成 s3: 引用。
    ("pl_pages", "storage_name", "s3:%"),
    ("pl_questions", "content_json", '%"s3:%'),
    ("cls_assets", "storage_name", "s3:%"),
)

# 进入本地 ZIP 的资产目录（相对 ``db.DATA_DIR`` 的顶层目录，递归收录）：
#   attempts   普通练习作答图；ai-files AI 资料附件；issues 题目报错截图；
#   paperlab   工坊页图 paperlab/pages 与裁切配图 paperlab/assets；
#   classroom  课堂素材 classroom/assets（TTS/配图/视频）与 classroom/video-work 产物；
#   official-sources 人工补采的官方批次线数据（需联网重建，体积小，值得随包保存）。
_BACKUP_FOLDERS = (
    "attempts", "ai-files", "issues", "paperlab", "classroom", "official-sources",
)
# 按设计不进入热备份的顶层条目：录取数据可随时重建，backups 是备份自身，
# WAL/SHM 是 SQLite 运行时文件（快照已含一致性数据）。manifest 记录实际存在的项，
# 恢复演练时能区分「设计排除」与「漏备」。
_BACKUP_EXCLUDED_ENTRIES = (
    "admission.sqlite3",
    "admission-official.sqlite3",
    "admission-gaps.json",
    ".admission-cache",
    "backups",
    "app.sqlite3-wal",
    "app.sqlite3-shm",
    # 本地模型目录（公式/表格/语音/向量）：由部署方预置，可由依赖与模型重新下载，
    # 不进入热备份，避免把数 GB 权重打进 ZIP。
    "models",
)
_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "app.sqlite3"
_KEY_NAME = "api-key.key"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _validate_archive_member(arcname) -> str:
    """校验 ZIP 成员名并返回原样的 POSIX 相对路径。

    同时按 POSIX 与 Windows 语义检查，覆盖 Windows (Zip-Slip) 与跨平台恢复场景。
    """
    if not isinstance(arcname, str) or not arcname or "\x00" in arcname:
        raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    if "\\" in arcname:
        # 备份成员统一使用 POSIX 分隔符；反斜杠在 Windows 上同样是分隔符，直接拒绝。
        raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    if ":" in arcname:
        # 驱动器号（C:...）或 NTFS 备用数据流（a::b）
        raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    posix = PurePosixPath(arcname)
    windows = PureWindowsPath(arcname)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    parts = posix.parts
    if not parts or parts != windows.parts:
        raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    for part in parts:
        # Windows 会静默去掉结尾的点与空格，可能把校验过的名字变成另一个文件。
        if part.rstrip(" .") != part:
            raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
        if part.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED_NAMES:
            raise UnsafeArchivePath(f"不安全的归档路径：{arcname!r}")
    return arcname


def _resolve_member_path(root: Path, arcname: str) -> Path:
    """把已校验的成员路径解析到 root 下，并再次确认没有越界。"""
    root = root.resolve()
    target = (root / Path(*PurePosixPath(arcname).parts)).resolve()
    if target == root or not target.is_relative_to(root):
        raise UnsafeArchivePath(f"路径越界：{arcname}")
    return target


def _load_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
    except KeyError:
        raise ValueError("备份包缺少 manifest.json") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("备份包 manifest.json 无法解析") from None
    if not isinstance(manifest, dict):
        raise ValueError("备份包 manifest.json 结构无效")
    files_meta = manifest.get("files")
    if not isinstance(files_meta, dict) or not files_meta:
        raise ValueError("备份包 manifest.json 缺少文件清单")
    return manifest


def _scan_external_references(con) -> None:
    tables = db.table_names(con)
    for table, column, pattern in _S3_REFERENCE_SCANS:
        if table not in tables:
            continue
        try:
            hit = con.execute(
                f'SELECT 1 FROM "{table}" WHERE "{column}" LIKE ? LIMIT 1', (pattern,)
            ).fetchone()
        except sqlite3.Error:
            # 旧库可能还没有该列：仅跳过此项扫描，不影响备份。
            continue
        if hit:
            raise ExternalStorageBackupRequired(
                "SQLite 快照引用了 S3 文件，请单独备份数据库和对象存储"
            )


def _collect_excluded_entries(src_dir: Path) -> list[str]:
    """实际存在、但按设计不进入本地 ZIP 的顶层条目（供 manifest/恢复演练核对）。"""
    return sorted(name for name in _BACKUP_EXCLUDED_ENTRIES if (src_dir / name).exists())


def create_backup(
    out_path: Path | None = None,
    *,
    data_dir: Path | None = None,
    include_key: bool = False,
) -> dict:
    """Create a consistent WAL-safe online snapshot zip with SHA-256 manifest.

    ``include_key`` 默认 False：``api-key.key`` 是应用级加密密钥，默认不随包分发；
    需要完整离线恢复时显式包含，并注意 ZIP 本身不加密。
    """
    if data_dir is None and db.postgres_enabled():
        raise ExternalStorageBackupRequired("PostgreSQL 部署请使用数据库和对象存储各自的备份工具")
    # 对象存储模式下新文件在 S3 桶里，本地 ZIP 只能覆盖数据卷中的历史文件；
    # 失败语义保持显式，避免「备份成功」的假象。
    if os.environ.get("S3_BUCKET", "").strip():
        raise ExternalStorageBackupRequired(
            "启用对象存储（S3_BUCKET）时本地 ZIP 不包含 S3 桶内文件，"
            "请用对象存储快照单独备份附件，或同时保留数据卷中的历史文件备份"
        )
    src_dir = (data_dir or db.DATA_DIR).resolve()
    db_file = src_dir / _DATABASE_NAME
    if not db_file.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{db_file}")

    if out_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        # 默认落在数据卷（Docker /data）里：容器重建不会丢，且随数据目录一起持久化。
        out_path = src_dir / "backups" / f"zhixu-backup-{stamp}.zip"
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    manifest_files: dict[str, dict] = {}
    directories: dict[str, dict] = {}
    key_included = False
    key_file = src_dir / _KEY_NAME
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        snap_db = tmp_path / _DATABASE_NAME
        # SQLite online backup API ensures consistent snapshot even under active WAL writes
        src_con = sqlite3.connect(str(db_file), timeout=20)
        dst_con = sqlite3.connect(str(snap_db))
        try:
            src_con.backup(dst_con)
            check = dst_con.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"备份快照完整性检查失败：{check}")
            _scan_external_references(dst_con)
        finally:
            dst_con.close()
            src_con.close()

        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            def add_entry(file_path: Path, arcname: str):
                manifest_files[arcname] = {
                    "size": file_path.stat().st_size,
                    "sha256": _sha256_file(file_path),
                }
                zf.write(file_path, arcname)

            add_entry(snap_db, _DATABASE_NAME)
            if include_key and key_file.is_file():
                add_entry(key_file, _KEY_NAME)
                key_included = True

            # 作答图片、AI 资料、报错截图、工坊页图/配图与课堂素材都必须进包，
            # 否则恢复后图片/视频入口 404、备份成功无法证明资产可恢复。
            for folder_name in _BACKUP_FOLDERS:
                folder = src_dir / folder_name
                if not folder.is_dir():
                    continue
                folder_files = 0
                folder_bytes = 0
                for item in sorted(folder.rglob("*")):
                    if not item.is_file():
                        continue
                    rel = item.relative_to(src_dir).as_posix()
                    add_entry(item, _validate_archive_member(rel))
                    folder_files += 1
                    folder_bytes += manifest_files[rel]["size"]
                directories[folder_name] = {
                    "file_count": folder_files,
                    "bytes": folder_bytes,
                }

            manifest = {
                "created_at": db.utcnow(),
                "file_count": len(manifest_files),
                "files": manifest_files,
                # 供核对与恢复演练：默认不含密钥；ZIP 未加密，完整性由本清单保证。
                "key_included": key_included,
                # 密钥文件是否存在（即使按默认策略排除，也能核对恢复后是否缺密钥）。
                "key_present": key_file.is_file(),
                "encrypted": False,
                # 恢复演练可核对「备份对应哪版题库、包含哪些数据目录」；旧包没有这些字段，
                # verify/restore 按缺失处理，保持向后兼容。
                "format_version": 2,
                "bank_version": db.bank_version_hint(),
                "bank_source": str(getattr(db, "BANK_SOURCE", "") or ""),
                "directories": directories,
                "excluded_entries": _collect_excluded_entries(src_dir),
            }
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "file_count": len(manifest_files),
        "created_at": manifest["created_at"],
        "key_included": key_included,
        "key_present": manifest["key_present"],
        "bank_version": manifest["bank_version"],
        "bank_source": manifest["bank_source"],
        "directories": directories,
        "excluded_entries": manifest["excluded_entries"],
    }


def verify_backup(backup_zip: Path) -> dict:
    """Verify archive manifest SHA-256 checksums and SQLite PRAGMA integrity_check."""
    backup_zip = Path(backup_zip).resolve()
    if not backup_zip.is_file():
        raise FileNotFoundError(f"备份文件不存在：{backup_zip}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(backup_zip, "r") as zf:
            # 连 ZIP 里未被清单收录的成员也一并校验路径：拒绝携带恶意目录穿越的包。
            for name in zf.namelist():
                _validate_archive_member(name)
            manifest = _load_manifest(zf)
            files_meta = manifest["files"]
            verified = 0
            for arcname, meta in files_meta.items():
                safe = _validate_archive_member(arcname)
                if not isinstance(meta, dict):
                    raise ValueError(f"文件清单条目无效：{arcname}")
                expected = meta.get("sha256")
                if not isinstance(expected, str) or len(expected) != 64:
                    raise ValueError(f"文件清单校验和格式无效：{arcname}")
                target = _resolve_member_path(tmp_path, safe)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    source = zf.open(safe, "r")
                except KeyError:
                    raise ValueError(f"备份包缺少清单中的文件：{safe}") from None
                with source, target.open("wb") as dst:
                    shutil.copyfileobj(source, dst)
                actual_hash = _sha256_file(target)
                if actual_hash != expected.lower():
                    raise ValueError(f"文件校验和不匹配：{safe}")
                verified += 1

        db_snap = tmp_path / _DATABASE_NAME
        if not db_snap.is_file():
            raise ValueError("备份包缺少 app.sqlite3")
        con = sqlite3.connect(str(db_snap))
        try:
            ic = con.execute("PRAGMA integrity_check").fetchone()[0]
            if ic != "ok":
                raise ValueError(f"SQLite 完整性校验失败：{ic}")
            users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            tables = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            con.close()

    directories = manifest.get("directories")
    if not isinstance(directories, dict):
        directories = {}
    excluded_entries = manifest.get("excluded_entries")
    if not isinstance(excluded_entries, list):
        excluded_entries = []

    return {
        "ok": True,
        "verified_files": verified,
        "integrity_check": "ok",
        "users": users,
        "tables": tables,
        "created_at": manifest.get("created_at"),
        # 兼容旧包：旧清单没有 key_included 字段时按是否收录 api-key.key 判断。
        "key_included": bool(manifest.get("key_included", _KEY_NAME in files_meta)),
        "key_present": bool(manifest.get("key_present", _KEY_NAME in files_meta)),
        # 旧包没有以下字段：返回 1 / 空字符串 / 空列表，调用方按「兼容旧包」处理。
        "format_version": int(manifest.get("format_version") or 1),
        "bank_version": str(manifest.get("bank_version") or ""),
        "bank_source": str(manifest.get("bank_source") or ""),
        "directories": directories,
        "excluded_entries": excluded_entries,
    }


def restore_backup(backup_zip: Path, target_dir: Path) -> dict:
    """Verify archive first, then restore atomically into target_dir.

    先把全部成员解压到目标目录内的临时暂存目录并完成清单校验，确认没有危险路径后，
    再逐个 ``os.replace`` 原子替换到目标位置；任何一步失败都不会留下半套数据。
    """
    info = verify_backup(backup_zip)
    backup_zip = Path(backup_zip).resolve()
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".zhixu-restore-", dir=str(target_dir)))
    restored_files = 0
    try:
        with zipfile.ZipFile(backup_zip, "r") as zf:
            manifest = _load_manifest(zf)
            for arcname in manifest["files"]:
                safe = _validate_archive_member(arcname)
                dest = _resolve_member_path(staging, safe)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(safe, "r") as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        # 全部成员已在暂存目录中通过路径校验，再原子替换目标文件。
        for staged in sorted(path for path in staging.rglob("*") if path.is_file()):
            relative = staged.relative_to(staging).as_posix()
            final = _resolve_member_path(target_dir, relative)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)
            restored_files += 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Remove stale WAL/SHM files if present so SQLite opens the restored snapshot cleanly
    for suffix in ("-wal", "-shm"):
        stale = target_dir / f"{_DATABASE_NAME}{suffix}"
        stale.unlink(missing_ok=True)

    return {
        "restored_to": str(target_dir),
        # 恢复演练证据：实际落盘文件数与包含的数据目录（旧包 directories 为空）。
        "restored_files": restored_files,
        "restored_directories": sorted((info.get("directories") or {}).keys()),
        **info,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="知序 · 数据库在线热备份与恢复演练工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bak = sub.add_parser("backup", help="创建一致性在线热备份")
    p_bak.add_argument("--out", type=Path, default=None, help="输出 zip 路径（默认数据目录 backups/）")
    p_bak.add_argument(
        "--include-key", dest="include_key", action="store_true",
        help="把 api-key.key 一并打入备份包（默认排除；ZIP 不加密，请与密钥分开保管）",
    )
    p_bak.add_argument(
        "--exclude-key", dest="include_key", action="store_false",
        help="显式排除 api-key.key（默认行为）",
    )
    p_bak.set_defaults(include_key=False)

    p_ver = sub.add_parser("verify", help="校验备份包 SHA-256 与 SQLite 完整性")
    p_ver.add_argument("archive", type=Path, help="备份 zip 路径")

    p_res = sub.add_parser("restore", help="从备份包恢复到目标目录")
    p_res.add_argument("archive", type=Path, help="备份 zip 路径")
    p_res.add_argument("--target", type=Path, required=True, help="恢复目标数据目录")

    args = parser.parse_args(argv)
    if args.cmd == "backup":
        try:
            print(json.dumps(create_backup(args.out, include_key=args.include_key), ensure_ascii=False, indent=2))
        except ExternalStorageBackupRequired as exc:
            parser.exit(1, f"{exc}\n")
    elif args.cmd == "verify":
        print(json.dumps(verify_backup(args.archive), ensure_ascii=False, indent=2))
    elif args.cmd == "restore":
        print(json.dumps(restore_backup(args.archive, args.target), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
