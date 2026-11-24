"""通用识别块校对组件与接线静态契约。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "web" / "js" / "components" / "blockProofreader.js"
PAPER_IMPORT = ROOT / "web" / "js" / "components" / "paperImport.js"
FORMULA_EDITOR = ROOT / "web" / "js" / "components" / "formulaEditor.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_component_uses_shared_block_contract_and_sanitizer():
    source = _read(COMPONENT)
    for marker in (
        "export function createBlockProofreader",
        "parseBlocksWithFormulas",
        "buildConfirmBlocks",
        "sanitizeTableHtml",
        "renderBlockLocation",
        "attachBlockOverlay",
    ):
        assert marker in source, marker
    # 用户文本必须走 textContent / el()，表格 HTML 只经 sanitizeTableHtml 后写入。
    assert "innerHTML = `" not in source
    assert "sanitizeTableHtml(tableHtml)" in source


def test_paperlab_page_wires_component_and_block_endpoints():
    source = _read(PAPER_IMPORT)
    assert "createBlockProofreader" in source
    assert "/blocks`" in source
    assert "expected_version" in source
    assert "paper.import.pageBlocks" in source


def test_formula_editor_exposes_generic_insert_helper():
    source = _read(FORMULA_EDITOR)
    assert "export function insertFormulaIntoField" in source
    assert "export function formulaEditorButton" in source
    for page in ("assistant.js", "question.js"):
        text = _read(ROOT / "web" / "js" / "pages" / page)
        assert "formulaEditorButton" in text, page
    classroom = _read(ROOT / "web" / "js" / "components" / "classroomEditor.js")
    assert "formulaEditorButton" in classroom


def test_chem_balance_entry_is_wired():
    question = _read(ROOT / "web" / "js" / "pages" / "question.js")
    assert "chemBalanceEntryButton" in question
    assert "data-chem-balance-open" in question
    component = _read(ROOT / "web" / "js" / "components" / "chemBalancer.js")
    assert "api('/chem/balance'" in component
    assert "normalizeArrow" in component


def test_explore_tools_menu_is_wired():
    question = _read(ROOT / "web" / "js" / "pages" / "question.js")
    assert "createExploreToolsMenu" in question
    assert "subject: q.subject" in question
    component = _read(ROOT / "web" / "js" / "components" / "exploreTools.js")
    assert "export function createExploreToolsMenu" in component
    assert "export function recommendedTool" in component
    assert "SUBJECT_ALIASES" in component
    assert "data-explore-tools" in component


def test_demo_create_menu_is_shared_by_notes_and_classroom():
    component = _read(ROOT / "web" / "js" / "components" / "demoCreateMenu.js")
    assert "export function createDemoCreateMenu" in component
    assert "target_kind" in component
    for page in ("notes.js", "classroom.js"):
        text = _read(ROOT / "web" / "js" / "pages" / page)
        assert "createDemoCreateMenu" in text, page
    notes = _read(ROOT / "web" / "js" / "pages" / "notes.js")
    assert "targetKind: 'note'" in notes
    classroom = _read(ROOT / "web" / "js" / "pages" / "classroom.js")
    assert "targetKind: 'classroom'" in classroom
