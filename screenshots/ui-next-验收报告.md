# 知序下一轮优化 · 前端交付报告（frontend-next）

> 范围：`web/js/**`、`web/styles.css`（末尾追加，不整篇重写）、`scripts/ui_check*.py`、`screenshots/`。
> 未改动 `app/**`、`tests/**`、`deploy/**`（这些目录的改动均来自后端同事）。
> 对照方案：`知序下一轮优化方案.md` §1.4 / §2.4 / §4.4。

## 一、验收结论

| 项目 | 结果 |
|---|---|
| `node --check` 全部 `web/js/**/*.js` | 通过（Node v24） |
| `scripts/ui_check.py` | **ok=true**，0 problems，5 张截图 |
| `scripts/ui_check_pages.py` | **ok=true**，0 problems，12 张截图 |
| `scripts/ui_check_flows.py` | **ok=true**，**18/18 项通过**，18 张截图 |
| 真实后端联调（provider-core 契约） | 通过：建连接 / 预览 / probe / 默认模型 / Embedding / 删除均 200，无页面异常 |
| 隔离实例 | `STUDY_DATA_DIR=%TEMP%\ui-next`，端口 `8817`，题库跟随 `releases/CURRENT`（`bank-20261120-r3`）；验收后已停止并清理临时数据 |

验收命令（仓库根目录）：

```powershell
$env:STUDY_DATA_DIR="$env:TEMP\ui-next"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8817

python scripts/ui_check.py         --base-url http://127.0.0.1:8817 --out screenshots/ui-next-accept-ui
python scripts/ui_check_pages.py   --base-url http://127.0.0.1:8817 --out screenshots/ui-next-accept-pages
python scripts/ui_check_flows.py   --base-url http://127.0.0.1:8817 --out screenshots/ui-next-accept-flows
```

产物：`screenshots/ui-next-accept-ui/`、`screenshots/ui-next-accept-pages/`、`screenshots/ui-next-accept-flows/`
（各目录内 `summary*.json` 为机器可读报告）。

---

## 二、A. 模型设置页重构（`web/js/pages/settings.js`，23 KB → 1641 行重写 + 局部追加）

| # | 问题（改前） | 改动 | 验证 |
|---|---|---|---|
| A1 | 只有内联表单；官方预设 `models` 全空；无法保存前预览/测通；默认把 `/chat/completions` 重复拼接；模型列表 `.slice(0,80)` 截断 | 新增统一 **添加/编辑连接弹窗**（`.provider-dialog`）：预设下拉顶部新增「常用自定义与本地」分组（自定义 OpenAI 兼容/中转站、本地 Ollama、LM Studio/vLLM、Azure OpenAI、自定义 Claude 中转、Gemini 原生接口），10 家官方预设补齐推荐模型；地址输入 debounce 500ms 调 `fetch-models-preview`，展示「将请求：POST …/chat/completions」、已清洗尾路径、`已向厂商请求模型列表` 与全量模型列表（搜索 + 全选 + 已选计数，DOM 不截断）；折叠「高级连接选项」：鉴权方式、≤8 条自定义请求头（保存后仅回显键名）、`max_tokens` 兼容策略、超时 15–600s、本地代理开关（按 `url_policy/public_mode` 判定，未知时始终显示 +「仅本地模式生效」说明）；底部流程 `拉取厂商模型 → 勾选 → 保存前测试连接(probe，展示耗时/诊断) → 保存`；Key 可留空（本地无鉴权时自动 `auth_mode=none`，远程缺 Key 给可执行报错）；编辑时不出 Key 只传 `provider_id` 复用已存密钥；保存失败就地显示错误、不关闭弹窗 | `ui_check_flows.py::settings_custom_provider`：mock 120 个模型、断言列表 120 个且第 120 个可搜索、全选 120、probe 请求含 `model/auth_mode/compat`、保存请求体含 `auth_mode/compat/extra_headers` 且 `models.length==120`；真实后端 UI 联调用「本地 Ollama」预设成功保存（Key 空 + `auth_mode=none`） |
| A2 | 顶部默认模型是只读列表；`judge` 清空不发请求；厂商名误查 `primary_provider_id`；保存后顶部不刷新；删除连接留下脏引用 | 顶部卡片改为**行内可编辑**：explain/grade/judge/notes/ocr/upload/tagging 每行「主模型 + 核验模型 + 厂商名 + 清空」；`judge` 用 `judge_provider_id/judge_model` 并支持真正清空（PUT null）；`grade` 槽位显示「跟随讲解模型（可单独指定）」；保存后仅重渲染该卡片（`ctx.renderApiStatus`），不 `renderRoute()`；删除连接时本地先清理 `primary/verify/judge` 引用再与服务端对账 | `ui_check_flows.py::settings_task_defaults`：断言 judge 设置/清空两次 PUT body（含 `judge_provider_id==11`、清空为 `null`）、厂商名刷新、`.pg-api-status` DOM 标记未被重建、explain 主+核验 body `mode=accurate` |
| A3 | `modelPicker` 切换模型/模式后未写回 `state.taskDefaults`，切下一题弹回旧模型；vision 回退静默 | `sync()` 同步写回 `state.taskDefaults[scope]`（primary/verify/judge + mode）并派发 `zhixu:task-default-changed`；新增「当前模型：… · 精确/快速」标签实时刷新；`groupedModelOptions()` 返回 `visionFallback`，无视觉模型时显示可见 warning（`vision-fallback-hint`） | `ui_check_flows.py::model_picker_persistence`：切精确/快速后切到第 2、3 题，主模型与模式均不回弹，PUT body 同时出现 `accurate/fast` |
| A4 | Embedding 只能靠环境变量，设置页/就绪页无状态 | 新增折叠卡片「语义检索（Embedding）配置」：关闭/复用已有连接/自定义地址+模型+Key；懒加载 `GET /providers/embeddings-config`（展示 mode/effective_source/env_configured/compatible_providers），「测试向量接口」展示维度与耗时并回显诊断；保存 PUT；展开时同时读取 `/api/health/ready` 的 embedding 状态（只展示状态/模型/维度，**不渲染任何路径**） | `settings_custom_provider` 内断言：测试显示维度 1536、PUT body `mode=custom`、页面文本不含 `C:\…`/`bank_dir`/`app.sqlite3` 等泄漏；真实后端 GET/PUT/测试均 200 |
| A5 | 改单个模型属性调用 `renderRoute()`，折叠/滚动丢失 | 模型能力弹窗保存后只更新 `state.providers` 对应 model_details 并重渲染 provider 卡片网格；补齐 `enabled/stream/context` 编辑 | 代码路径 `ctx.renderProviders()`；`ui_check_pages.py` settings 检查 + 手动截图验证 |

