# AI 课堂 API 实际响应样例（由真实实现 + mock 模型跑出，供前端对接）

> 说明：样例由 `TestClient` + 假模型在隔离数据目录跑出，未联网。任务完成结果里还带
> `classroom_prompt_version`（本模块 prompt 版本）与 `calls`（模型调用/usage 摘要，均已脱敏）；
> 课程 `description` 若为空，会在确认大纲后自动补全为大纲简介。
> 幻灯片画布固定 `1000 × 562.5`；所有 `left/top/width/height` 单位为画布像素。
>
> **版本与增量**：prompt 版本已升到 `classroom-2026-11-21-v2`（深度交互模式大纲分支 +
> slide 动手提示块 + 配图表单）。P1/P2 增量（多智能体名册/讨论/插话、深度交互模式、
> 课程任务自动名册、可选配图、白板 0-1 坐标动作）见文末
> 「P1/P2 增量接口与字段」一节，详细说明见 `MULTIAGENT-API.md`、`GENERATION-PLUS.md`；
> 全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。

## GET /api/classroom/meta

```json
{
  "enabled": true,
  "scene_types": [
    "slide",
    "quiz",
    "interactive",
    "pbl"
  ],
  "scene_type_labels": {
    "slide": "幻灯片",
    "quiz": "课堂测验",
    "interactive": "交互场景",
    "pbl": "项目式学习"
  },
  "modes": [
    "standard",
    "interactive"
  ],
  "mode_labels": {
    "standard": "标准课堂",
    "interactive": "深度交互"
  },
  "depths": [
    "brief",
    "standard",
    "deep"
  ],
  "widgets": [
    {
      "id": "simulation",
      "label": "模拟实验"
    },
    {
      "id": "mindmap",
      "label": "思维导图"
    },
    {
      "id": "game",
      "label": "互动游戏"
    },
    {
      "id": "code",
      "label": "在线编程"
    },
    {
      "id": "visualization3d",
      "label": "3D 可视化"
    }
  ],
  "tts": {
    "provider": null,
    "model": null
  },
  "agent_roles": [
    {"id": "teacher", "label": "老师"},
    {"id": "assistant", "label": "助教"},
    {"id": "student", "label": "同学"}
  ],
  "discussion_modes": [
    {"id": "discussion", "label": "多角色讨论"},
    {"id": "qa", "label": "自由问答"},
    {"id": "lecture", "label": "多角色讲授"}
  ],
  "interactive_mode": {
    "enabled": true,
    "mode": "interactive",
    "interactive_scenes_min": 1,
    "interactive_scenes_max": 2,
    "slide_hint": true
  },
  "images": {
    "enabled": false,
    "provider_id": null,
    "provider": null,
    "model": null,
    "max_per_scene": 2,
    "size": "1024x1024"
  },
  "limits": {
    "scene_count_min": 3,
    "scene_count_max": 12,
    "scene_count_default": 6,
    "questions_max": 12,
    "elements_max": 24,
    "actions_max": 40,
    "html_max_bytes": 2000000,
    "tts_text_max": 2000,
    "topic_max": 500,
    "requirements_max": 4000,
    "agents_max": 6,
    "classmates_max": 5,
    "agent_name_max": 24,
    "agent_persona_max": 400,
    "discussion_turns_max": 6,
    "interject_turns_max": 2,
    "discussion_rounds_max": 3,
    "discussion_text_max": 500,
    "whiteboard_ops_max": 40,
    "whiteboard_text_max": 200,
    "whiteboard_kinds": ["text", "formula", "line", "rect", "circle", "arrow", "path"],
    "interactive_scenes_max": 2,
    "images_per_scene_max": 2
  },
  "prompt_version": "classroom-2026-11-21-v2",
  "multiagent_prompt_version": "classroom-multiagent-2026-11-21-v1"
}
```

## POST /api/classroom/courses

```json
{
  "course": {
    "id": 1,
    "title": "光合作用入门",
    "topic": "光合作用",
    "subject": "生物",
    "grade": "初中二年级",
    "mode": "standard",
    "description": "",
    "requirements": "重点讲清原料、产物与影响因素；语言适合初中生。",
    "status": "draft",
    "scene_count": 0,
    "created_at": "2026-11-20T16:56:25.153580+00:00",
    "updated_at": "2026-11-20T16:56:25.153580+00:00",
    "outline": {},
    "config": {
      "document_ids": []
    },
    "cover": {}
  }
}
```

## GET /api/classroom/courses

