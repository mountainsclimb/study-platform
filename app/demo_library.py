"""机械/工厂演示模板库 + 固定种子离散事件仿真（DES）（整合方案 §5.4「机械与工厂」/ §8 M5）。

本模块只做两件事，**不引入任何第三方仿真库、不访问网络、不调用模型**：

1. **模板库**（代码常量 :data:`TEMPLATES`）：每个模板登记 ``template_id``、学习附件 ``kind``、
   中文标题/说明、可调参数元数据（名称/中文标签/类型/默认值/上下界/步长/单位）、教学假设与
   来源说明。模型/前端只允许传「模板 ID + 结构化参数」，服务端负责参数校验与确定性构建，
   不在前端拼几何或公式。
2. **固定种子离散事件仿真**（``factory-line``）：三工位串行产线，每工位单服务台 + 有限缓冲，
   按指数分布生成到达间隔与各工位服务时间；随机数**全部**来自 ``random.Random(seed)``，
   按固定顺序抽取（先到达间隔，再按工位 0→2 抽服务时间），因此
   ``template_id + params + seed`` 相同则事件序列与指标逐字段相同。

固定种子口径
------------
* 唯一随机源是 ``random.Random(seed)``（CPython 的 Mersenne Twister 固定种子序列）；
* **不使用**系统时间、进程随机源、哈希随机化、并发顺序或任何环境相关的抽样；
* 到达间隔与服务时间按固定抽取顺序生成：每个物料先抽到达间隔，再按工位号 0、1、2 依次抽
  服务时间；抽取顺序写死在 :func:`_generate_items`，改动即视为不兼容变更；
* 事件时间统一保留 5 位小数；同 ``t`` 的事件按固定类型顺序
  ``arrival → queue → service_start → service_end → blocked`` 再按工位号、物料号稳定排序；
* 事件最多保留 2000 条（超出按时间升序截断并在 ``assumptions`` 说明），指标始终按全量事件计算；
  事件循环硬上限 20 万步，必要时提前结束并在 ``assumptions`` 标注。

数据与错误口径
--------------
* ``POST /api/demos/build`` 只构建、不落库；``POST /api/demos/simulate`` 把运行记录写入
  ``demo_sim_runs`` 并返回可回读的 ``run_id``；``GET /api/demos/simulate/{run_id}`` 只允许本人读取；
* 未知模板 404、他人/不存在运行记录 404；参数越界/类型不符/未知参数/``service_s`` 不是长度 3
  的数字数组一律 422，``detail`` 全部中文；本模块不产生 409；
* 运行记录只存 ``params_json`` / ``result_json`` 两个稳定序列化 JSON TEXT，不存 HTML/脚本。

存储与兼容
----------
建表只在 :func:`init_demo_library` 里用 ``con.executescript`` 幂等完成（由 ``app.main.lifespan``
调用；未经 lifespan 直接调用公开函数时由 :func:`_ensure_ready` 懒建表）；只依赖 ``app.db`` 的
``connection`` / ``write_lock`` / ``utcnow``，JSON 列存 TEXT、SQL 占位符统一 ``?``，不使用
SQLite 专有函数/PRAGMA/RETURNING，因此 SQLite 与 PostgreSQL 行为一致；所有写路径先取
``db.write_lock(con, "demo-library")`` 串行化，``demo_sim_runs`` 已登记 ``app.db._ID_TABLES``
（PostgreSQL 下 ``cursor.lastrowid`` 可用）。

接线方式：应用启动时调用 :func:`init_demo_library`，并把 :data:`router` 挂到主应用
（``app.include_router(demo_library.router)``）；前端按 ``GET /api/demos/templates`` 渲染模板与
参数表单，按 ``POST /api/demos/build`` 取 3D/流程数据，按 ``POST /api/demos/simulate`` 复算并
保存固定种子运行记录，再用 ``GET /api/demos/simulate/{run_id}`` 回放。
"""
from __future__ import annotations

from contextlib import contextmanager
import heapq
import json
import math
import random

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/api/demos", tags=["Demo library"])

# ---------------------------------------------------------------------------
# 常量、限额与建表
# ---------------------------------------------------------------------------

