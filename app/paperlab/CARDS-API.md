# 试卷工坊后端 API（P1：答题卡模板 / SVG / 打印分栏）

前缀 `/api/paperlab`，全部需登录（`current_user`），错误统一 `{detail: "中文说明"}`。
实现：`app/paperlab/{cards,card_svg,exporters,papers}.py`；几何/SVG 纯函数在 `card_svg.py`
（不依赖第三方 SVG 库）；测试：`tests/test_paperlab_cards.py`。
契约：`INTEGRATION-CONTRACT.md` §4.2；报告/导出口径见 `PAPERS-API.md`。

## 0. 数据表（`cards.py::ensure_tables()` 幂等建表）

| 表 | 说明 | 关键字段 |
|---|---|---|
| `pl_cards` | 答题卡模板 | `user_id,paper_id,title,layout_json,status(ready\|stale\|draft),version,created_at,updated_at` |

- 一张试卷最多保留 **20** 张答题卡；试卷删除时卡级联删除；卡只允许本人访问（他人 404）。
- **stale**：试卷结构（题目/顺序/分值/选项数）变化后，旧卡在下次读取时自动标 `stale`
  （惰性刷新，比较卡内 `layout.template.paper_fingerprint` 与当前试卷指纹）。
  stale 卡不可 PATCH（409），但历史 SVG/HTML 仍可导出（响应头 `X-Card-Stale: 1` 且 HTML 顶部提示），
  前端应引导重新生成。

## 1. 模板几何（`layout_json`，毫米坐标）

```json
{
  "schema": "zhixu-card-v1",
  "preset": "A4", "orientation": "portrait",
  "columns": 2, "bubble": "circle", "include_subjective": true,
  "student_no": {"digits": 6, "bubble": true},
  "paper": {"width_mm": 210, "height_mm": 297, "margin_mm": 12, "header_mm": 41.4, "footer_mm": 22},
  "template": {"paper_id": 1, "title": "函数练习 · 答题卡", "subject": "数学",
                "paper_fingerprint": "sha256[:16]", "signature": 1292818435, "page_count": 1},
  "pages": [{
    "page_no": 1, "width_mm": 210, "height_mm": 297,
    "anchors": [
      {"corner": "top_left", "x": 12, "y": 12, "w": 12, "h": 5},
      {"corner": "top_right", "x": 186, "y": 12, "w": 12, "h": 5},
      {"corner": "bottom_left", "x": 12, "y": 280, "w": 12, "h": 5},
      {"corner": "bottom_right", "x": 186, "y": 280, "w": 12, "h": 5}
    ],
    "header": {
      "title": {"x": 16, "y": 19, "size": 5.2, "text": "…"},
      "meta": {"x": 105, "y": 26, "size": 3.2, "anchor": "middle", "text": "数学 · 高中一年级"},
      "fields": [{"label": "姓名", "x": 16, "y": 32, "w": 42, "h": 6}, {"label": "班级", …}],
      "student_no": {"x": 140, "y": 16, "w": 58, "h": 33.4, "digits": 6, "bubble": true,
        "digit_rows": [{"position": 1, "y": 20,
          "bubbles": [{"value": 0, "x": 149.4, "y": 21.2, "r": 1.15}, … 10 个]}, …]}
    },
    "groups": [{
      "title": "一、选择题", "x": 12, "y": 53, "w": 90, "h": 42,
      "questions": [{
        "no": 1, "type": "SINGLE", "kind": "question", "x": 12, "y": 60, "w": 90, "h": 9.5,
        "bubbles": [{"label": "A", "x": 23, "y": 65, "r": 1.45}, …],
        "write_area": null
      }, …]
    }],
    "footer": {
      "notice": {"x": 12, "y": 283, "size": 2.8, "text": "请用 2B 铅笔填涂…"},
      "page_text": {"x": 105, "y": 283, "size": 3.6, "anchor": "middle", "text": "第 1 / 2 页"},
      "page_code": {"x": 105, "y": 290, "size": 3.4, "anchor": "middle",
                     "text": "101…46 位…110"}
    },
    "qr": {"x": 158, "y": 267, "size_mm": 16,
           "text": "zhixu://card/1?page=1&sig=1292818435"}
  }]
}
```

- **纸张**：A4（210×297 纵向）/ A3（默认 297 高 × 420 宽横向；`orientation` 可覆盖）。
- **定位点**：四角 `12×5mm` 黑色矩形，顺序 `top_left/top_right/bottom_left/bottom_right`，
  距页边距内 12mm；扫描侧按四角透视矫正。
- **页面码**：`101` + FNV32(模板签名) 的 32 位二进制 + 页码 8 位 + `110`，共 46 位；
  模板签名 = 对 paper_id/试卷指纹/纸张/分栏/气泡/学号设置做规范化 JSON 后 FNV-1a 32bit。
