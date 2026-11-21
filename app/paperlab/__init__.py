"""试卷工坊（整合自 Oh My Paper / 小鹿老师）：试卷识别、可写题库、组卷、答题卡、巩固卷。

契约见 `INTEGRATION-CONTRACT.md` §4.2。已实现：
- ``db``：幂等建表 ``pl_imports`` / ``pl_pages`` / ``pl_import_items`` / ``pl_questions``；
- ``digitize``：上传（图片/PDF/DOCX）→ 页图 → ``paper-import`` 视觉识别任务 → 草稿核验/入库；
- ``bank``：可写题库查询 / CRUD（version CAS）/ 核验 / meta；
- ``bank_io``：题库统计、JSON/CSV 导出与导入（dry_run 预览、内容摘要去重）；
- ``enrich``：``paper-enrich`` 任务，AI 补全答案/解析/评分标准/知识点；
- ``papers``：P1 试卷与题目快照（``pl_papers`` / ``pl_paper_items``）、手工建卷、发布/排序；
- ``compose``：``paper-compose`` AI 组卷（Hamilton 配额 + 二分匹配硬校验）；
- ``revision``：错题复现卷（replay）与 ``paper-revision`` AI 巩固卷（consolidate）；
- ``cards``：答题卡模板（``pl_cards``）/ 几何校验 / SVG / 打印导出（``card_svg`` 纯函数）；
- ``exporters``：``docx`` / 自包含打印 ``html`` 导出（支持 columns 分栏与自动分页）；
- ``annotate``：切题与答题区标注（``pl_page_edits`` 页级 edit.json / version CAS / 重切 / 发布校验）；
- ``assets``：个人题图资产登记（``pl_assets``）、读图所有权校验与旧引用迁移（P0 安全）；
- ``prompts``：中文识别与补全提示词（``PROMPT_VERSION``）。
"""
from fastapi import APIRouter

from . import annotate, bank, bank_io, cards, compose, digitize, enrich, exporters, papers, revision
from . import practice as paper_practice
from . import scan

router = APIRouter(prefix="/api/paperlab", tags=["Paper lab"])
router.include_router(digitize.router)
router.include_router(enrich.router)
# bank_io 的 /questions/stats、/questions/export 必须在 bank 的 /questions/{id} 之前注册，
# 否则会被整数路径参数先匹配（任务中心/设置页的题库页依赖这两个固定路径）。
router.include_router(bank_io.router)
router.include_router(bank.router)
# P1：组卷/巩固卷/试卷 CRUD/导出/答题卡。compose/revision 的 /papers/compose、/revision 与 papers 不冲突；
# 导出路由比 GET /papers/{id} 更具体，注册顺序不影响匹配。
router.include_router(papers.router)
router.include_router(exporters.router)
router.include_router(compose.router)
router.include_router(revision.router)
router.include_router(cards.router)
# P1：切题/答题区标注（页级 edit.json、重切、发布校验），路由前缀均与既有路由不冲突。
router.include_router(annotate.router)
# P1：整卷练习（个人自测作答/判分/错题联动）；路由 /papers/{id}/attempts、/attempts*。
router.include_router(paper_practice.router)
# P9：答题卡扫描识别（OMR，个人自测）；路由 /papers/{id}/scan、/scans/{id}。
router.include_router(scan.router)


def init_paperlab() -> None:
    """幂等初始化：建表（P0 + P1）、注册任务执行器、准备素材目录。"""
    from .. import db
    from .db import init_tables

    for name in ("paperlab", "paperlab/pages", "paperlab/assets"):
        (db.DATA_DIR / name).mkdir(parents=True, exist_ok=True)
    init_tables()
    papers.ensure_tables()
    cards.ensure_tables()
    annotate.ensure_tables()
    paper_practice.ensure_tables()
    scan.ensure_tables()
    register_jobs()


def register_jobs() -> None:
    """向 `app.ai.jobs` 的任务注册表登记本模块的扩展任务。"""
    from ..ai.jobs import register_task_handler
    from .annotate import run_annotate_job, validate_annotate_job
    from .compose import run_compose_job, validate_compose_job
    from .digitize import run_import_job, validate_import_job
    from .enrich import run_enrich_job, validate_enrich_job
    from .revision import run_revision_job, validate_revision_job

    register_task_handler(
        "paper-import", run_import_job, validate_import_job, label="试卷识别导入",
    )
    register_task_handler(
        "paper-enrich", run_enrich_job, validate_enrich_job, label="AI 补全答案解析",
    )
    register_task_handler(
        "paper-compose", run_compose_job, validate_compose_job, label="AI 组卷",
    )
    register_task_handler(
        "paper-revision", run_revision_job, validate_revision_job, label="AI 错题巩固卷",
    )
    register_task_handler(
        "paper-annotate", run_annotate_job, validate_annotate_job, label="试卷切题重切",
    )
    scan.register()