#: 随机种子上限（0..2147483647，覆盖常见 32 位有符号整数种子）。
MAX_SEED = 2_147_483_647
#: 单次构建/仿真返回的事件条数上限（超出按时间升序截断，指标仍按全量事件计算）。
MAX_EVENTS = 2000
#: 单次仿真事件循环硬上限，防止极端参数导致请求长时间占用。
MAX_EVENT_STEPS = 200_000
#: 单次仿真的到达数量安全上限（极端种子下指数分布可能产生过多到达）。
MAX_ARRIVAL_ITEMS = 20_000
#: 产线工位数（固定三工位串行）。
STATION_COUNT = 3
#: 事件类型（与 ``events[].type`` 取值一致，列表顺序即同 ``t`` 时的稳定排序顺序）。
EVENT_TYPES = ("arrival", "queue", "service_start", "service_end", "blocked")
_EVENT_ORDER = {name: index for index, name in enumerate(EVENT_TYPES)}
#: 事件堆内的先后：同一时刻先处理服务结束（释放服务台），再处理到达。
_HEAP_SERVICE_END = 0
_HEAP_ARRIVAL = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_sim_runs(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL);
"""

#: 进程内就绪标记（DATA_DIR + 存储引擎）：直接调用本模块函数（未经 lifespan）时懒建表。
_ready_key: tuple[str, bool] | None = None


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_demo_library() -> None:
    """应用启动时调用：幂等创建 ``demo_sim_runs`` 表（SQLite / PostgreSQL 同一套 DDL）。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "demo-library")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：未经 lifespan 直接调用公开函数时也保证表存在（建表仍只在本模块 init 完成）。"""
    if _ready_key != _schema_key():
        init_demo_library()


# ---------------------------------------------------------------------------
# 连接与 JSON 序列化
# ---------------------------------------------------------------------------


def _lock_write(con) -> None:
    """在写事务内取跨进程命名写锁；外层事务已持锁时（SQLite 不可嵌套 BEGIN IMMEDIATE）复用。"""
    if getattr(con, "in_transaction", False):
        return
    db.write_lock(con, "demo-library")


@contextmanager
def _write_connection(con=None):
    """取得写连接：``con`` 为空时自建并提交，否则复用调用方事务（由调用方负责提交）。"""
    if con is not None:
        _lock_write(con)
        yield con
        return
    with db.connection() as own:
        _lock_write(own)
        yield own


@contextmanager
def _read_connection(con=None):
    """取得读连接：``con`` 为空时自建连接，否则复用调用方连接。"""
    if con is not None:
        yield con
        return
    with db.connection() as own:
        yield own


def _dump_json(value) -> str:
    """稳定序列化：键排序、紧凑分隔符、不转义中文、拒绝 NaN/Infinity（落库与回读同一口径）。"""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise HTTPException(422, "仿真结果包含无法序列化的数据") from None


def _parse_json(text, default):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 参数元数据与校验
# ---------------------------------------------------------------------------


def _param(
    name: str,
    label: str,
    kind: str,
    default,
    minimum,
    maximum,
    *,
    step=1,
    unit: str = "",
    length: int | None = None,
) -> dict:
    """构造一个参数元数据条目（模板注册表与 ``GET /templates`` 共用同一份结构）。"""
    item = {
        "name": name,
        "label": label,
        "type": kind,
        "default": default,
        "min": minimum,
        "max": maximum,
        "step": step,
        "unit": unit,
    }
    if length is not None:
        item["length"] = length
    return item


_SEED_PARAM = _param("seed", "随机种子", "int", 1, 0, MAX_SEED, step=1)


def _fmt_number(value) -> str:
    """把参数上下界/当前值格式化成中文提示里好看的数字（整数不带 .0）。"""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = ("%.6f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


def _range_error(name: str, spec: dict, number) -> HTTPException:
    message = "参数 %s 需在 %s–%s 之间（当前 %s）" % (
        name,
        _fmt_number(spec["min"]),
        _fmt_number(spec["max"]),
        _fmt_number(number),
    )
    return HTTPException(422, message)


def _coerce_param(spec: dict, value):
    """校验并归一化单个参数；类型不符/越界抛 422（中文 detail，可直接给前端展示）。"""
    name = spec["name"]
    kind = spec["type"]
    if kind == "list":
        length = int(spec.get("length") or 3)
        if not isinstance(value, (list, tuple)):
            raise HTTPException(422, "参数 %s 必须是长度为 %d 的数字数组" % (name, length))
        if len(value) != length:
            raise HTTPException(
                422,
                "参数 %s 必须是长度为 %d 的数字数组（当前 %d 个）" % (name, length, len(value)),
            )
        numbers: list[float] = []
        for index, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise HTTPException(422, "参数 %s[%d] 必须是数字" % (name, index))
            number = float(item)
            if not math.isfinite(number):
                raise HTTPException(422, "参数 %s[%d] 必须是有限数字" % (name, index))
            if number < spec["min"] or number > spec["max"]:
                raise _range_error("%s[%d]" % (name, index), spec, number)
            numbers.append(number)
        return numbers
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(422, "参数 %s 必须是整数" % name)
        if isinstance(value, float) and not value.is_integer():
            raise HTTPException(422, "参数 %s 必须是整数" % name)
        number = int(value)
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(422, "参数 %s 必须是数字" % name)
        number = float(value)
    else:  # pragma: no cover - 模板注册表为代码常量，正常不会出现未定义类型
        raise HTTPException(422, "参数 %s 的类型未定义" % name)
    if not math.isfinite(number):
        raise HTTPException(422, "参数 %s 必须是有限数字" % name)
    if number < spec["min"] or number > spec["max"]:
        raise _range_error(name, spec, number)
    return number


def _normalize_params(template: dict, raw) -> dict:
    """按模板参数表校验并补默认值，返回归一化后的参数字典（未知参数 422）。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "参数 params 必须是 JSON 对象")
    specs = {spec["name"]: spec for spec in template["params"]}
    for name in raw:
        if name not in specs:
            raise HTTPException(422, "未知参数：%s" % name)
    normalized: dict = {}
    for spec in template["params"]:
        name = spec["name"]
        value = raw[name] if name in raw else spec["default"]
        normalized[name] = _coerce_param(spec, value)
    return normalized


# ---------------------------------------------------------------------------
# 模板一：二级齿轮减速箱（kind = mech_3d）
# ---------------------------------------------------------------------------

_GEARBOX_ASSUMPTIONS = (
    "教学示意：部件几何按视觉比例压缩，不对应厂商图纸或工程尺寸；读数用于理解传动关系。",
    "速比 = 从动轮齿数 / 主动轮齿数；输出转速 = 输入转速 / 速比；扭矩放大倍数按速比近似，"
    "忽略效率与摩擦损失。",
    "齿轮分度圆直径按 d = m·z（模数 × 齿数）估算，中心距按两分度圆半径之和；实际齿轮还需考虑"
    "变位与齿顶高。",
    "所有读数为教学近似，不作为设计与制造依据；3D 部件树由前端播放器按本地几何渲染。",
)
_SOURCE_NOTE_MECH = (
    "本地教学模板：部件树与读数由服务端按固定参数与固定种子生成，不含在线资产；"
    "机械 3D 由前端播放器按本地几何渲染，不需要 WebGL 时可降级为等距图或海报。"
)


def _vec3(x, y, z) -> list[float]:
    return [round(float(x), 3), round(float(y), 3), round(float(z), 3)]


