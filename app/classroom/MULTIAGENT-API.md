# AI 课堂 P1：多智能体课堂 API（角色名册 / 多角色讨论 / 学生插话 / 增强白板动作）

> 版本：`classroom-multiagent-2026-11-21-v1`（`app.classroom.multiagent.PROMPT_VERSION`）。
> 所有接口都在 `/api/classroom` 下、需要登录；私有资源先校验归属（他人资源一律 **404**，
> 不泄露存在性）。错误统一 `{"detail": "中文说明"}`。本文件是前端播放器/编辑器的接入依据，
> 与既有 `API-SAMPLES.md`（tutor 单智能体等 P0 接口）互补，**不改动既有接口的字段与行为**。
> 交叉阅读：课程生成任务会自动生成名册、导出/导入 ZIP 会携带名册（见 §11 与
> `GENERATION-PLUS.md`）；全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。

## 0. 接口清单

| 方法 | 路径 | 说明 | 是否调用模型 |
|---|---|---|---|
| GET | `/api/classroom/meta` | 元信息（本版本**增量**加入 `agent_roles`、`discussion_modes` 与多智能体 limits） | 否 |
| GET | `/courses/{course_id}/agents` | 读取课程角色名册（老师 + 同学） | 否 |
| POST | `/courses/{course_id}/agents/generate` | 一次结构化调用生成角色名册，覆盖已存名册 | 是（同步） |
| PATCH | `/courses/{course_id}/agents` | 编辑名册：完整替换 / 按角色局部改 / 清空 | 否 |
| POST | `/sessions/{session_id}/discussion` | 一轮多角色讨论脚本（讨论 / 自由问答 / 讲授），写入 `cls_turns` | 是（同步） |
| POST | `/sessions/{session_id}/interject` | 学生插话后的 1-2 段多角色回应，写入 `cls_turns` | 是（同步） |

无可用模型连接时统一 **422**（中文引导「请先在「模型设置」中配置并启用一个 AI 连接…」），
不伪造 AI 结果；模型/JSON 校验失败（`call_structured` 自动带纠错提示重试一次后仍失败）返回 **503**。

## 1. 数据约定

### 1.1 名册存储

名册保存在 `cls_courses.config_json.agents`（不新增表、不影响既有字段）：

```json
{
  "document_ids": [],
  "agents": {
    "teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher",
                 "persona": "……", "avatar": "", "color": "#3b82f6", "priority": 10},
    "classmates": [
      {"id": "student_1", "name": "小舟", "role": "student",
       "persona": "……", "avatar": "", "color": "#10b981", "priority": 5}
    ]
  },
  "agents_meta": {"prompt_version": "classroom-multiagent-2026-11-21-v1",
                   "model": "…", "provider_name": "…", "generated_at": "2026-11-20T18:16:22+00:00",
                   "updated_at": "…", "agent_count": 4}
}
```

约束：**恰好 1 名 `teacher`**，同学 1-5 名（`classmates`，`role` 可为 `student`/`assistant`），
总角色 ≤ 6；`name` ≤ 24 字、`persona` ≤ 400 字（≥2 字，写性格 + 教学/学习风格）；
`color` 为 `#rgb/#rrggbb[aa]`；`priority` 1-10（默认 teacher=10 / assistant=7 / student=5）。
模型只产出 `name`/`persona`，`id`/`color`/`priority` 由服务端确定（避免重复与越界）。

### 1.2 对话持久化（既有 `cls_turns`，字段不变）

多角色脚本按发言顺序逐条写入 `cls_turns`：

| 列 | 用户发言 | 角色发言 |
|---|---|---|
| `role` | `"user"` | `"teacher"` / `"assistant"` / `"student"` |
| `speaker` | `"student"` | 角色名（如「林老师」「小舟」） |
| `content` | 用户插话/提问原文 | 该角色的台词 |
| `actions_json` | `[]` | 该段发言的动作数组（白名单校验后） |

