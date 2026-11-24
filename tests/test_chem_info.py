"""RDKit 可选化学信息验收（方案 §9）：安装时的 SMILES/分子式/分子量，未安装时 503。"""
from __future__ import annotations

import importlib.util

import pytest

from app import chem_info


def _rdkit_available() -> bool:
    return bool(importlib.util.find_spec("rdkit"))


def test_chem_info_returns_503_when_rdkit_missing(client, monkeypatch):
    def unavailable():
        raise chem_info.ChemInfoUnavailable("RDKit 未安装，分子信息接口暂不可用")

    monkeypatch.setattr(chem_info, "_load_rdkit", unavailable)
    response = client.post("/api/chem/info", json={"smiles": "CCO"})
    assert response.status_code == 503
    assert "RDKit" in response.text


def test_molecule_viewer_uses_optional_chem_info():
    from pathlib import Path

    component = (Path(__file__).resolve().parents[1] / "web" / "js" / "components"
                 / "moleculeViewer.js").read_text(encoding="utf-8")
    assert "/chem/info" in component
    assert "study.oss.molecule.info" in component
    assert "loadChemInfo" in component


def test_chem_info_input_validation(client):
    empty = client.post("/api/chem/info", json={"smiles": ""})
    assert empty.status_code == 422
    too_long = client.post("/api/chem/info", json={"smiles": "C" * 501})
    assert too_long.status_code == 422


@pytest.mark.skipif(not _rdkit_available(), reason="rdkit 未安装（可选依赖）")
def test_analyze_smiles_known_molecules():
    ethanol = chem_info.analyze_smiles("CCO")
    assert ethanol["ok"] is True
    assert ethanol["formula"] == "C2H6O"
    assert ethanol["molecular_weight"] == pytest.approx(46.069, abs=0.001)
    assert ethanol["canonical_smiles"] == "CCO"

    benzene = chem_info.analyze_smiles("C1=CC=CC=C1")
    assert benzene["ok"] is True
    assert benzene["formula"] == "C6H6"
    assert benzene["canonical_smiles"] == "c1ccccc1"

    bad = chem_info.analyze_smiles("INVALID(")
    assert bad["ok"] is False
    assert bad["reason"]


@pytest.mark.skipif(not _rdkit_available(), reason="rdkit 未安装（可选依赖）")
def test_chem_info_route_with_rdkit(client):
    response = client.post("/api/chem/info", json={"smiles": "CCO"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["formula"] == "C2H6O"
    assert body["molecular_weight"] == pytest.approx(46.069, abs=0.001)

    bad = client.post("/api/chem/info", json={"smiles": "INVALID("})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
