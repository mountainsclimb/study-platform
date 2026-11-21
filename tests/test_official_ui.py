"""WS20 平台精选来源筛选/徽标/收录入口（Phase 3 前端）静态契约测试。

覆盖：
1. 练习页：来源筛选（高考真题/平台精选/全部 → `/api/questions?source=bank|official|all`）、
   题目卡来源徽标与审核状态、分页/空态兼容、URL 深链恢复、后端未就绪时能力探测降级。
2. 题目详情：平台精选来源标签/public_id/精选版本、不适用动作降级（AI 讲解/管理员标注/
   报错占位/答案展开兜底）、高考题库题「收录到我的题库」（POST /paperlab/questions/collect，
   含幂等 already_collected 与已收录状态）。
3. 试卷工坊：组卷选题来源筛选（我的题库/高考题库/平台精选/全部）、选中项来源徽标、
   `official:<public_id>` 统一引用与建卷来源快照提示（paperCompose.js 最小改动）。
4. 首页：题库口径卡片的「平台精选」可练数量（接口未就绪时能力探测降级不显示）。
5. locale：practice.source.* / question.source.* / question.collect.* / paperlab.source.* /
   home.featured.* 中英文齐全、键集合对齐、占位符一致。
6. 改动的 ESM 文件真 ESM 解析（vm.SourceTextModule）+ node --check（无 Node 时跳过）。

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

PRACTICE_JS = WEB / "js" / "pages" / "practice.js"
QUESTION_JS = WEB / "js" / "pages" / "question.js"
PAPERLAB_JS = WEB / "js" / "pages" / "paperlab.js"
HOME_JS = WEB / "js" / "pages" / "home.js"
PAPER_COMPOSE_JS = WEB / "js" / "components" / "paperCompose.js"
LOCALES = WEB / "locales"

FRONTEND_FILES = [PRACTICE_JS, QUESTION_JS, PAPERLAB_JS, HOME_JS, PAPER_COMPOSE_JS]
NODE = shutil.which("node")

# 各页面/组件里通过常量映射间接引用的 locale 键（静态扫描 t('...') 覆盖不到）。
INDIRECT_LOCALE_KEYS = [
    "practice.source.app", "practice.source.reviewCommunity", "practice.source.reviewVerified",
    "question.source.reviewCommunity", "question.source.reviewVerified",
    "paperlab.source.reviewCommunity", "paperlab.source.reviewVerified",
]


def read(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def compact(source: str) -> str:
    """去掉多余空白便于跨行断言（不改字符串字面量内容）。"""
    return re.sub(r"\s+", " ", source)


def direct_t_keys(source: str) -> set[str]:
    return set(re.findall(r"\bt\('([^']+)'", source))


# ---------------------------------------------------------------------------
# 1. 练习页：来源筛选 + 徽标 + 分页/空态 + 能力探测
# ---------------------------------------------------------------------------

def test_practice_source_filter_contract():
    source = read(PRACTICE_JS)
    text = compact(source)
    # 三个来源选项：默认高考真题（省略参数，后端默认 bank）、平台精选、全部来源。
    for token in (
        "practice.source.label",
        "practice.source.bank",
        "practice.source.official",
        "practice.source.all",
        "sourceSel",
    ):
        assert token in source, f"练习页来源筛选缺少：{token}"
    assert "{ value: 'official', label: t('practice.source.official') }" in text
    assert "{ value: 'all', label: t('practice.source.all') }" in text
    # 来源进入 URL 深链恢复与哈希参数（刷新/分享保持筛选）。
    assert "'include_ai_difficulty', 'source'" in text
    # 筛选值写回统一 state.filters，由 loadSearch 的 query({...state.filters}) 带进 /api/questions。
    assert "state.filters.source = officialSourceReady ? sourceSel.value : ''" in text
    assert "api(`/questions?${query({" in text
    # 高级筛选计数与清除芯片都识别 source。
    assert "'status', 'source', 'year_from'" in text
    assert "source: t('practice.source.label')," in text
    assert "else if (key === 'source') text =" in text


def test_practice_source_capability_probe_and_degrade():
    source = read(PRACTICE_JS)
    text = compact(source)
    # 能力探测走合法 official 请求（200 + official_total）：旧后端忽略参数、响应无
    # official_total → 未就绪；不用非法 source 触发 4xx 控制台错误。
    assert "api('/questions?page=1&page_size=1&source=official')" in source
    assert "official_total" in source
    assert "OFFICIAL_SOURCE_PROBE" not in source
    assert "export function detectOfficialSource()" in source
    assert "detectOfficialSource()," in text
    # 未就绪：不渲染来源控件、丢弃 URL/本地状态里的 source，页面其余功能照常。
    assert "officialSourceReady ? field(t('practice.source.label'), sourceSel) : null" in text
    assert "if (!officialSourceReady && state.filters.source)" in text
    assert "delete state.filters.source;" in text


def test_practice_source_badges_and_empty_state():
    source = read(PRACTICE_JS)
    text = compact(source)
    # 来源徽标（高考题库/平台精选/我的题库）与平台精选题的审核状态。
    assert "const SOURCE_BADGE_KEYS = {" in source
    for key in ("practice.source.bank", "practice.source.official", "practice.source.app"):
        assert key in source, f"来源徽标缺少键：{key}"
    for key in ("practice.source.reviewCommunity", "practice.source.reviewVerified"):
        assert key in source, f"审核状态徽标缺少键：{key}"
    assert "badges.append(sourceBadge(q));" in text
    assert "const reviewBadge = reviewStatusBadge(q);" in text
    # 精简 payload 只有 official:<id> 引用时按 qid 前缀兜底（不能误标成高考题库）。
    assert "String(q?.qid || '').startsWith('official:')" in text
    # 分页沿用原 total/page_size；平台精选空态有专门文案，并提示 official 筛选适用范围。
    assert "const pages = Math.max(1, Math.ceil((results.total || 0) / state.pageSize));" in text
    assert "practice.source.emptyOfficial" in source
    assert "practice.source.emptyOfficialHint" in source
    assert "practice.source.officialFilterNotice" in source
    # 练习作答链路未改：结果卡打开题目后仍是同一套提交/揭晓流程。
    assert "await openQuestion(item.qid, detailContainer" in text
    assert "onSubmitted: () => loadSearch({ keepSelection: true })" in text


# ---------------------------------------------------------------------------
# 2. 题目详情：平台精选来源 + 收录入口
# ---------------------------------------------------------------------------

def test_question_official_source_labels_and_degradation():
    source = read(QUESTION_JS)
    text = compact(source)
    # 来源标签 + review_status 徽标 + public_id/精选版本（WS19 official 读取层字段）。
    assert "function isOfficialQuestion(q)" in source
    assert "const isOfficial = isOfficialQuestion(q);" in text
    for token in (
        "question.source.official", "question.source.bank",
        "question.source.publicId", "question.source.featuredVersion",
        "question.source.reviewCommunity", "question.source.reviewVerified",
    ):
        assert token in source, f"题目详情缺少来源/审核文案：{token}"
    # featured 已发布时来源徽标即「平台精选」，不再重复；其余状态用后端标签兜底。
    assert "status === 'featured' || status === 'published'" in compact(source)
    assert "q?.review_status_label" in compact(source)
    # 后端可见性提示（撤回/暂停展示）原样展示。
    assert "String(q.notice || '').trim()" in compact(source)
    # 平台精选题不再叠加题库侧「原卷参考答案待核验」提示。
    assert "if (quality && !isOfficial) container.append(quality);" in compact(source)
    assert "badge(t('question.source.official'), 'blue')" in text
    assert "isOfficial ? officialReviewBadge(q) : null" in text
    # 不适用动作降级：AI 讲解、管理员标注隐藏；报错入口保留为占位提示；
    # /reveal 未接 official 时在答案区给一次降级提示而不是整页报错。
    assert "if (!isApp && !isOfficial) container.append(aiSection);" in text
    assert "state.user.role === 'admin' && !isApp && !isOfficial" in text
    assert "question.source.reportPlaceholder" in source
    assert "question.source.officialNotice" in source
    assert "official-reveal-notice" in source
    assert "question.source.revealUnavailable" in source
    # official 题不再请求只读题库专属的讲解库（避免每道题都弹加载失败）。
    assert "if (!String(qid).startsWith('official:'))" in text


def test_question_collect_to_my_bank_contract():
    source = read(QUESTION_JS)
    text = compact(source)
    # 收录调用：高考题库题专用，POST /api/paperlab/questions/collect {qid}。
    assert "'/paperlab/questions/collect'" in source
    assert "method: 'POST', data: { qid: q.qid }" in text
    assert "!isApp && !isOfficial ? collectToMyBankButton(q) : null" in text
    # 成功/已收录提示、已收录状态与「去我的题库」入口。
    for key in (
        "question.collect.save", "question.collect.collected", "question.collect.goMyBank",
        "question.collect.success", "question.collect.already", "question.collect.failed",
        "question.collect.hint", "question.collect.unavailable",
    ):
        assert key in source, f"收录入口缺少文案：{key}"
    assert "already_collected" in source
    assert "function applyCollectedState(node)" in source
    assert "go('mybank')" in text
    # 已收录本地缓存按 bank:<qid> 统一引用存储；跨会话点击时由服务端幂等纠正。
    assert "COLLECTED_STORAGE_KEY" in source
    assert "`bank:${value}`" in text
    # 收录入口不影响作答：详情页仍按 q.qid 提交到 /attempts（official/bank 一致）。
    assert "data.set('qid', q.qid);" in text
    assert "api('/attempts', { method: 'POST', body: data })" in text


# ---------------------------------------------------------------------------
# 3. 试卷工坊：组卷来源筛选（paperlab.js 能力探测 + paperCompose.js 最小改动）
# ---------------------------------------------------------------------------

def test_paperlab_page_probes_official_sources_and_wires_compose():
    source = read(PAPERLAB_JS)
    text = compact(source)
    # 合法 official 请求探测（200 + official_total），不再用非法 source 触发 4xx。
    assert "api('/questions?page=1&page_size=1&source=official')" in source
    assert "official_total" in source
    assert "OFFICIAL_SOURCE_PROBE" not in source
    assert "function detectOfficialSource()" in source
    # 仅在组卷页签探测一次，并把布尔能力传给工坊组件（不改变其它页签行为）。
    assert "const officialSourcesSupported = await detectOfficialSource();" in text
    assert "officialSourcesSupported," in text
    assert "await renderComposeWorkbench({" in text


def test_paper_compose_source_filter_and_snapshot():
    source = read(PAPER_COMPOSE_JS)
    text = compact(source)
    # 四个来源页签：我的题库 / 高考题库 / 平台精选 / 全部；后两者按能力探测显示。
    assert "paperlab.source.label" in source
    for key in ("paperlab.source.app", "paperlab.source.bank", "paperlab.source.official", "paperlab.source.all"):
        assert key in source, f"组卷来源页签缺少：{key}"
    assert "value: 'official'" in text and "value: 'all'" in text
    assert "tab.hidden = true;" in text
    assert "applyOfficialSources(officialSourcesSupported)" in text
    # 平台精选/全部来源通过 /api/questions?source= 检索；高考题库仍走默认（省略 source）。
    assert "values.source = pickerSource" in text
    assert "path: `/questions?${params}`" in text
    # 选中题目来源徽标 + 审核状态，试题篮条目按来源显示徽标。
    assert "pickerSourceBadge(card.source)" in text
    assert "pickerSourceBadge(item.source)" in text
    assert "pickerReviewBadge" in source
    for key in ("paperlab.source.reviewCommunity", "paperlab.source.reviewVerified"):
        assert key in source, f"组卷审核状态徽标缺少：{key}"
    # 平台精选题用统一题目引用 official:<public_id> 入篮，建卷由服务端固定来源快照。
    assert "function officialCardToBasket(item)" in source
    assert "ref: `official:${publicId}`" in text
    assert "String(item.qid || '').startsWith('official:')" in text
    # 平台精选卡的知识点是 knowledge_tags 字符串数组，转换时兼容两种来源。
    assert "Array.isArray(item.knowledge_points) && item.knowledge_points.length" in text
    assert "paperlab.source.publicId" in source
    assert "paperlab.source.snapshotHint" in source
    assert "paperlab.source.pickerHelp" in source
    # 建卷 payload 仍只传 ref/score/section（不新增来源字段，保持现有组卷流程）。
    payload = source.split("const payload = {", 1)[1].split("};", 1)[0]
    for field in ("ref: item.ref", "score:", "section:"):
        assert field in payload, f"建卷 payload 字段缺失：{field}"
    assert "source:" not in payload, "建卷 payload 不应新增 source 字段（来源由 ref 前缀表达）"


# ---------------------------------------------------------------------------
# 4. 首页：平台精选可练数量（能力探测降级）
# ---------------------------------------------------------------------------

def test_home_featured_count_capability_and_degrade():
    source = read(HOME_JS)
    text = compact(source)
    assert "function detectFeaturedPractice()" in source
    # 合法 official 请求探测（200 + official_total）；旧后端忽略参数、无该字段 → 降级。
    assert "api('/questions?page=1&page_size=1&source=official')" in source
    assert "official_total" in source
    assert "OFFICIAL_SOURCE_PROBE" not in source
    assert "'official_total' in data" in text
    assert "return null;" in source
    # 口径卡片：有计数才追加「平台精选可练」并给直达平台精选练习的入口。
    assert "function bankCaliberCard(bank, featuredPractice = null)" in text
    assert "home.featured.practiceLabel" in source
    assert "home.featured.value" in source
    assert "home.featured.goPractice" in source
    assert "go('practice', null, { source: 'official' })" in text
    assert "detectFeaturedPractice()," in text
    assert "bankCaliberCard(bank, featuredPractice)" in text


# ---------------------------------------------------------------------------
# 5. locale：新命名空间中英文齐全且对齐
# ---------------------------------------------------------------------------

def load_locale(name: str) -> dict:
    path = LOCALES / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
    return data


def placeholders(value: str) -> list[str]:
    return sorted(re.findall(r"\{([A-Za-z0-9_]+)\}", str(value)))


def test_locale_official_namespaces_complete_and_aligned():
    zh = load_locale("zh-CN")
    en = load_locale("en-US")
    minimum = {
        "practice.source.": 10,
        "question.source.": 10,
        "question.collect.": 8,
        "paperlab.source.": 10,
        "home.featured.": 3,
    }
    for prefix, expected in minimum.items():
        zh_keys = {key for key in zh if key.startswith(prefix)}
        en_keys = {key for key in en if key.startswith(prefix)}
        assert len(zh_keys) >= expected, f"{prefix} 中文键不足：{sorted(zh_keys)}"
        assert zh_keys == en_keys, f"{prefix} 中英文键不一致：{sorted(zh_keys ^ en_keys)[:10]}"
        for key in sorted(zh_keys):
            assert zh[key].strip() and en[key].strip(), f"{key} 中英文文案为空"
            assert placeholders(zh[key]) == placeholders(en[key]), f"{key} 占位符不一致"

    # 页面代码直接/间接引用的新键必须都存在（缺键会让界面回退显示键名）。
    used: set[str] = set()
    for path in FRONTEND_FILES:
        used.update(direct_t_keys(read(path)))
    used.update(INDIRECT_LOCALE_KEYS)
    prefixes = tuple(minimum)
    new_keys = sorted(key for key in used if key.startswith(prefixes))
    assert len(new_keys) >= 30, f"新增命名空间使用过少：{new_keys}"
    missing_zh = [key for key in new_keys if key not in zh]
    missing_en = [key for key in new_keys if key not in en]
    assert not missing_zh, f"zh-CN 缺少键：{missing_zh}"
    assert not missing_en, f"en-US 缺少键：{missing_en}"


# ---------------------------------------------------------------------------
# 6. ESM 解析与 node --check
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="本机未安装 Node.js，跳过前端语法检查")
def test_frontend_esm_and_syntax():
    # `node --check` 对 .js 走 CommonJS 自动检测；这里再用 vm.SourceTextModule 做一次
    # 真 ESM 解析，确保改动页面能被浏览器 import。
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
    files = [str(path) for path in FRONTEND_FILES]
    completed = subprocess.run(
        [NODE, "--experimental-vm-modules", "-e", esm_check, *files],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, f"ESM 解析失败：\n{completed.stderr}"

    for path in FRONTEND_FILES:
        assert path.is_file(), path
        completed = subprocess.run(
            [NODE, "--check", str(path)],
            capture_output=True, text=True, timeout=90,
        )
        assert completed.returncode == 0, f"node --check 失败：{path.name}\n{completed.stderr}"