- **客观题**：按选项数生成气泡（A–D 最多 6 列/行，多选项换行）；题组外框按「题型/自定义 section」合并，
  跨栏/跨页时标题重复（续排）。
- **主观题**：按分值估算书写行数（3–14 行）的横线书写区；`include_subjective=false` 时只排客观题。
- **学号**：6/8 位；`bubble=true` 每位 10 个填涂气泡，`false` 为手写线。
- 几何校验 `card_svg.validate_card_layout`：元素越界、气泡重叠、四角定位点名称/位置/尺寸、
  学号区位/气泡数、页数与页码、页面码格式；失败抛中文 `ValueError` → API 422。

## 2. 答题卡 API

- `POST /papers/{paper_id}/cards`
  `{preset?:'A4'|'A3', orientation?:'portrait'|'landscape', columns?:1–4(默认2),
    bubble?:'circle'|'oval', include_subjective?:bool(默认true),
    student_no?:{digits:6|8, bubble:bool}, title?}`
  → `{card, layout}`；试卷无题/几何非法/超 20 张 → 422；试卷非本人 404。
  `card = {id,paper_id,title,status,version,preset,orientation,columns,bubble,include_subjective,
  page_count,paper_width_mm,paper_height_mm,template_signature,created_at,updated_at}`
- `GET /papers/{paper_id}/cards` → `{items:[card…],total}`（不含 layout，惰性刷新 stale 状态）。
- `GET /cards/{card_id}` → `{card, layout}`（完整几何，前端编辑/预览用）。
- `PATCH /cards/{card_id}` `{version, title?, layout?}` → `{card,layout}`；
  `version` 乐观锁 CAS（冲突 409）；`layout` 需通过几何校验（422）；
  手工 layout 会重算 `template.page_count/paper_fingerprint/signature`；stale 卡 409。
- `DELETE /cards/{card_id}` → `{deleted:true,card_id}`。

## 3. SVG 与打印导出

- `GET /cards/{card_id}/svg?page=1&color=black|red` → `image/svg+xml` 单页 SVG。
  - `width/height` 为 96 DPI 像素（A4 = 793.7×1122.5，A3 = 1587.4×1122.5），`viewBox` 同像素，
    `mm→px = 96/25.4`；矢量打印不受 DPI 限制。
  - 定位点/页面码固定黑色（便于 OMR）；`color=red` 时其它描边/文字换为印刷红 `#c41e1e`。
  - 响应头：`X-Card-Page-Count`、`X-Card-Stale`；页码越界 404、非法颜色 422。
- `GET /cards/{card_id}/export?format=html|svg&page=all|N&color=black|red`
  - `html`：自包含打印页（内联所有页 SVG、内联 CSS、`@page size`、`page-break-after`），
    `X-Frame-Options: SAMEORIGIN`，前端可 iframe 后 `contentWindow.print()`；stale 时顶部提示。
  - `svg`：单页（`page=all` 返回 422，请用 html 多页）。
- 可选二维码：环境已装 `qrcode`（8.2）时，SVG 页脚区域渲染含 `paper_id/page/签名` 的 data URI
  二维码（直接画模块矩形，无新增依赖）；缺包时自动忽略，不影响导出。

## 4. 试卷打印/导出分栏增强（`exporters.py`）

- `GET /papers/{id}/export?format=html&columns=1|2|3|4&answers=0|1`
  - 逐页 `.sheet`（页眉标题/卷头信息 + 分栏正文 + 页脚页码），正文 CSS `column-count` +
    `break-inside: avoid`（题目不跨栏），A4/A3 `@page`；超过一页自动分页
    （按题目高度估算 + 分栏容量），页数在响应头 `X-Page-Count` 与 `<meta name="page-count">`。
- `GET /papers/{id}/export?format=docx&columns=1|2|3|4`
  - python-docx 写入 `w:cols w:num` 实现 Word 分栏；A4/A3 页面尺寸与方向；`answers=1` 时
    答案页为独立 1 栏 section；响应头 `X-Page-Count`/`X-Paper-Columns`。
- `columns` 缺省时用建卷 `layout.columns`（默认 1）；已发布 manual/smart 卷同样可导出。

## 5. 已知边界

- 答题卡只做「模板 + SVG + 打印」，扫描 OMR（定位点检测/页面码校验/气泡读卡）属下一阶段，
  本期不接扫描接口（不会返回假识别结果）。
- 题目高度为估算值（用于分页），实际打印以 CSS 分页/Word 排版为准；对超大配图建议前端预览后再打印。
- `GET /meta` 的 `capabilities.answer_sheet` 自 P1 起已为 `true`（由 `bank.py` 提供）；
  前端可直接按能力位展示答题卡入口。
- 全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。