def _build_gearbox(params: dict) -> dict:
    """构建二级齿轮减速箱教学示意数据（纯函数：同参数同输出，不使用随机源）。"""
    seed = int(params["seed"])
    input_rpm = int(params["input_rpm"])
    driver_teeth = int(params["driver_teeth"])
    driven_teeth = int(params["driven_teeth"])
    module_mm = float(params["module_mm"])

    ratio = driven_teeth / driver_teeth
    output_rpm = input_rpm / ratio
    driver_pitch_mm = module_mm * driver_teeth
    driven_pitch_mm = module_mm * driven_teeth
    center_distance_mm = (driver_pitch_mm + driven_pitch_mm) / 2.0

    # 场景单位：按分度圆直径 / 40mm 压缩成视觉尺寸（读数仍按工程公式与毫米给出）。
    driver_scene = module_mm * driver_teeth / 40.0
    driven_scene = module_mm * driven_teeth / 40.0
    center_gap = (driver_scene + driven_scene) / 2.0
    housing_width = round(driver_scene + driven_scene + 1.2, 3)
    housing_depth = round(max(driver_scene, driven_scene) + 0.8, 3)
    housing_height = 1.4
    left_edge = center_gap / 2.0 - housing_width / 2.0
    right_edge = center_gap / 2.0 + housing_width / 2.0
    input_shaft_length = max(-driver_scene / 2.0 - left_edge, 0.4)
    output_shaft_length = max(right_edge - (center_gap + driven_scene / 2.0), 0.4)

    parts = [
        {
            "id": "housing",
            "label": "减速箱箱体",
            "kind": "box",
            "position": _vec3(center_gap / 2.0, 0.0, 0.0),
            "size": _vec3(housing_width, housing_height, housing_depth),
            "color": "#7a8794",
        },
        {
            "id": "cover",
            "label": "箱盖",
            "kind": "plate",
            "position": _vec3(center_gap / 2.0, housing_height / 2.0 + 0.05, 0.0),
            "size": _vec3(housing_width, 0.08, housing_depth),
            "color": "#9aa7b4",
            "parent": "housing",
        },
        {
            "id": "bearing-plate",
            "label": "轴承端盖",
            "kind": "cylinder",
            "position": _vec3(center_gap / 2.0, 0.0, housing_depth / 2.0 + 0.05),
            "size": _vec3(max(driver_scene, driven_scene) * 0.7, max(driver_scene, driven_scene) * 0.7, 0.12),
            "color": "#5b6770",
            "parent": "housing",
        },
        {
            "id": "input-shaft",
            "label": "输入轴",
            "kind": "rod",
            "position": _vec3(left_edge + input_shaft_length / 2.0, 0.0, 0.0),
            "size": _vec3(input_shaft_length, 0.1, 0.1),
            "color": "#c8ced4",
        },
        {
            "id": "driver-gear",
            "label": "主动小齿轮",
            "kind": "gear",
            "position": _vec3(0.0, 0.0, 0.0),
            "size": _vec3(driver_scene, driver_scene, 0.24),
            "color": "#e0a33a",
        },
        {
            "id": "driven-gear",
            "label": "从动大齿轮",
            "kind": "gear",
            "position": _vec3(center_gap, 0.0, 0.0),
            "size": _vec3(driven_scene, driven_scene, 0.24),
            "color": "#3f7fbf",
        },
        {
            "id": "output-shaft",
            "label": "输出轴",
            "kind": "rod",
            "position": _vec3(right_edge - output_shaft_length / 2.0, 0.0, 0.0),
            "size": _vec3(output_shaft_length, 0.12, 0.12),
            "color": "#c8ced4",
        },
    ]
    steps = [
        {
            "label": "整机观察：确认输入轴、输出轴与箱体的安装关系",
            "part_ids": ["housing", "input-shaft", "output-shaft"],
            "explode": 0.0,
        },
        {
            "label": "拆下箱盖与轴承端盖，露出齿轮副",
            "part_ids": ["cover", "bearing-plate"],
            "explode": 0.3,
        },
        {
            "label": "取出主动小齿轮（输入级）",
            "part_ids": ["driver-gear"],
            "explode": 0.65,
        },
        {
            "label": "取出从动大齿轮（输出级），对照读数核对速比",
            "part_ids": ["driven-gear"],
            "explode": 1.0,
        },
    ]
    readouts = [
        {
            "id": "ratio",
            "label": "传动比（速比）",
            "value": round(ratio, 4),
            "unit": "",
            "note": "教学近似：速比 = 从动轮齿数 / 主动轮齿数",
        },
        {
            "id": "output_rpm",
            "label": "输出转速",
            "value": round(output_rpm, 4),
            "unit": "rpm",
            "note": "教学近似：输出转速 = 输入转速 / 速比（忽略效率损失）",
        },
        {
            "id": "torque_amplification",
            "label": "扭矩放大倍数",
            "value": round(ratio, 4),
            "unit": "倍",
            "note": "教学近似：理想齿轮副按速比放大扭矩，实际需考虑效率与摩擦",
        },
        {
            "id": "pitch_diameter_driver",
            "label": "主动轮分度圆直径",
            "value": round(driver_pitch_mm, 4),
            "unit": "mm",
            "note": "教学近似：d = m·z（模数 × 齿数）",
        },
        {
            "id": "pitch_diameter_driven",
            "label": "从动轮分度圆直径",
            "value": round(driven_pitch_mm, 4),
            "unit": "mm",
            "note": "教学近似：d = m·z（模数 × 齿数）",
        },
        {
            "id": "center_distance",
            "label": "齿轮中心距",
            "value": round(center_distance_mm, 4),
            "unit": "mm",
            "note": "教学近似：a = (d1 + d2) / 2",
        },
    ]
    camera = {
        "type": "perspective",
        "position": _vec3(center_gap / 2.0 + 3.2, 2.6, 4.0),
        "target": _vec3(center_gap / 2.0, 0.2, 0.0),
        "up": [0.0, 1.0, 0.0],
        "fov": 45.0,
    }
    return {
        "version": 1,
        "basis": "local-teaching",
        "seed": seed,
        "parts": parts,
        "steps": steps,
        "readouts": readouts,
        "camera": camera,
        "assumptions": list(_GEARBOX_ASSUMPTIONS),
        "source_note": _SOURCE_NOTE_MECH,
    }


# ---------------------------------------------------------------------------
# 模板二：简易活塞泵（kind = mech_3d）
# ---------------------------------------------------------------------------