```json
{
  "items": [
    {
      "id": 1,
      "title": "光合作用入门",
      "topic": "光合作用",
      "subject": "生物",
      "grade": "初中二年级",
      "mode": "standard",
      "status": "draft",
      "description": "",
      "scene_count": 0,
      "cover": {},
      "created_at": "2026-11-20T16:56:25.153580+00:00",
      "updated_at": "2026-11-20T16:56:25.153580+00:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

## POST /api/classroom/courses/{id}/generate

```json
{
  "job": {
    "id": 1,
    "user_id": 1,
    "task": "course",
    "status": "queued",
    "stage": "queued",
    "error": null,
    "created_at": "2026-11-20T16:56:25.197123+00:00",
    "updated_at": "2026-11-20T16:56:25.197123+00:00",
    "partial_content": "",
    "cancelled": 0,
    "cancel_requested_at": null,
    "finished_at": null,
    "heartbeat_at": null,
    "request": {
      "task": "course",
      "qid": null,
      "attempt_id": null,
      "document_id": null,
      "document_ids": null,
      "page_from": 1,
      "page_to": null,
      "upload_id": null,
      "provider_id": 1,
      "model": "test-model",
      "verify_provider_id": null,
      "verify_model": null,
      "judge_provider_id": null,
      "judge_model": null,
      "cross_verify": false,
      "structured_verify": false,
      "peer_review": false,
      "explain_mode": null,
      "modes": null,
      "template": "checklist",
      "prompt_config": null,
      "source_mode": "user_docs",
      "kp_ids": null,
      "subject": null,
      "confirm_outline": false,
      "map_extract": false,
      "tagging_limit": 50,
      "requirements": "重点讲清原料、产物与影响因素；语言适合初中生。",
      "params": {
        "course_id": 1,
        "stage": "all",
        "scene_types": [
          "slide",
          "interactive",
          "quiz"
        ],
        "scene_count": 4,
        "depth": "standard"
      }
    },
    "result": null,
    "has_checkpoint": false,
    "needs_attention": false
  },
  "course_id": 1
}
```

## GET /api/ai/jobs/{id}（awaiting_outline，两阶段第一阶段）

```json
{
  "id": 1,
  "user_id": 1,
  "task": "course",
  "status": "awaiting_outline",
  "stage": "awaiting_outline",
  "error": null,
  "created_at": "2026-11-20T16:56:25.197123+00:00",
  "updated_at": "2026-11-20T16:56:25.261073+00:00",
  "partial_content": "",
  "cancelled": 0,
  "cancel_requested_at": null,
  "finished_at": null,
  "heartbeat_at": "2026-11-20T16:56:25.211130+00:00",
  "request": {
    "task": "course",
    "qid": null,
    "attempt_id": null,
    "document_id": null,
    "document_ids": null,
    "page_from": 1,
    "page_to": null,
    "upload_id": null,
    "provider_id": 1,
    "model": "test-model",
    "verify_provider_id": null,
    "verify_model": null,
    "judge_provider_id": null,
    "judge_model": null,
    "cross_verify": false,
    "structured_verify": false,
    "peer_review": false,
    "explain_mode": null,
    "modes": null,
    "template": "checklist",
    "prompt_config": null,
    "source_mode": "user_docs",
    "kp_ids": null,
    "subject": null,
    "confirm_outline": false,
    "map_extract": false,
    "tagging_limit": 50,
    "requirements": "重点讲清原料、产物与影响因素；语言适合初中生。",
    "params": {
      "course_id": 1,
      "stage": "all",
      "scene_types": [
        "slide",
        "interactive",
        "quiz"
      ],
      "scene_count": 4,
      "depth": "standard"
    }
  },
  "result": {
    "course_id": 1,
    "course_title": "光合作用入门",
    "stage": "all",
    "outline": {
      "title": "光合作用入门",
      "description": "面向初中生物的光合作用原理与影响因素入门课。",
      "language_directive": "使用简体中文授课；术语首次出现时给出中文解释，专有名词保留英文。",
      "scenes": [
        {
          "id": "scene_1",
          "title": "为什么植物离不开光",
          "type": "slide",
          "description": "",
          "outline_points": [
            "生活现象导入",
            "光合作用的定义",
            "本节学习目标"
          ],
          "order": 1
        },
        {
          "id": "scene_2",
          "title": "原料与产物",
          "type": "slide",
          "description": "",
          "outline_points": [
            "反应物与生成物",
            "叶绿体的作用",
            "总反应式"
          ],
          "order": 2
        },
        {
          "id": "scene_3",
          "title": "光照强度模拟",
          "type": "interactive",
          "description": "",
          "outline_points": [
            "调节光照观察速率",
            "找到光饱和点"
          ],
          "widget": "simulation",
          "order": 3
        },
        {
          "id": "scene_4",
          "title": "课堂小测",
          "type": "quiz",
          "description": "",
          "outline_points": [
            "场所与原料",
            "影响因素"
          ],
          "quiz": {
            "question_count": 2,
            "difficulty": "medium"
          },
          "order": 4
        }
      ],
      "warnings": []
    },
    "notice": "大纲已生成，请核对/增删场景后点击「确认大纲并生成」。",
    "prompt_version": "classroom-2026-11-21-v2",
    "classroom_prompt_version": "classroom-2026-11-21-v2"
  },
  "has_checkpoint": true,
  "needs_attention": false
}
```

## POST /api/ai/jobs/{id}/confirm-outline（确认后任务重新排队）

```json
{
  "id": 1,
  "status": "queued",
  "stage": "resuming"
}
```

## GET /api/ai/jobs/{id}（completed 结果摘要）

```json
{
  "course_id": 1,
  "course_title": "光合作用入门",
  "scene_count": 4,
  "generated_scene_count": 4,
  "failed_scene_ids": [],
  "summary": "课程生成完成：4 个场景可用",
  "classroom_prompt_version": "classroom-2026-11-21-v2"
}
```

## GET /api/classroom/courses/{id}（完整样例课程 JSON：course + scenes）

```json
{
  "course": {
    "id": 1,
    "title": "光合作用入门",
    "topic": "光合作用",
    "subject": "生物",
    "grade": "初中二年级",
    "mode": "standard",
    "description": "面向初中生物的光合作用原理与影响因素入门课。",
    "requirements": "重点讲清原料、产物与影响因素；语言适合初中生。",
    "status": "ready",
    "scene_count": 4,
    "created_at": "2026-11-20T16:56:25.153580+00:00",
    "updated_at": "2026-11-20T16:56:25.513328+00:00",
    "outline": {
      "title": "光合作用入门",
      "description": "面向初中生物的光合作用原理与影响因素入门课。",
      "language_directive": "使用简体中文授课；术语首次出现时给出中文解释，专有名词保留英文。",
      "scenes": [
        {
          "id": "scene_1",
          "title": "为什么植物离不开光",
          "type": "slide",
          "description": "",
          "outline_points": [
            "生活现象导入",
            "光合作用的定义",
            "本节学习目标"
          ],
          "order": 1
        },
        {
          "id": "scene_2",
          "title": "原料与产物",
          "type": "slide",
          "description": "",
          "outline_points": [
            "反应物与生成物",
            "叶绿体的作用",
            "总反应式"
          ],
          "order": 2
        },
        {
          "id": "scene_3",
          "title": "光照强度模拟",
          "type": "interactive",
          "description": "",
          "outline_points": [
            "调节光照观察速率",
            "找到光饱和点"
          ],
          "widget": "simulation",
          "order": 3
        },
        {
          "id": "scene_4",
          "title": "课堂小测",
          "type": "quiz",
          "description": "",
          "outline_points": [
            "场所与原料",
            "影响因素"
          ],
          "quiz": {
            "question_count": 2,
            "difficulty": "medium"
          },
          "order": 4
        }
      ]
    },
    "config": {
      "document_ids": [],
      "depth": "standard",
      "scene_types": [
        "slide",
        "interactive",
        "quiz"
      ],
      "scene_count": 4,
      "prompt_version": "classroom-2026-11-21-v2"
    },
    "cover": {}
  },
  "scenes": [
    {
      "id": 1,
      "stage_id": "course_1",
      "course_id": 1,
      "title": "为什么植物离不开光",
      "type": "slide",
      "scene_type": "slide",
      "order": 1,
      "order_index": 1,
      "status": "ready",
      "content": {
        "type": "slide",
        "schemaVersion": 1,
        "canvas": {
          "width": 1000.0,
          "height": 562.5,
          "theme": {
            "backgroundColor": "#ffffff",
            "themeColors": [
              "#1f6feb",
              "#e8590c",
              "#2f9e44",
              "#7048e8",
              "#c2255c"
            ],
            "fontColor": "#222222",
            "fontName": "Microsoft YaHei"
          },
          "elements": [
            {
              "id": "title_001",
              "type": "text",
              "left": 60.0,
              "top": 50.0,
              "width": 880.0,
              "height": 76.0,
              "rotate": 0.0,
              "content": "<p style=\"font-size: 32px\"><strong>为什么植物离不开光</strong></p>",
              "text": "为什么植物离不开光",
              "fontSize": 32,
              "color": "#333333",
              "defaultFontName": "",
              "defaultColor": "#333333",
              "lineHeight": 1.5
            },
            {
              "id": "point_001",
              "type": "text",
              "left": 80.0,
              "top": 170.0,
              "width": 500.0,
              "height": 130.0,
              "rotate": 0.0,
              "content": "<p style=\"font-size: 20px\">• 植物把光能转化为化学能</p><p style=\"font-size: 20px\">• 原料：水、二氧化碳</p><p style=\"font-size: 20px\">• 场所：叶绿体</p>",
              "text": "• 植物把光能转化为化学能 • 原料：水、二氧化碳 • 场所：叶绿体",
              "fontSize": 20,
              "color": "#333333",
              "defaultFontName": "",
              "defaultColor": "#333333",
              "lineHeight": 1.5
            },
            {
              "id": "shape_001",
              "type": "shape",
              "left": 640.0,
              "top": 150.0,
              "width": 280.0,
              "height": 240.0,
              "rotate": 0.0,
              "path": "M 0 0 L 1 0 L 1 1 L 0 1 Z",
              "viewBox": [
                1.0,
                1.0
              ],
              "fill": "#e7f5ff",
              "fixedRatio": false
            },
            {
              "id": "latex_001",
              "type": "latex",
              "left": 660.0,
              "top": 250.0,
              "width": 240.0,
              "height": 80.0,
              "rotate": 0.0,
              "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2",
              "color": "#1f6feb",
              "align": "center"
            }
          ]
        }
      },
      "actions": [
        {
          "id": "act_001",
          "type": "spotlight",
          "element_id": "title_001",
          "dim_opacity": 0.5
        },
        {
          "id": "act_002",
          "type": "speech",
          "text": "我们先从生活现象出发：为什么植物离开光就长不好？"
        },
        {
          "id": "act_003",
          "type": "spotlight",
          "element_id": "point_001",
          "dim_opacity": 0.5
        },
        {
          "id": "act_004",
          "type": "speech",
          "text": "植物把光能转化成化学能，原料是水和二氧化碳，场所是叶绿体。"
        },
        {
          "id": "act_005",
          "type": "laser",
          "element_id": "latex_001",
          "color": "#ff0000"
        },
        {
          "id": "act_006",
          "type": "speech",
          "text": "这是总反应式，二氧化碳和水在光照与叶绿体作用下生成有机物并释放氧气。"
        }
      ],
      "meta": {
        "actions": [
          {
            "id": "act_001",
            "type": "spotlight",
            "element_id": "title_001",
            "dim_opacity": 0.5
          },
          {
            "id": "act_002",
            "type": "speech",
            "text": "我们先从生活现象出发：为什么植物离开光就长不好？"
          },
          {
            "id": "act_003",
            "type": "spotlight",
            "element_id": "point_001",
            "dim_opacity": 0.5
          },
          {
            "id": "act_004",
            "type": "speech",
            "text": "植物把光能转化成化学能，原料是水和二氧化碳，场所是叶绿体。"
          },
          {
            "id": "act_005",
            "type": "laser",
            "element_id": "latex_001",
            "color": "#ff0000"
          },
          {
            "id": "act_006",
            "type": "speech",
            "text": "这是总反应式，二氧化碳和水在光照与叶绿体作用下生成有机物并释放氧气。"
          }
        ],
        "warnings": [],
        "placeholder": false,
        "requires_generation": false,
        "prompt_version": "classroom-2026-11-21-v2",
        "model": "test-model",
        "position": 1,
        "total": 4,
        "digest": "场景《为什么植物离不开光》（幻灯片）\n要点：生活现象导入；光合作用的定义；本节学习目标\n- 元素 title_001（text）：为什么植物离不开光\n- 元素 point_001（text）：• 植物把光能转化为化学能 • 原料：水、二氧化碳 • 场所：叶绿体\n- 元素 latex_001（latex）：6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2"
      },
      "created_at": "2026-11-20T16:56:25.368813+00:00",
      "updated_at": "2026-11-20T16:56:25.378320+00:00"
    },
    {
      "id": 2,
      "stage_id": "course_1",
      "course_id": 1,
      "title": "原料与产物",
      "type": "slide",
      "scene_type": "slide",
      "order": 2,
      "order_index": 2,
      "status": "ready",
      "content": {
        "type": "slide",
        "schemaVersion": 1,
        "canvas": {
          "width": 1000.0,
          "height": 562.5,
          "theme": {
            "backgroundColor": "#ffffff",
            "themeColors": [
              "#1f6feb",
              "#e8590c",
              "#2f9e44",
              "#7048e8",
              "#c2255c"
            ],
            "fontColor": "#222222",
            "fontName": "Microsoft YaHei"
          },
          "elements": [
            {
              "id": "title_002",
              "type": "text",
              "left": 60.0,
              "top": 50.0,
              "width": 880.0,
              "height": 58.0,
              "rotate": 0.0,
              "content": "<p style=\"font-size: 28px\"><strong>原料与产物</strong></p>",
              "text": "原料与产物",
              "fontSize": 28,
              "color": "#333333",
              "defaultFontName": "",
              "defaultColor": "#333333",
              "lineHeight": 1.5
            },
            {
              "id": "table_001",
              "type": "table",
              "left": 70.0,
              "top": 150.0,
              "width": 560.0,
              "height": 200.0,
              "rotate": 0.0,
              "colWidths": [
                0.34,
                0.33,
                0.33
              ],
              "data": [
                [
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "类别"
                  },
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "物质"
                  },
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "说明"
                  }
                ],
                [
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "原料"
                  },
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "水、二氧化碳"
                  },
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "叶绿体吸收"
                  }
                ],
                [
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "产物"
                  },
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "有机物、氧气"
                  },
                  {
                    "id": null,
                    "colspan": 1,
                    "rowspan": 1,
                    "text": "释放到环境"
                  }
                ]
              ],
              "outline": {
                "width": 1,
                "style": "solid",
                "color": "#cccccc"
              }
            },
            {
              "id": "chart_001",
              "type": "chart",
              "left": 660.0,
              "top": 150.0,
              "width": 280.0,
              "height": 200.0,
              "rotate": 0.0,
              "chartType": "line",
              "data": {
                "labels": [
                  "弱光",
                  "中光",
                  "强光"
                ],
                "legends": [
                  "光合速率"
                ],
                "series": [
                  [
                    12.0,
                    28.0,
                    36.0
                  ]
                ]
              },
              "themeColors": [
                "#2f9e44",
                "#e8590c"
              ]
            }
          ]
        }
      },
      "actions": [
        {
          "id": "act_002",
          "type": "speech",
          "text": "我们先从生活现象出发：为什么植物离开光就长不好？"
        },
        {
          "id": "act_004",
          "type": "speech",
          "text": "植物把光能转化成化学能，原料是水和二氧化碳，场所是叶绿体。"
        },
        {
          "id": "act_006",
          "type": "speech",
          "text": "这是总反应式，二氧化碳和水在光照与叶绿体作用下生成有机物并释放氧气。"
        }
      ],
      "meta": {
        "actions": [
          {
            "id": "act_002",
            "type": "speech",
            "text": "我们先从生活现象出发：为什么植物离开光就长不好？"
          },
          {
            "id": "act_004",
            "type": "speech",
            "text": "植物把光能转化成化学能，原料是水和二氧化碳，场所是叶绿体。"
          },
          {
            "id": "act_006",
            "type": "speech",
            "text": "这是总反应式，二氧化碳和水在光照与叶绿体作用下生成有机物并释放氧气。"
          }
        ],
        "warnings": [
          "spotlight 动作引用了不存在的元素 title_001，已丢弃",
          "spotlight 动作引用了不存在的元素 point_001，已丢弃",
          "laser 动作引用了不存在的元素 latex_001，已丢弃"
        ],
        "placeholder": false,
        "requires_generation": false,
        "prompt_version": "classroom-2026-11-21-v2",
        "model": "test-model",
        "position": 2,
        "total": 4,
        "digest": "场景《原料与产物》（幻灯片）\n要点：反应物与生成物；叶绿体的作用；总反应式\n- 元素 title_002（text）：原料与产物"
      },
      "created_at": "2026-11-20T16:56:25.409320+00:00",
      "updated_at": "2026-11-20T16:56:25.421320+00:00"
    },
    {
      "id": 3,
      "stage_id": "course_1",
      "course_id": 1,
      "title": "光照强度模拟",
      "type": "interactive",
      "scene_type": "interactive",
      "order": 3,
      "order_index": 3,
      "status": "ready",
      "content": {
        "type": "interactive",
        "widget": "simulation",
        "html": "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>光照强度模拟</title><style>html,body{margin:0;height:100%;font-family:system-ui,sans-serif;}body{display:flex;align-items:center;justify-content:center;background:#f8fafc;color:#334155;}.box{max-width:520px;padding:28px;border:1px dashed #94a3b8;border-radius:12px;background:#fff;}.badge{display:inline-block;padding:2px 10px;border-radius:999px;background:#e2e8f0;font-size:12px;}h1{font-size:20px;margin:12px 0 8px;}p{line-height:1.7;margin:6px 0;font-size:14px;}</style></head><body><div id=\"experiment\" class=\"box\"><span class=\"badge\">占位 · 待生成</span><h1>光照强度模拟</h1><p>交互类型：模拟实验</p><p>该交互场景的教学内容尚未生成：P0 版本先提供可由播放器加载的占位页，后续会按场景大纲生成自包含的模拟/可视化内容，不会伪造互动结果。</p></div></body></html>",
        "height": 480
      },
      "actions": [
        {
          "id": "act_001",
          "type": "speech",
          "text": "这一页是一个交互场景：光照强度模拟。请在页面里动手操作，观察变化后再回答下面的问题。"
        }
      ],
      "meta": {
        "actions": [
          {
            "id": "act_001",
            "type": "speech",
            "text": "这一页是一个交互场景：光照强度模拟。请在页面里动手操作，观察变化后再回答下面的问题。"
          }
        ],
        "warnings": [
          "该场景内容需要后续版本生成"
        ],
        "placeholder": true,
        "requires_generation": true,
        "prompt_version": "classroom-2026-11-21-v2",
        "model": "test-model",
        "position": 3,
        "total": 4,
        "digest": "场景《光照强度模拟》（交互场景）\n要点：调节光照观察速率；找到光饱和点\n交互类型：simulation；模拟实验"
      },
      "created_at": "2026-11-20T16:56:25.447323+00:00",
      "updated_at": "2026-11-20T16:56:25.455420+00:00"
    },
    {
      "id": 4,
      "stage_id": "course_1",
      "course_id": 1,
      "title": "课堂小测",
      "type": "quiz",
      "scene_type": "quiz",
      "order": 4,
      "order_index": 4,
      "status": "ready",
      "content": {
        "type": "quiz",
        "questions": [
          {
            "id": "q1",
            "type": "single",
            "question": "光合作用发生的场所是？",
            "options": [
              {
                "label": "叶绿体",
                "value": "A"
              },
              {
                "label": "线粒体",
                "value": "B"
              },
              {
                "label": "细胞核",
                "value": "C"
              },
              {
                "label": "液泡",
                "value": "D"
              }
            ],
            "answer": [
              "A"
            ],
            "analysis": "叶绿体含有叶绿素，是光合作用的场所。",
            "points": 10.0,
            "hasAnswer": true
          },
          {
            "id": "q2",
            "type": "multiple",
            "question": "光合作用的原料包括哪些？（多选）",
            "options": [
              {
                "label": "水",
                "value": "A"
              },
              {
                "label": "二氧化碳",
                "value": "B"
              },
              {
                "label": "氧气",
                "value": "C"
              },
              {
                "label": "有机物",
                "value": "D"
              }
            ],
            "answer": [
              "A",
              "B"
            ],
            "analysis": "水和二氧化碳是原料，氧气和有机物是产物。",
            "points": 10.0,
            "hasAnswer": true
          }
        ]
      },
      "actions": [
        {
          "id": "act_001",
          "type": "speech",
          "text": "接下来我们做一组课堂小测：课堂小测。请先独立作答，提交后我们一起看解析。"
        }
      ],
      "meta": {
        "actions": [
          {
            "id": "act_001",
            "type": "speech",
            "text": "接下来我们做一组课堂小测：课堂小测。请先独立作答，提交后我们一起看解析。"
          }
        ],
        "warnings": [],
        "placeholder": false,
        "requires_generation": false,
        "prompt_version": "classroom-2026-11-21-v2",
        "model": "test-model",
        "position": 4,
        "total": 4,
        "digest": "场景《课堂小测》（课堂测验）\n要点：场所与原料；影响因素\n第 1 题：光合作用发生的场所是？\n第 2 题：光合作用的原料包括哪些？（多选）"
      },
      "created_at": "2026-11-20T16:56:25.487103+00:00",
      "updated_at": "2026-11-20T16:56:25.494616+00:00"
    }
  ]
}
```

## POST /api/classroom/courses/{id}/sessions

```json
{
  "session": {
    "id": 1,
    "course_id": 1,
    "current_scene_id": 1,
    "created_at": "2026-11-20T16:56:25.570847+00:00",
    "updated_at": "2026-11-20T16:56:25.570847+00:00",
    "progress": {}
  }
}
```

## PATCH /api/classroom/sessions/{id}

```json
{
  "session": {
    "id": 1,
    "course_id": 1,
    "current_scene_id": 1,
    "created_at": "2026-11-20T16:56:25.570847+00:00",
    "updated_at": "2026-11-20T16:56:25.697185+00:00",
    "progress": {
      "scene_index": 0,
      "action_index": 2,
      "consumed": []
    }
  }
}
```

## GET /api/classroom/sessions/{id}

```json
{
  "session": {
    "id": 1,
    "course_id": 1,
    "current_scene_id": 1,
    "created_at": "2026-11-20T16:56:25.570847+00:00",
    "updated_at": "2026-11-20T16:56:25.697185+00:00",
    "progress": {
      "scene_index": 0,
      "action_index": 2,
      "consumed": []
    }
  },
  "course": {
    "id": 1,
    "title": "光合作用入门",
    "topic": "光合作用",
    "subject": "生物",
    "grade": "初中二年级",
    "mode": "standard",
    "status": "ready",
    "description": "面向初中生物的光合作用原理与影响因素入门课。",
    "scene_count": 4,
    "cover": {},
    "created_at": "2026-11-20T16:56:25.153580+00:00",
    "updated_at": "2026-11-20T16:56:25.513328+00:00"
  },
  "progress": {
    "scene_index": 0,
    "action_index": 2,
    "consumed": []
  },
  "scenes": [
    {
      "id": 1,
      "title": "为什么植物离不开光",
      "type": "slide",
      "order": 1,
      "status": "ready",
      "has_actions": true,
      "placeholder": false
    },
    {
      "id": 2,
      "title": "原料与产物",
      "type": "slide",
      "order": 2,
      "status": "ready",
      "has_actions": true,
      "placeholder": false
    },
    {
      "id": 3,
      "title": "光照强度模拟",
      "type": "interactive",
      "order": 3,
      "status": "ready",
      "has_actions": true,
      "placeholder": true
    },
    {
      "id": 4,
      "title": "课堂小测",
      "type": "quiz",
      "order": 4,
      "status": "ready",
      "has_actions": true,
      "placeholder": false
    }
  ],
  "turns": []
}
```

## POST /api/classroom/sessions/{id}/tutor

```json
{
  "reply": {
    "speaker": "teacher",
    "text": "叶绿体是光合作用发生的场所；水是原料之一，在光照下被分解。",
    "actions": [
      {
        "id": "act_001",
        "type": "spotlight",
        "element_id": "point_001",
        "dim_opacity": 0.5
      }
    ]
  },
  "scene_hint": null,
  "usage": {
    "prompt_tokens": 900,
    "completion_tokens": 320
  },
  "model": "test-model",
  "provider_name": "mock",
  "turn_id": 2,
  "warnings": []
}
```

## POST /api/classroom/sessions/{id}/quiz

```json
{
  "attempt_id": 1,
  "scene_id": 4,
  "score": 10.0,
  "max_score": 20.0,
  "correct_count": 1,
  "question_count": 2,
  "needs_review_count": 0,
  "detail": [
    {
      "question_id": "q1",
      "type": "single",
      "question": "光合作用发生的场所是？",
      "given": [
        "A"
      ],
      "correct": true,
      "needs_review": false,
      "correct_answer": [
        "A"
      ],
      "points": 10.0,
      "earned": 10.0,
      "analysis": "叶绿体含有叶绿素，是光合作用的场所。"
    },
    {
      "question_id": "q2",
      "type": "multiple",
      "question": "光合作用的原料包括哪些？（多选）",
      "given": [
        "A"
      ],
      "correct": false,
      "needs_review": false,
      "correct_answer": [
        "A",
        "B"
      ],
      "points": 10.0,
      "earned": 0.0,
      "analysis": "水和二氧化碳是原料，氧气和有机物是产物。"
    }
  ]
}
```

## POST /api/classroom/courses/{id}/tts（未配置 TTS 厂商 → 浏览器兜底）

```json
{
  "fallback": "browser",
  "course_id": 1
}
```

## GET /api/classroom/scenes/{id}/html（交互场景，独立 CSP）

状态码：`200`；响应头：

```json
{
  "content-type": "text/html; charset=utf-8",
  "content-security-policy": "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' data: blob:; style-src 'unsafe-inline' data:; img-src data: blob:; font-src data:; media-src data: blob:; worker-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'",
  "x-frame-options": "SAMEORIGIN",
  "cache-control": "private, max-age=300"
}
```

响应体（片段）：

```html
<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>光照强度模拟</title><style>html,body{margin:0;height:100%;font-family:system-ui,sans-serif;}body{display:flex;align-items:center;justify-content:center;background:#f8fafc;color:#334155;}.box{max-width:520px;padding:28px;border:1px dashed #94a3b8;border-radius:12px;background:#fff;}.badge{display:inline-block;padding:2px 10px;border-radius:999px;background:#e2e8f0;font-size:12px;}h1{font-size:20px;margin:12px 0 8px;}p{line-height:1.7;margin:6px 0;font-size:14px;}</s
```

## POST /api/classroom/courses/{id}/scenes/{scene_id}/regenerate

```json
{
  "job": {
    "id": 2,
    "user_id": 1,
    "task": "course",
    "status": "queued",
    "stage": "queued",
    "error": null,
    "created_at": "2026-11-20T16:56:25.845297+00:00",
    "updated_at": "2026-11-20T16:56:25.845297+00:00",
    "partial_content": "",
    "cancelled": 0,
    "cancel_requested_at": null,
    "finished_at": null,
    "heartbeat_at": null,
    "request": {
      "task": "course",
      "qid": null,
      "attempt_id": null,
      "document_id": null,
      "document_ids": null,
      "page_from": 1,
      "page_to": null,
      "upload_id": null,
      "provider_id": 1,
      "model": "test-model",
      "verify_provider_id": null,
      "verify_model": null,
      "judge_provider_id": null,
      "judge_model": null,
      "cross_verify": false,
      "structured_verify": false,
      "peer_review": false,
      "explain_mode": null,
      "modes": null,
      "template": "checklist",
      "prompt_config": null,
      "source_mode": "user_docs",
      "kp_ids": null,
      "subject": null,
      "confirm_outline": false,
      "map_extract": false,
      "tagging_limit": 50,
      "requirements": "重点讲清原料、产物与影响因素；语言适合初中生。",
      "params": {
        "course_id": 1,
        "stage": "scenes",
        "scene_ids": [
          4
        ],
        "instruction": "题目再简单一点",
        "scene_types": [
          "quiz"
        ],
        "scene_count": 6,
        "depth": "standard"
      }
    },
    "result": null,
    "has_checkpoint": false,
    "needs_attention": false
  },
  "course_id": 1,
  "scene_id": 4
}
```

## GET /api/ai/jobs/{id}（单场景重新生成完成）

```json
{
  "status": "completed",
  "result": {
    "course_id": 1,
    "generated_scene_count": 1,
    "failed_scene_ids": [],
    "summary": "课程生成完成：1 个场景可用"
  }
}
```

## POST /api/classroom/courses/{id}/tts（已配置语音连接 → 素材）

```json
{
  "asset": {
    "id": 1,
    "kind": "tts",
    "mime_type": "audio/mpeg",
    "url": "/api/classroom/assets/1",
    "meta": {
      "voice": "alloy",
      "size": 29
    },
    "created_at": "2026-11-20T16:56:26.019356+00:00"
  },
  "provider": "示例语音连接",
  "model": "tts-1",
  "voice": "alloy",
  "course_id": 1
}
```

## GET /api/classroom/assets/{asset_id}（本人素材）

状态码：`200`；响应头：

```json
{
  "content-type": "audio/mpeg",
  "cache-control": "private, max-age=3600",
  "etag": "\"c2f6f3ab815be07c34090b113ea3feaaca499b1af43ce67ea078498aa24ff1d3\""
}
```

带 `If-None-Match` 重新请求 → 状态码 `304`（命中 ETag，304 缓存）

## GET /api/classroom/meta（配置 TTS 后的 tts 字段）

```json
{
  "tts": {
    "provider": "示例语音连接",
    "model": "tts-1"
  }
}
```

## 错误响应样例（统一 {detail: 中文说明}）

```json
{
  "未登录 401": {
    "detail": "请先登录"
  },
  "课程不存在 404": {
    "detail": "未找到该课程"
  },
  "场景数量越界 422": {
    "detail": [
      {
        "type": "less_than_equal",
        "loc": [
          "body",
          "scene_count"
        ],
        "msg": "Input should be less than or equal to 12",
        "input": 99,
        "ctx": {
          "le": 12
        }
      }
    ]
  }
}
```

## DELETE /api/classroom/courses/{id}

```json
{"deleted": true, "course_id": 1}
```

---

# P1/P2 增量接口与字段（多智能体 / 深度交互模式 / 自动名册 / 可选配图）

> 本节的样例同样由 mock 模型/假图片接口跑出（未联网）。完整字段见 `MULTIAGENT-API.md`
> 与 `GENERATION-PLUS.md`；`GET /api/classroom/meta` 的最新结构已在上文更新。

## 1. 多智能体课堂（角色名册 / 讨论 / 插话）

- `GET /courses/{id}/agents` → `{roster:{teacher,classmates}, agents, has_roster, prompt_version, generated_at, limits, roles}`
- `POST /courses/{id}/agents/generate` `{provider_id?, model?, requirements?, classmate_count?}` → 一次结构化调用生成名册
- `PATCH /courses/{id}/agents`：`{roster}` 完整替换 / `{teacher:{id,...}, classmates:[{id,...}]}` 按 id 局部编辑 / `{clear:true}` 清空
- `POST /sessions/{id}/discussion` `{scene_id?, mode:'discussion'|'qa'|'lecture', topic?, rounds?1-3, user_message?, provider_id?, model?}` → 1-6 段多角色脚本
- `POST /sessions/{id}/interject` `{scene_id?, message}` → 1-2 段插话回应

名册样例（LE 生成后颜色/id/priority 由服务端补齐）：

```json
{
  "roster": {
    "teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher",
                 "persona": "善于用追问推进推导，讲题时先让学生猜再纠正。",
                 "avatar": "", "color": "#3b82f6", "priority": 10},
    "classmates": [
      {"id": "student_1", "name": "小舟", "role": "student",
       "persona": "好奇心强，常问“为什么”，但计算容易粗心。",
       "avatar": "", "color": "#10b981", "priority": 5}
    ]
  },
  "agents": [],
  "has_roster": true,
  "prompt_version": "classroom-multiagent-2026-11-21-v1",
  "generated_at": "2026-11-20T18:16:22.693858+00:00"
}
```

讨论脚本样例（`actions` 已过白名单；白板动作坐标 0-1 且带 `coord_space:"unit"`）：

```json
{
  "turns": [
    {"speaker_id": "teacher_1", "speaker_name": "林老师", "role": "teacher", "color": "#3b82f6",
     "text": "这一页我们看原料与产物。",
     "actions": [{"id": "act_001", "type": "spotlight", "element_id": "text_001", "dim_opacity": 0.5}]},
    {"speaker_id": "student_1", "speaker_name": "小舟", "role": "student", "color": "#10b981",
     "text": "老师，水为什么算原料？",
     "actions": [{"id": "act_001", "type": "whiteboard", "coord_space": "unit", "ops": [
       {"kind": "rect", "x": 0.06, "y": 0.18, "width": 0.22, "height": 0.15,
        "fill": "transparent", "stroke": "#5b9bd5"},
       {"kind": "arrow", "x1": 0.28, "y1": 0.26, "x2": 0.38, "y2": 0.26, "color": "#333333", "width": 2}
     ]}]}
  ],
  "scene_hint": {"index": 1, "reason": "下一页会用曲线图讲影响因素"},
  "mode": "discussion", "rounds": 2,
  "usage": {"prompt_tokens": 880, "completion_tokens": 260, "total_tokens": 1140},
  "model": "mock-model", "provider_name": "示例连接",
  "turn_ids": [2, 3], "user_turn_id": 1, "warnings": [],
  "prompt_version": "classroom-multiagent-2026-11-21-v1"
}
```

## 2. 深度交互模式（`course.mode="interactive"`）

课程对象（列表/详情/会话的 `course`）新增 `interactive_mode: true`；生成完成后
`config.interactive_mode=true`。大纲约束（服务端确定性策略）：每课 interactive 场景 1-2 个，
超过 2 个的第 3 个起改 `slide` 并在 `outline.warnings` 说明；缺 `widget` 兜底 `simulation`；
0 个 interactive 时给警告（不伪造交互页）。

slide 在交互模式下追加「动手试一试」提示块（模型输出 `hint_*` 元素则复用，缺失时按大纲要点
确定性补入，并写 `content.interactive_hint` + `meta.interactive_hint`）：

```json
{
  "type": "slide",
  "content": {
    "type": "slide", "schemaVersion": 1,
    "canvas": {"width": 1000, "height": 562.5, "elements": [
      {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
       "content": "<p style=\"font-size:32px;\"><strong>光合作用</strong></p>"},
      {"id": "hint_001", "type": "text", "left": 60, "top": 466, "width": 880, "height": 46,
       "content": "<p style=\"font-size:16px;\"><strong>动手试一试</strong>：结合本页内容想一想：水是原料——你能自己举例或验证一次吗？</p>",
       "text": "动手试一试：结合本页内容想一想：水是原料——你能自己举例或验证一次吗？"}
    ]},
    "interactive_hint": {"element_id": "hint_001",
                          "text": "动手试一试：结合本页内容想一想：水是原料——你能自己举例或验证一次吗？"}
  },
  "meta": {"interactive_hint": {"element_id": "hint_001", "text": "…"},
           "warnings": ["已补入「动手试一试」提示块（按大纲要点生成，未编造交互结果）"]}
}
```

## 3. 课程生成任务：自动名册 + 可选配图

`POST /courses/{id}/generate` 新增可选参数（`stage=all/scenes` 均生效）：

```json
{"stage": "all", "provider_id": 1, "model": "chat-model",
 "images": true, "images_per_scene": 1, "image_provider_id": 2, "image_model": "gpt-image-1"}
