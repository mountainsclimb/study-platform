import asyncio
import json

from test_practice import bank_question


def test_community_explanations_require_consent_green_verification_and_unlock(
    client, second_client, monkeypatch,
):
    from app import ai
    from app.ai.verify import _save_explanation

    monkeypatch.delenv("COMMUNITY_EXPLANATIONS_ENABLED", raising=False)
    question = bank_question()
    qid, answer = question["qid"], question["answer_text"]
    assert client.post("/api/attempts", data={"qid": qid, "answer": answer}).status_code == 200
    owner = client.get("/api/auth/me").json()["id"]
    current_answer = answer

    async def mock_call(_provider, model, *_args, **_kwargs):
        return {
            "content": json.dumps({
                "final_answer": {"kind": "choice", "value": current_answer},
                "steps": [{"no": 1, "title": "分析", "content": "解题过程", "score_point": True}],
            }),
            "model": model, "provider_name": "private-connection-name",
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }

    monkeypatch.setattr(ai, "_call_model", mock_call)
    data = ai.JobInput(
        task="explain", qid=qid, provider_id=1, model="author-model",
        cross_verify=True, verify_provider_id=2, verify_model="verifier-model", structured_verify=True,
    )

    def generate(**changes):
        return asyncio.run(ai._explain_job(
            owner, data.model_copy(update=changes), {"id": 1}, {"id": 2},
        ))["explanation_id"]

    exp_id = generate()
    path = f"/api/explanations/{exp_id}/share"
    feature_path = f"/api/admin/community-explanations/{exp_id}/feature"
    params = {"qid": qid}
    assert client.put(path, json={"shared": True}).status_code == 404
    private = client.get("/api/explanations", params=params).json()
    assert private["items"][0]["id"] == exp_id
    assert not private["items"][0]["can_share"]
    assert not client.get("/api/admin/community-explanations").json()["enabled"]

    monkeypatch.setenv("COMMUNITY_EXPLANATIONS_ENABLED", "1")
    custom_id = generate(requirements="Only for my own reference")
    current_answer = next(option["label"] for option in question["options"] if option["label"] != answer)
    conflict_id = generate()
    uploaded_id = _save_explanation(owner, None, 1, "detailed", {"content": "私有上传内容"})
    for rejected_id in (custom_id, conflict_id, uploaded_id):
        assert client.put(f"/api/explanations/{rejected_id}/share", json={"shared": True}).status_code == 422

    assert second_client.put(path, json={"shared": True}).status_code == 404
    assert client.put(feature_path, json={"featured": True}).status_code == 404
    assert client.put(path, json={"shared": True}).json()["shared"]
    assert second_client.get("/api/explanations", params=params).json()["community_items"] == []
    assert second_client.post("/api/attempts", data={"qid": qid, "answer": answer}).status_code == 200
    visible = second_client.get("/api/explanations", params=params).json()
    assert visible["items"] == []
    assert visible["community_items"][0]["models"] == [{"model": "author-model"}, {"model": "verifier-model"}]
    assert not visible["community_items"][0]["featured"]
    assert second_client.delete(f"/api/explanations/{exp_id}").status_code == 404
    assert second_client.put(feature_path, json={"featured": True}).status_code == 403
    assert client.get("/api/admin/community-explanations").json()["items"][0]["id"] == exp_id
    assert client.put(feature_path, json={"featured": True}).json()["featured"]
    assert second_client.get("/api/explanations", params=params).json()["community_items"][0]["featured"]

    monkeypatch.delenv("COMMUNITY_EXPLANATIONS_ENABLED")
    assert second_client.get("/api/explanations", params=params).json()["community_items"] == []
    monkeypatch.setenv("COMMUNITY_EXPLANATIONS_ENABLED", "1")
    assert not client.put(path, json={"shared": False}).json()["featured"]
    assert second_client.get("/api/explanations", params=params).json()["community_items"] == []
    assert not client.put(path, json={"shared": True}).json()["featured"]


def test_prompt_templates_publication_and_admin_recommendation(client, second_client):
    from app import db

    response = second_client.post("/api/prompt-templates", json={
        "name": "冲刺模板", "config": {"format": "one_pager", "unknown_field": "not published"},
        "custom_requirements": "精简",
    })
    assert response.status_code == 200
    tpl_id = response.json()["id"]
    path = f"/api/prompt-templates/{tpl_id}"
    with db.connection() as con:
        con.execute("UPDATE prompt_templates SET is_recommended=1 WHERE id=?", (tpl_id,))
    assert not second_client.get("/api/prompt-templates").json()["items"][0]["is_recommended"]
    assert client.get("/api/prompt-templates").json()["items"] == []
    assert client.get("/api/admin/prompt-templates").json()["items"] == []
    assert client.post(f"{path}/recommend").status_code == 404
    assert client.put(f"{path}/share", json={"shared": True}).status_code == 404
    assert second_client.put(f"{path}/share", json={"shared": True}).json()["is_shared"]
    shared = client.get("/api/prompt-templates").json()["items"][0]
    assert shared["config"] == {"format": "one_pager"}
    assert shared["custom_requirements"] == "精简"
    assert "user_id" not in shared and not shared["own"]
    assert not shared["is_recommended"]
    assert second_client.post(f"{path}/recommend").status_code == 403
    assert second_client.get("/api/admin/prompt-templates").status_code == 403
    assert client.get("/api/admin/prompt-templates").json()["items"][0]["id"] == tpl_id
    for _ in range(2):
        assert client.post(f"{path}/recommend", json={"recommended": True}).json()["is_recommended"]
    assert client.get("/api/prompt-templates").json()["items"][0]["is_recommended"]
    assert not second_client.put(f"{path}/share", json={"shared": False}).json()["is_recommended"]
    assert client.get("/api/prompt-templates").json()["items"] == []
    assert not second_client.put(f"{path}/share", json={"shared": True}).json()["is_recommended"]
