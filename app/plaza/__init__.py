"""知序广场（Phase 2 + Phase 3 质量运营）：公开只读、投稿、评审、发布与运营后端。

模块划分（方案 §6「投稿、评审和发布」、§8 阶段 3「平台精选与质量运营」）：

* ``db``：``plaza_*`` 表、状态/阈值常量、事件与角色查询；
* ``service``：不可变快照、来源/授权校验、评审阈值、序列化与权限依赖；
* ``public``：匿名只读接口（社区待核验/已核验/精选；公开图与撤回/争议状态）；
* ``figures``：公开衍生图登记（复制 + sha256 校验 + ``public_derivative``）与匿名读图授权；
* ``submissions``：作者端（创建/预览/送审/改版/撤回/我的投稿）；
* ``review``：编辑初审、按学科队列、独立评审、我的评审历史与阈值自动流转；
* ``release``：编辑签发精选、公开衍生图登记、撤回整条链、回滚、发布历史与事件链；
* ``roles``：``/me`` 能力摘要、身份申请、管理员角色授权；
* ``admin``：编辑台投稿队列、身份认证审核队列与审核动作；
* ``notify``：通知中心（事件钩子、个人通知读取/已读）；
* ``favorites``：收藏/取消收藏、我的收藏与撤回通知收藏者；
* ``feedback``：题目纠错、举报与作者申诉（用户提交 + 编辑端队列/处理）；
* ``quality``：质量抽检记录、待抽检/已抽检队列与 ``high`` 自动撤回；
* ``metrics``：质量指标（题量/可判分率/图可用率/评审时长/抽检问题率/反馈积压/撤回）。
"""
from fastapi import APIRouter

from . import (
    admin,
    favorites,
    feedback,
    metrics,
    notify,
    public,
    quality,
    release,
    review,
    roles,
    submissions,
)

router = APIRouter(prefix="/api/plaza", tags=["Plaza"])
# 固定路径（/me、/questions、/submissions/mine、/notifications）先注册，避免被 {id} 动态段抢占。
router.include_router(roles.router)
router.include_router(notify.router)
router.include_router(favorites.router)
router.include_router(public.router)
router.include_router(feedback.router)
router.include_router(submissions.router)
router.include_router(review.router)
router.include_router(release.router)
router.include_router(quality.router)
router.include_router(metrics.router)
router.include_router(admin.router)

__all__ = ["router"]
