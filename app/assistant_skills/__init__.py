"""AI 助手内置技能库（随包只读）。

每个技能由「元数据 + Markdown 方法论（``*.md``）」组成：

* ``name/description``：给用户看的名称与一句话说明；
* ``tool_hints``：建议优先使用的工具名（仅提示，**不改变**助手全局工具白名单与写操作确认规则）；
* ``content``：注入系统提示末尾的方法论文本，含输出结构与边界。

内置技能不可被用户修改/删除（API 对内置 id 返回 409）；用户自建技能存 ``asst_skills`` 表，
内容同样只作为方法论注入，不得覆盖系统规则。
"""
from functools import lru_cache
from pathlib import Path

BUILTIN_IDS = (
    "wrong-question-diagnosis",
    "revision-plan",
    "knowledge-explain",
    "paper-compose-help",
    "study-review",
)

BUILTIN_SPECS = [
    {
        "slug": "wrong-question-diagnosis",
        "name": "错题诊断",
        "description": "从错题本与真实统计定位最该复习的错因，给出可执行的归因与复习建议。",
        "tool_hints": ["list_wrongs", "read_question", "get_stats"],
    },
    {
        "slug": "revision-plan",
        "name": "巩固计划",
        "description": "按薄弱知识点与错题准备错题重练/AI 巩固卷（写操作，需用户确认）。",
        "tool_hints": ["get_stats", "list_wrongs", "list_knowledge_points", "create_revision_paper"],
    },
    {
        "slug": "knowledge-explain",
        "name": "知识点讲解",
        "description": "围绕知识点树与真题证据做分步讲解，先定位再讲透，不堆砌公式。",
        "tool_hints": ["list_knowledge_points", "search_source_bank", "read_question", "read_note"],
    },
    {
        "slug": "paper-compose-help",
        "name": "组卷助手",
        "description": "把「想要一份什么卷」翻译成组卷参数，先预选再由用户确认生成。",
        "tool_hints": ["search_source_bank", "list_mybank_questions", "compose_paper"],
    },
    {
        "slug": "study-review",
        "name": "阶段复盘",
        "description": "基于真实作答/错题统计生成周复盘与下周计划，数字只引用工具结果。",
        "tool_hints": ["get_stats", "list_wrongs", "list_notes"],
    },
]

ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def builtin_skills() -> list[dict]:
    """内置技能完整内容（只读副本；Markdown 在 import 时从包内文件读取）。"""
    items = []
    for spec in BUILTIN_SPECS:
        content = (ROOT / f"{spec['slug']}.md").read_text(encoding="utf-8").strip()
        items.append({
            "id": f"builtin:{spec['slug']}",
            "builtin": True,
            "slug": spec["slug"],
            "name": spec["name"],
            "description": spec["description"],
            "tool_hints": list(spec["tool_hints"]),
            "content": content,
            "system_extra": content,
            "enabled": True,
            "version": 1,
        })
    return items


def builtin_by_slug() -> dict[str, dict]:
    return {item["slug"]: item for item in builtin_skills()}


def is_builtin_skill_id(skill_id) -> bool:
    text = str(skill_id or "").strip()
    if text.startswith("builtin:"):
        return text.split(":", 1)[1] in BUILTIN_IDS
    return text in BUILTIN_IDS


__all__ = ["BUILTIN_IDS", "BUILTIN_SPECS", "builtin_skills", "builtin_by_slug", "is_builtin_skill_id"]
