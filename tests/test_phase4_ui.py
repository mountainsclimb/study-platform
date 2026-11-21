"""WS29 第四轮前端收口（Phase 4）静态契约测试。

覆盖：
1. 题目详情：平台精选公开配图（payload images[].accessible/url，兼容 figure_urls，
   回退 figure_notice）、精选纠错入口接线 `POST /api/plaza/questions/{public_id}/issues`
   （登录可用/未登录引导登录/成功后 toast）、「在广场查看」精选编号链接；
2. 练习页：学段筛选（`/api/questions?grade=` 能力探测、catalog.grade_stages/固定学段、
   URL 状态恢复、重置与清除芯片），来源/审核徽标保持；
3. 学情/首页：official 精选口径（精选可练/已练精选/精选错题）能力探测降级、不写死数字；
4. 任务中心：paper-import/enrich/compose/revision、course、notes、ocr 深链参数格式；
5. 笔记/Anki：站内题目引用（bank:/app:/official:）链接 + 版本提示；Anki 卡片背面
   题目引用/来源版本字段（不改变 .apkg 模板字段）；
6. locale 中英文键对齐（新命名空间齐全、占位符一致）；
7. 改动 ESM 文件真 ESM 解析（vm.SourceTextModule）+ node --check（无 Node 时跳过）。

本文件只做静态契约（不启动服务、不联网），与浏览器验收互补。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

QUESTION_JS = WEB / "js" / "pages" / "question.js"
PRACTICE_JS = WEB / "js" / "pages" / "practice.js"
INSIGHT_JS = WEB / "js" / "pages" / "insight.js"
HOME_JS = WEB / "js" / "pages" / "home.js"
TASKS_JS = WEB / "js" / "pages" / "tasks.js"
NOTES_JS = WEB / "js" / "pages" / "notes.js"
LOCALES = WEB / "locales"
ANKI_PY = ROOT / "app" / "anki_export.py"

CHANGED_JS = [QUESTION_JS, PRACTICE_JS, INSIGHT_JS, HOME_JS, TASKS_JS, NOTES_JS]
NODE = shutil.which("node")

# 通过常量映射 / 模板字符串间接引用的 locale 键（静态扫描 t('...') 覆盖不到）。
INDIRECT_LOCALE_KEYS = [
    "question.source.issue.field.stem",
    "question.source.issue.field.options",
    "question.source.issue.field.answer",
    "question.source.issue.field.explanation",
    "question.source.issue.field.figure",
    "question.source.issue.field.source",
    "question.source.issue.field.other",
    "notes.questionRef.bank",
    "notes.questionRef.app",
    "notes.questionRef.official",
]


def read(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def compact(source: str) -> str:
    """去掉多余空白便于跨行断言（不改字符串字面量内容）。"""
    return re.sub(r"\s+", " ", source)


def function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start >= 0, f"未找到代码块起点：{start_marker}"
    end = source.find(end_marker, start)
    assert end > start, f"未找到代码块终点：{end_marker}"
    return source[start:end]


def direct_t_keys(source: str) -> set[str]:
    return set(re.findall(r"\bt\('([^']+)'", source))


# ---------------------------------------------------------------------------
# 1. 题目详情：平台精选公开配图 + 广场纠错 + 广场查看
# ---------------------------------------------------------------------------

def test_question_official_public_figures_contract():
    source = read(QUESTION_JS)
    text = compact(source)
    # WS26 契约：payload.images[] 每项 {accessible,url,role,alt,...}；只渲染可公开的图。
    for token in (
        "function officialFigures(q)",
        "function officialTextNode(",
        "function officialFigureGallery(",
        "function officialStemNode(",
        "function isRenderableImageUrl(url)",
        "entry.accessible === false",
        "figure_urls",
        "figure_notice",
        "[图N]/[公式N]",
    ):
        assert token in source, f"精选配图缺少契约标记：{token}"
    assert "for (const image of Array.isArray(q?.images) ? q.images : []) push(image, 'figure');" in text
    assert "const figureUrls = q?.figure_urls;" in text
    # 题干/选项都走 official 渲染分支；公开图只在 accessible+url 时进 <img>。
    assert "container.append(...officialStemNode(q, officialFigures(q)));" in text
    assert "if (isOfficialQuestion(q)) {" in text
    assert 'class: \'bank-image official-figure\'' in text
    assert "data-official-figure" in source
    assert "question.source.figureLoadFailed" in source
    # 不可公开/加载失败降级：figure_notice 提示与可读占位，不拼接私有存储路径。
    assert "question.source.figureUnavailable" in source


def test_question_official_issue_endpoint_and_login_guide():
    source = read(QUESTION_JS)
    text = compact(source)
    # 纠错接口接线：POST /api/plaza/questions/{public_id}/issues {field, description}。
    assert "/plaza/questions/${encodeURIComponent(publicId)}/issues" in source
    assert "method: 'POST'" in text
    assert "data: { field: fieldSel.value, description: text }" in text
    assert "function openOfficialIssueDialog(q, publicId)" in source
    assert "description: text" in text
    assert "maxlength: '4000'" in source
    assert "text.length < 2" in text
    assert "question.source.issueDescriptionRequired" in source
    assert "question.source.issueSubmitted" in source
    # 登录可用；未登录引导登录（保留 hash 语义由 main.js 负责）。
    assert "if (!state.user) {" in text
    assert "question.source.issueLoginRequired" in source
    assert "go('home');" in text
    # 报错按钮对 official 走纠错接口，不再只是占位 toast（占位文案保留给旧 payload）。
    assert "data-official-issue" in source
    assert "openOfficialIssueDialog(q, officialPublicId)" in text
    assert "question.source.reportPlaceholder" in source
    # 字段白名单与后台契约一致。
    assert "const OFFICIAL_ISSUE_FIELDS = ['stem', 'options', 'answer', 'explanation', 'figure', 'source', 'other'];" in text


def test_question_official_plaza_link_and_bank_paths_kept():
    source = read(QUESTION_JS)
    text = compact(source)
    # 精选编号 → 广场公开详情（#/plaza?id=public_id）。
    assert "data-official-plaza-link" in source
    assert "go('plaza', null, { id: officialPublicId })" in text
    assert "question.source.publicId" in source
    assert "question.source.featuredVersion" in source
    # 既有能力保持：题库题「收录到我的题库」、来源徽标、official 不接题库讲解/管理员标注。
    assert "'/paperlab/questions/collect'" in source
    assert "!isApp && !isOfficial ? collectToMyBankButton(q) : null" in text
    assert "if (!isApp && !isOfficial) container.append(aiSection);" in text
    assert "state.user.role === 'admin' && !isApp && !isOfficial" in text


# ---------------------------------------------------------------------------
# 2. 练习页：学段筛选（能力探测降级 + URL 状态）
# ---------------------------------------------------------------------------

def test_practice_grade_filter_capability_probe():
    source = read(PRACTICE_JS)
    text = compact(source)
    assert "export function detectGradeFilter(" in source
    # 合法 200 请求探测：响应带 grade_filter 才认为后端就绪；旧后端忽略参数 → 隐藏控件。
    assert "api(`/questions?page=1&page_size=1&grade=${encodeURIComponent(probeValue)}`)" in source
    assert "data.grade_filter && typeof data.grade_filter === 'object'" in text
    assert "FALLBACK_GRADE_STAGES = ['小学', '初中', '高中']" in source
    assert "catalog?.grade_stages" in text
    assert "detectGradeFilter()," in text
    assert "gradeFilterReady" in source
    # 未就绪整体隐藏，且丢弃 URL/本地状态里的 grade（避免假筛选）。
    assert "gradeFilterReady ? field(t('practice.grade'), gradeSel) : null" in text
    assert "if (!gradeFilterReady && state.filters.grade)" in text
    assert "delete state.filters.grade;" in text


def test_practice_grade_filter_state_and_reset():
    source = read(PRACTICE_JS)
    text = compact(source)
    # grade 进入 URL 恢复 / 筛选芯片 / 高级筛选计数 / 控件同步。
    assert "'include_ai_difficulty', 'source', 'grade'" in text
    assert "grade: t('practice.grade')," in text
    assert "'include_ai_difficulty', 'grade', 'kp_id'" in text
    assert "if (gradeFilterReady) gradeSel.value = state.filters.grade || '';" in text
    # 选项来自后端 catalog（未下发时退回固定学段）+ 全部学段空值。
    assert "practice.allGrades" in source
    assert "const gradeSel = makeSelect('grade', t('practice.grade')," in text
    # 生效范围提示：题库无 grade 列时只对平台精选生效（grade_filter.applied=false）。
    assert "results.grade_filter && results.grade_filter.applied === false" in text
    assert "practice.gradeFilterNotice" in source
    # 来源/审核徽标与 official 来源筛选保持（不在本任务回归范围）。
    assert "practice.source.official" in source
    assert "badges.append(sourceBadge(q));" in text
    assert "api('/questions?page=1&page_size=1&source=official')" in source


# ---------------------------------------------------------------------------
# 3. 学情/首页：official 精选口径
# ---------------------------------------------------------------------------

def test_insight_official_caliber_cards():
    source = read(INSIGHT_JS)
    text = compact(source)
    assert "function officialCaliber(homeStats)" in source
    assert "const official = homeStats?.official;" in text
    # WS26 契约字段：practice_pool / practiced / wrong_count，缺失即整块不渲染。
    for token in ("official.practice_pool", "official.total_questions", "official.practiced", "official.wrong_count"):
        assert token in source, f"学情 official 口径缺少字段：{token}"
    assert "officialCaliberCards(homeStats)" in text
    for key in ("insight.official.pool", "insight.official.practiced", "insight.official.wrong", "insight.official.caption"):
        assert key in source, f"学情 official 口径缺少文案键：{key}"
    # 文案不写死数字：数值全部经 number(...) 渲染。
    assert "number(caliber.pool)" in text
    assert "number(caliber.practiced)" in text
    assert "number(caliber.wrong)" in text


def test_home_official_caliber_card_capability_and_degrade():
    source = read(HOME_JS)
    text = compact(source)
    assert "function officialCaliberOf(stats, homeStats)" in source
    assert "function officialCaliberCard(stats, homeStats)" in source
    for token in ("official.practice_pool, official.total_questions", "official.practiced", "official.wrong_count"):
        assert token in source, f"首页 official 口径缺少字段：{token}"
    # 能力探测降级：字段缺失返回 null（不渲染空卡、不显示 0 假数字）。
    assert "if (!official || typeof official !== 'object') return null;" in text
    assert "if (!caliber) return null;" in text
    assert "const officialCard = officialCaliberCard(stats, homeStats);" in text
    assert "officialCard," in text
    for key in ("home.official.title", "home.official.practicedLabel", "home.official.wrongLabel", "home.official.aria"):
        assert key in source, f"首页 official 口径缺少文案键：{key}"
    # 既有精选可练数量能力探测保持。
    assert "detectFeaturedPractice()," in text
    assert "bankCaliberCard(bank, featuredPractice)" in text


# ---------------------------------------------------------------------------
# 4. 任务中心：深链参数格式
# ---------------------------------------------------------------------------

def _result_route_body() -> str:
    source = read(TASKS_JS)
    body = function_body(source, "function resultRoute(job) {", "function openResult(job) {")
    return compact(body)


def test_tasks_paperlab_deep_links():
    body = _result_route_body()
    source = read(TASKS_JS)
    assert "function firstId(" in source
    # 试卷识别导入 → #/paperlab?import=<import_id>（paperlab.js 已能读 import 参数）。
    assert "case 'paper-import':" in body
    assert "params: importId ? { import: importId } : null" in body
    # AI 组卷 / 错题巩固卷 → #/paperlab?tab=papers&paper=<paper_id>。
    assert "case 'paper-compose':" in body and "case 'paper-revision':" in body
    assert "{ route: 'paperlab', params: { tab: 'papers', paper: paperId } }" in body
    # AI 补全（我的题库个人题）→ #/mybank?edit=<question_id>；有试卷 id 时回试卷详情。
    assert "case 'paper-enrich':" in body
    assert "{ route: 'mybank', params: questionId ? { edit: questionId } : null }" in body


def test_tasks_classroom_notes_ocr_deep_links():
    body = _result_route_body()
    # 课堂 → #/classroom?course=<id>（classroom.js 已读 course 参数）。
    assert "case 'course':" in body
    assert "params: { course: courseId }" in body
    # 笔记 → #/notes?note=<id>；资料 OCR → #/notes?document=<id>。
    assert "case 'notes':" in body
    assert "params: noteId ? { note: noteId } : null" in body
    assert "case 'ocr':" in body
    assert "params: { document: documentId }" in body
    # 既有「拍题识别 → #/upload?upload=<upload_id>」深链保持。
    assert "case 'recognize':" in body
    assert "params: { upload: uploadId }" in body
    assert "request.upload_id" in body


# ---------------------------------------------------------------------------
# 5. 笔记/Anki：站内题目引用与版本
# ---------------------------------------------------------------------------

def test_notes_question_refs_and_versions():
    source = read(NOTES_JS)
    text = compact(source)
    # 统一题目引用（bank:/app:/official:）→ 可点击链接回练习页。
    assert "const QUESTION_REF_PATTERN =" in source
    assert "function questionRefsOf(note)" in source
    assert "official:plz-[A-Za-z0-9]+" in source
    assert "function noteQuestionRefsNode(note)" in source
    assert "function noteSourceVersionsNode(note)" in source
    assert "go('practice', entry.ref)" in text
    assert "data-note-question-refs" in source
    assert 'data-question-ref' in source
    for key in ("notes.questionRefsTitle", "notes.questionRefVersion", "notes.sourceVersionsLine"):
        assert key in source, f"笔记题目引用缺少文案键：{key}"
    # 来源资料版本来自 note.source_versions（生成时资料版本，替换资料不改变旧笔记引用）。
    assert "note?.source_versions" in text


def test_notes_document_and_note_deep_links():
    source = read(NOTES_JS)
    text = compact(source)
    assert "export async function renderNotes(container, version, params = null)" in source
    assert "params?.get?.('note')" in source
    assert "params?.get?.('document')" in source
    assert "'data-document-id': String(d.id)" in text
    assert "is-deep-linked" in source
    assert "deepDocumentId" in source


def test_anki_export_reference_fields_without_template_change():
    source = read(ANKI_PY)
    for token in (
        "_QUESTION_REF_RE",
        "def question_ref_of(",
        "def source_version_of(",
        "def _question_ref_footer(",
        "def _note_refs_footer(",
        "def _note_question_refs(",
        "def _note_source_versions(",
        "question_refs=None, source_versions=None",
        "题目引用：",
        "来源版本：",
    ):
        assert token in source, f"Anki 题目引用/版本字段缺少：{token}"
    # 不破坏 .apkg 结构与既有测试语义：模型字段仍是 Front/Back/Tags。
    assert 'fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Tags"}]' in source
    assert '"front": text_to_html(question)' in source


# ---------------------------------------------------------------------------
# 6. locale：本轮新键中英文齐全且对齐
# ---------------------------------------------------------------------------

def load_locale(name: str) -> dict:
    path = LOCALES / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
    return data


def placeholders(value: str) -> list[str]:
    return sorted(re.findall(r"\{([A-Za-z0-9_]+)\}", str(value)))


def test_locale_phase4_namespaces_complete_and_aligned():
    zh = load_locale("zh-CN")
    en = load_locale("en-US")
    minimum = {
        "question.source.": 30,
        "insight.official.": 4,
        "home.official.": 5,
        "notes.questionRef": 7,
    }
    for prefix, expected in minimum.items():
        zh_keys = {key for key in zh if key.startswith(prefix)}
        en_keys = {key for key in en if key.startswith(prefix)}
        assert len(zh_keys) >= expected, f"{prefix} 中文键不足：{sorted(zh_keys)}"
        assert zh_keys == en_keys, f"{prefix} 中英文键不一致：{sorted(zh_keys ^ en_keys)[:10]}"
        for key in sorted(zh_keys):
            assert zh[key].strip() and en[key].strip(), f"{key} 中英文文案为空"
            assert placeholders(zh[key]) == placeholders(en[key]), f"{key} 占位符不一致"

    # 学段筛选与笔记资料版本键存在。
    for key in ("practice.grade", "practice.allGrades", "notes.sourceVersionsLine"):
        assert key in zh and key in en, f"缺少 locale 键：{key}"

    # 页面代码直接/间接引用的新键必须都存在（缺键会让界面回退显示键名）。
    used: set[str] = set()
    for path in CHANGED_JS:
        used.update(direct_t_keys(read(path)))
    used.update(INDIRECT_LOCALE_KEYS)
    prefixes = tuple(minimum) + ("practice.grade", "practice.allGrades", "notes.sourceVersionsLine")
    new_keys = sorted(key for key in used if key.startswith(prefixes))
    assert len(new_keys) >= 40, f"本轮新增命名空间使用过少：{new_keys}"
    assert not [key for key in new_keys if key not in zh], \
        f"zh-CN 缺少键：{[key for key in new_keys if key not in zh]}"
    assert not [key for key in new_keys if key not in en], \
        f"en-US 缺少键：{[key for key in new_keys if key not in en]}"

    # 中英文整体键集合一致（并行任务各自追加后仍应保持成对）。
    assert set(zh) == set(en), f"中英文键集合不一致：{sorted(set(zh) ^ set(en))[:10]}"


# ---------------------------------------------------------------------------
# 7. ESM 解析与 node --check
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="本机未安装 Node.js，跳过前端语法检查")
def test_frontend_esm_and_syntax():
    esm_check = (
        "const fs=require('fs');"
        "const vm=require('vm');"
        "let bad=false;"
        "for (const p of process.argv.slice(1)) {"
        "  try { new vm.SourceTextModule(fs.readFileSync(p,'utf8'),{identifier:p}); }"
        "  catch (e) { console.error(p+': '+e.message); bad=true; }"
        "}"
        "process.exit(bad?1:0);"
    )
    files = [str(path) for path in CHANGED_JS]
    completed = subprocess.run(
        [NODE, "--experimental-vm-modules", "-e", esm_check, *files],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, f"ESM 解析失败：\n{completed.stderr}"

    for path in CHANGED_JS:
        assert path.is_file(), path
        completed = subprocess.run(
            [NODE, "--check", str(path)],
            capture_output=True, text=True, timeout=90,
        )
        assert completed.returncode == 0, f"node --check 失败：{path.name}\n{completed.stderr}"
