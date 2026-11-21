"""统一题目引用（方案 §6「统一题目引用」）。

引用格式：
- ``bank:<qid>``：只读高考题库题目（r5 基线）；**裸 qid 仍按 bank 解析**，兼容旧数据；
- ``app:<pl_questions.id>``：我的题库个人题（归属与删除校验在读取层完成，本模块只做格式校验）；
- ``official:<public_id>``：平台精选公开题（公开题号 ``plz-...``；可见性/读取在
  ``app/official.py`` 按查询时状态判定，本模块只做格式校验）。

本模块是纯字符串工具：不导入数据库、HTTP 或业务模块，供练习（``app/practice.py``）、
错题/复习队列与试卷工坊（``app/paperlab/practice.py``）共用同一题目身份口径。

同时提供两类幂等键工具：
- :func:`submission_key`：单题作答幂等键（客户端 ``submission_id`` → 服务端唯一键）；
- :func:`paper_item_key`：整卷逐题入账幂等键（``attempt_id:item_position``）。
"""
from __future__ import annotations

from dataclasses import dataclass
import re

SOURCE_BANK = "bank"
SOURCE_APP = "app"
SOURCE_OFFICIAL = "official"
SOURCES = (SOURCE_BANK, SOURCE_APP, SOURCE_OFFICIAL)

#: 面向用户的来源展示标签；题库题保持既有中文文案「高考题库」。
SOURCE_LABELS = {
    SOURCE_BANK: "高考题库",
    SOURCE_APP: "我的题库",
    SOURCE_OFFICIAL: "平台精选",
}

#: 已实现读取的来源：bank=只读题库、app=我的题库、official=平台精选公开题（广场）。
SUPPORTED_SOURCES = (SOURCE_BANK, SOURCE_APP, SOURCE_OFFICIAL)

MAX_REF_LENGTH = 300
MAX_SUBMISSION_ID_LENGTH = 160
_SUBMISSION_ID_RE = re.compile(r"^[A-Za-z0-9._:+-]+$")
_APP_KEY_RE = re.compile(r"^[1-9][0-9]{0,17}$")


class QuestionRefError(ValueError):
    """题目引用格式非法（调用方统一转成 4xx，不向上抛 500）。"""


@dataclass(frozen=True)
class QuestionRef:
    """解析后的题目引用。``key`` 只对 ``source`` 有意义（bank=qid / app=正整数 id）。"""

    source: str
    key: str
    version: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.key}"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def is_app(self) -> bool:
        return self.source == SOURCE_APP

    @property
    def is_bank(self) -> bool:
        return self.source == SOURCE_BANK

    @property
    def is_official(self) -> bool:
        return self.source == SOURCE_OFFICIAL

    @property
    def supported(self) -> bool:
        return self.source in SUPPORTED_SOURCES

    @property
    def storage_key(self) -> str:
        """写入既有 ``attempts/wrong_questions/mastery.qid`` 的键。

        bank 保持**裸 qid**（旧数据、旧接口、学情/导出全部不变）；
        app 用 ``app:<id>``；official 用 ``official:<id>``。
        """
        return self.key if self.source == SOURCE_BANK else self.ref

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "source": self.source,
            "source_label": self.source_label,
            "key": self.key,
            "version": self.version,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志排查
        return self.ref


def _make(source: str, key: str) -> QuestionRef:
    if source not in SOURCES:  # pragma: no cover - parse 已限定
        raise QuestionRefError(f"未知题目来源：{source}")
    if source == SOURCE_APP:
        if not _APP_KEY_RE.match(key):
            raise QuestionRefError("个人题引用必须形如 app:<正整数 id>")
        return QuestionRef(source, str(int(key)))
    if not key:
        raise QuestionRefError("题目引用缺少题目 ID")
    if any(character.isspace() for character in key):
        raise QuestionRefError("题目引用不能包含空白字符")
    if any(character in key for character in ("\x00", "\r", "\n")):
        raise QuestionRefError("题目引用包含非法字符")
    return QuestionRef(source, key)