## 三、B. 移动端与前端核心 Bug

| # | 问题 | 改动 | 验证 |
|---|---|---|---|
| B1 | ≤900px 整块隐藏 `.filter-head`（连「更多筛选/清空筛选」一起消失）；`.question-list` 280px 裁剪；`@media(max-width:520px)` 在 900px 之前导致 1 列被覆盖为 3 列；多选面板贴近右缘溢出 | `styles.css` 末尾追加：≤900 只隐藏 `.filter-head h2`，保留两个按钮；`.question-list{max-height:none;overflow:visible}`、`.results-scroll{overflow:visible}`；520px 断点移到 900 之后并压缩移动端筛选高度（保证首条结果 y=405 ≤420）；`.multi-select-panel{max-width:min(320px,100vw-24px)}` + `practice.js` 打开时按视口右缘自动 `.align-right` | `ui_check.py` mobile 指标（first_result_y 405、overflow 0）；`ui_check_flows.py::mobile_filters`（390/320：按钮可见、列表高度>300 且无内部裁剪、面板不越界、清空后搜索框为空、320 无横向溢出） |
| B2 | `reloadControls()` 不同步搜索框；清空筛选后关键词残留；已选芯片 `kp_id/method_id` 显示英文 ID | `reloadControls()` 同步 `keyword.value`、调用 `refreshKpPanel()`；`activeFilterChips` 增加知识点树/`/methods` 名称映射，缺失时显示 `知识点#id` 而非空白 | mobile_filters 断言清空后输入框为空；`multi_filter_and_summary` 既有项继续通过 |
| B3 | 连续复习结果 `push`，重复提交出现「3 题答对 4 道」 | `results[index]` 覆盖写（提交/跳过均按当前题号） | `continuous_review` 既有检查 + 代码路径 |
| B4 | 重命名笔记后侧栏标题不更新 | `editNote` 保存后回调 `onPatched`：同步 `allNotes` 与侧栏 `renderList(false)`，不整页重渲染 | 浏览器实测：重命名后侧栏 `.note-card h3` 与详情标题立即变为新标题（见报告验证记录） |
| B5/B6 | 登出/401 后 `resize/keydown` 监听残留、`practiceView/revealedCache/adminTab/noteSection` 跨账号残留；`X-Zhixu-Bank` 变化不失效缓存；登录页↔主壳切换「应用已更新」横幅丢失/标志位死锁 | `main.js` 增加 `shellTeardown()`（登出/401 移除监听）；`state.js` 增加 `onSessionReset/onBankChange/noteBankVersion` 钩子并清空 `practiceView/resultsScrollTop/noteTab/…`；`api.js` 观察 `X-Zhixu-Bank` → 作废 `catalog/knowledgePoints/search`，`question.js` 作废 `revealedCache`；横幅改为单节点复用 + `remountBuildBanner()`，登录页/主壳重渲染后重新挂载 | `build_banner` 新增断言：`clearUserSession()` 后 user/practiceView/filters/taskDefaults/providers/noteTab/seen/catalog 全部复位；浏览器实测：横幅在登录页、重新登录后的主壳均可见，x=238（未被侧栏覆盖） |
| B6b | 管理员编辑区打印进试卷；任务完成后 `state.questionJobId` 不清 | 编辑区补 `.admin-only`（打印 CSS 已隐藏）；讲解/核验任务完成回调先置 `state.questionJobId=null` 再重开题目 | `ui_check.py` print 检查通过；代码路径复核 |
| B7 | 轮询 `setTimeout` 不在 cleanup 取消；SSE 瞬断永久放弃；容器销毁不清理 | `jobs.js`：统一 `pollTimer/reconnectTimer/watchdogTimer` 并在 `cleanup()` 清理；SSE 断线有限重连 2 次（600/1200ms）后再退回轮询；容器脱离文档 4s 看门狗自动 cleanup | 专用 SSE mock 实测：正常流 done 后 EventSource 清空；连续两次 500 → 第 3 次成功（请求计数 3）；容器移除后 activeEventSources=0 |