`GET /api/classroom/sessions/{session_id}` 的 `turns` 直接返回这些行（最近 20 条），前端可用
`speaker` + `role` + 名册 `color` 渲染气泡与不同声音；`actions` 按出现顺序播放。

> 注意：tutor 接口写入的角色仍为 `assistant`（P0 行为不变）；只有多智能体讨论/插话写入
> `teacher`/`assistant`/`student` 角色值。

### 1.3 白板坐标：讨论脚本用 0-1 归一化

| 来源 | 白板动作 | 坐标 |
|---|---|---|
| 课程生成管线（`schema.normalize_actions`，既有场景 `meta.actions`） | `{"type":"whiteboard","ops":[…]}` | **1000×562.5 画布像素** |
| 本文件的多智能体讨论/插话脚本 | `{"type":"whiteboard","ops":[…],"coord_space":"unit"}` | **0-1 归一化**（左上 `(0,0)`、右下 `(1,1)`） |

前端用 `action.coord_space === "unit"` 判断：像素 = 归一化值 × 画布宽/高（幻灯片画布固定
`1000 × 562.5`）。既有场景动作没有该字段，行为不变。

## 2. 角色名册

### 2.1 `GET /api/classroom/courses/{course_id}/agents`

未生成名册时：

```json
{
  "roster": {"teacher": null, "classmates": []},
  "agents": [],
  "has_roster": false,
  "prompt_version": "classroom-multiagent-2026-11-21-v1",
  "generated_at": null,
  "limits": {"agents_max": 6, "classmates_max": 5, "name_max": 24, "persona_max": 400},
  "roles": [{"id": "teacher", "label": "老师"}, {"id": "assistant", "label": "助教"},
            {"id": "student", "label": "同学"}]
}
```

已生成时 `roster` 为完整名册、`agents` 为扁平列表（老师在前），`has_roster: true`：

```json
{
  "roster": {
    "teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher",
                 "persona": "善于用追问推进推导，讲题时先让学生猜再纠正。",
                 "avatar": "", "color": "#3b82f6", "priority": 10},
    "classmates": [
      {"id": "student_1", "name": "小舟", "role": "student",
       "persona": "好奇心强，常问“为什么”，但计算容易粗心。",
       "avatar": "", "color": "#10b981", "priority": 5},
      {"id": "student_2", "name": "小雨", "role": "student",
       "persona": "喜欢先记结论，需要老师带着看推导过程。",
       "avatar": "", "color": "#f59e0b", "priority": 5}
    ]
  },
  "agents": [
    {"id": "teacher_1", "name": "林老师", "role": "teacher", "persona": "…",
     "avatar": "", "color": "#3b82f6", "priority": 10},
    {"id": "student_1", "name": "小舟", "role": "student", "persona": "…",
     "avatar": "", "color": "#10b981", "priority": 5}
  ],
  "has_roster": true,
  "prompt_version": "classroom-multiagent-2026-11-21-v1",
  "generated_at": "2026-11-20T18:16:22.693858+00:00",
  "limits": {"agents_max": 6, "classmates_max": 5, "name_max": 24, "persona_max": 400},
  "roles": [{"id": "teacher", "label": "老师"}, {"id": "assistant", "label": "助教"},
            {"id": "student", "label": "同学"}]
}
```

### 2.2 `POST /api/classroom/courses/{course_id}/agents/generate`

请求（字段都可省略）：

```json
{
  "provider_id": 1,
  "model": "your-model",
  "requirements": "重点讲清原料、产物与影响因素；语言适合初中生。",
  "classmate_count": 3
}
```

- `provider_id` + `model` 不传时使用用户第一条带模型的 AI 连接（与 tutor 同策略：默认连接）；
  只传 `provider_id` 不传 `model` → **422**。
- `requirements` 缺省用课程 `requirements`；`classmate_count` 1-5，默认 3。
- 生成覆盖已存名册（`config_json.agents`）。

