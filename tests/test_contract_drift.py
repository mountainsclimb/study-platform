"""端点索引与契约文档漂移检查。"""
import json
from pathlib import Path

from scripts.update_api_index import INDEX_PATH, collect_routes

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DOCS = (
    ROOT / "CONTRACT.md",
    ROOT / "INTEGRATION-CONTRACT.md",
    ROOT / "INTEGRATION-API-INDEX.md",
    ROOT / "app" / "paperlab" / "API.md",
)
NEW_CAPABILITY_PREFIXES = (
    "/api/vocab",
    "/api/vocab/dictation",
    "/api/chem/info",
    "/api/chem/balance",
    "/api/insight/kp-graph",
    "/lab/python",
)


def test_api_index_matches_application_routes():
    payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    routes = collect_routes()
    assert payload["count"] == len(routes)
    assert payload["routes"] == routes


def test_index_script_check_mode_passes():
    from scripts import update_api_index

    assert update_api_index.main(["--check"]) == 0


def test_new_capability_routes_are_indexed():
    paths = {item["path"] for item in collect_routes()}
    for prefix in NEW_CAPABILITY_PREFIXES:
        assert any(
            path == prefix or path.startswith(prefix + "/")
            for path in paths
        ), prefix


def test_contract_docs_cover_new_capabilities():
    combined = "\n".join(path.read_text(encoding="utf-8") for path in CONTRACT_DOCS)
    for prefix in NEW_CAPABILITY_PREFIXES:
        assert prefix in combined, prefix
    for kind in ("geometry_demo", "timeline", "molecule_3d", "physics_sandbox"):
        assert kind in combined, kind