_PUMP_ASSUMPTIONS = (
    "教学示意：部件几何按视觉比例压缩，不对应厂商图纸或工程尺寸。",
    "排量按单缸单作用估算 V = π·(缸径/2)²·行程；理论流量 = 排量 × 转速，忽略容积效率与泄漏。",
    "活塞平均速度按 v = 2·行程·转速/60 估算；曲轴角速度按 ω = 2π·转速/60 估算。",
    "所有读数为教学近似，不作为设计与制造依据；实际泵的流量受阀门、粘度和压力影响。",
)
_SOURCE_NOTE_PUMP = (
    "本地教学模板：缸体/活塞/连杆/阀门/曲轴部件树与流量读数由服务端按固定参数生成，"
    "不含在线资产；机械 3D 由前端播放器渲染，无 WebGL 时可降级为等距图或海报。"
)


def _build_pump(params: dict) -> dict:
    """构建简易活塞泵教学模型数据（纯函数：同参数同输出，不使用随机源）。"""
    seed = int(params["seed"])
    stroke_mm = int(params["stroke_mm"])
    rpm = int(params["rpm"])
    bore_mm = int(params["bore_mm"])

    radius_mm = bore_mm / 2.0
    displacement_mm3 = math.pi * radius_mm * radius_mm * stroke_mm
    displacement_cm3 = displacement_mm3 / 1000.0
    flow_l_min = displacement_cm3 * rpm / 1000.0
    mean_piston_speed = 2.0 * stroke_mm * rpm / 60000.0
    crank_omega = 2.0 * math.pi * rpm / 60.0

    # 场景单位：60mm 行程约 1 个场景单位（视觉比例，非工程尺寸）。
    scale = 1.0 / 60.0
    piston_size = max(bore_mm * scale, 0.3)
    stroke_scene = max(stroke_mm * scale, 0.3)
    crank_radius = stroke_scene / 2.0

    parts = [
        {
            "id": "cylinder-block",
            "label": "缸体",
            "kind": "box",
            "position": _vec3(-0.6, 0.0, 0.0),
            "size": _vec3(2.6, piston_size + 0.5, piston_size + 0.5),
            "color": "#7a8794",
        },
        {
            "id": "cylinder-bore",
            "label": "缸套（剖切示意）",
            "kind": "cylinder",
            "position": _vec3(-0.8, 0.0, 0.0),
            "size": _vec3(piston_size, piston_size, 2.1),
            "color": "#b9c3cc",
            "parent": "cylinder-block",
        },
        {
            "id": "piston",
            "label": "活塞",
            "kind": "cylinder",
            "position": _vec3(-0.4, 0.0, 0.0),
            "size": _vec3(piston_size * 0.92, piston_size * 0.92, max(stroke_scene * 0.45, 0.18)),
            "color": "#e8b04b",
        },
        {
            "id": "connecting-rod",
            "label": "连杆",
            "kind": "rod",
            "position": _vec3(0.6, 0.0, 0.0),
            "size": _vec3(1.5, 0.12, 0.12),
            "color": "#c8ced4",
        },
        {
            "id": "crankshaft",
            "label": "曲轴",
            "kind": "cylinder",
            "position": _vec3(1.5, -crank_radius, 0.0),
            "size": _vec3(0.9, 0.9, 0.5),
            "color": "#5b6770",
        },
        {
            "id": "intake-valve",
            "label": "进气阀",
            "kind": "rod",
            "position": _vec3(-1.3, piston_size / 2.0 + 0.22, 0.0),
            "size": _vec3(0.14, 0.34, 0.14),
            "color": "#2f9e44",
            "parent": "cylinder-block",
        },
        {
            "id": "exhaust-valve",
            "label": "排气阀",
            "kind": "rod",
            "position": _vec3(-1.3, -(piston_size / 2.0 + 0.22), 0.0),
            "size": _vec3(0.14, 0.34, 0.14),
            "color": "#e8590c",
            "parent": "cylinder-block",
        },
    ]
    steps = [
        {
            "label": "观察缸体、活塞与阀门的位置关系",
            "part_ids": ["cylinder-block", "cylinder-bore", "piston", "intake-valve", "exhaust-valve"],
            "explode": 0.0,
        },
        {
            "label": "沿轴线取出活塞与连杆",
            "part_ids": ["piston", "connecting-rod"],
            "explode": 0.35,
        },
        {
            "label": "拆下曲轴，观察行程与曲柄半径",
            "part_ids": ["crankshaft"],
            "explode": 0.7,
        },
        {
            "label": "检查进/排气阀开闭时机（教学示意）",
            "part_ids": ["intake-valve", "exhaust-valve"],
            "explode": 1.0,
        },
    ]
    readouts = [
        {
            "id": "stroke",
            "label": "活塞行程",
            "value": stroke_mm,
            "unit": "mm",
            "note": "教学近似：曲柄旋转一周活塞移动 2 倍曲柄半径，即 1 个行程",
        },
        {
            "id": "displacement_cm3",
            "label": "理论排量",
            "value": round(displacement_cm3, 4),
            "unit": "cm³",
            "note": "教学近似：V = π·(缸径/2)²·行程（单缸单作用）",
        },
        {
            "id": "flow_l_min",
            "label": "理论流量",
            "value": round(flow_l_min, 4),
            "unit": "L/min",
            "note": "教学近似：流量 = 排量 × 转速，忽略容积效率与泄漏",
        },
        {
            "id": "mean_piston_speed",
            "label": "活塞平均速度",
            "value": round(mean_piston_speed, 4),
            "unit": "m/s",
            "note": "教学近似：v = 2·行程·转速/60（行程单位 mm，转速单位 rpm）",
        },
        {
            "id": "crank_omega",
            "label": "曲轴角速度",
            "value": round(crank_omega, 4),
            "unit": "rad/s",
            "note": "教学近似：ω = 2π·转速/60",
        },
    ]
    camera = {
        "type": "perspective",
        "position": [2.8, 2.2, 3.6],
        "target": [0.2, 0.0, 0.0],
        "up": [0.0, 1.0, 0.0],
        "fov": 45.0,
    }
    return {
        "version": 1,
        "basis": "local-teaching",
        "seed": seed,
        "parts": parts,
        "steps": steps,
        "readouts": readouts,
        "camera": camera,
        "assumptions": list(_PUMP_ASSUMPTIONS),
        "source_note": _SOURCE_NOTE_PUMP,
    }


