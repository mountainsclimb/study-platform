"""P0「题图需要独立的所有权校验」验收：

- 服务端签发 asset_id：工坊裁剪/标注产生的配图登记 ``pl_assets``，content 只存 asset_id；
- 读图同时校验：题目归属+未删除、资产存在且可用、资产归属当前用户（或已发布公开衍生图）；
  任何一项不满足统一 404，不泄露他人资产存在性；
- 两个真实 TestClient 账号（管理员/学生）覆盖跨用户越权：他人题目引用我的资产被拒；
- 旧原始文件名引用经 ``migrate_legacy_assets`` 确认归属前后行为；
- 已删除题目的配图不可读；``public_derivative`` 状态位预留（默认 0）；
- 附加（lead 协调）：上传改为有界读取，伪造超大文件 413 拒绝。
"""
import asyncio
import hashlib
import json
from io import BytesIO

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

from app import db, storage
from app.main import app
from app.paperlab import assets, digitize
from app.paperlab.db import ASSET_FOLDER, PAGE_FOLDER


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def png_bytes(width: int = 160, height: int = 120, color=(200, 40, 40)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def register_asset(client, data: bytes | None = None, **kwargs) -> str:
    """直接走服务端登记入口（等价于裁剪/标注时登记），返回 asset_id。"""
    return assets.save_asset_bytes(
        user_id(client), png_bytes() if data is None else data, **kwargs,
    )


def create_question(client, *, images=None, **overrides) -> dict:
    payload = {
        "type": "SUBJECTIVE",
        "subject": "数学",
        "grade": "高中一年级",
        "knowledge_tags": ["函数"],
        "content": {"stem": "证明函数 $f(x)=x^2$ 单调递增。", "images": images or []},
        "status": "draft",
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


def insert_question_raw(owner_id: int, content: dict, *, source: str = "manual") -> int:
    """绕过写入层直接落库（模拟修复前已存在的旧 content / 直连数据库的伪造内容）。"""
    now = db.utcnow()
    with db.connection() as con:
        return int(con.execute(
            "INSERT INTO pl_questions(user_id,source,type,subject,grade,difficulty,"
            "knowledge_tags_json,content_json,status,verify_status,version,ai_job_id,"
            "ai_model,prompt_version,created_at,updated_at,deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'','',?,?,NULL)",
            (
                int(owner_id), source, "SUBJECTIVE", "数学", "高中一年级", 0.5,
                "[]", json.dumps(content, ensure_ascii=False),
                "draft", "pending", 1, now, now,
            ),
        ).lastrowid)


def asset_row(asset_id: str) -> dict:
    row = assets.fetch_asset(asset_id)
    assert row is not None, f"资产未登记：{asset_id}"
    return row


def upload_import(client, files, **form):
    return client.post(
        "/api/paperlab/imports",
        files=[("files", (name, data, media)) for name, data, media in files],
        data=form,
    )


# ---------------------------------------------------------------------------
# 1. 本人正常读取（新签发的 asset_id）
# ---------------------------------------------------------------------------

def test_owner_reads_question_image_by_issued_asset_id(client):
    image_data = png_bytes()
    asset_id = register_asset(client, image_data)
    assert asset_id.startswith("pl_") and len(asset_id) > 20
    # asset 是服务端随机 token，不是内容寻址文件名
    assert asset_id != hashlib.sha256(image_data).hexdigest()[:32] + ".png"

    question = create_question(client, images=[{"asset": asset_id, "role": "stem"}])
    image = question["content"]["images"][0]
    assert image["asset"] == asset_id
    assert image["url"] == f"/api/paperlab/questions/{question['id']}/images/0"

    response = client.get(image["url"])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == image_data

    row = asset_row(asset_id)
    assert int(row["user_id"]) == user_id(client)
    assert row["purpose"] == "question_image"
    assert row["status"] == "active"
    assert int(row["public_derivative"]) == 0  # 默认不公开（预留平台精选位）
    assert row["sha256"] == hashlib.sha256(image_data).hexdigest()

    # 写入层校验：未知/非法引用被拒（不落库）
    bad = client.post("/api/paperlab/questions", json={
        "subject": "数学", "content": {"stem": "题目", "images": [{"asset": "../../etc/passwd"}]},
    })
    assert bad.status_code == 422
    unknown = client.post("/api/paperlab/questions", json={
        "subject": "数学", "content": {"stem": "题目", "images": [{"asset": "pl_not_exists"}]},
    })
    assert unknown.status_code == 422


# ---------------------------------------------------------------------------
# 2. 跨用户越权（两个真实账号、管理员/学生两种角色）
# ---------------------------------------------------------------------------

def test_other_user_cannot_borrow_my_asset(client, second_client):
    asset_id = register_asset(client)
    question = create_question(client, images=[{"asset": asset_id}])
    url = f"/api/paperlab/questions/{question['id']}/images/0"

    # 题目归属校验：他人看不到题目，也读不到配图
    assert second_client.get(f"/api/paperlab/questions/{question['id']}").status_code == 404
    assert second_client.get(url).status_code == 404

    # 写入层：他人的资产不能引用进自己的题目（403/404 语义不泄露存在性 → 422 拒绝）
    borrow = second_client.post("/api/paperlab/questions", json={
        "subject": "数学",
        "content": {"stem": "借图题", "images": [{"asset": asset_id}]},
    })
    assert borrow.status_code == 422

    # 绕过写入层直接落库（模拟旧数据/直连伪造）：读图仍按归属拒绝
    stolen_question = insert_question_raw(
        user_id(second_client), {"stem": "借图题", "images": [{"asset": asset_id}]},
    )
    forged = second_client.get(f"/api/paperlab/questions/{stolen_question}/images/0")
    assert forged.status_code == 404
    # 原题主仍然正常
    assert client.get(url).status_code == 200

    # 角色不提供后门：管理员（client）也不能读学生的私人资产
    student_asset = assets.save_asset_bytes(user_id(second_client), png_bytes(color=(10, 120, 10)))
    admin_question = insert_question_raw(
        user_id(client), {"stem": "跨角色借图", "images": [{"asset": student_asset}]},
    )
    assert client.get(
        f"/api/paperlab/questions/{admin_question}/images/0"
    ).status_code == 404

    # 未登录（匿名）不得读取私人衍生图
    anonymous = TestClient(app)
    try:
        assert anonymous.get(url).status_code == 401
    finally:
        anonymous.close()


# ---------------------------------------------------------------------------
# 3. 非法/未知 asset；已删除题目
# ---------------------------------------------------------------------------

def test_unknown_and_illegal_asset_return_404(client):
    owner = user_id(client)
    unknown = insert_question_raw(owner, {"stem": "未知图", "images": [{"asset": "pl_unknown_token"}]})
    assert client.get(f"/api/paperlab/questions/{unknown}/images/0").status_code == 404

    legacy_orphan = insert_question_raw(
        owner, {"stem": "旧图未迁移", "images": [{"asset": "0123456789abcdef0123456789abcdef.png"}]},
    )
    assert client.get(
        f"/api/paperlab/questions/{legacy_orphan}/images/0"
    ).status_code == 404

    # 越界索引/不存在的图
    question = create_question(client, images=[{"asset": register_asset(client)}])
    assert client.get(
        f"/api/paperlab/questions/{question['id']}/images/5"
    ).status_code == 404
    assert client.get("/api/paperlab/questions/999999/images/0").status_code == 404


def test_deleted_question_images_are_not_readable(client):
    asset_id = register_asset(client)
    question = create_question(client, images=[{"asset": asset_id}])
    url = f"/api/paperlab/questions/{question['id']}/images/0"
    assert client.get(url).status_code == 200

    assert client.delete(f"/api/paperlab/questions/{question['id']}").status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(f"/api/paperlab/questions/{question['id']}").status_code == 404


# ---------------------------------------------------------------------------
# 4. 旧原始文件名引用：迁移前后行为 + 幂等
# ---------------------------------------------------------------------------

def test_legacy_filename_migration_before_and_after(client):
    owner = user_id(client)
    legacy_data = png_bytes(color=(30, 30, 200))
    legacy_name = hashlib.sha256(legacy_data).hexdigest()[:32] + ".png"
    storage.save_file(ASSET_FOLDER, legacy_name, legacy_data, "image/png")
    question_id = insert_question_raw(
        owner, {"stem": "旧数据题", "images": [{"asset": legacy_name}]},
    )
    url = f"/api/paperlab/questions/{question_id}/images/0"

    # 迁移前：旧引用没有归属记录，不可读（统一 404，不暴露存储细节）
    assert client.get(url).status_code == 404
    assert assets.fetch_asset(legacy_name) is None

    # dry-run 只预检不写库
    preview = assets.migrate_legacy_assets(dry_run=True)
    assert preview["dry_run"] is True
    assert preview["migrated"] >= 1
    assert assets.fetch_asset(legacy_name) is None
    assert client.get(url).status_code == 404

    # apply 后旧引用确认归属到题目 owner，立即可读
    report = assets.migrate_legacy_assets()
    assert report["migrated"] >= 1
    row = assets.find_own_asset_by_ref(owner, legacy_name)
    assert row is not None and row["purpose"] == "legacy_migration"
    assert row["storage_ref"] == legacy_name
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == legacy_data

    # 幂等：重复迁移不再重复登记
    again = assets.migrate_legacy_assets()
    assert again["migrated"] == 0
    assert again["already"] >= 1


def test_legacy_asset_migrated_for_one_user_is_not_readable_by_another(client, second_client):
    owner = user_id(client)
    legacy_data = png_bytes(color=(90, 90, 20))
    legacy_name = hashlib.sha256(legacy_data).hexdigest()[:32] + ".png"
    storage.save_file(ASSET_FOLDER, legacy_name, legacy_data, "image/png")
    question_id = insert_question_raw(
        owner, {"stem": "旧数据题", "images": [{"asset": legacy_name}]},
    )
    asset = assets.adopt_reference(owner, legacy_name)
    assert asset

    forged = insert_question_raw(
        user_id(second_client),
        {"stem": "他人旧图", "images": [{"asset": legacy_name}]},
    )
    assert second_client.get(f"/api/paperlab/questions/{forged}/images/0").status_code == 404
    assert client.get(f"/api/paperlab/questions/{question_id}/images/0").status_code == 200

    # 已被他人登记的旧文件名不能被「抢占」为自用
    assert assets.adopt_reference(user_id(second_client), legacy_name) is None


# ---------------------------------------------------------------------------
# 5. 「平台精选」预留：public_derivative 已发布才允许他人读，默认不公开
# ---------------------------------------------------------------------------

def test_public_derivative_status_bit_is_reserved_and_default_private(client, second_client):
    student = user_id(second_client)
    private_asset = assets.save_asset_bytes(student, png_bytes(color=(1, 2, 3)))
    published_asset = assets.save_asset_bytes(
        student, png_bytes(color=(4, 5, 6)), status="published", public_derivative=True,
    )
    assert int(asset_row(private_asset)["public_derivative"]) == 0

    # 未发布/未标记公开：跨用户 404
    private_question = insert_question_raw(
        user_id(client), {"stem": "私人图", "images": [{"asset": private_asset}]},
    )
    assert client.get(
        f"/api/paperlab/questions/{private_question}/images/0"
    ).status_code == 404

    # 已发布且 public_derivative=1：允许其他用户经自己的题目读取（广场后续再做）
    public_question = insert_question_raw(
        user_id(client), {"stem": "精选图", "images": [{"asset": published_asset}]},
    )
    response = client.get(f"/api/paperlab/questions/{public_question}/images/0")
    assert response.status_code == 200
    assert response.content == storage.read_file(
        ASSET_FOLDER, str(asset_row(published_asset)["storage_ref"]),
    )


# ---------------------------------------------------------------------------
# 6. 工坊裁剪登记（导入链路）：产生的是 asset_id，不是原始文件名
# ---------------------------------------------------------------------------

def test_import_crop_registers_asset_id_and_question_is_served(client, second_client):
    owner = user_id(client)
    page_data = png_bytes(400, 300, color=(240, 240, 240))
    page_name = "test-page.png"
    storage.save_file(PAGE_FOLDER, page_name, page_data, "image/png")
    draft = {
        "images": [{"bbox": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.2}, "role": "stem"}],
    }
    cropped, warnings = digitize._crop_draft_images(
        draft, {"storage_name": page_name, "width": 400, "height": 300}, owner,
    )
    assert warnings == [], warnings
    asset_id = cropped["images"][0]["asset"]
    assert asset_id.startswith("pl_")
    row = asset_row(asset_id)
    assert int(row["user_id"]) == owner
    assert row["purpose"] == "import_crop"
    assert int(row["bytes"]) > 0 and row["sha256"]

    # 入库后的题目配图可按 URL 读取（content 存的是 asset_id）
    question = create_question(client, images=[{"asset": asset_id, "role": "stem"}])
    response = client.get(f"/api/paperlab/questions/{question['id']}/images/0")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")

    # 草稿里直接塞他人资产：入库前被移除并给出中文提示（不写进题目）
    other_asset = assets.save_asset_bytes(user_id(second_client), png_bytes())
    sanitized, warns = assets.sanitize_draft_images(owner, {"images": [{"asset": other_asset}]})
    assert "asset" not in sanitized["images"][0]
    assert warns and "无法归属" in warns[0]


# ---------------------------------------------------------------------------
# 7. 附加项（lead 协调）：上传有界读取，伪造超大文件 413 拒绝
# ---------------------------------------------------------------------------

class _FakeUpload:
    """模拟伪造 content-length 的 UploadFile：size 未声明，read 只返回请求的字节数。"""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.size = None
        self._data = data
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size is None or size < 0:
            return self._data
        return self._data[:size]


def test_import_upload_is_bounded_and_rejects_oversized_with_413(client, monkeypatch):
    monkeypatch.setattr(digitize, "MAX_FILE_BYTES", 1024)
    oversized = _FakeUpload("big.png", b"0" * (1024 * 1024))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(digitize._collect_pages([oversized]))
    assert caught.value.status_code == 413
    # 只读了上限 +1 字节，绝不整读伪造的大文件
    assert oversized.read_sizes == [1025]

    # 正常小文件仍通过（有界读取不影响正常上传）
    small = _FakeUpload("small.png", png_bytes(16, 16))
    pages, sources, title, notes = asyncio.run(digitize._collect_pages([small]))
    assert len(pages) == 1 and pages[0]["media_type"] == "image/jpeg"
    assert sources[0]["filename"] == "small.png"
    assert small.read_sizes == [1025]


def test_pixel_bomb_image_rejected_before_decode(client, monkeypatch):
    """小体积但声明尺寸巨大的图片：在 exif/convert 整图解码前按像素预算拒绝。"""
    monkeypatch.setattr(digitize, "MAX_PAGE_PIXELS", 500)
    payload = png_bytes(100, 100)  # 10000 像素 > 预算，但 PNG 体积很小
    assert len(payload) < 4096
    response = upload_import(client, [("炸弹.png", payload, "image/png")])
    assert response.status_code == 422, response.text
    assert "像素过大" in response.json()["detail"]

    # 正常小图在预算内仍然通过
    ok = upload_import(client, [("正常.png", png_bytes(20, 20), "image/png")])
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["pages"]) == 1


def test_pixel_bomb_pdf_page_rejected_before_render(client, monkeypatch):
    """超大页面尺寸的 PDF：渲染前按「点尺寸 × 倍率」像素预算拒绝。"""
    monkeypatch.setattr(digitize, "MAX_PAGE_PIXELS", 10_000)
    writer = PdfWriter()
    writer.add_blank_page(width=2000, height=2000)  # 渲染后 ≈ 4000×4000 像素
    buffer = BytesIO()
    writer.write(buffer)
    response = upload_import(client, [("超大页.pdf", buffer.getvalue(), "application/pdf")])
    assert response.status_code == 422, response.text
    assert "页面尺寸过大" in response.json()["detail"]
