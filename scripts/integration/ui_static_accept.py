"""WS30 前端静态验收（任务深链 / 精选配图与纠错 / 学段筛选 / 笔记-Anki 版本 / locale 对齐）。

本脚本**不启动服务、不联网、不读数据库**，只做「可脚本化、可重复」的前端静态契约断言，
作为 WS30 第四轮端到端验收的 UI 维度补充（浏览器冒烟见 ``plaza_accept.py`` /
``classroom_accept.py`` / ``modules_accept.py``）：

1. **语法**：``node --check`` 遍历 ``web/js/**/*.js``（全量 ESM 页面/组件），并用
   ``vm.SourceTextModule`` 做一次真 ESM 解析（Node 不可用时记录 warning，不静默跳过）。
2. **任务深链格式**：``tasks.js`` 的任务结果路由与各页面读取的参数保持一致：课堂
   ``#/classroom?course=``、笔记 ``#/notes?note=``、资料 OCR ``#/notes?document=``、
   试卷导入/工坊 ``#/paperlab?import=|tab=papers&paper=``、我的题库 ``#/mybank?edit=``、
   拍题草稿 ``#/upload?upload=``；各页面读取 ``params.get(...)`` 的参数名与路由一致。
3. **平台精选配图与纠错接口**：``question.js`` 只渲染 ``accessible && url`` 的公开图
   （``images[]``/``figure_urls`` 兼容、``figure_notice`` 降级），纠错走
   ``POST /api/plaza/questions/{public_id}/issues``（登录可用、未登录引导），精选编号
   可跳转广场详情；配图/纠错接口路径与后端公开字段同口径。
4. **学段筛选**：``practice.js`` 能力探测 ``/api/questions?grade=``、``catalog.grade_stages``
   选项、URL 状态恢复/重置/清除芯片、``grade_filter.applied=false`` 降级提示。
5. **Anki 题链接版本字段**：``app/anki_export.py`` 保留统一题目引用（bank:/app:/official:）
   与「来源版本」脚注/标签，且不改变 ``.apkg`` 模型字段（Front/Back/Tags）。
6. **locale 中英文对齐**：两语言键集合一致、占位符一致；前端字面量 ``t('...')`` 键
   必须都存在；WS30 相关命名空间（tasks/question.source/practice.grade/insight.official/
   home.official/notes.questionRef/admin.monitor/plaza.ops/quality/notify/metrics）键量与非空校验。

产出：``screenshots/integration/ui-static-accept-report.json``；退出码 0/1。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
JS = WEB / "js"
LOCALES = WEB / "locales"
DEFAULT_OUT = ROOT / "screenshots" / "integration"
DEFAULT_REPORT = DEFAULT_OUT / "ui-static-accept-report.json"

TASKS_JS = JS / "pages" / "tasks.js"
QUESTION_JS = JS / "pages" / "question.js"
PRACTICE_JS = JS / "pages" / "practice.js"
INSIGHT_JS = JS / "pages" / "insight.js"
HOME_JS = JS / "pages" / "home.js"
NOTES_JS = JS / "pages" / "notes.js"
MINDMAP_JS = JS / "components" / "mindmap.js"
CLASSROOM_JS = JS / "pages" / "classroom.js"
PAPERLAB_JS = JS / "pages" / "paperlab.js"
MYBANK_JS = JS / "pages" / "mybank.js"
UPLOAD_JS = JS / "pages" / "upload.js"
ADMIN_JS = JS / "pages" / "admin.js"
PLAZA_JS = JS / "pages" / "plaza.js"
PLAZA_ADMIN_JS = JS / "pages" / "plazaAdmin.js"
NOTIFY_JS = JS / "components" / "plazaNotifications.js"
# 第五轮（方案 §5.1/§5.2/§5.4）：公式输入、四档评分、原理演示与离线示例。
WRONGBOOK_JS = JS / "pages" / "wrongbook.js"
FORMULA_INPUT_JS = JS / "components" / "formulaInput.js"
REVIEW_RATING_JS = JS / "components" / "reviewRating.js"
PRINCIPLE_DEMO_JS = JS / "components" / "principleDemo.js"
PRINCIPLE_DIR = WEB / "vendor" / "principles"
PRINCIPLE_FLOW_JS = PRINCIPLE_DIR / "flow.js"
PRINCIPLE_ISO_JS = PRINCIPLE_DIR / "iso.js"
PRINCIPLE_THREE_JS = PRINCIPLE_DIR / "threeLite.js"
PRINCIPLE_PRESETS_JS = PRINCIPLE_DIR / "presets.js"
ROUND5_STYLES = ("styles-formula.css", "styles-review.css", "styles-principles.css")
# M3（§5.2/§5.3/§5.4）：函数图 / 白板工具、资料定位与掌握路径页面。
M3_COMPONENTS = (JS / "components" / "functionGraph.js", JS / "components" / "sketchBoard.js")
M3_STYLES = ("styles-study-tools.css", "styles-m3-pages.css")
# M4（§4/§5.4）：课堂版本化演示场景、播放器渲染、模板与预设扩展。
CLASSROOM_PLAYER_JS = JS / "components" / "classroomPlayer.js"
CLASSROOM_PAGE_JS = JS / "pages" / "classroom.js"
PRESETS_JS = WEB / "vendor" / "principles" / "presets.js"
FLOW_JSON_JS = WEB / "vendor" / "principles" / "flow.js"
ISO_JS = WEB / "vendor" / "principles" / "iso.js"
CLASSROOM_CSS = WEB / "styles-classroom.css"
M4_PRESET_IDS = (
    "flow-review-loop", "flow-function-timeline", "flow-photosynthesis-states",
    "iso-anatomy-gearbox", "iso-biology-stomata",
)
# M5（§5.4）：机械 3D 与工厂仿真演示组件。
MECH_DEMO_JS = JS / "components" / "mechDemo.js"
MECH_PRESETS_JS = WEB / "vendor" / "principles" / "mechPresets.js"
MECH_CSS = WEB / "styles-mech.css"
# M6（§5.3）：专题报告与互动讲义。
M6_COMPONENTS = (JS / "components" / "learningDocs.js",)
M6_CSS = WEB / "styles-m6.css"
# M7（§5.3/§5.4）：课堂离线交互演示播放器与报告转笔记。
OFFLINE_DEMO_BUNDLE = WEB / "vendor" / "offline" / "offline-demo.js"
OFFLINE_DEMO_BUILD = ROOT / "scripts" / "build_offline_demo_bundle.py"
INDEX_HTML = WEB / "index.html"
ANKI_PY = ROOT / "app" / "anki_export.py"
APP_PRACTICE_PY = ROOT / "app" / "practice.py"
APP_OFFICIAL_PY = ROOT / "app" / "official.py"

#: locale 键必须存在且成对：WS30 涉及的命名空间（前缀, 最少键数）。
LOCALE_NAMESPACES = (
    ("tasks.", 8),
    ("question.source.", 30),
    ("practice.grade", 1),
    ("practice.allGrades", 1),
    ("insight.official.", 4),
    ("home.official.", 5),
    ("notes.questionRef", 7),
    ("admin.monitor.", 50),
    ("plaza.ops.", 10),
    ("plaza.quality.", 10),
    ("plaza.notify.", 5),
    ("plaza.metrics.", 8),
    # 第五轮新增命名空间：公式校对、四档复习评分、原理演示（键量下限按本轮交付）。
    ("study.formula.", 70),
    ("study.review.", 30),
    ("study.demo.", 40),
    # M3 命名空间：题目/错题学习工具与资料定位/掌握路径（键量下限按本轮交付）。
    ("study.tools.", 100),
    ("study.m3.", 90),
    # M4 命名空间：课堂版本化演示与学习附件（键量下限按本轮交付）。
    ("study.m4.", 60),
    # M5 命名空间：机械/工厂演示（键量下限按本轮交付）。
    ("study.m5.", 20),
    # M6 命名空间：专题报告与互动讲义（键量下限按本轮交付）。
    ("study.m6.", 100),
    # M7 命名空间：离线交互演示与报告转笔记（键量下限按本轮交付）。
    ("study.m7.", 2),
    # 开源组件整合：MathLive 公式编辑、3Dmol 分子模型、物理沙盒、几何演示等
    #（批次二/五，键量下限按本轮交付）。
    ("study.oss.", 109),
)

#: 直接 t('literal') 键扫描的前端文件（WS30 相关页面/组件；其余页面键由既有 pytest 覆盖）。
LOCALE_SCAN_FILES = (
    TASKS_JS, QUESTION_JS, PRACTICE_JS, INSIGHT_JS, HOME_JS, NOTES_JS, CLASSROOM_JS,
    PAPERLAB_JS, MYBANK_JS, UPLOAD_JS, ADMIN_JS, PLAZA_JS, PLAZA_ADMIN_JS, NOTIFY_JS,
    # 第五轮接线页面/组件（t('...') 字面量必须都能在两种语言中找到）。
    WRONGBOOK_JS, REVIEW_RATING_JS, PRINCIPLE_DEMO_JS,
    # 批次五 §6.4：交互脑图的错误降级文案。
    MINDMAP_JS,
)

#: 通过常量映射/模板字符串间接引用的 locale 键（静态扫描 t('...') 覆盖不到）。
INDIRECT_LOCALE_KEYS = (
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
    "practice.source.app",
    "practice.source.reviewCommunity",
    "practice.source.reviewVerified",
    "question.source.reviewCommunity",
    "question.source.reviewVerified",
    "paperlab.source.reviewCommunity",
    "paperlab.source.reviewVerified",
    "admin.monitor.group.tasks",
    "admin.monitor.group.ai",
    "admin.monitor.group.media",
    "admin.monitor.group.data",
    "admin.monitor.group.plaza",
    "admin.monitor.status.ok",
    "admin.monitor.status.warn",
    "admin.monitor.status.error",
    # 第五轮：原理演示在常量映射里按 labelKey 间接引用（静态 t('...') 扫描不到）。
    "study.demo.modeFlow",
    "study.demo.modeIso",
    "study.demo.modeThree",
)


class StaticFailure(RuntimeError):
    """单步静态断言失败（记录后继续收集后续步骤）。"""


class UiStaticAccept:
    def __init__(self, out_dir: Path, report_path: Path, run_id: str):
        self.out = out_dir
        self.report_path = report_path
        self.run_id = run_id
        self.report: dict = {
            "ok": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "kind": "static-frontend-contract",
            "steps": [],
            "warnings": [],
            "problems": [],
            "data": {},
            "exit_code": 1,
        }
        self._started = 0.0

    # -- 基础工具 ---------------------------------------------------------
    def warning(self, message: str) -> None:
        if message not in self.report["warnings"]:
            self.report["warnings"].append(message)

    @staticmethod
    def _read(path: Path) -> str:
        if not path.is_file():
            raise StaticFailure(f"缺少文件：{path.relative_to(ROOT) if path.is_absolute() else path}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(source: str) -> str:
        """去掉多余空白便于跨行断言（不改字符串字面量内容）。"""
        return re.sub(r"\s+", " ", source)

    @staticmethod
    def _body_between(source: str, start: str, end: str) -> str:
        begin = source.find(start)
        if begin < 0:
            raise StaticFailure(f"未找到代码块起点：{start}")
        finish = source.find(end, begin + len(start))
        if finish <= begin:
            raise StaticFailure(f"未找到代码块终点：{end}")
        return source[begin:finish]

    def step(self, name: str, checks: list, *, started: float | None = None,
             data: dict | None = None) -> None:
        started = started if started is not None else self._started
        elapsed = round((time.time() - started) * 1000, 1) if started else 0.0
        normalized, failures = [], []
        for item in checks:
            label, ok = str(item[0]), bool(item[1])
            detail = str(item[2]) if len(item) > 2 and item[2] else ""
            normalized.append({"label": label, "ok": ok, "detail": "" if ok else detail})
            if not ok:
                failures.append((label, detail))
        entry = {
            "name": name,
            "ok": not failures,
            "status": "passed" if not failures else "failed",
            "elapsed_ms": elapsed,
            "checks": normalized,
        }
        if data:
            entry["data"] = data
        self.report["steps"].append(entry)
        if failures:
            detail = "；".join(f"{label}（{extra}）" if extra else label
                              for label, extra in failures)
            self.report["problems"].append(f"{name}: {detail}")
            raise StaticFailure(f"{name}: {detail}")

    # -- 1. 语法：node --check 全量 + ESM 真解析 --------------------------
    def check_syntax(self) -> None:
        started = time.time()
        node = shutil.which("node")
        js_files = sorted(path for path in JS.rglob("*.js") if path.is_file())
        if not js_files:
            self.step("前端语法检查（node --check web/js）",
                      [("web/js 下存在 JavaScript 文件", False, "未找到任何 .js")],
                      started=started)
            return
        if node is None:
            self.warning("本机未安装 Node.js：node --check 与 ESM 解析已跳过（记录为 warning，不静默）")
            self.step("前端语法检查（node --check web/js）",
                      [("Node.js 可用（node --check 需要）", False,
                        "未找到 node：请安装 Node.js 后重跑本脚本")],
                      started=started, data={"files": len(js_files), "node_missing": True})
            return
        version = subprocess.run([node, "--version"], capture_output=True, text=True,
                                 timeout=30).stdout.strip()
        failed: list[dict] = []
        for path in js_files:
            try:
                completed = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True, text=True, timeout=90,
                )
            except subprocess.TimeoutExpired:
                failed.append({"file": str(path.relative_to(ROOT)), "error": "node --check 超时"})
                continue
            if completed.returncode != 0:
                failed.append({
                    "file": str(path.relative_to(ROOT)),
                    "error": (completed.stderr or completed.stdout or "").strip()[:600],
                })
        esm_check = (
            "const fs=require('fs');const vm=require('vm');let bad=false;"
            "for (const p of process.argv.slice(2)) {"
            "  try { new vm.SourceTextModule(fs.readFileSync(p,'utf8'),{identifier:p}); }"
            "  catch (e) { console.error(p+': '+e.message); bad=true; }"
            "}"
            "process.exit(bad?1:0);"
        )
        esm_error = ""
        try:
            completed = subprocess.run(
                [node, "--experimental-vm-modules", "-e", esm_check, *[str(p) for p in js_files]],
                capture_output=True, text=True, timeout=180,
            )
            if completed.returncode != 0:
                esm_error = (completed.stderr or completed.stdout or "").strip()[:2000]
        except subprocess.TimeoutExpired:
            esm_error = "ESM 解析超时"
        self.step(
            "前端语法检查（node --check web/js + vm.SourceTextModule ESM 解析）",
            [
                (f"node --check 全部通过（{len(js_files)} 个文件）", not failed,
                 json.dumps(failed[:5], ensure_ascii=False)),
                ("ESM 语法解析通过（vm.SourceTextModule）", not esm_error, esm_error[:600]),
            ],
            started=started,
            data={"node": version, "files": len(js_files), "failed": failed[:10]},
        )

    # -- 2. 任务深链格式 ---------------------------------------------------
    def check_task_deep_links(self) -> None:
        started = time.time()
        source = self._read(TASKS_JS)
        text = self._compact(source)
        route_body = self._compact(self._body_between(
            source, "function resultRoute(job) {", "function openResult(job) {"))
        doc_links = (
            "#/paperlab?import=",
            "#/paperlab?tab=papers&paper=",
            "#/classroom?course=",
            "#/notes?note=",
            "#/notes?document=",
            "#/mybank?edit=",
            "#/upload?upload=",
        )
        checks = [("tasks.js 深链约定文档化（7 个页面入口参数）", all(
            token in source for token in doc_links),
            f"missing={[token for token in doc_links if token not in source]}")]
        route_expectations = (
            ("case 'paper-import':", "params: importId ? { import: importId } : null"),
            ("case 'paper-enrich':", "{ route: 'mybank', params: questionId ? { edit: questionId } : null }"),
            ("case 'paper-compose':", "{ route: 'paperlab', params: { tab: 'papers', paper: paperId } }"),
            ("case 'paper-revision':", "{ route: 'paperlab', params: { tab: 'papers', paper: paperId } }"),
            ("case 'course':", "params: { course: courseId }"),
            ("case 'notes':", "params: noteId ? { note: noteId } : null"),
            ("case 'ocr':", "params: { document: documentId }"),
            ("case 'recognize':", "params: { upload: uploadId }"),
        )
        for case, expected in route_expectations:
            matched = case in route_body and expected in route_body
            checks.append((f"tasks.js 结果路由 {case.strip('case :')} → {expected[:48]}",
                           matched, "" if matched else f"route_body 未命中 {case}"))
        checks.extend([
            ("tasks.js 通过 go(route, qid, params) 统一跳转",
             "go(target.route, target.qid || null, target.params || null)" in text,
             "未找到统一 go(...) 跳转"),
            ("课堂页读取 params.get('course')",
             "params.get('course')" in self._read(CLASSROOM_JS),
             "classroom.js 未读取 course 参数"),
            ("笔记页读取 params.get('note') 与 params.get('document')",
             "params?.get?.('note')" in self._read(NOTES_JS)
             and "params?.get?.('document')" in self._read(NOTES_JS),
             "notes.js 深链参数缺失"),
            ("试卷工坊读取 import / paper 深链参数",
             "params?.get?.('import')" in self._read(PAPERLAB_JS)
             and "params?.get?.('paper')" in self._read(PAPERLAB_JS),
             "paperlab.js 深链参数缺失"),
            ("我的题库读取 edit 深链参数",
             "params?.get?.('edit')" in self._read(MYBANK_JS),
             "mybank.js 未读取 edit 参数"),
            ("拍题页读取 upload 深链参数",
             "params?.get?.('upload')" in self._read(UPLOAD_JS),
             "upload.js 未读取 upload 参数"),
        ])
        self.step("任务中心深链格式与页面参数一致（课堂/笔记/工坊/我的题库/拍题）",
                  checks, started=started, data={"routes": [item[0] for item in route_expectations]})

    # -- 3. 平台精选配图与纠错接口 ----------------------------------------
    def check_official_assets_and_issues(self) -> None:
        started = time.time()
        question = self._read(QUESTION_JS)
        text = self._compact(question)
        checks = [
            ("精选公开配图：images[] 契约与 accessible=false 过滤",
             "function officialFigures(q)" in question
             and "entry.accessible === false" in question
             and "for (const image of Array.isArray(q?.images) ? q.images : []) push(image, 'figure');" in text
             and "figure_urls" in question,
             "images[]/accessible 契约缺失"),
            ("精选公开配图：只渲染站内相对路径或 http(s)，加载失败降级",
             "function isRenderableImageUrl(url)" in question
             and "question.source.figureLoadFailed" in question
             and "question.source.figureUnavailable" in question
             and "officialFigureNode(entry, index, q)" in text,
             "配图 URL 校验/降级缺失"),
            ("精选题干 [图N]/[公式N] 占位映射公开图（不拼接私有路径）",
             "[图N]/[公式N]" in question
             and "officialTextNode(" in question
             and "officialStemNode(q, officialFigures(q))" in text,
             "配图占位映射缺失"),
            ("精选纠错走 POST /api/plaza/questions/{public_id}/issues",
             "/plaza/questions/${encodeURIComponent(publicId)}/issues" in question
             and "method: 'POST'" in text
             and "data: { field: fieldSel.value, description: text }" in text,
             "精选纠错接口未接线"),
            ("精选纠错字段白名单与后端 db.ISSUE_FIELDS 同口径（7 类）",
             "const OFFICIAL_ISSUE_FIELDS = ['stem', 'options', 'answer', 'explanation', 'figure', 'source', 'other'];" in text,
             "纠错字段白名单缺失"),
            ("未登录点击纠错引导登录、成功后 toast",
             "question.source.issueLoginRequired" in question
             and "go('home');" in text
             and "question.source.issueSubmitted" in question,
             "纠错登录引导/成功提示缺失"),
            ("精选编号可跳转广场公开详情（#/plaza?id=public_id）",
             "data-official-plaza-link" in question
             and "go('plaza', null, { id: officialPublicId })" in text,
             "精选编号广场链接缺失"),
            ("精选不叠加题库专属讲解/管理标注动作",
             "if (!isApp && !isOfficial) container.append(aiSection);" in text
             and "state.user.role === 'admin' && !isApp && !isOfficial" in text,
             "精选动作降级缺失"),
        ]
        # 后端公开字段与接口路径静态交叉校验（app/official.py、app/plaza/public.py 口径）。
        official_py = self._read(APP_OFFICIAL_PY)
        checks.append((
            "后端 official 详情投影含配图/来源声明/版本字段",
            "figure_notice" in official_py and "source_declaration" in official_py
            and "published_version" in official_py,
            "app/official.py 缺少 figure_notice/source_declaration/published_version"))
        self.step("平台精选公开配图与纠错接口（question.js ↔ 后端字段）",
                  checks, started=started)

    # -- 4. 学段筛选 -------------------------------------------------------
    def check_grade_filter(self) -> None:
        started = time.time()
        source = self._read(PRACTICE_JS)
        text = self._compact(source)
        practice_py = self._read(APP_PRACTICE_PY)
        official_py = self._read(APP_OFFICIAL_PY)
        checks = [
            ("能力探测：合法 /api/questions?grade= 请求 + grade_filter 响应字段",
             "export function detectGradeFilter(" in source
             and "api(`/questions?page=1&page_size=1&grade=${encodeURIComponent(probeValue)}`)" in source
             and "data.grade_filter && typeof data.grade_filter === 'object'" in text,
             "学段能力探测缺失"),
            ("学段选项优先 catalog.grade_stages，未下发回退固定小学/初中/高中",
             "catalog?.grade_stages" in text
             and "FALLBACK_GRADE_STAGES = ['小学', '初中', '高中']" in source
             and "practice.allGrades" in source,
             "学段选项来源/回退缺失"),
            ("未就绪整体隐藏并丢弃 URL/本地状态里的 grade（避免假筛选）",
             "gradeFilterReady ? field(t('practice.grade'), gradeSel) : null" in text
             and "if (!gradeFilterReady && state.filters.grade)" in text
             and "delete state.filters.grade;" in text,
             "学段控件降级缺失"),
            ("grade 进入 URL 恢复 / 筛选芯片 / 控件同步",
             "'include_ai_difficulty', 'source', 'grade'" in text
             and "grade: t('practice.grade')," in text
             and "if (gradeFilterReady) gradeSel.value = state.filters.grade || '';" in text,
             "学段 URL/芯片契约缺失"),
            ("题库无 grade 列时提示 grade_filter.applied=false（精选侧仍生效）",
             "results.grade_filter && results.grade_filter.applied === false" in text
             and "practice.gradeFilterNotice" in source,
             "学段生效范围提示缺失"),
            ("后端 /api/questions 支持 grade 参数并下发 grade_filter 元数据",
             "grade: str | None = None" in practice_py
             and "_normalize_question_grade_filter" in practice_py
             and '"grade_filter"' in practice_py,
             "app/practice.py 学段参数/元数据缺失"),
            ("后端学段口径：标准年级精确 + 学段前缀（official 快照过滤）",
             "STANDARD_GRADES" in official_py and "GRADE_STAGES" in official_py
             and "def normalize_grade_filter(" in official_py,
             "app/official.py 学段口径缺失"),
        ]
        self.step("学段筛选（practice.js ↔ /api/questions?grade=）", checks, started=started)

    # -- 5. Anki 题链接版本字段 -------------------------------------------
    def check_anki_reference_fields(self) -> None:
        started = time.time()
        source = self._read(ANKI_PY)
        checks = [
            ("统一题目引用正则覆盖 bank:/app:/official:",
             "_QUESTION_REF_RE" in source
             and "official:plz-[A-Za-z0-9]+" in source
             and "def question_ref_of(" in source,
             "题目引用正则/解析缺失"),
            ("来源版本字段（published_version/bank_version/content_revision）",
             "def source_version_of(" in source
             and "published_version" in source
             and "bank_version" in source,
             "来源版本解析缺失"),
            ("错题卡背面「题目引用 / 来源 / 来源版本」脚注与版本标签",
             "def _question_ref_footer(" in source
             and "题目引用：" in source
             and "来源版本：" in source
             and "def _reference_tags(" in source,
             "题链接/版本脚注缺失"),
            ("笔记卡引用脚注与来源资料版本（source_versions）",
             "def _note_refs_footer(" in source
             and "def _note_question_refs(" in source
             and "def _note_source_versions(" in source,
             "笔记引用/资料版本缺失"),
            ("不改变 .apkg 模型字段（Front/Back/Tags）与既有模板",
             'fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Tags"}]' in source
             and '"qfmt": "{{Front}}"' in source,
             ".apkg 模型字段被改动"),
        ]
        # 笔记页侧：统一引用可点击回站内题目、来源版本行渲染。
        notes = self._read(NOTES_JS)
        checks.extend([
            ("notes.js 统一题目引用可点击回练习页（official 引用同样可跳转）",
             "official:plz-[A-Za-z0-9]+" in notes
             and "go('practice', entry.ref)" in self._compact(notes)
             and "data-question-ref" in notes,
             "笔记题目引用链接缺失"),
            ("notes.js 渲染题目引用版本与来源资料版本",
             "notes.questionRefVersion" in notes
             and "notes.sourceVersionsLine" in notes
             and "note?.source_versions" in self._compact(notes),
             "笔记版本提示缺失"),
        ])
        self.step("笔记 / Anki 题链接与来源版本字段（app/anki_export.py ↔ notes.js）",
                  checks, started=started)

    # -- 6. locale 中英文对齐 ---------------------------------------------
    def check_locales(self) -> None:
        started = time.time()
        zh = json.loads(self._read(LOCALES / "zh-CN.json"))
        en = json.loads(self._read(LOCALES / "en-US.json"))
        if not isinstance(zh, dict) or not isinstance(en, dict) or not zh or not en:
            raise StaticFailure("locale 文件结构异常（应为非空 JSON 对象）")
        checks = [
            ("zh-CN / en-US 键集合完全一致",
             set(zh) == set(en),
             f"差异: {sorted(set(zh) ^ set(en))[:20]}"),
        ]
        placeholder_mismatch = []
        for key in sorted(set(zh) & set(en)):
            if not str(zh[key]).strip() or not str(en[key]).strip():
                placeholder_mismatch.append(f"{key}: 空文案")
                continue
            if sorted(re.findall(r"\{([A-Za-z0-9_]+)\}", str(zh[key]))) != \
                    sorted(re.findall(r"\{([A-Za-z0-9_]+)\}", str(en[key]))):
                placeholder_mismatch.append(f"{key}: 占位符不一致")
        checks.append(("中英文文案非空且 {placeholders} 一致",
                       not placeholder_mismatch,
                       json.dumps(placeholder_mismatch[:10], ensure_ascii=False)))
        used: set[str] = set()
        for path in LOCALE_SCAN_FILES:
            used.update(re.findall(r"\bt\('([^']+)'", self._read(path)))
        used.update(INDIRECT_LOCALE_KEYS)
        missing_zh = sorted(key for key in used if key not in zh)
        missing_en = sorted(key for key in used if key not in en)
        checks.append((f"前端字面量/间接引用的 locale 键在两种语言中齐全（{len(used)} 个）",
                       not missing_zh and not missing_en,
                       f"zh 缺={missing_zh[:10]} en 缺={missing_en[:10]}"))
        namespace_report = {}
        for prefix, minimum in LOCALE_NAMESPACES:
            zh_keys = {key for key in zh if key.startswith(prefix)}
            en_keys = {key for key in en if key.startswith(prefix)}
            ok = len(zh_keys) >= minimum and zh_keys == en_keys
            namespace_report[prefix] = {"zh": len(zh_keys), "en": len(en_keys), "min": minimum}
            checks.append((f"命名空间 {prefix} 键量 >= {minimum} 且中英对齐",
                           ok, json.dumps(namespace_report[prefix], ensure_ascii=False)))
        # 关键键实际存在（缺键会让界面回退显示键名）。
        required = (
            "tasks.noRouteToast", "question.source.issueSubmit",
            "practice.grade", "practice.allGrades", "insight.official.pool",
            "home.official.title", "notes.questionRefVersion", "notes.sourceVersionsLine",
            "admin.monitor.unavailable", "plaza.ops.action.resolve", "plaza.quality.highWithdrawn",
        )
        missing_required = [key for key in required if key not in zh or key not in en]
        checks.append(("WS30 关键 locale 键存在", not missing_required,
                       f"缺少: {missing_required}"))
        self.step("locale 中英文键对齐与 WS30 命名空间完整性", checks, started=started,
                  data={"keys": {"zh": len(zh), "en": len(en)},
                        "namespaces": namespace_report,
                        "scanned_files": len(LOCALE_SCAN_FILES)})

    def check_round5_components(self) -> None:
        """第五轮/M2/M3（方案 §5.1/§5.2/§5.3/§5.4）：组件、工具与页面接线的静态契约。

        只做可脚本化的静态断言：组件文件存在、页面确有接入点、样式随主站加载、
        本地资源（KaTeX / three.js）不引 CDN、演示有三视图与无 WebGL 降级分支、
        逐步提示/资料定位/掌握路径的接口调用点齐全。
        """
        started = time.time()
        checks = []
        components = {
            "公式输入组件": FORMULA_INPUT_JS,
            "四档评分组件": REVIEW_RATING_JS,
            "原理演示播放器": PRINCIPLE_DEMO_JS,
            "流程示例": PRINCIPLE_FLOW_JS,
            "等距示例": PRINCIPLE_ISO_JS,
            "最小 3D 示例": PRINCIPLE_THREE_JS,
            "示例预置数据": PRINCIPLE_PRESETS_JS,
        }
        missing = [name for name, path in components.items() if not path.is_file()]
        checks.append((f"第五轮组件文件齐全（{len(components)} 个）", not missing,
                       f"缺少: {missing}"))
        sources = {}
        for path in components.values():
            try:
                sources[path.name] = self._read(path)
            except Exception:  # noqa: BLE001 - 缺失已在上一项记录
                sources[path.name] = ""

        upload = self._read(UPLOAD_JS)
        checks.append(("拍题页接入公式输入与识别块提交",
                       "createFormulaInput" in upload and "buildConfirmBlocks" in upload
                       and 'blocks' in upload and 'ocr-draft' in upload,
                       "upload.js 需引用 createFormulaInput/buildConfirmBlocks 并提交 blocks"))

        wrongbook = self._read(WRONGBOOK_JS)
        checks.append(("错题复习页接入四档评分与到期读取",
                       "renderReviewRating" in wrongbook and "/review-cards/due" in wrongbook
                       and "/ratings" in wrongbook,
                       "wrongbook.js 需引用 renderReviewRating 并调用 due/ratings 接口"))
        # M2（统一到期读路径）：首页读服务端 review 块并可回退旧字段；错题页标注统一口径。
        home = self._read(HOME_JS)
        checks.append(("首页接入统一复习到期口径（M2）",
                       all(token in home for token in
                           ("due_now", "study.review.homeDue", "study.review.homeUnavailable",
                            "study.review.goReview"))
                       and "personal.due_reviews" in home,
                       "home.js 需优先读 home.review 块并回退 personal.due_reviews"))
        checks.append(("错题页标注统一到期与服务端排程（M2）",
                       "due_source" in wrongbook and "study.review.unifiedNote" in wrongbook
                       and "study.review.serverSchedule" in wrongbook,
                       "wrongbook.js 需按 due_source 显示统一口径说明与服务端排程徽标"))

        # M3：函数图/白板工具 + 逐步提示 + 资料定位/摘录 + 掌握路径接线。
        missing_m3 = [str(path.name) for path in M3_COMPONENTS if not path.is_file()]
        checks.append(("M3 函数图/白板组件文件齐全", not missing_m3, f"缺少: {missing_m3}"))
        question_source = self._read(QUESTION_JS)
        checks.append(("题目详情接入函数图/白板/逐步提示",
                       all(token in question_source for token in
                           ("functionGraph.js", "sketchBoard.js", "/api/guided-hints")),
                       "question.js 需懒加载两个组件并调用逐步提示接口"))
        checks.append(("错题页复用解题工具入口",
                       all(token in wrongbook for token in
                           ("sketchBoard.js", "functionGraph.js", "openGuidedHintsPanel")),
                       "wrongbook.js 需复用题目页的函数图/白板/逐步提示入口"))
        notes = self._read(NOTES_JS)
        checks.append(("资料页接入原文定位与摘录",
                       all(token in notes for token in
                           ("/locate", "/api/doc-annotations", "refresh")),
                       "notes.js 需接入 locate 与摘录 CRUD/refresh"))
        insight_source = self._read(INSIGHT_JS)
        checks.append(("学情页接入掌握路径",
                       all(token in insight_source for token in
                           ("/api/mastery-paths", "restate", "refresh")),
                       "insight.js 需接入掌握路径列表/详情/节点操作"))

        # M4：课堂版本化演示场景、播放器渲染、模板扩展与预设。
        player_source = self._read(CLASSROOM_PLAYER_JS)
        checks.append(("课堂播放器渲染固定版本演示场景",
                       all(token in player_source for token in
                           ("renderDemoStage", "cls-demo-stage", "zhixu:demo-ready", "function_graph")),
                       "classroomPlayer.js 需渲染 demo 场景并广播就绪事件"))
        classroom_page = self._read(CLASSROOM_PAGE_JS)
        checks.append(("课堂页支持插入/删除演示场景",
                       "scenes/demo" in classroom_page and "data-demo-insert" in classroom_page,
                       "classroom.js 需调用 scenes/demo 并保留演示管理入口"))
        checks.append(("笔记页展示学习附件并可只读打开",
                       "/artifacts/by-target" in notes and "study.m4.notes" in notes,
                       "notes.js 需按 by-target 读取学习附件"))
        presets_source = self._read(PRESETS_JS)
        missing_presets = [preset for preset in M4_PRESET_IDS if preset not in presets_source]
        checks.append((f"M4 新增 {len(M4_PRESET_IDS)} 个学科演示预设", not missing_presets,
                       f"缺少: {missing_presets}"))
        checks.append(("流程渲染器支持循环/时间轴/状态与手绘风格",
                       all(token in self._read(FLOW_JSON_JS) for token in
                           ("timeline", "state", "loop", "sketch")),
                       "flow.js 需支持 layout/style 扩展"))
        checks.append(("等距渲染器支持圆柱/齿轮/杆形状与手绘风格",
                       all(token in self._read(ISO_JS) for token in
                           ("cylinder", "gear", "rod", "sketch")),
                       "iso.js 需支持 shape/style 扩展"))
        checks.append(("题目页支持按学科推荐并切换演示预设",
                       "SUBJECT_PRESET_HINTS" in question_source
                       and "study.m4.switchDemo" in question_source,
                       "question.js 需实现学科推荐 + 切换演示"))
        checks.append(("课堂演示场景样式已加载", "cls-demo-" in self._read(CLASSROOM_CSS),
                       "styles-classroom.css 需包含 cls-demo-* 样式"))

        # M5：机械 3D / 工厂仿真组件与接线。
        m5_files = (MECH_DEMO_JS, MECH_PRESETS_JS, MECH_CSS)
        checks.append(("M5 机械/工厂演示文件齐全",
                       all(path.is_file() for path in m5_files),
                       f"缺少: {[str(path.name) for path in m5_files if not path.is_file()]}"))
        mech_source = self._read(MECH_DEMO_JS)
        checks.append(("机械 3D 支持拆解/剖切/运转读数与本地 three.js",
                       all(token in mech_source for token in
                           ("createMechView", "localClippingEnabled", "readouts",
                            "vendor/interactive/three.module.min.js")),
                       "mechDemo.js 需实现 three 本地渲染、剖切与读数"))
        checks.append(("工厂仿真支持指标/事件回放与瓶颈",
                       all(token in mech_source for token in
                           ("createFactoryView", "bottleneck", "events")),
                       "mechDemo.js 需渲染 metrics/events 与瓶颈"))
        checks.append(("机械演示无 WebGL 时降级示意",
                       ("WebGL" in mech_source or "webgl" in mech_source)
                       and ("SVG" in mech_source or "svg" in mech_source)
                       and "降级" in mech_source,
                       "mechDemo.js 需在无 WebGL/加载失败时降级 SVG 示意"))
        checks.append(("题目页接入机械/工厂演示入口",
                       "mechDemo" in question_source and "study.m5" in question_source,
                       "question.js 需懒加载 mechDemo 并展示机械演示入口"))
        checks.append(("课堂播放器与笔记面板支持机械/工厂附件",
                       all(token in player_source for token in ("mech_3d", "factory_sim", "mechDemo"))
                       and all(token in notes for token in ("mech_3d", "factory_sim", "mechDemo")),
                       "classroomPlayer.js/notes.js 需映射 mech_3d 与 factory_sim"))
        index_source_m5 = self._read(INDEX_HTML)
        checks.append(("主站加载机械演示样式",
                       "styles-mech.css" in index_source_m5,
                       "index.html 需引入 /static/styles-mech.css"))

        # M6：专题报告与互动讲义组件、笔记页接入与样式。
        learning_docs_js = JS / "components" / "learningDocs.js"
        m6_css = WEB / "styles-m6.css"
        checks.append(("M6 学习文档组件与样式齐全",
                       learning_docs_js.is_file() and m6_css.is_file(),
                       f"缺少: {[str(path.name) for path in (learning_docs_js, m6_css) if not path.is_file()]}"))
        learning_docs = self._read(learning_docs_js) if learning_docs_js.is_file() else ""
        checks.append(("专题报告面板接入列表/创建/编辑/导出",
                       all(token in learning_docs for token in
                           ("/topic-reports", "createReport", "expected_version",
                            "downloadMarkdown")),
                       "learningDocs.js 需实现专题报告列表/创建/编辑/下载"))
        checks.append(("互动讲义面板接入列表/小节/HTML/PDF 导出",
                       all(token in learning_docs for token in
                           ("/api/handouts", "sections", "export?format", "pdf")),
                       "learningDocs.js 需实现互动讲义列表/小节/导出"))
        checks.append(("笔记页接入学习文档区块",
                       "renderTopicReportsPanel" in notes and "renderHandoutsPanel" in notes,
                       "notes.js 需接入学习文档区块"))
        checks.append(("主站加载学习文档样式",
                       "styles-m6.css" in index_source_m5,
                       "index.html 需引入 /static/styles-m6.css"))

        # M7：课堂离线交互演示播放器与专题报告转笔记。
        m7_files = (OFFLINE_DEMO_BUNDLE, OFFLINE_DEMO_BUILD, JS / "offline" / "offlineDemo.js")
        checks.append(("M7 离线演示播放器与构建脚本齐全",
                       all(path.is_file() for path in m7_files),
                       f"缺少: {[str(path.name) for path in m7_files if not path.is_file()]}"))
        offline_bundle = self._read(OFFLINE_DEMO_BUNDLE) if OFFLINE_DEMO_BUNDLE.is_file() else ""
        checks.append(("离线演示播放器自包含（无外链/无 eval）",
                       "ZhixuOfflineDemo" in offline_bundle
                       and "http://" not in offline_bundle and "https://" not in offline_bundle
                       and "eval(" not in offline_bundle,
                       "offline-demo.js 需暴露 ZhixuOfflineDemo 且不引外链/不 eval"))
        checks.append(("专题报告详情提供保存为笔记入口",
                       "save-note" in learning_docs and "study.m7" in learning_docs,
                       "learningDocs.js 需调用 /save-note 并提供中文文案"))
        bundle_check = subprocess.run(
            [sys.executable, "-X", "utf8", str(OFFLINE_DEMO_BUILD), "--check"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        checks.append(("离线播放器产物与源一致（可重复构建）",
                       bundle_check.returncode == 0,
                       (bundle_check.stdout or bundle_check.stderr or "").strip()[-160:]))

        question = self._read(QUESTION_JS)
        checks.append(("题目详情接入「查看原理」并懒加载播放器",
                       "renderPrincipleDemo" in question and "study.demo.open" in question
                       and "principleDemo.js" in question,
                       "question.js 需按需 import 播放器并提供查看原理入口"))

        player = sources.get("principleDemo.js", "")
        checks.append(("播放器具备三视图分派、帧就绪回执与无 WebGL 降级",
                       all(token in player for token in
                           ("flow_animation", "isometric", "demo_3d", "onFrameReady",
                            "isWebGLAvailable", "webglFallback")),
                       "principleDemo.js 需含三视图分派/onFrameReady/WebGL 探测与降级文案"))
        three = sources.get("threeLite.js", "")
        checks.append(("3D 示例只从站内 vendor 动态加载 three.js",
                       "vendor/interactive/three.module.min.js" in three
                       and "https://" not in three and "http://" not in three,
                       "threeLite.js 必须走本地 vendor 且无外链"))
        external = [name for name, text in sources.items()
                    if re.search(r"https?://(?:cdn|unpkg|jsdelivr|esm\.sh)", text)]
        checks.append(("第五轮组件无 CDN 外链 / 无远程脚本", not external,
                       f"含外链: {external}"))

        index_source = self._read(INDEX_HTML)
        styles_missing = [name for name in ROUND5_STYLES + M3_STYLES if name not in index_source]
        checks.append(("主站加载第五轮与 M3 样式表", not styles_missing,
                       f"index.html 缺少: {styles_missing}"))

        zh = json.loads(self._read(LOCALES / "zh-CN.json"))
        en = json.loads(self._read(LOCALES / "en-US.json"))
        required = (
            "study.formula.review.sectionTitle", "study.formula.validate.braces",
            "study.review.scaleNote", "study.review.degraded",
            "study.demo.open", "study.demo.webglFallback", "study.demo.saveArtifact",
            # M2：统一到期读路径的关键文案。
            "study.review.unifiedNote", "study.review.homeDue",
            "study.review.homeUnavailable", "study.review.serverSchedule",
            # M3：学习工具/资料定位/掌握路径关键文案。
            "study.tools.open.graph", "study.tools.open.board", "study.tools.open.hints",
            "study.tools.hints.lockedHint", "study.m3.locateOriginal", "study.m3.saveExcerpt",
            "study.m3.masteryPath", "study.m3.submitRestate", "study.m3.pathCompleted",
            # M4：课堂版本化演示与学习附件关键文案。
            "study.m4.classroom.insert", "study.m4.demoStage.fixedVersion",
            "study.m4.notes.title", "study.m4.switchDemo", "study.m4.scene.demo",
        )
        missing_keys = [key for key in required if key not in zh or key not in en]
        checks.append(("第五轮关键 locale 键存在（公式/四档/原理）", not missing_keys,
                       f"缺少: {missing_keys}"))
        self.step("第五轮/M2/M3 组件与页面接线静态契约", checks, started=started,
                  data={"components": list(components), "styles": list(ROUND5_STYLES + M3_STYLES)})

    # -- 收尾 -------------------------------------------------------------
    def run(self) -> None:
        for name, fn in (
            ("syntax", self.check_syntax),
            ("task_deep_links", self.check_task_deep_links),
            ("official_assets_issues", self.check_official_assets_and_issues),
            ("grade_filter", self.check_grade_filter),
            ("anki_reference", self.check_anki_reference_fields),
            ("locales", self.check_locales),
            ("round5_components", self.check_round5_components),
        ):
            self._started = time.time()
            try:
                fn()
            except StaticFailure:
                continue
            except Exception as exc:  # noqa: BLE001 - 静态检查异常也要记录可读原因
                message = f"{name}: 未预期异常 {exc.__class__.__name__}: {exc}"
                if message not in self.report["problems"]:
                    self.report["problems"].append(message)
                self.report["steps"].append({
                    "name": name, "ok": False, "status": "failed", "elapsed_ms": 0,
                    "checks": [{"label": message, "ok": False, "detail": ""}],
                })

    def finalize(self) -> int:
        passed = sum(1 for item in self.report["steps"] if item.get("ok"))
        total = len(self.report["steps"])
        self.report["http"] = {
            "ok": not self.report["problems"],
            "passed_steps": passed,
            "step_count": total,
            "failed_steps": total - passed,
        }
        self.report["ok"] = not self.report["problems"]
        self.report["exit_code"] = 0 if self.report["ok"] else 1
        self.report["environment"] = {
            "kind": "纯静态（不启动服务/不联网/不读业务库）",
            "node": shutil.which("node") or "",
            "node_check_scope": "web/js/**/*.js",
            "report": str(self.report_path),
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.report["exit_code"]


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 非交互流不支持时忽略
            pass


def _summarize(report: dict) -> dict:
    http = report.get("http") or {}
    return {
        "ok": report.get("ok"),
        "exit_code": report.get("exit_code"),
        "steps": f"{http.get('passed_steps', 0)}/{http.get('step_count', 0)}",
        "problems": report.get("problems") or [],
        "warnings": (report.get("warnings") or [])[:10],
        "report": report.get("environment", {}).get("report"),
    }


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        description="WS30 前端静态验收（任务深链/精选配图与纠错/学段筛选/Anki 版本/locale）")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出目录（相对仓库根，默认 screenshots/integration）")
    parser.add_argument("--json", default=str(DEFAULT_REPORT),
                        help="报告 JSON 路径（相对仓库根，默认 "
                             "screenshots/integration/ui-static-accept-report.json）")
    options = parser.parse_args(argv)

    out_dir = Path(options.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(options.json)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    flow = UiStaticAccept(out_dir, report_path, run_id)
    started = time.time()
    flow.run()
    flow.report["elapsed_seconds"] = round(time.time() - started, 2)
    code = flow.finalize()
    print(json.dumps(_summarize(flow.report), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