# ---------------------------------------------------------------------------
# 模板三/四：三工位串行产线固定种子 DES（kind = factory_sim / flow_animation）
# ---------------------------------------------------------------------------

_FACTORY_ASSUMPTIONS = (
    "固定种子口径：仅使用 random.Random(seed)（Mersenne Twister 固定种子序列），按「到达间隔→"
    "各工位服务时间（工位 0→2）」固定顺序抽取，不使用系统时间或进程随机源。",
    "到达间隔与各工位服务时间均服从指数分布（平均值为参数值），假设相互独立、无故障与换型；"
    "这是课堂对比用的教学简化分布。",
    "三工位串行、每工位单服务台 + 有限缓冲（容量 = buffer_capacity）；工位间为阻塞式"
    "（blocking-after-service），首个工位前队列满时新到料被丢弃并计入 dropped。",
    "utilization 计入服务与阻塞占用时间；仿真截止时刻仍在系统中的在制品不计入 completed，"
    "指标按 sim_seconds 窗口统计。",
    "指标与事件序列均由本模块确定性算法计算，未使用外部仿真库，不代表真实工厂排产数据。",
)
_SOURCE_NOTE_FACTORY = (
    "本地固定种子离散事件仿真（factory-line 模板）：随机数全部由 seed 派生，事件与指标可复现；"
    "覆盖三工位串行产线的排队、阻塞与丢弃，用于课堂参数对比演示，不代表真实工厂排产数据。"
)


def _generate_items(rng: random.Random, sim_seconds: int, arrival_mean: float, service_means: list[float]):
    """按固定顺序抽样生成到达事件：每个物料先抽到达间隔，再按工位 0→2 抽服务时间。

    返回 ``(items, capped)``；``items`` 内每个元素为
    ``{"id": int, "arrival": float, "service": [float, float, float]}``。这是本模块唯一的随机
    来源，抽取顺序一旦改动即破坏固定种子口径。
    """
    items: list[dict] = []
    clock = 0.0
    item_id = 1
    capped = False
    while len(items) < MAX_ARRIVAL_ITEMS:
        # 固定抽取顺序 1：到达间隔（指数分布，均值 arrival_interval_s）。
        clock += rng.expovariate(1.0 / arrival_mean)
        if clock > sim_seconds:
            break
        # 固定抽取顺序 2：工位 0→2 的服务时间（指数分布，均值 service_s[i]）。
        service = [rng.expovariate(1.0 / mean) for mean in service_means]
        items.append({"id": item_id, "arrival": clock, "service": service})
        item_id += 1
    else:  # pragma: no cover - 仅在极端种子/参数下触发
        capped = True
    return items, capped


def _simulate_tandem(items: list[dict], sim_seconds: int, service_means: list[float], capacity: int) -> dict:
    """三工位串行产线事件驱动仿真（无随机：随机序列已在 :func:`_generate_items` 生成）。

    模型：每工位一台服务台 + 容量 ``capacity`` 的输入队列；工位间为阻塞式（服务完成后若下游
    队列已满则服务台保持占用，直到下游腾出空位）；首个工位前队列满时新到料丢弃。事件堆按
    ``(t, 先服务结束后到达, 调度序)`` 处理，保证同一时刻先释放服务台再接纳到达。
    """
    station_count = len(service_means)
    service_by_id = {item["id"]: item["service"] for item in items}
    queues: list[list[dict]] = [[] for _ in range(station_count)]
    server: list[dict | None] = [None] * station_count
    blocked: list[dict | None] = [None] * station_count
    busy = [0.0] * station_count
    wait_sum = [0.0] * station_count
    wait_count = [0] * station_count
    max_queue = [0] * station_count
    events: list[dict] = []
    events_total = 0
    dropped = 0
    blocked_count = 0
    completed = 0
    seq = 0
    step_capped = False
    heap: list[tuple] = []
    heap_push = heapq.heappush

    def push(time_value: float, rank: int, kind: str, station: int, item_id: int) -> None:
        nonlocal seq
        heap_push(heap, (time_value, rank, seq, kind, station, item_id))
        seq += 1

    def add_event(time_value: float, event_type: str, station: int, item_id: int, queue_length: int) -> None:
        nonlocal events_total
        events_total += 1
        if len(events) < MAX_EVENTS:
            events.append(
                {
                    "t": round(float(time_value), 5),
                    "type": event_type,
                    "station": int(station),
                    "item_id": int(item_id),
                    "queue": int(queue_length),
                }
            )

    def enqueue(station: int, item_id: int, timestamp: float) -> None:
        queues[station].append({"id": item_id, "enter": timestamp})
        if len(queues[station]) > max_queue[station]:
            max_queue[station] = len(queues[station])
        add_event(timestamp, "queue", station, item_id, len(queues[station]))

    def start_service(station: int, timestamp: float) -> None:
        while server[station] is None and queues[station]:
            entry = queues[station].pop(0)
            wait_sum[station] += timestamp - entry["enter"]
            wait_count[station] += 1
            finish_at = timestamp + service_by_id[entry["id"]][station]
            server[station] = {"id": entry["id"], "start": timestamp, "finish": finish_at}
            add_event(timestamp, "service_start", station, entry["id"], len(queues[station]))
            push(finish_at, _HEAP_SERVICE_END, "service_end", station, entry["id"])
            if station > 0:
                unblock(station - 1, timestamp)

    def unblock(station: int, timestamp: float) -> None:
        if station < 0 or blocked[station] is None:
            return
        if len(queues[station + 1]) >= capacity:
            return
        entry = server[station]
        payload = blocked[station]
        blocked[station] = None
        busy[station] += timestamp - entry["blocked_at"]
        server[station] = None
        enqueue(station + 1, payload["id"], timestamp)
        start_service(station, timestamp)

    def finish_service(station: int, timestamp: float) -> None:
        nonlocal completed, blocked_count
        entry = server[station]
        busy[station] += timestamp - entry["start"]
        add_event(timestamp, "service_end", station, entry["id"], len(queues[station]))
        if station == station_count - 1:
            completed += 1
            server[station] = None
            start_service(station, timestamp)
            return
        next_station = station + 1
        if len(queues[next_station]) < capacity:
            server[station] = None
            enqueue(next_station, entry["id"], timestamp)
            start_service(next_station, timestamp)
            start_service(station, timestamp)
            return
        entry["blocked_at"] = timestamp
        blocked[station] = {"id": entry["id"]}
        blocked_count += 1
        add_event(timestamp, "blocked", station, entry["id"], len(queues[next_station]))

    def handle_arrival(item: dict, timestamp: float) -> None:
        nonlocal dropped
        add_event(timestamp, "arrival", 0, item["id"], len(queues[0]))
        if len(queues[0]) >= capacity:
            dropped += 1
            add_event(timestamp, "blocked", 0, item["id"], len(queues[0]))
            return
        enqueue(0, item["id"], timestamp)
        start_service(0, timestamp)

    items_by_id = {item["id"]: item for item in items}
    for item in items:
        push(item["arrival"], _HEAP_ARRIVAL, "arrival", 0, item["id"])
    steps = 0
    while heap:
        timestamp, _rank, _seq, kind, station, item_id = heapq.heappop(heap)
        if timestamp > sim_seconds:
            break
        steps += 1
        if steps > MAX_EVENT_STEPS:  # pragma: no cover - 正常参数远低于上限
            step_capped = True
            break
        if kind == "arrival":
            handle_arrival(items_by_id[item_id], timestamp)
        else:
            finish_service(station, timestamp)
    events.sort(
        key=lambda event: (
            event["t"],
            _EVENT_ORDER[event["type"]],
            event["station"],
            event["item_id"],
        )
    )
    return {
        "events": events,
        "events_total": events_total,
        "events_truncated": events_total > MAX_EVENTS,
        "completed": completed,
        "dropped": dropped,
        "blocked": blocked_count,
        "wait_sum": wait_sum,
        "wait_count": wait_count,
        "max_queue": max_queue,
        "busy": busy,
        "step_capped": step_capped,
    }