```

- `images`：`null/省略`=自动（检测到可用图片连接才配图）、`false`=关闭、`true`=强制（无连接 422）；
- `images_per_scene`：0-2（默认 1）；`image_provider_id`+`image_model` 显式指定连接（须同时给）。

任务结果增量（`GET /api/ai/jobs/{id}.result`）：

```json
{
  "course_id": 1, "course_title": "光合作用入门",
  "scene_count": 5, "generated_scene_count": 5, "failed_scene_ids": [],
  "summary": "课程生成完成：5 个场景可用",
  "roster_generated": true, "roster_agent_count": 3, "roster_warnings": [],
  "prompt_version": "classroom-2026-11-21-v2",
  "classroom_prompt_version": "classroom-2026-11-21-v2"
}
```

配图成功时 slide 追加 image 元素（引用 `cls_assets` 素材，内容寻址去重）：

```json
{"id": "img_001", "type": "image", "left": 640, "top": 108, "width": 300, "height": 192,
 "src": "/api/classroom/assets/1", "asset_id": 1, "asset_sha256": "82a58bf5…",
 "alt": "教学配图", "fixedRatio": true}
```

`content.image_assets` / `meta.images` 同时记录 `{asset_id, element_id, url, mime_type, alt, prompt, sha256}`。
配图失败只写 `meta.warnings`（「配图生成失败，本页保持纯文本」），不阻塞课程、不造假图；
无可用连接时**不生成**（`images.enabled=false`）。

## 4. ZIP 导出/导入增量（manifest）

`GET /courses/{id}/export?format=zip` 的 `manifest.json` 新增（详见 `EXPORT-INTERACTIVE.md`）：

```json
{
  "interactiveMode": true,
  "agents": {"teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher",
                          "persona": "…", "avatar": "", "color": "#3b82f6", "priority": 10},
             "classmates": [{"id": "student_1", "name": "小舟", "role": "student",
                              "persona": "…", "avatar": "", "color": "#10b981", "priority": 5}]},
  "agentsPromptVersion": "classroom-multiagent-2026-11-21-v1",
  "capabilities": ["slide", "quiz", "interactive", "pbl", "tts", "agents", "interactive-mode", "images"],
  "resources": [{"path": "assets/image/82a58bf59c35bf52.png", "sha256": "82a58bf5…",
                  "size": 69, "mimeType": "image/png", "kind": "image", "assetId": 1}]
}
```

导入时优先 `manifest.agents`、退化为 `course.json.config.agents`，经服务端重新校验后恢复；
图片素材按 `resources[].assetId` 建立旧→新映射并重写 slide image 元素的 `asset_id/src`。
离线 HTML/PPTX 导出会把配图内联成 `data:` URI（无外链），ZIP 内保留素材引用。
