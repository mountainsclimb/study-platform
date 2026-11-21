# 试卷工坊 P1 · 切题与答题区标注 API（供前端切题编辑器对接）

> 实现：`app/paperlab/annotate.py`；测试：`tests/test_paperlab_annotate.py`；
> 依据：`INTEGRATION-CONTRACT.md` §4.2、recon/xl-frontend.md §3.4 / §5（切题系统深挖）。
> 前缀 `/api/paperlab`，全部需登录；错误统一 `{detail: "中文说明"}`（409 冲突的 `detail` 是对象，见 §4）。
> 全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。

切题编辑器 = 在既有「识别导入」的页图上画 **question**（题目框）与 **answer**（答题区框），
保存页级 `edit.json`，再「重切」生成/更新题目草稿（裁图 + 答题区坐标），最后走既有
`POST /imports/{id}/commit` 入库。P1 只提供**中间态编辑 + 重切 + 发布前校验**，
不改变 P0 的识别/核验/入库语义。

## 1. 数据表

| 表 | 说明 | 关键字段 |
|---|---|---|
| `pl_page_edits`（新增） | 页级切题编辑态 | `user_id, import_id, page_no, version, data_json, updated_at, UNIQUE(import_id, page_no)` |

`data_json` 结构：

```json
{
  "boxes": [
    {"id": "q1", "kind": "question", "x": 0.05, "y": 0.05, "w": 0.45, "h": 0.25,
     "label": "第1题", "item_id": 12, "score": 5},
    {"id": "a1", "kind": "answer", "x": 0.15, "y": 0.2, "w": 0.2, "h": 0.05, "item_id": 12}
  ],
  "page": {"page_no": 1, "width": 1654, "height": 2338},
  "updated_at": "2026-11-21T02:00:00.000000+00:00"
}
```

`page.width/height` 是页图自然像素（= API 返回的 `naturalWidth/naturalHeight`），
服务端每次保存时用 `pl_pages` 的实际值覆盖，前端不需要回填。

## 2. 坐标系（务必按此实现，否则裁图/答题区会错位）

- 所有 box 坐标 **归一化到 `[0,1]`**，原点是**页图左上角**，`x/y` 为左上角，`w/h` 为宽高；
- 页图自然尺寸 = `GET .../annotation` 里 `page.width/height`（同时以 `naturalWidth/naturalHeight`
  别名返回），来源是「上传/PDF 渲染后实际落盘的页图像素」；
- 像素换算：`left = round(x*naturalWidth)`、`top = round(y*naturalHeight)`、
  `right = round((x+w)*naturalWidth)`、`bottom = round((y+h)*naturalHeight)`；
- 服务端裁图时把像素坐标 clamp 到页图范围，并保证宽高 **≥8px**（极小框会被扩到 8px）；
- 答题区（answer）只存坐标、**不裁图**；P1 的 `draft.answer_box` 是
  `{x,y,w,h}`（归一化），与 xl-frontend 下游 `coordinates.data[].pos_list[0]`
  的「左上/右下两点」完全可互换：
  `{x: p0.x, y: p0.y, w: p2.x-p0.x, h: p2.y-p0.y}`（若原系统使用 200DPI 原图像素，
  先除以该页原图宽高再存归一化值）。
- 同 kind 的框允许重叠（编辑器把重叠框视为合法，便于子题/斜排），但服务端会返回
  `warnings`（重叠比例 >50% 时提示），不阻塞保存。

## 3. 页级 annotation

### 3.1 `GET /api/paperlab/imports/{import_id}/pages/{page_no}/annotation`

从未编辑过：

```json
{
  "page": {"page_no": 1, "width": 1654, "height": 2338,
           "naturalWidth": 1654, "naturalHeight": 2338,
           "image_url": "/api/paperlab/imports/3/pages/1/image", "has_text": false},
  "edit": {"version": 0, "boxes": [], "updated_at": null, "edited": false}
}
```

已编辑：`edit.version` 为当前版本，`boxes` 为归一化后的框（服务端会补齐 id、去掉未知字段）。
错误：`404` 导入/页不存在或非本人；`401` 未登录。

### 3.2 `PUT /api/paperlab/imports/{import_id}/pages/{page_no}/annotation`

请求：

```json
{"version": 2, "boxes": [
  {"id": "q1", "kind": "question", "x": 0.05, "y": 0.05, "w": 0.45, "h": 0.25, "label": "第1题"},
  {"kind": "answer", "x": 0.15, "y": 0.2, "w": 0.2, "h": 0.05}
]}
```

- `version` 是 CAS 基准：**新建时传 0**，之后必须传上一次响应里的 `edit.version`；
- 成功 `200`：