def _bottleneck_reason(station: int, avg_wait: list[float], utilization: list[float]) -> str:
    """生成中文瓶颈说明：主序平均等待、次序利用率（与瓶颈判定完全同口径）。"""
    if max(avg_wait) <= 1e-9:
        return (
            "本时段无明显排队瓶颈：三工位平均等待均接近 0，产出主要受到达率限制；"
            "可提高到达率或缩短服务时间再观察排队变化。"
        )
    return (
        "瓶颈在工位 %d：平均等待 %.2f 秒、利用率 %.2f%%，上游物料在该工位前排队或受阻最多；"
        "优先缩短该工位服务时间或增加并行服务台。"
        % (station + 1, avg_wait[station], utilization[station] * 100.0)
    )


def _build_factory_line(params: dict) -> dict:
    """构建三工位串行产线的固定种子 DES 结果（build 与 simulate 共用同一实现）。"""
    seed = int(params["seed"])
    sim_seconds = int(params["sim_seconds"])
    arrival_mean = float(params["arrival_interval_s"])
    service_means = [float(value) for value in params["service_s"]]
    capacity = int(params["buffer_capacity"])

    rng = random.Random(seed)
    items, item_capped = _generate_items(rng, sim_seconds, arrival_mean, service_means)
    simulation = _simulate_tandem(items, sim_seconds, service_means, capacity)

    wait_sum = simulation["wait_sum"]
    wait_count = simulation["wait_count"]
    busy = simulation["busy"]
    avg_wait = [
        round(wait_sum[index] / wait_count[index], 5) if wait_count[index] else 0.0
        for index in range(STATION_COUNT)
    ]
    utilization = [
        round(min(max(busy[index] / float(sim_seconds), 0.0), 1.0), 5)
        for index in range(STATION_COUNT)
    ]
    bottleneck = max(
        range(STATION_COUNT), key=lambda index: (avg_wait[index], utilization[index])
    )
    metrics = {
        "sim_seconds": sim_seconds,
        "arrivals": len(items),
        "completed": simulation["completed"],
        "throughput_per_min": round(simulation["completed"] / float(sim_seconds) * 60.0, 4),
        "avg_wait_s": avg_wait,
        "max_queue": [int(value) for value in simulation["max_queue"]],
        "utilization": utilization,
        "bottleneck_station": int(bottleneck),
        "bottleneck_reason": _bottleneck_reason(bottleneck, avg_wait, utilization),
        "dropped": int(simulation["dropped"]),
        "blocked": int(simulation["blocked"]),
    }

    station_labels = [
        "工位 %d（平均服务 %.1f 秒，缓冲 %d 件）"
        % (index + 1, service_means[index], capacity)
        for index in range(STATION_COUNT)
    ]
    nodes: list[dict] = [{"id": "source", "label": "来料", "kind": "source"}]
    for index, label in enumerate(station_labels):
        nodes.append(
            {
                "id": "station-%d" % (index + 1),
                "label": label,
                "kind": "station",
                "capacity": capacity,
                "service_mean_s": round(service_means[index], 4),
            }
        )
    nodes.append({"id": "sink", "label": "成品", "kind": "sink"})
    edges: list[dict] = []
    previous = "source"
    for index in range(STATION_COUNT):
        node_id = "station-%d" % (index + 1)
        edges.append({"from": previous, "to": node_id, "capacity": capacity})
        previous = node_id
    edges.append({"from": previous, "to": "sink", "capacity": capacity})

    assumptions = list(_FACTORY_ASSUMPTIONS)
    extra_notes: list[str] = []
    if simulation["events_truncated"]:
        extra_notes.append(
            "事件序列共 %d 条，超过 %d 条上限，已按时间升序截断保留前 %d 条（指标仍按全量事件计算）。"
            % (simulation["events_total"], MAX_EVENTS, MAX_EVENTS)
        )
    if simulation["step_capped"]:  # pragma: no cover - 正常参数远低于上限
        extra_notes.append(
            "事件循环达到 %d 步上限，仿真提前结束，指标仅覆盖已处理事件。" % MAX_EVENT_STEPS
        )
    if item_capped:  # pragma: no cover - 仅在极端种子/参数下触发
        extra_notes.append(
            "到达数量达到 %d 条安全上限，后续到达未纳入本次仿真。" % MAX_ARRIVAL_ITEMS
        )
    if extra_notes:
        assumptions.append("；".join(extra_notes)[:200])

    return {
        "version": 1,
        "basis": "deterministic-des",
        "seed": seed,
        "flow": {"nodes": nodes, "edges": edges},
        "station_labels": station_labels,
        "metrics": metrics,
        "events": simulation["events"],
        "assumptions": assumptions,
        "source_note": _SOURCE_NOTE_FACTORY,
    }


