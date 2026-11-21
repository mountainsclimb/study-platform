"""WS16 广场工作台前端静态契约（Phase 2 前端之二）。

无需浏览器：直接读源码断言三个页面模块、接口路径/请求体字段、locale 键、
样式文件与「练这道」深链。改动 JS 时同时做 node --check（有 Node 时）。

覆盖：
1. 三个 renderer 导出与统一签名 ``(container, version, params)``；
2. 投稿台接口路径与请求体（创建/送审/改版/撤回/事件链）；
3. 评审台接口路径与请求体（队列、独立评审、我的评审历史兜底）；
4. 编辑台接口路径与请求体（初审三项勾选、签发、发布历史撤回、身份审核/角色授权）;
5. 能力探测与降级：/plaza/me、capabilities.*、未就绪接口占位（不崩页）；
6. 我的题库 / 拍题入库：「练这道」= #/practice?qid=app:<id>，并保留 WS9/WS10 功能；
7. locale 中英文键齐全（三个命名空间 + mybank.submit.* + upload.intake.submit*）；
8. styles-plaza-workbench.css 存在且包含工作台类名；
9. 改动 JS 通过 node --check（无 Node 时跳过）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_JS = ROOT / "web/js/pages/submission.js"
REVIEW_JS = ROOT / "web/js/pages/review.js"
ADMIN_JS = ROOT / "web/js/pages/plazaAdmin.js"
MYBANK_JS = ROOT / "web/js/pages/mybank.js"
UPLOAD_JS = ROOT / "web/js/pages/upload.js"
ROUTER_JS = ROOT / "web/js/core/router.js"
WORKBENCH_CSS = ROOT / "web/styles-plaza-workbench.css"
ZH = ROOT / "web/locales/zh-CN.json"
EN = ROOT / "web/locales/en-US.json"

FRONTEND_FILES = (SUBMISSION_JS, REVIEW_JS, ADMIN_JS, MYBANK_JS, UPLOAD_JS)
NODE = shutil.which("node")


def _text(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


def _locale(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# 1. 三个页面模块与 renderer 导出
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (SUBMISSION_JS, "renderSubmission"),
        (REVIEW_JS, "renderReview"),
        (ADMIN_JS, "renderPlazaAdmin"),
    ],
)
def test_page_modules_export_renderers(path: Path, name: str) -> None:
    text = _text(path)
    # 统一签名：renderXxx(container, version, params)，params 为 URLSearchParams。
    pattern = re.compile(
        rf"export\s+async\s+function\s+{name}\s*\(\s*container\s*,\s*version\s*,\s*params"
    )
    assert pattern.search(text), f"{path.name} 应导出 {name}(container, version, params)"
    assert "params?.get?." in text, f"{path.name} 应通过 URLSearchParams 读取深链参数"
    # 页面根节点带稳定 data-page 标记，便于验收脚本定位与防回归。
    assert "data-page" in text


def test_page_modules_use_shared_dom_and_i18n() -> None:
    for path in (SUBMISSION_JS, REVIEW_JS, ADMIN_JS):
        text = _text(path)
        assert "from '../core/dom.js'" in text, path.name
        assert "from '../core/i18n.js'" in text, path.name
        assert "from '../core/api.js'" in text, path.name
        # 不直接拼 /api 前缀（core/api.js 已统一加前缀），避免双重 /api/api。
        assert "fetch(" not in text, f"{path.name} 不应绕过 core/api.js 的 api() 封装"


# ---------------------------------------------------------------------------
# 2. 投稿台：接口路径与请求体
# ---------------------------------------------------------------------------


def test_submission_page_api_contract() -> None:
    text = _text(SUBMISSION_JS)
    # 能力探测与列表/详情/事件链
    assert "api('/plaza/me')" in text
    assert "/plaza/submissions/mine?" in text
    assert "/plaza/submissions/${encodeURIComponent(id)}" in text
    assert "/events" in text
    # 创建投稿：question_id + source + license_confirmed（与 app/plaza/submissions.py 一致）
    assert "api('/plaza/submissions'" in text
    assert "question_id" in text
    assert "license_confirmed" in text
    # 送审：confirm_preview 必填 + version CAS
    assert "/submit" in text
    assert "confirm_preview: true" in text
    # 改版 / 撤回 / 草稿改声明
    assert "/revise" in text
    assert "/withdraw" in text
    assert "method: 'PATCH'" in text
    # 不可变快照字段与来源声明逐项字段
    for field in (
        "stem", "options", "answers", "explanation", "parts", "images",
        "url", "original_author", "license_basis", "rights_scope",
        "attribution", "reference", "page_no", "question_no",
    ):
        assert field in text, f"投稿台缺少快照/来源字段 {field}"
    # 阻断项与评审阈值展示
    assert "blocking" in text
    assert "threshold_met" in text
    assert "review_summary" in text
    # 无投稿能力时给出说明而不是报错
    assert "capabilities?.submit" in text
    assert "noPermission" in text


# ---------------------------------------------------------------------------
# 3. 评审台
# ---------------------------------------------------------------------------


def test_review_page_api_contract() -> None:
    text = _text(REVIEW_JS)
    assert "api('/plaza/me')" in text
    assert "capabilities?.review" in text
    # 按学科队列 + 独立评审 + 我的评审历史（优先服务端，未就绪时本地兜底）
    assert "/plaza/review/queue?" in text
    assert "/plaza/review/mine" in text
    assert "/reviews" in text
    # 四类结论与必填原因/独立作答
    for verdict in ("approve", "revise", "unsure", "reject"):
        assert f"'{verdict}'" in text, f"评审台缺少结论 {verdict}"
    assert "reasons" in text
    assert "independent_answer" in text
    assert "conflict_declared" in text
    assert "version:" in text
    # 提交前隐藏他人意见：展示后端 hidden 摘要，不在前端伪造投票
    assert "review_summary" in text
    assert "hidden" in text
    assert "提交你的独立评审前" in text or "plaza.review.detail.hidden" in text
    # 一人一版本一票提示 + 本地兜底记录
    assert "zhixu.plaza.reviewRecords" in text
    assert "my_review" in text


# ---------------------------------------------------------------------------
# 4. 编辑台
# ---------------------------------------------------------------------------


def test_admin_page_api_contract() -> None:
    text = _text(ADMIN_JS)
    assert "api('/plaza/me')" in text
    # 初审 / 签发 / 发布历史与撤回
    assert "/initial-review" in text
    assert "/publish" in text
    assert "/plaza/releases?" in text
    assert "/withdraw" in text
    assert "source_checked" in text
    assert "privacy_checked" in text
    assert "completeness_checked" in text
    assert "decision: 'accept'" not in text  # decision 来自表单值，不应硬编码
    assert "mode: 'normal'" not in text and "modeSelect.value" in text
    # 队列接口（WS14 新增）与未就绪降级占位
    assert "/plaza/admin/submissions" in text
    assert "notReady" in text
    assert "data-queue-not-ready" in text
    # 深链：?id=<投稿ID> 直接打开
    assert "params?.get?.('id')" in text
    assert "params?.get?.('tab')" in text
    # 身份审核（WS14 可能尚未就绪 → 占位）与角色授权
    assert "/plaza/admin/identity/queue" in text
    assert "/review" in text
    assert "decision" in text
    assert "/plaza/admin/roles" in text
    assert "action" in text
    for role in ("reviewer", "subject_reviewer", "editor"):
        assert role in text, f"角色授权缺少 {role}"
    # 阈值/阻断/回避提示
    assert "threshold_met" in text
    assert "blocking" in text
    assert "avoidHint" in text
    # 编辑能力探测
    assert "capabilities?.initial_review" in text
    assert "capabilities?.publish" in text


# ---------------------------------------------------------------------------
# 5. 我的题库 / 拍题入库：「练这道」app:<id> 深链 + WS9/WS10 功能保留
# ---------------------------------------------------------------------------


def test_mybank_practice_deeplink_and_submit_entry() -> None:
    text = _text(MYBANK_JS)
    # 统一题目引用：go('practice', `app:${question.id}`) → #/practice?qid=app:<id>
    assert "go('practice', `app:${question.id}`)" in text
    assert "data-ref" in text and "`app:${question.id}`" in text
    # 投稿入口：POST /plaza/submissions 后跳 #/submission?id=...
    assert "api('/plaza/submissions'" in text
    assert "go('submission', null, { id: submissionId })" in text
    assert "data-action': 'submit-to-plaza'" in text
    # 保留 WS10 的收录版本/来源/「有新版」功能
    assert "/source-status" in text
    assert "/versions" in text
    assert "has_new_version" in text
    assert "mybank.version.newVersionBadge" in text
    assert "sourceBadge" in text


def test_upload_practice_deeplink_and_submit_entry() -> None:
    text = _text(UPLOAD_JS)
    # 保存成功页「练这道」→ #/practice?qid=app:<id>
    assert "go('practice', `app:${personalId}`)" in text
    assert "data-action': 'practice-personal'" in text
    # 投稿入口：POST /plaza/submissions 后跳 #/submission?id=...
    assert "api('/plaza/submissions'" in text
    assert "go('submission', null, { id: submissionId })" in text
    assert "data-action': 'submit-to-plaza'" in text
    # 保留 WS9 的 OCR-first 主路径与保存/复习入口
    assert "/ocr-draft" in text
    assert "/save-to-bank" in text
    assert "intake.confirm" in text
    assert "intake.practiceThis" in text
    assert "intake.addReview" in text
    assert "intake.continueShoot" in text


def test_practice_deeplink_supported_by_router_and_practice_page() -> None:
    router = _text(ROUTER_JS)
    # go(route, qid) 会把 qid 写进 hash，因此 #/practice?qid=app:<id> 是真实可用的深链。
    assert "params.qid = qid" in router
    practice = _text(ROOT / "web/js/pages/practice.js")
    assert "urlParams?.get('qid')" in practice or "urlParams?.get?.('qid')" in practice
    assert "app:" in practice


# ---------------------------------------------------------------------------
# 6. locale：中英文键齐全
# ---------------------------------------------------------------------------


def test_locale_keys_present_in_both_languages() -> None:
    zh = _locale(ZH)
    en = _locale(EN)

    # 三个工作台命名空间 + 我的题库投稿 + 拍题入库投稿：两语言键集合一致。
    prefixes = (
        "plaza.submission.",
        "plaza.review.",
        "plaza.admin.",
        "mybank.submit.",
        "upload.intake.submit",
    )
    def scoped(table: dict) -> set[str]:
        return {
            key for key in table
            if key.startswith(prefixes) or key == "mybank.practiceThis"
        }

    zh_keys, en_keys = scoped(zh), scoped(en)
    assert len(zh_keys) >= 400, f"工作台 locale 键太少：{len(zh_keys)}"
    assert zh_keys == en_keys, (
        f"中英文键不一致：缺 en={sorted(zh_keys - en_keys)[:10]} "
        f"缺 zh={sorted(en_keys - zh_keys)[:10]}"
    )

    # 页面源码里所有 t('literal') 键必须都存在（缺键会回退中文/显示 key）。
    used: set[str] = set()
    for path in (SUBMISSION_JS, REVIEW_JS, ADMIN_JS):
        used |= set(re.findall(r"t\('([a-zA-Z0-9_.]+)'", _text(path)))
    assert used, "页面源码未发现 t(...) 调用"
    assert not (used - set(zh)), f"zh-CN 缺少键：{sorted(used - set(zh))}"
    assert not (used - set(en)), f"en-US 缺少键：{sorted(used - set(en))}"

    # 状态机 / 结论 / 校验项等模板键（运行期拼接）也必须在两个语言包里。
    template_keys: list[str] = []
    for code in (
        "draft", "pending_initial", "community_review", "verified",
        "disputed", "pending_release", "published", "withdrawn", "rejected",
    ):
        template_keys += [f"plaza.submission.status.{code}", f"plaza.admin.status.{code}"]
    for verdict in ("approve", "revise", "reject", "unsure"):
        template_keys += [f"plaza.submission.verdict.{verdict}", f"plaza.review.verdict.{verdict}"]
    for check in (
        "stem", "answer", "subject", "type", "images", "self_verified",
        "license_confirmed", "license_basis", "rights_scope", "attribution", "provenance",
    ):
        template_keys += [
            f"plaza.submission.check.{check}.label",
            f"plaza.submission.check.{check}.hint",
        ]
    for field in (
        "url", "original_author", "license_basis", "rights_scope",
        "attribution", "reference", "page_no", "question_no", "note",
    ):
        template_keys.append(f"plaza.submission.source.{field}")
    for transition in ("pending", "verified", "disputed"):
        template_keys.append(f"plaza.review.transition.{transition}")
    for action in (
        "created", "updated", "submitted", "revised", "author_withdrawn", "withdrawn",
        "initial_review_accepted", "initial_review_rejected", "review_submitted",
        "threshold_met", "disputed", "published", "release_withdrawn",
    ):
        template_keys.append(f"plaza.submission.events.action.{action}")
    for kind in ("teacher", "student_teacher", "student", "contributor"):
        template_keys.append(f"plaza.admin.identity.kind.{kind}")
    missing = [key for key in template_keys if key not in zh or key not in en]
    assert not missing, f"模板 locale 键缺失：{missing}"


# ---------------------------------------------------------------------------
# 7. 样式文件
# ---------------------------------------------------------------------------


def test_workbench_styles_appended() -> None:
    text = _text(WORKBENCH_CSS)
    assert ".plzw-page" in text
    for klass in (
        ".plzw-grid", ".plzw-list", ".plzw-item", ".plzw-checks",
        ".plzw-metrics", ".plzw-timeline", ".plzw-verdict",
        ".plzw-checkbox", ".plzw-not-ready", ".plzw-dialog",
    ):
        assert klass in text, f"工作台样式缺少 {klass}"
    # 移动端可用：至少有一个窄屏断点。
    assert "@media (max-width: 640px)" in text or "@media (max-width: 960px)" in text
    # 只新增 .plzw-* 类名，不重写其它页面的样式（文件不应出现全站选择器覆盖）。
    assert "plzw-page" in text
    assert ":root {" not in text


# ---------------------------------------------------------------------------
# 8. 前端语法检查（有 Node 时）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="本机未安装 Node.js，跳过前端语法检查")
def test_frontend_syntax() -> None:
    # `node --check` 对 .js 走 CommonJS 自动检测，无法发现 ESM 专属语法错误
    # （例如三元表达式漏写 `:` 时会被当成两种错误位置报出）。这里再加一次
    # vm.SourceTextModule 真 ESM 解析，确保改动页面能被浏览器 import。
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
        assert completed.returncode == 0, f"{path}\n{completed.stderr}"