响应（在 GET 字段基础上追加调用摘要）：

```json
{
  "roster": {"teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher",
                          "persona": "善于用追问推进推导，讲题时先让学生猜再纠正。",
                          "avatar": "", "color": "#3b82f6", "priority": 10},
             "classmates": [{"id": "student_1", "name": "小舟", "role": "student",
                              "persona": "好奇心强，常问“为什么”，但计算容易粗心。",
                              "avatar": "", "color": "#10b981", "priority": 5}]},
  "agents": [],
  "has_roster": true,
  "prompt_version": "classroom-multiagent-2026-11-21-v1",
  "generated_at": "2026-11-20T18:16:22.693858+00:00",
  "limits": {"agents_max": 6, "classmates_max": 5, "name_max": 24, "persona_max": 400},
  "roles": [],
  "usage": {"prompt_tokens": 880, "completion_tokens": 260, "total_tokens": 1140},
  "model": "mock-model",
  "provider_name": "示例连接",
  "warnings": []
}
```

错误：`404` 课程不存在/非本人；`422` 未配置连接、`provider_id`/`model` 不匹配、课程无主题等；
`503` 模型不可用或两次输出都无法通过名册校验（如 0 或 2 名老师、缺 persona）。

### 2.3 `PATCH /api/classroom/courses/{course_id}/agents`

三种用法（`clear` 优先于其它字段；`roster` 与局部编辑二选一、`roster` 优先）：

```jsonc
// A. 完整替换（编辑器保存整份名册；严格校验，同生成）
{"roster": {"teacher": {"id": "teacher_1", "name": "林老师", "persona": "…"},
            "classmates": [{"id": "student_1", "name": "小舟", "persona": "…"}]}}

// B. 局部编辑（按 id 改名字/人设/颜色/优先级；传 null 的字段不改）
{"teacher": {"id": "teacher_1", "name": "王老师"},
 "classmates": [{"id": "student_1", "persona": "好奇但更细心，喜欢先画图再算。"}]}

// C. 清空名册
{"clear": true}
```

响应与 GET 相同（追加 `warnings`）；`B` 若 id 不在名册 → **422**「角色名册中没有该角色」。
**老师只能有 1 名**：完整替换里出现 2 名 teacher / `classmates` 里出现 `role:"teacher"` 都会 422。

## 3. `POST /api/classroom/sessions/{session_id}/discussion`

请求：

