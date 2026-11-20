from io import BytesIO

from PIL import Image, ImageDraw
from pypdf import PdfWriter
import pytest

from app import ai, db, ops, practice, storage
from app.ai import notes_agent
from app.ai.verify import _load_upload_evidence
from test_practice import bank_question


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.fail_after = None
        self.fail_delete = False

    def put_object(self, *, Bucket, Key, Body, ContentType):
        if self.fail_after is not None and len(self.objects) >= self.fail_after:
            raise OSError("write unavailable")
        self.objects[Bucket, Key] = Body, ContentType

    def get_object(self, *, Bucket, Key):
        return {"Body": BytesIO(self.objects[Bucket, Key][0])}

    def delete_object(self, *, Bucket, Key):
        if self.fail_delete:
            raise OSError("delete unavailable")
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def object_store(monkeypatch):
    client = MemoryS3()
    monkeypatch.setenv("S3_BUCKET", "test-private")
    monkeypatch.setenv("S3_PREFIX", "zhixu/test")
    monkeypatch.setattr(storage, "_s3_client", lambda *args: client)
    return client


def _png():
    """正常尺寸且带笔迹的作答图：空白/小图门禁对作答截图同样生效。"""
    output = BytesIO()
    image = Image.new("RGB", (240, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.line((20, 20, 220, 220), fill="black", width=4)
    draw.rectangle((40, 40, 200, 200), outline="red", width=3)
    image.save(output, "PNG")
    return output.getvalue()


def test_mixed_storage_private_question_and_attempt_images(client, second_client, isolated_db, object_store, monkeypatch):
    monkeypatch.delenv("S3_BUCKET")
    legacy = client.post("/api/uploads/question", files={"file": ("old.txt", b"question", "text/plain")})
    assert legacy.status_code == 200, legacy.text
    monkeypatch.setenv("S3_BUCKET", "test-private")

    raw = _png()
    upload = client.post("/api/uploads/question", files=[
        ("files", ("first.png", raw, "image/png")),
        ("files", ("second.png", raw, "image/png")),
    ])
    assert upload.status_code == 200, upload.text
    question = upload.json()
    for url in (question["preview_url"], question["extra_previews"][0]["preview_url"]):
        preview = client.get(url)
        assert preview.content == raw and preview.headers["content-type"] == "image/png"
        assert "no-store" in preview.headers["cache-control"]
        assert second_client.get(url).status_code == 404
    assert client.get(legacy.json()["preview_url"]).content == b"question"
    user_id = client.get("/api/auth/me").json()["id"]
    assert len(_load_upload_evidence(user_id, question["id"])[1]) == 2

    qid = bank_question(objective=False)["qid"]
    attempt = client.post("/api/attempts", data={"qid": qid}, files={"file": ("answer.png", raw, "image/png")})
    assert attempt.status_code == 200, attempt.text
    image_url = attempt.json()["attempt"]["image_url"]
    result = client.get(image_url)
    assert result.content == practice.get_attempt_image(user_id, attempt.json()["attempt_id"])
    assert result.headers["content-type"] == "image/jpeg"
    assert second_client.get(image_url).status_code == 404
    assert len(object_store.objects) == 3
    assert all(key.startswith("zhixu/test/") for _, key in object_store.objects)
    assert len(list((isolated_db / "ai-files").iterdir())) == 1
    assert list((isolated_db / "attempts").iterdir()) == []


def test_s3_document_ocr_delete_and_backup_detection(client, second_client, isolated_db, object_store, monkeypatch):
    monkeypatch.setattr(notes_agent, "_selected_provider", lambda *args: {})

    async def ocr(_provider, _model, _prompt, images, **_kwargs):
        assert len(images) == 1 and images[0]["mime"] == "image/jpeg"
        return {"content": "扫描资料 OCR 内容"}

    monkeypatch.setattr(ai, "_call_model", ocr)
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    pdf.write(buffer)
    result = client.post("/api/documents", files={"file": ("scan.pdf", buffer.getvalue(), "application/pdf")})
    assert result.status_code == 200, result.text
    url = f"/api/documents/{result.json()['id']}"
    assert result.json()["status"] == "needs_ocr"
    options = {"provider_id": 1, "model": "test-vision", "confirmed": True}
    assert second_client.post(url + "/ocr", json=options).status_code == 404
    assert second_client.delete(url).status_code == 404
    result = client.post(url + "/ocr", json=options)
    assert result.status_code == 200, result.text
    assert result.json()["document"]["status"] == "ready"

    with monkeypatch.context() as local_config:
        local_config.delenv("S3_BUCKET")
        with pytest.raises(ops.ExternalStorageBackupRequired):
            ops.create_backup(isolated_db / "incomplete.zip", data_dir=isolated_db)
    assert not (isolated_db / "incomplete.zip").exists()
    object_store.fail_delete = True
    assert client.delete(url).status_code == 503
    assert client.get("/api/documents").json()["items"]
    object_store.fail_delete = False
    assert client.delete(url).status_code == 200
    assert client.get("/api/documents").json()["items"] == []
    assert not object_store.objects


def test_multi_image_storage_failure_cleans_up_first_object(client, object_store):
    raw = _png()
    object_store.fail_after = 1
    response = client.post("/api/uploads/question", files=[
        ("files", ("first.png", raw, "image/png")),
        ("files", ("second.png", raw, "image/png")),
    ])
    assert response.status_code == 503
    assert not object_store.objects
    with db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM ai_uploads").fetchone()[0] == 0