def _build_factory_flow(params: dict) -> dict:
    """复用三工位 DES 结果，补充流程动画所需的布局与步骤字段（kind = flow_animation）。"""
    data = _build_factory_line(params)
    data.update(
        {
            "layout": "line",
            "style": "sketch",
            "title": "三工位串行产线（流程动画）",
            "steps": [
                {"label": "来料进入工位 1 前队列"},
                {"label": "工位 1 加工完成，进入工位 2"},
                {"label": "工位 2 加工完成，进入工位 3"},
                {"label": "工位 3 加工完成，成品下线"},
            ],
        }
    )
    return data


# ---------------------------------------------------------------------------
# 模板注册表（代码常量，不含用户数据）
# ---------------------------------------------------------------------------

_GEARBOX_PARAMS = (
    _SEED_PARAM,
    _param("input_rpm", "输入转速", "int", 1200, 300, 3000, step=10, unit="rpm"),
    _param("driver_teeth", "主动轮齿数", "int", 12, 10, 40, step=1, unit="齿"),
    _param("driven_teeth", "从动轮齿数", "int", 48, 20, 80, step=1, unit="齿"),
    _param("module_mm", "齿轮模数", "float", 2.0, 1.0, 5.0, step=0.5, unit="mm"),
)
_PUMP_PARAMS = (
    _SEED_PARAM,
    _param("stroke_mm", "活塞行程", "int", 60, 20, 120, step=5, unit="mm"),
    _param("rpm", "曲轴转速", "int", 180, 60, 600, step=10, unit="rpm"),
    _param("bore_mm", "缸径", "int", 60, 30, 120, step=5, unit="mm"),
)
_FACTORY_PARAMS = (
    _SEED_PARAM,
    _param("sim_seconds", "仿真时长", "int", 180, 60, 600, step=30, unit="s"),
    _param("arrival_interval_s", "到达间隔均值", "float", 8.0, 4.0, 20.0, step=0.5, unit="s"),
    _param(
        "service_s",
        "各工位平均服务时间",
        "list",
        [6.0, 4.0, 8.0],
        2.0,
        30.0,
        step=0.5,
        unit="s",
        length=3,
    ),
    _param("buffer_capacity", "缓冲容量", "int", 5, 1, 20, step=1, unit="件"),
)

TEMPLATES: dict[str, dict] = {
    "gearbox-3d": {
        "template_id": "gearbox-3d",
        "kind": "mech_3d",
        "title": "二级齿轮减速箱（机械 3D 教学示意）",
        "description": "由主动小齿轮与从动大齿轮组成的一级减速副：支持部件树拆解、工程读数与 3D 相机；"
        "速比、输出转速、扭矩放大与分度圆直径均按教学公式计算并标注近似口径。",
        "params": _GEARBOX_PARAMS,
        "assumptions": _GEARBOX_ASSUMPTIONS,
        "source_note": _SOURCE_NOTE_MECH,
        "builder": _build_gearbox,
    },
    "pump-3d": {
        "template_id": "pump-3d",
        "kind": "mech_3d",
        "title": "简易活塞泵（机械 3D 教学模型）",
        "description": "缸体、活塞、连杆、进/排气阀与曲轴的简化装配：支持行程、转速、缸径调节，"
        "输出排量与理论流量读数并标注教学简化。",
        "params": _PUMP_PARAMS,
        "assumptions": _PUMP_ASSUMPTIONS,
        "source_note": _SOURCE_NOTE_PUMP,
        "builder": _build_pump,
    },
    "factory-line": {
        "template_id": "factory-line",
        "kind": "factory_sim",
        "title": "三工位串行产线（固定种子离散事件仿真）",
        "description": "三工位串行、每工位单服务台 + 有限缓冲，指数分布到达与服务时间；"
        "固定种子输出可复现的事件序列、吞吐/等待/利用率与瓶颈判定，用于课堂参数对比。",
        "params": _FACTORY_PARAMS,
        "assumptions": _FACTORY_ASSUMPTIONS,
        "source_note": _SOURCE_NOTE_FACTORY,
        "builder": _build_factory_line,
    },
    "factory-line-2d": {
        "template_id": "factory-line-2d",
        "kind": "flow_animation",
        "title": "三工位串行产线（流程动画）",
        "description": "复用 factory-line 的确定性 DES 结果，补充流程动画布局与步骤，可直接作为"
        "流程演示附件保存；事件与指标口径与工厂仿真一致。",
        "params": _FACTORY_PARAMS,
        "assumptions": _FACTORY_ASSUMPTIONS,
        "source_note": _SOURCE_NOTE_FACTORY,
        "builder": _build_factory_flow,
    },
}


# ---------------------------------------------------------------------------
# 校验与查询基础件
# ---------------------------------------------------------------------------


def _validate_user_id(user_id) -> int:
    if isinstance(user_id, bool):
        raise HTTPException(422, "用户标识不正确")
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(422, "用户标识不正确") from None
    if value < 1:
        raise HTTPException(422, "用户标识不正确")
    return value


def _require_template(template_id) -> dict:
    """按模板 ID 取注册表条目：缺 ID/类型错 422，未知模板 404（中文 detail）。"""
    if template_id is None or (isinstance(template_id, str) and not template_id.strip()):
        raise HTTPException(422, "缺少模板 ID")
    if not isinstance(template_id, str):
        raise HTTPException(422, "模板 ID 必须是文本")
    key = template_id.strip()
    template = TEMPLATES.get(key)
    if template is None:
        raise HTTPException(404, "模板不存在：%s" % key)
    return template