## 四、C. Playwright 补盲（`scripts/ui_check_flows.py`：14 → 18 项）

新增检查（既有 14 项保持全绿）：

1. **`settings_custom_provider`**：mock `GET/POST /api/providers`、`/api/ai/task-defaults`、`fetch-models-preview`、`probe`、`embeddings-config`、`embeddings-test`、`/api/health/ready`。覆盖：预设分组、误粘贴 `/chat/completions` 的规范化预览与清洗提示、120 个模型不截断可搜索可全选、probe 耗时/诊断、保存请求体 `auth_mode/compat/extra_headers/models`、Embedding 测试维度 + PUT、`/health/ready` 精简响应下页面不出现绝对路径。
2. **`settings_task_defaults`**：mock providers/task-defaults；断言行内设置与**清空 judge 的真实 PUT body**、厂商名解析、顶部卡片局部刷新（DOM 标记不变，证明未 `renderRoute`）。
3. **`model_picker_persistence`**：mock providers/task-defaults；切换主模型/精确-快速模式后写回 `state.taskDefaults`，连续切两题不回弹，PUT 模式序列含 accurate/fast。
4. **`mobile_filters`**：390/320 下「更多筛选」「清空筛选」可见、`.question-list` 不被 280px 裁剪、多选面板不越界、清空后搜索框为空、320 无横向溢出。

另外：

- **启动特性探测**：三个脚本均新增 `launch_browser()`，`msedge → chrome → 自带 chromium` 依次回退，不再硬编码 `channel="msedge"`。
- **全局断言（flows）**：context/page 级收集 `console.error` 与 4xx/5xx（排除 401/403/409 门禁），出现即失败；对既有检查中**故意 mock 的失败/噪音**做精确放行（`/x` 净化用例 404、上传保存 mock 500、stale-job 422、CSP inline 噪音），真实 4xx/5xx 仍会被 response 断言捕获。
- 固定等待收敛：新增检查用 `wait_for_selector/wait_for_function/expect_response` 代替长固定等待（保存后等 dialog detached、清空筛选后等输入框为空等）；既有检查的关键等待保留。
- `ui_check.py` / `ui_check_pages.py` 同步加浏览器回退；`ui_check_pages.py` settings 检查改为断言 `.pg-task-row` 行内编辑控件，适配新设置页。

### mock 契约（供后端回归）