```json
{
  "page": {…同上…},
  "edit": {"version": 3, "boxes": [{"id": "q1", …}, {"id": "a_1", …}],
           "updated_at": "2026-11-21T02:00:00+00:00", "edited": true},
  "warnings": ["同类型切题框重叠：q1 与 q2（重叠比例 63%，已容忍，请确认）"]
}
```

- **409 版本冲突**（`detail` 是对象，不是字符串；另带响应头 `X-Edit-Version` /
  `X-Edit-Updated-At` 方便前端直接读取）：

```json
{"detail": {"message": "切题标注已被他人修改，请刷新后重试",
            "version": 5, "updated_at": "2026-11-21T02:05:00+00:00"}}
```

  前端处理：提示「他人已修改」→ 重新 `GET` 当前页再合并（或让用户选择覆盖时用最新 version 重试）。
- 校验失败 `422`（中文消息）：`kind` 非 question/answer、缺 x/y/w/h、坐标超出 `[0,1]`、
  `w/h<=0`、`x+w>1`/`y+h>1`、单页超过 **200** 个框、数值非有限数、score 越界；
- 纯文本页（DOCX 只有文字，无页图）`422`「该页为纯文本页，没有页面图，无法标注切题框」；
- `id` 可省略（服务端按 kind 生成 `q_1`/`a_1`）；重复 id 自动加后缀（`q1_2`），
  并在响应里返回规范化后的 id；`label ≤80` 字、`score 0-1000`、`item_id` 可选（关联已有草稿）。

### 3.3 `GET /api/paperlab/imports/{import_id}/pages`

编辑器和页列表用：返回每页尺寸/图地址 + 标注概况 + 草稿统计。

```json
{"items": [
  {"page_no": 1, "width": 1654, "height": 2338, "naturalWidth": 1654, "naturalHeight": 2338,
   "image_url": "/api/paperlab/imports/3/pages/1/image", "has_text": false,
   "annotation": {"edited": true, "version": 3, "updated_at": "…",
                  "box_count": 12, "question_count": 6, "answer_count": 6,
                  "items": {"items": 6, "accepted": 2, "committed": 0}}}
], "total": 1}
```

## 4. 重切 `POST /api/paperlab/imports/{import_id}/resegment`

请求：

```json
{"pages": [1, 2], "keep_items": true}
```

- `pages` 省略 = 所有页；`keep_items=true`（默认）保留未匹配的旧草稿，
  `false` = 重切后删除本页「未匹配且未入库」的旧草稿（相当于整页替换）。
- 处理逻辑（每页）：
  1. 读该页 `edit.json` 的 box；`question` 框按 y/x 排序；
  2. 找对应草稿：`box.item_id` 优先 → 否则按 bbox IoU≥0.9 匹配同页已有草稿；
  3. 用 Pillow 从页图裁 `question` 框为 PNG（内容寻址写 `paperlab/assets`），
     写入/更新草稿：`draft.bbox`、`draft.images[0]={role:'stem',asset,…}`、
     `draft.answer_box`（answer 框按「中心点落在题框内/矩形相交/最近题框≤0.35 页对角线」归属，
     最多 8 个）；
  4. **已入库草稿（`question_id` 非空）绝不覆盖**：只把 `needs_review=1`、
     `review_reason` 与 `warnings` 追加 `stale：…`，并在响应 `stale_items` 列出；
  5. 新建草稿为 `pending` + `needs_review=1`，题干是占位（`第 N 页题框 i（题干待补全）`），
     前端须在核验时补全题干/题型；不会伪造 AI 识别结果。
- 成功 `200`：

```json
{
  "import_id": 3, "pages": [1, 2], "keep_items": true,
  "created": 4, "updated": 0, "skipped": 0, "stale": 1,
  "summary": "重切完成：新建 4，更新 0，跳过 0，stale 1",
  "page_results": [{"page_no": 1, "created": 2, "updated": 0, "skipped": 0, "stale": 1}],
  "warnings": ["第 2 页没有切题框，已跳过（请先在标注接口保存 boxes）"],
  "stale_items": [{"item_id": 12, "page_no": 1, "question_id": 33, "box": {…}}]
}
```

- 重复重切幂等：bbox 近似相同的题框会更新原草稿（`updated`），不会重复建题；
- `404` 页不存在/非本人；`422` 该页无图/框退化；单次重切页数 > **20** 时改为异步任务
  （导入上限 20 页，正常不会触发；响应 `{job, id, status, task:"paper-annotate", pages}`，
  需要请求里带 `provider_id/model` 仅为任务框架入参，本任务不调用模型）。

## 5. 单题答题区 `POST /api/paperlab/imports/{import_id}/items/{item_id}/answer-box`