def _validate_run_id(run_id) -> int:
    """运行记录编号：格式非法与不存在统一按 404 处理，不向前端泄露存在性。"""
    if isinstance(run_id, bool):
        raise HTTPException(404, "仿真记录不存在或无权访问")
    try:
        value = int(run_id)
    except (TypeError, ValueError):
        raise HTTPException(404, "仿真记录不存在或无权访问") from None
    if value < 1:
        raise HTTPException(404, "仿真记录不存在或无权访问")
    return value


def _run_dict(row) -> dict:
    """运行记录投影：解析参数/结果 JSON，不暴露 user_id。"""
    return {
        "run_id": int(row["id"]),
        "template_id": str(row["template_id"]),
        "seed": int(row["seed"]),
        "params": _parse_json(row["params_json"], {}),
        "result": _parse_json(row["result_json"], {}),
        "created_at": str(row["created_at"] or ""),
    }


def _template_public(template: dict) -> dict:
    """模板的公开元数据投影（不含 builder 等内部字段）。"""
    return {
        "template_id": template["template_id"],
        "kind": template["kind"],
        "title": template["title"],
        "description": template["description"],
        "parametric": True,
        "params": [dict(spec) for spec in template["params"]],
        "assumptions": list(template["assumptions"]),
        "source_note": template["source_note"],
    }


# ---------------------------------------------------------------------------
# 公开函数：模板列表 / 构建 / 仿真 / 回读
# ---------------------------------------------------------------------------


def list_templates() -> dict:
    """列出全部演示模板及参数元数据（前端据此渲染模板卡与参数表单）。"""
    items = [_template_public(template) for template in TEMPLATES.values()]
    return {"items": items, "total": len(items)}


def build_template(template_id, *, params=None) -> dict:
    """按模板与参数确定性构建演示数据（不落库、不校验用户），返回公开构建结果。

    未知模板 → 404；参数未知/越界/类型不符 → 422（中文 detail）；``params`` 省略时全部用默认值。
    """
    template = _require_template(template_id)
    normalized = _normalize_params(template, params)
    data = template["builder"](normalized)
    return {
        "template_id": template["template_id"],
        "kind": template["kind"],
        "title": template["title"],
        "seed": int(normalized["seed"]),
        "params": normalized,
        "data": data,
        "assumptions": list(data.get("assumptions") or template["assumptions"]),
        "source_note": str(data.get("source_note") or template["source_note"]),
    }


def simulate_template(user_id, template_id, *, params=None, con=None) -> dict:
    """构建并持久化一次固定种子仿真运行，返回 ``{run, deterministic: true}``。

    仅 ``kind == "factory_sim"`` 模板可持久化（其余模板 → 422）；未知模板 → 404；
    参数校验与 :func:`build_template` 完全同口径；运行记录只允许创建者本人回读。
    """
    uid = _validate_user_id(user_id)
    template = _require_template(template_id)
    if template["kind"] != "factory_sim":
        raise HTTPException(
            422, "模板 %s 不支持持久化仿真（仅 factory_sim 模板可保存运行记录）" % template["template_id"]
        )
    normalized = _normalize_params(template, params)
    data = template["builder"](normalized)
    params_json = _dump_json(normalized)
    result_json = _dump_json(data)
    created_at = db.utcnow()
    _ensure_ready()
    with _write_connection(con) as active:
        cursor = active.execute(
            "INSERT INTO demo_sim_runs(user_id, template_id, seed, params_json, result_json, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (
                uid,
                template["template_id"],
                int(normalized["seed"]),
                params_json,
                result_json,
                created_at,
            ),
        )
        run_id = cursor.lastrowid
        row = active.execute(
            "SELECT * FROM demo_sim_runs WHERE id=? AND user_id=?", (run_id, uid)
        ).fetchone()
    if row is None:  # pragma: no cover - 正常写入后必然可读回
        raise HTTPException(503, "仿真运行记录写入失败，请稍后重试")
    return {"run": _run_dict(row), "deterministic": True}


def get_sim_run(user_id, run_id, *, con=None) -> dict:
    """读取本人某次仿真运行（含事件与指标）；不存在或属于他人统一 404。"""
    uid = _validate_user_id(user_id)
    rid = _validate_run_id(run_id)
    _ensure_ready()
    with _read_connection(con) as active:
        row = active.execute(
            "SELECT * FROM demo_sim_runs WHERE id=? AND user_id=?", (rid, uid)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "仿真记录不存在或无权访问")
    return {"run": _run_dict(row)}


# ---------------------------------------------------------------------------
# HTTP 请求模型与路由（静态路径先于 /{run_id} 注册）
# ---------------------------------------------------------------------------


class BuildRequest(BaseModel):
    """构建请求体：``template_id`` 必填、``params`` 为可选参数对象（服务端校验并补默认值）。"""

    template_id: object = None
    params: object = None


@router.get("/templates")
def route_list_templates():
    """列出机械/工厂演示模板与参数元数据（固定顺序，便于前端渲染与快照测试）。"""
    return list_templates()


@router.post("/build")
def route_build(body: BuildRequest | None = None):
    """按模板 ID + 结构化参数确定性构建演示数据（不落库）。"""
    request = body if body is not None else BuildRequest()
    return build_template(request.template_id, params=request.params)


@router.post("/simulate")
def route_simulate(body: BuildRequest | None = None, user=Depends(current_user)):
    """构建并持久化固定种子仿真运行，返回 ``run`` 与 ``deterministic`` 标记。"""
    request = body if body is not None else BuildRequest()
    return simulate_template(user["id"], request.template_id, params=request.params)


@router.get("/simulate/{run_id}")
def route_get_sim_run(run_id: str, user=Depends(current_user)):
    """读取本人仿真运行（他人/不存在统一 404）；静态路径 ``/templates`` 已在前面注册。"""
    return get_sim_run(user["id"], run_id)


__all__ = [
    "EVENT_TYPES",
    "MAX_ARRIVAL_ITEMS",
    "MAX_EVENTS",
    "MAX_EVENT_STEPS",
    "MAX_SEED",
    "STATION_COUNT",
    "TEMPLATES",
    "build_template",
    "get_sim_run",
    "init_demo_library",
    "list_templates",
    "router",
    "simulate_template",
]