```json
{
  "scene_id": 1,
  "mode": "discussion",
  "topic": "光合作用的原料与产物",
  "rounds": 2,
  "user_message": "老师，这页主要讲什么？",
  "provider_id": 1,
  "model": "your-model"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `scene_id` | 否 | 缺省用会话当前场景；显式指定必须属于本课程（否则 404） |
| `mode` | 否 | `discussion`(多角色讨论，可含圆桌辩论，默认) / `qa`(自由问答) / `lecture`(多角色讲授) |
| `topic` | 否 | 本次讨论主题（≤200 字），缺省沿用场景主题 |
| `rounds` | 否 | 讨论轮次 1-3，默认 1（影响建议段数：约 `1+2*rounds`，总上限 6 段） |
| `user_message` | 否 | 学生本轮发言；给了就先写一条用户 turn |
| `history` | 否 | 自定义最近对话（≤12 条，`[{role,speaker,content}]`）；缺省自动读最近 8 条 `cls_turns` |
| `provider_id`/`model` | 否 | 模型连接；都不传用默认连接 |

响应（同步，一次模型调用；`turns` 为 1-6 段）：

```json
{
  "turns": [
    {
      "speaker_id": "teacher_1",
      "speaker_name": "林老师",
      "role": "teacher",
      "color": "#3b82f6",
      "text": "这一页我们解决两个问题：光合作用的原料是什么，产物又去了哪里。先别急着记结论，看这页的元素。",
      "actions": [
        {"id": "act_001", "type": "spotlight", "element_id": "text_001", "dim_opacity": 0.5}
      ]
    },
    {
      "speaker_id": "student_1",
      "speaker_name": "小舟",
      "role": "student",
      "color": "#10b981",
      "text": "老师，为什么水也算原料？我一直以为只需要二氧化碳。",
      "actions": []
    },
    {
      "speaker_id": "teacher_1",
      "speaker_name": "林老师",
      "role": "teacher",
      "color": "#3b82f6",
      "text": "问得好。水在光反应里被分解，提供氢和电子，所以它确实是原料。我们用白板把这条链条画出来。",
      "actions": [
        {
          "id": "act_001",
          "type": "whiteboard",
          "coord_space": "unit",
          "ops": [
            {"kind": "rect", "x": 0.06, "y": 0.18, "width": 0.22, "height": 0.15,
             "fill": "transparent", "stroke": "#5b9bd5"},
            {"kind": "text", "x": 0.08, "y": 0.23, "content": "光反应", "font_size": 20, "color": "#333333"},
            {"kind": "arrow", "x1": 0.28, "y1": 0.26, "x2": 0.38, "y2": 0.26, "color": "#333333", "width": 2},
            {"kind": "formula", "x": 0.06, "y": 0.42, "width": 0.4,
             "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2", "color": "#1f6feb"}
          ]
        }
      ]
    },
    {
      "speaker_id": "student_2",
      "speaker_name": "小雨",
      "role": "student",
      "color": "#f59e0b",
      "text": "那氧气是从水来的，还是从二氧化碳来的？",
      "actions": []
    },
    {
      "speaker_id": "teacher_1",
      "speaker_name": "林老师",
      "role": "teacher",
      "color": "#3b82f6",
      "text": "氧气来自水的分解，这一点和植物释放氧气是一致的。",
      "actions": [
        {"id": "act_001", "type": "laser", "element_id": "latex_001", "color": "#ff0000"}
      ]
    }
  ],
  "scene_hint": {"index": 1, "reason": "下一页会用曲线图讲影响光合速率的因素"},
  "mode": "discussion",
  "rounds": 2,
  "usage": {"prompt_tokens": 880, "completion_tokens": 260, "total_tokens": 1140},
  "model": "mock-model",
  "provider_name": "示例连接",
  "turn_ids": [2, 3, 4, 5, 6],
  "user_turn_id": 1,
  "warnings": [],
  "prompt_version": "classroom-multiagent-2026-11-21-v1"
}
```

约定：

- `turns` 顺序即播放顺序；`actions` 与该段台词同步执行（`id` 只在单段内唯一，重新编号 `act_001...`）。
- `scene_hint` 可为 `null`；`{"index": n}` 命中时服务端**已**把会话 `current_scene_id` 切到第 n+1 页
  （`index` 从 0 开始），前端刷新会话即可。`next_scene`/`show_slide` 动作由播放器就地执行。
- `user_turn_id` 为 `null` 表示本次没有学生发言。
- 所有 `turns` 已写入 `cls_turns`（`turn_ids` 对应行 id），可回放/续聊。

### 3.1 客户端可回放的「多角色脚本」最小样例

```json
{
  "mode": "qa",
  "turns": [
    {"speaker_id": "teacher_1", "text": "水是光合作用的原料，因为水在光反应中被分解。", "actions": []},
    {"speaker_id": "student_2", "text": "那氧气呢？", "actions": []}
  ],
  "scene_hint": null
}
```

> 模型原始输出只需 `{turns:[{speaker_id,text,actions}], scene_hint?}`；`speaker_name/role/color`
> 由服务端按名册回填（模型不能伪造名册外的角色，未知角色会触发一次自动重试）。

**错误**：`401` 未登录；`404` 会话/场景不存在或非本人；`422` 模式非法、无模型连接、未生成名册、
`rounds` 越界（Pydantic 1-3）、课程无场景；`503` 模型故障或两次输出的脚本都无法通过校验
（如角色不在名册、空台词、JSON 非法）。

## 4. `POST /api/classroom/sessions/{session_id}/interject`

学生插话后的即时多角色回应（**1-2 段**，第一段必须是老师/助教）。

请求：

```json
{"scene_id": 1, "message": "那如果光照不足会怎样？", "provider_id": 1, "model": "your-model"}
```

响应（结构同 discussion，`mode: "interject"`，`rounds` 字段省略）：

```json
{
  "turns": [
    {"speaker_id": "teacher_1", "speaker_name": "林老师", "role": "teacher", "color": "#3b82f6",
     "text": "你问到了关键：光照不足时，光反应先受限，后面的暗反应也就跟着慢下来。",
     "actions": [{"id": "act_001", "type": "spotlight", "element_id": "chart_001", "dim_opacity": 0.5}]},
    {"speaker_id": "student_3", "speaker_name": "阿哲", "role": "student", "color": "#ec4899",
     "text": "那温度过高呢？是先伤叶绿体吗？", "actions": []}
  ],
  "scene_hint": null,
  "mode": "interject",
  "usage": {"prompt_tokens": 880, "completion_tokens": 260, "total_tokens": 1140},
  "model": "mock-model",
  "provider_name": "示例连接",
  "turn_ids": [9, 10],
  "user_turn_id": 8,
  "warnings": [],
  "prompt_version": "classroom-multiagent-2026-11-21-v1"
}
```

规则：总是先把用户 `message` 写成一条 `cls_turns` 用户发言（`user_turn_id`），再写 1-2 段回应；
超过 2 段截断并给 `warnings`。错误码同 discussion。

## 5. 动作白名单（服务端校验后才会下发）

讨论/插话脚本允许的动作：

| type | 字段 | 说明 |
|---|---|---|
| `spotlight` | `element_id`, `dim_opacity`(0-1, 默认 0.5) | 聚光灯照当前页元素 |
| `laser` | `element_id`, `color`(默认 `#ff0000`) | 激光笔 |
| `highlight` | `element_id`, `color`(默认 `#ffd43b`) | 元素高亮框 |
| `whiteboard` | `coord_space:"unit"`, `ops:[…]`（≤40） | 白板绘制，见 §6 |
| `next_scene` | `scene_id` 或空 | 切下一场景 |
| `show_slide` | `index`（0 起）或 `scene_id` | 跳到指定场景 |
| `play_audio` | `asset_id` 或站内 `url` | 播放语音；讨论脚本中无素材可引用，模型被要求不要输出 |

