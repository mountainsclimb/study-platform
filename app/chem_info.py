"""可选化学信息（RDKit）：SMILES 校验、分子式与相对分子质量。

方案 §9 进阶项验证后的落点：RDKit 仅作为**服务端可选依赖**，不进入前端；
未安装时本模块所有接口返回 503，不影响 `/api/chem/balance` 与分子查看器的既有
OpenChemLib/3Dmol 渲染链路。

安装：``pip install -r requirements-chem.txt``（锁定 rdkit / numpy / Pillow）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import current_user

__all__ = ["router", "analyze_smiles", "MAX_SMILES_LENGTH"]

router = APIRouter(prefix="/api/chem", tags=["Chem info"])

MAX_SMILES_LENGTH = 500


class ChemInfoInput(BaseModel):
    smiles: str = Field(min_length=1, max_length=MAX_SMILES_LENGTH)


class ChemInfoUnavailable(RuntimeError):
    """RDKit 未安装或无法初始化。"""


def _load_rdkit():
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import Descriptors, rdMolDescriptors
    except ImportError as error:
        raise ChemInfoUnavailable("RDKit 未安装，分子信息接口暂不可用") from error
    # RDKit 会把解析错误打到 stderr；接口用 ok:false 返回原因，避免污染服务日志。
    RDLogger.DisableLog("rdApp.*")
    return Chem, Descriptors, rdMolDescriptors


def analyze_smiles(smiles: str) -> dict:
    """解析 SMILES；合法返回规范 SMILES/分子式/相对分子质量，非法返回 ok:false。

    不抛 422/404：调用方（分子查看器）需要把“无效 SMILES”作为可展示的普通结果；
    未安装 RDKit 时抛 :class:`ChemInfoUnavailable`，由路由转 503。
    """
    Chem, Descriptors, rdMolDescriptors = _load_rdkit()
    text = str(smiles or "").strip()
    if not text:
        return {"ok": False, "reason": "SMILES 不能为空"}
    if len(text) > MAX_SMILES_LENGTH:
        return {"ok": False, "reason": f"SMILES 不能超过 {MAX_SMILES_LENGTH} 个字符"}
    try:
        molecule = Chem.MolFromSmiles(text)
    except Exception:  # noqa: BLE001 - RDKit 对极端输入可能抛内部异常
        return {"ok": False, "reason": "SMILES 无法解析"}
    if molecule is None:
        return {"ok": False, "reason": "SMILES 无法解析"}
    try:
        formula = rdMolDescriptors.CalcMolFormula(molecule)
        weight = round(float(Descriptors.MolWt(molecule)), 3)
        canonical = Chem.MolToSmiles(molecule)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "分子信息计算失败"}
    return {
        "ok": True,
        "canonical_smiles": canonical,
        "formula": formula,
        "molecular_weight": weight,
    }


@router.post("/info")
def api_chem_info(payload: ChemInfoInput, user=Depends(current_user)):
    try:
        return analyze_smiles(payload.smiles)
    except ChemInfoUnavailable as error:
        raise HTTPException(503, str(error)) from error