def parse(value, *, default_source: str = SOURCE_BANK) -> QuestionRef:
    """解析引用；未知前缀按裸 qid（bank）处理，保持旧客户端兼容。

    - ``"docA:1"`` → bank / key=``docA:1``（题库 qid 本身含冒号，只按首个冒号切分）；
    - ``"bank:docA:1"`` → bank / key=``docA:1``；
    - ``"app:12"`` → app / key=``12``；
    - ``"official:7"`` → official / key=``7``（读取层会拒绝）。
    """
    text = str(value or "").strip()
    if not text:
        raise QuestionRefError("题目引用不能为空")
    if len(text) > MAX_REF_LENGTH:
        raise QuestionRefError("题目引用过长")
    source, separator, rest = text.partition(":")
    if separator and source.lower() in SOURCES:
        return _make(source.lower(), rest.strip())
    return _make(default_source, text)


def try_parse(value, *, default_source: str = SOURCE_BANK) -> QuestionRef | None:
    try:
        return parse(value, default_source=default_source)
    except QuestionRefError:
        return None


def build(source: str, key) -> str:
    """由来源和题目 ID 构造规范化引用。"""
    return _make(str(source or "").lower(), str(key or "").strip()).ref


def normalize(value) -> str:
    """规范化为 ``bank:<qid>`` / ``app:<id>`` / ``official:<id>``；裸 qid 补 bank 前缀。"""
    return parse(value).ref


def split(value) -> tuple[str, str]:
    parsed = parse(value)
    return parsed.source, parsed.key


def storage_key(value) -> str:
    """写库键：bank 裸 qid（向后兼容），app/official 带前缀。"""
    return parse(value).storage_key


def source_of(value) -> str:
    return parse(value).source


def source_label(value) -> str:
    return parse(value).source_label


def is_app(value) -> bool:
    return parse(value).source == SOURCE_APP


def describe(value) -> dict:
    return parse(value).as_dict()


def validate_submission_id(value) -> str:
    """校验客户端 ``submission_id``：非空、≤160、只允许 URL 安全字符。

    客户端重试/弱网重复提交复用同一值；服务端以 ``(user_id, qid, submission_id)``
    唯一约束（落库为 ``submission_ledger`` 主键）保证只入账一次。
    """
    text = str(value or "").strip()
    if not text:
        raise QuestionRefError("submission_id 不能为空")
    if len(text) > MAX_SUBMISSION_ID_LENGTH:
        raise QuestionRefError("submission_id 过长")
    if not _SUBMISSION_ID_RE.match(text):
        raise QuestionRefError("submission_id 只能包含字母、数字与 . _ : + -")
    return text


def submission_key(question, submission_id) -> str:
    """单题作答幂等键：``<写库 qid>|<submission_id>``。

    包含规范化后的题目键，因此不同题目的相同 ``submission_id`` 不会互相吞并；
    同一用户 + 同一题 + 同一 ``submission_id`` 只入账一次。
    """
    token = validate_submission_id(submission_id)
    return f"{storage_key(question)}|{token}"


def paper_item_key(attempt_id, item_position) -> str:
    """整卷逐题入账幂等键：``paper:<attempt_id>:<item_position>``。"""
    if attempt_id is None or item_position is None:
        raise QuestionRefError("整卷幂等键缺少 attempt_id 或 item_position")
    return f"paper:{int(attempt_id)}:{int(item_position)}"


__all__ = [
    "MAX_REF_LENGTH",
    "MAX_SUBMISSION_ID_LENGTH",
    "SOURCES",
    "SOURCE_APP",
    "SOURCE_BANK",
    "SOURCE_LABELS",
    "SOURCE_OFFICIAL",
    "SUPPORTED_SOURCES",
    "QuestionRef",
    "QuestionRefError",
    "build",
    "describe",
    "is_app",
    "normalize",
    "paper_item_key",
    "parse",
    "source_label",
    "source_of",
    "split",
    "storage_key",
    "submission_key",
    "try_parse",
    "validate_submission_id",
]
