"""AI 互动课堂（整合自 OpenMAIC）：课程生成、场景播放数据、课堂对话与测验、TTS、导出与导入。

契约见 ``INTEGRATION-CONTRACT.md`` §4.1；设计见 ``.omx/plans/integration/recon/openmaic.md``。
子模块：``db``(建表/归属)、``schema``(DSL 校验)、``prompts``(中文 prompt)、``generate``(course 任务)、
``grading``(客观题判分)、``tts``(分层语音)、``api``(HTTP 路由)、``interactive``(交互 HTML 沙箱/事件)、
``pbl``(项目式学习结构化生成)、``exports``(PPTX/离线 HTML/ZIP/PDF 导出)、``importer``(课程 ZIP 导入)、
``multiagent``(P1 多智能体课堂：角色名册/多角色讨论与插话/增强白板动作；路由见 ``api.py``)、
``images``(P2 可选配图：OpenAI 兼容 /images/generations，内容寻址入 cls_assets，失败降级)、
``video``(P5 课堂视频：离线播放器截帧 + ffmpeg 合成 MP4，``course-video`` 任务与下载路由)。
"""
from fastapi import APIRouter

from .api import router as api_router
from .editor import router as editor_router
from .exports import router as export_router
from .importer import router as import_router
from .interactive import router as interactive_router
from .narration import router as narration_router
from .video import router as video_router

router = APIRouter(prefix="/api/classroom", tags=["AI classroom"])
router.include_router(api_router)
# 增量路由：交互运行时事件上报、导出、导入（均不改动既有路由语义）。
router.include_router(interactive_router)
router.include_router(export_router)
router.include_router(import_router)
# P4 旁白批量合成 / P6a 幻灯片结构化编辑（各自模块内实现，接线先占位）。
router.include_router(narration_router)
router.include_router(editor_router)
# P5 课堂视频导出：POST /courses/{id}/video、GET /courses/{id}/video（mp4 下载走既有 export 路由）。
router.include_router(video_router)


def init_classroom() -> None:
    """幂等初始化：建表、注册任务执行器、准备素材目录。"""
    from .. import db

    (db.DATA_DIR / "classroom" / "assets").mkdir(parents=True, exist_ok=True)
    from .db import init_classroom_db

    init_classroom_db()
    from .video import init_video_storage

    init_video_storage()
    register_jobs()


def register_jobs() -> None:
    """向 ``app.ai.jobs`` 的任务注册表登记本模块的扩展任务。

    P1 的交互/PBL 场景生成复用既有 ``course`` 任务（逐场景生成器内部按 scene_type 分派），
    不新增任务类型，因此这里保持与 P0 完全一致的注册行为。
    """
    from ..ai.jobs import register_task_handler
    from .editor import register as register_editor
    from .generate import run_course_job, validate_course_job
    from .narration import register as register_narration
    from .video import register as register_video

    register_task_handler("course", run_course_job, validate_course_job, label="AI 课堂生成")
    # P4 旁白批量合成 / P6a 编辑 / P5 视频导出（各自模块内注册）。
    register_narration()
    register_editor()
    register_video()


__all__ = ["router", "init_classroom", "register_jobs"]