- `element_id` 必须是当前场景摘要里列出的元素 id，否则该动作被丢弃（`warnings` 里说明）。
- 未知动作类型、`speech`（台词请写 `text`）、外网音频 URL 一律拒绝/丢弃。
- 单段发言最多 6 个动作（超出截断并告警）；讨论脚本整体最多 6 段。

## 6. 白板 `ops` 规范（0-1 归一化坐标）

`{"type":"whiteboard","coord_space":"unit","ops":[…]}`
ops ≤ **40**；坐标/尺寸都取 **0-1**（越界收敛到端点）；不支持的类型/非法字段的 op 直接丢弃。

| kind | 字段（* 必填） | 说明 |
|---|---|---|
| `text` | `x*`,`y*`,`content*`(≤200 字),`font_size`(10-72, 默认 18),`color` | 白板文字（纯文本，渲染时勿用 innerHTML 拼接不可信内容） |
| `formula` | `x*`,`y*`,`latex*`(≤300 字),`width`(0-1, 默认 0.3),`color` | KaTeX 公式（源可带 `$`，服务端会去掉） |
| `line` | `x1*`,`y1*`,`x2*`,`y2*`,`color`,`width`(1-8 描边, 默认 2) | 直线 |
| `arrow` | 同 `line` | 带箭头连线（从 `(x1,y1)` 指向 `(x2,y2)`） |
| `rect` | `x*`,`y*`,`width*`,`height*`,`fill`(默认 transparent),`stroke`(默认 #5b9bd5) | 矩形框（`width/height` 为 0-1 尺寸） |
| `circle` | 同 `rect` | 圆/椭圆（`x/y` 为外接框左上角，`width/height` 为 0-1 尺寸） |
| `path` | `x*`,`y*`,`width*`,`height*`,`d*`（SVG path，≤600 字符，仅 `Mm Ll Hh Vv Cc Ss Qq Tt Aa Zz` + 数字/空格/`.`/`,`/`+`/`-`/`e`） | 自定义曲线/形状；`view_box:[w,h]`（局部坐标，默认 `[1,1]`），`fill`(默认 transparent),`stroke`(默认 #333333),`stroke_width`(1-8, 默认 2) |

渲染换算：`px = 归一化值 × 画布尺寸`（画布 `1000 × 562.5`）。`path` 的 `d` 用局部坐标，
元素盒子为 `x*W, y*H, width*W, height*H`，`viewBox` 用 `view_box`。

## 7. 示例白板动作序列（可直接作为 `whiteboard` 动作下发）

来源：`app.classroom.multiagent.WHITEBOARD_EXAMPLES`（`derivation`/`flow`/`formula`），
已通过服务端同款校验。

### 7.1 做题推导（已知 → 公式 → 结论）

```json
{"type": "whiteboard", "coord_space": "unit", "ops": [
  {"kind": "text", "x": 0.06, "y": 0.08, "content": "已知：半径 r = 3", "font_size": 22, "color": "#333333"},
  {"kind": "formula", "x": 0.06, "y": 0.22, "width": 0.32, "latex": "C = 2\\pi r", "color": "#1f6feb"},
  {"kind": "arrow", "x1": 0.06, "y1": 0.36, "x2": 0.06, "y2": 0.46, "color": "#333333", "width": 2},
  {"kind": "text", "x": 0.06, "y": 0.5, "content": "代入：C = 2 × 3.14 × 3 = 18.84", "font_size": 20, "color": "#2f9e44"},
  {"kind": "line", "x1": 0.06, "y1": 0.66, "x2": 0.4, "y2": 0.66, "color": "#e8590c", "width": 2},
  {"kind": "text", "x": 0.06, "y": 0.7, "content": "答：周长约 18.84", "font_size": 24, "color": "#c2255c"}
]}
```

### 7.2 流程图（三步流程 + 箭头）

```json
{"type": "whiteboard", "coord_space": "unit", "ops": [
  {"kind": "rect", "x": 0.06, "y": 0.18, "width": 0.22, "height": 0.16, "fill": "transparent", "stroke": "#5b9bd5"},
  {"kind": "text", "x": 0.09, "y": 0.24, "content": "光反应", "font_size": 20, "color": "#333333"},
  {"kind": "arrow", "x1": 0.28, "y1": 0.26, "x2": 0.38, "y2": 0.26, "color": "#333333", "width": 2},
  {"kind": "rect", "x": 0.38, "y": 0.18, "width": 0.22, "height": 0.16, "fill": "transparent", "stroke": "#5b9bd5"},
  {"kind": "text", "x": 0.41, "y": 0.24, "content": "ATP / NADPH", "font_size": 18, "color": "#333333"},
  {"kind": "arrow", "x1": 0.6, "y1": 0.26, "x2": 0.7, "y2": 0.26, "color": "#333333", "width": 2},
  {"kind": "rect", "x": 0.7, "y": 0.18, "width": 0.22, "height": 0.16, "fill": "transparent", "stroke": "#5b9bd5"},
  {"kind": "text", "x": 0.73, "y": 0.24, "content": "暗反应", "font_size": 20, "color": "#333333"}
]}
```

### 7.3 公式推导（多行公式 + 结论）

```json
{"type": "whiteboard", "coord_space": "unit", "ops": [
  {"kind": "text", "x": 0.06, "y": 0.08, "content": "由勾股定理：", "font_size": 20, "color": "#333333"},
  {"kind": "formula", "x": 0.06, "y": 0.2, "width": 0.5, "latex": "a^2 + b^2 = c^2", "color": "#1f6feb"},
  {"kind": "formula", "x": 0.06, "y": 0.36, "width": 0.6, "latex": "c = \\sqrt{a^2 + b^2}", "color": "#1f6feb"},
  {"kind": "path", "x": 0.72, "y": 0.2, "width": 0.18, "height": 0.18,
   "d": "M0 1 L1 0 M1 0 L0.8 0 M1 0 L1 0.2", "view_box": [1, 1],
   "fill": "transparent", "stroke": "#e8590c", "stroke_width": 2}
]}
```

## 8. 前端播放器接入要点

1. **名册 UI**：进课堂前调用 `GET /courses/{id}/agents`；`has_roster:false` 时显示「生成课堂角色」
   按钮（`POST …/agents/generate`，`provider_id`/`model` 从模型设置取，未配置则按钮引导去配置）。
   生成中按钮 loading；失败显示 503 的中文 `detail` 与重试。生成后展示老师+同学卡片（姓名/人设/配色）。
2. **讨论入口**：播放器暂停在任意场景时提供「讨论」「问答深度」「讲授」入口；调用
   `POST /sessions/{id}/discussion` 后**按 `turns` 顺序**渲染气泡并逐段播放：
   台词（`speaker_name` + `color` + `role` 标签，`role==="user"` 不出现）→ 该段 `actions`。
   播放期间禁用重复提交；可提供「停止播放」（纯前端停队列，不必取消请求）。
3. **动作执行**：`spotlight/laser/highlight` 按 `element_id` 在当前场景画布定位（沿用既有播放器
   的 effectBox 逻辑）；`whiteboard` 若 `coord_space === "unit"`，先换算
   `x*1000, y*562.5, width*1000, height*562.5` 再绘制；否则按既有像素逻辑。`next_scene`/
   `show_slide` 走既有切场景逻辑；`play_audio` 忽略或按既有 `<audio>` 播放。
4. **插话**：讨论/播放中提供输入框（或 ASR 文本）；提交 `POST /sessions/{id}/interject`
   后把 `message` 先以用户气泡上屏，再顺序播放 1-2 段回应。请求 token 较大（最长 2.5k 输出），
   建议按钮 loading + 15-120s 兜底超时（超时给「模型响应慢，可稍后重试」）。
5. **回放**：`GET /sessions/{id}` 的 `turns` 已含 `role/speaker/content/actions`；按 `id` 升序
   渲染即可回放历史（最近 20 条）。名册配色用 `GET /agents` 的 `agents[].color`。
6. **TTS（可选）**：按 `speaker_id` 映射到 `AGENT_COLOR_PALETTE` 对应的音色占位（P1 未接
   角色级语音，可先用浏览器 TTS 按 `speaker_name` 区分语速/音高，或在 `speechSynthesis` 里
   为 teacher/student 设不同 pitch），后续接 `POST /courses/{id}/tts`。
7. **空状态/错误**：无名册 → 引导生成；无模型连接 → 422 中文引导去「模型设置」；503 → 提示重试；
   404（他人会话/场景不属于本课程）→ 回课程列表。不要在前端伪造角色台词或动作。

## 9. 上限与错误码

| 项 | 值 | 来源 |
|---|---|---|
| 角色总数 / 同学数 | 6 / 5 | `MAX_AGENTS` / `MAX_CLASSMATES` |
| 角色名 / 人设 | 24 / 400 字 | `MAX_AGENT_NAME_CHARS` / `MAX_AGENT_PERSONA_CHARS` |
| 讨论段数 / 插话段数 | 6 / 2 | `MAX_DISCUSSION_TURNS` / `MAX_INTERJECT_TURNS` |
| 讨论轮次 | 1-3 | `MIN_ROUNDS` / `MAX_ROUNDS` |
| 单段台词 | 500 字（超出截断） | `MAX_TURN_TEXT_CHARS` |
| 单段动作 / 白板 ops | 6 / 40 | `MAX_ACTIONS_PER_TURN` / `MAX_WHITEBOARD_OPS` |
| 白板 text / formula / path | 200 / 300 / 600 字符 | `MAX_WB_*` |
| 同步调用 token 上限 | 名册 1500 / 讨论 2500 / 插话 1500 | 见 `api.py` |

| 状态码 | 场景 |
|---|---|
| 401 | 未登录 |
| 404 | 课程/会话/场景不存在或非本人（不泄露存在性） |
| 422 | 参数非法、无模型连接、未生成名册、课程无场景、名册校验失败 |
| 503 | 模型调用失败 / 两次输出都无法通过严格 JSON 校验（不会写入半成品 `cls_turns`） |

## 10. 本版本范围与遗留

- **已交付**：名册生成/编辑/读取；同步多角色讨论（discussion/qa/lecture）；学生插话；增强白板
  （7 种 op、0-1 坐标、ops/文字/path 上限）；动作白名单与元素归属校验；脚本写入 `cls_turns`；
  名册与 prompt 版本落库可追溯；`/meta` 增量暴露多智能体能力与上限。
  （task-19 起：`course` 生成任务会自动补一次名册、ZIP 导出/导入携带名册，见 §11。）
- **未做（后续 P2）**：
  - 未做 SSE / 逐段流式与任务化（同步接口已满足播放需求；如需长脚本再走 `ai_jobs` + SSE）；
  - 未做服务端白板账本（AOF 权威白板）与角色级 TTS 音色绑定；
  - 多智能体脚本属会话运行期数据，不进课程 DSL；导出 ZIP 只携带**名册**，不携带讨论历史。
- **向后兼容**：不改既有接口字段、`cls_turns` 表结构、课程/场景 DSL；白板动作新增
  `coord_space` 字段仅出现在本模块产出的动作上，既有像素坐标动作不受影响。

## 11. 与生成管线 / 导出导入的交叉说明（task-19 增量）

完整说明见 `GENERATION-PLUS.md`；这里只列与多智能体直接相关的点：

1. **课程生成自动名册**：`course` 任务在场景生成前调用一次名册生成（`stage=all` 确认大纲后 /
   `stage=scenes`；`stage=outline` 与单场景 `regenerate` 不触发）。已有合法名册则跳过（幂等）；
   失败**不阻塞**课程，只写任务结果 `roster_generated=false` + `roster_warnings[]` + summary 提示，
   前端之后可照常调用 `POST /courses/{id}/agents/generate`。
2. **名册随 ZIP 迁移**：导出 `manifest.agents`（+`agentsPromptVersion`）与 `course.json.config.agents`；
   导入优先 manifest、退化 config，经 `normalize_agent_roster` 重新校验后恢复
   （`GET /courses/{id}/agents` 直接 `has_roster:true`）。
3. **错误码对照**：名册/讨论/插话之外的生成期错误按课程生成任务处理（任务 `failed`，中文 `error`）；
   本模块同步接口错误码见 §9 表。无可用模型连接 → 422；两次输出无法通过严格校验 → 503 且不落半成品。
4. **`/meta` 版本字段**：`prompt_version` 为课堂生成 prompt 版本（task-19 起
   `classroom-2026-11-21-v2`），`multiagent_prompt_version` 为本模块 prompt 版本；
   名册 `agents_meta.prompt_version` 记录生成时使用的多智能体 prompt 版本。
5. **白板坐标交叉提醒**：本模块讨论脚本的 `whiteboard.ops` 为 **0-1 归一化**且带
   `coord_space:"unit"`；课程生成管线写入场景 `meta.actions` 的白板动作仍是**像素**坐标、无该字段。
   前端按 `coord_space` 区分两套坐标，不要混用。