请求：`{"box": {"x": 0.1, "y": 0.7, "w": 0.6, "h": 0.2}}`（归一化，同 §2）。

- 写 `draft.answer_box` / `draft.answer_boxes`；若该页已有 edit.json，会把对应 answer 框
  （`item_id` 匹配）同步进 `edit.boxes` 并 `version+1`（没有 edit.json 则只写草稿）；
- 成功返回 `{item: {…public_item…}, edit: {version, boxes, updated_at, edited}}`；
- 已入库草稿 `409`「该题已入库，答题区坐标请到题库中编辑」；坐标非法 `422`；他人/不存在 `404`。

## 6. 发布前校验 `POST /api/paperlab/imports/{import_id}/publish`

只做「统计 + 发布前顺序/字段校验」，**不复制 commit 语义**（真正入库仍调用既有
`POST /imports/{id}/commit`）。

通过 `200`：

```json
{
  "ready": true, "import_id": 3,
  "summary": {"import_id": 3, "title": "期中卷", "page_count": 2, "item_count": 6,
              "pending": 0, "accepted": 6, "skipped": 0, "committed": 0,
              "page_summaries": [{"page_no": 1, "items": 4, "accepted": 4, "committed": 0,
                                  "skipped": 0, "annotation_version": 3,
                                  "question_boxes": 2, "answer_boxes": 2}],
              "checks": {"candidates": 6, "errors": 0, "missing_annotation_pages": []}},
  "errors": [], "warnings": [],
  "order": [{"page_no": 1, "item_no": 1, "item_id": 8, "question_id": null, "status": "accepted"}],
  "notice": "发布校验通过；实际入库请调用 POST /imports/{id}/commit（既有语义，未复制）。"
}
```

不通过 `422`（`detail` 为对象，含 `ready:false` + 全部统计）：

```json
{"detail": {"message": "发布校验未通过，请修正后重试", "ready": false,
            "summary": {…}, "errors": [{"item_id": 8, "item_no": 1, "page_no": 1,
                                       "status": "accepted", "errors": ["题目缺少完整的 bbox（需要 x/y/w/h）"]}],
            "order": […], "warnings": []}}
```

规则：候选 = `status=accepted` 或已入库（`question_id` 非空）的草稿；每个候选必须有非空题干、
合法 `bbox`（有答题区时 `answer_box` 也需合法）；没有任何候选 → 422。
已入库草稿字段异常只进 `warnings`（历史数据不阻塞）；`missing_annotation_pages` 列出未编辑过
切题框的页（不阻塞，仅提示）。

## 7. 前端对接建议（切题编辑器）

1. 进入编辑器：`GET /imports/{id}/pages` 拿页列表 → 当前页 `GET .../annotation`（懒加载）；
2. 画布：底图 `<img src=image_url>`，overlay 用绝对定位 div；鼠标坐标 → 归一化：
   `x = (clientX - rect.left) / rect.width`（rect 为底图显示区域）；渲染时反算
   `left = x * 显示宽`。`naturalWidth/Height` 用于「100% 缩放」与像素坐标显示；
3. 题目框蓝色、答题框橙色；8 控制点拖拽/缩放；`id` 由前端生成（如 `q1/a1`），
   保存前把关联草稿的 `item_id` 写进 answer 框（便于重切与 `answer-box` 同步）；
4. 脏页保存：只 `PUT` 改动页（带 `version`）；409 时提示刷新/合并；
5. 「切题并上传」= 先保存脏页 → `POST /resegment`（可指定 pages）→ 刷新
   `GET /imports/{id}` 查看草稿 → 例外（stale/需要补题的）由左栏提示人工处理；
6. 「发布」按钮 → `POST /publish`，`422 detail` 直接展示 `errors` 列表；通过后再调用既有
   `POST /imports/{id}/commit` 完成入库。

## 8. 验收与边界

- 测试：`python -m pytest tests/test_paperlab_annotate.py -q`（26 项：读写/CAS/box 校验/
  归属/页列表/多页重切/裁图尺寸/幂等/keep_items/stale/answer-box/发布/任务分支/未登录/上限）。
- 边界：P1 是**页级** `version` CAS（不是文档级 `base_version/edit_version` 双版本）；
  已入库草稿不覆盖，stale 通过 `review_reason` + `warnings`（`status` 枚举保持 P0 的
  pending/accepted/skipped 不变）表达；未做切图独立导出/OSS 上传/撤销重做（前端命令栈实现）。
- **部署注意（需 Lead 处理）**：新表 `pl_page_edits` 需加入 `app/db.py::_ID_TABLES`
  （PostgreSQL 模式下 `cursor.lastrowid` 依赖该集合；SQLite 不受影响）。