- `POST /api/providers/fetch-models-preview` → `{ok, normalized_base_url, resolved_endpoint, models:[{model_id,caps}], model_ids, capabilities, http_status, diagnosis, notes}`（失败也返回 200 + `ok:false`）。
- `POST /api/providers/probe` → `{ok, latency_ms, diagnosis, request_url, http_status, model, content_preview, notes}`。
- `GET /api/providers/embeddings-config` → `{mode:off|inherit|custom, provider_id, base_url, model, has_api_key, env_configured, effective_source, effective:{ready,model,…}, compatible_providers}`；`PUT` 返回同结构；`POST /embeddings-test` → `{ok, dimension, latency_ms, model, diagnosis}`。
- `/api/health/ready` 匿名精简：`{status, version, build_id, study_ready, ai_ready, checks:{app_db:{ok},bank:{ok},storage:{ok}}}`（不含任何路径）。

## 五、与后端最终契约的差异（联调备注）

provider-core 最终实现与任务书初始描述有几处细节差异，前端已按**后端最终返回**兼容（两种字段都读）：

1. **预览响应模型的键名**：任务书写 `models:[{id,...}]`，实际为 `[{model_id, caps}]`，另有 `model_ids`；前端 `normalizeModelList()` 同时兼容 `id/model_id/name` 与 `models/data/items/model_ids`。
2. **`capabilities`**：任务书为单对象，实际为 `{model_id: caps}` 映射；前端按映射统计「N 个模型支持视觉」，单对象写法也兼容。
3. **`resolved_endpoint` 语义**：预览接口返回的是「拉模型列表」端点（如 `…/v1/models`），不是对话端点；前端据此自行计算并展示「将请求：POST …/v1/chat/completions」，同时把实际列表端点作为说明展示，避免误导。
4. **Provider 回显字段**：不是 `extra_headers`（脱敏对象），而是 `extra_header_names: string[]` + `has_api_key: bool`；前端优先读取，兼容旧 `extra_headers/extra_header_keys`。
5. **`GET /api/providers` 附带** `url_policy`（local_open/public_https/allowlist）与 `public_mode`；前端用它决定代理开关提示（`/api/health/live` 作为兜底，未知时仍显示 +「仅本地模式生效」）。
6. **创建连接鉴权**：`api_key` 可空但必须显式 `auth_mode='none'`；前端在本地地址/本地预设且未填 Key 时自动置 none，远程缺 Key 给出可执行错误提示。
7. **`extra_headers` 为整体替换语义**（PATCH 传对象即覆盖）：前端仅在用户确实编辑请求头时才发送，并明确提示「已保存请求头值无法回显，将整体替换」；未编辑时省略以保留服务端加密值。任务书未定义 `extra_headers_remove`，前端已不再依赖该字段。
8. **Embedding 模式值**：实际为 `off|inherit|custom`（任务书文字为「关闭/复用/自定义」），前端使用 `inherit`；`embeddings-config` 额外提供 `effective_source/effective/compatible_providers`，前端已展示。
9. **`resolved_endpoint/request_url` 已脱敏**（Key 不落前端），截图/断言均按脱敏值处理。

## 六、截图

- 三套验收产物：`screenshots/ui-next-accept-ui/`、`screenshots/ui-next-accept-pages/`、`screenshots/ui-next-accept-flows/`
- 关键新增截图：
  - `screenshots/ui-next-accept-flows/flows-settings-custom-provider.png`（自定义厂商 + 模型列表 + Embedding）
  - `screenshots/ui-next-accept-flows/flows-settings-task-defaults.png`（默认模型行内编辑）
  - `screenshots/ui-next-accept-flows/flows-model-picker-persistence.png`（精确/快速模式持久化）
  - `screenshots/ui-next-accept-flows/flows-mobile-filters-320.png`（320 窄屏筛选/多选面板）
  - `screenshots/ui-next-provider-dialog.png`（连接向导：规范化预览 + 模型全选 + 高级选项）
  - `screenshots/ui-next-embedding-card.png`（Embedding 配置卡片）
  - `screenshots/ui-next-settings-layout.png` / `ui-next-settings-mobile.png`（设置页桌面/390 布局）
  - `screenshots/ui-next-real-settings.png`（真实后端建连后设置页）

## 七、取舍与未覆盖

- **§3.2 整卡流式重构**未做（任务明确“仅局部 patch，整卡流式重构留待下一阶段”）；本次只修轮询取消、有限重连、容器销毁清理。
- `ui_check.py` 手机首条结果目标仍保持 ≤420px：新增「更多筛选」行后通过压缩 520px 断点内间距/控件高度达成（实测 405px），未放宽阈值。
- `ui_check_pages.py` settings 区块已适配新设置页结构（`.pg-task-row` 行内编辑、`.provider-form` 保留为「打开连接向导」入口）。
