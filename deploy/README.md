# 知序 · P3 评测、运维备份与公网部署指南

## 1. 评测集与分层评测
```bash
# 旧基线：100 题纯文本黄金集（保留作历史基线，不覆盖）
python -m app.eval --build-golden

# 分层评测集 v2：纯文本 / 原卷图与公式 / 阅读材料与共用题干 / 多问题组
python -m app.eval --build-golden --strata

# 单模型评测
python -m app.eval --provider-id 1 --model gpt-4o --limit 50 --out eval/run_single.json

# 双模型交叉验证 2.0 评测（统计单模型准确率、交叉验证准确率、一致率与「一致但错」比例）
python -m app.eval --provider-id 1 --model gpt-4o --verify-provider-id 2 --verify-model deepseek-chat --out eval/run_cross.json

# 对比两份评测报告（提示词/模型 A/B）
python -m app.eval --compare eval/run_a.json eval/run_b.json
```

## 1.1 题库版本与构建号

| 变量 | 行为 |
|---|---|
| `STUDY_BANK_DIR` | 容器内挂载路径（默认 `/bank`）。指向题库根目录且设置 `STUDY_BANK_FOLLOW_POINTER=1` 时跟随 `releases/CURRENT`；指向具体 `releases/<version>` 时固定该版本。 |
| `STUDY_BANK_FOLLOW_POINTER` | `1` 强制跟随版本指针（Compose 默认），`0` 强制不跟随。根目录里同时存在 `题库.db` 与 `releases/CURRENT` 时，显式指向根目录默认按基线库运行。 |
| `STUDY_EXPECTED_BANK_VERSION` | 声明期望版本；就绪检查发现实读版本不一致时降级（`checks.bank.ok=false`，`study_ready=false`），避免新旧题库混用。 |
| `STUDY_BUILD_ID` | 覆盖构建号（建议镜像里写提交号）；写入 OpenAPI、就绪信息与 `X-Zhixu-Build` 响应头。不设时按源码与前端资源内容指纹生成。 |

就绪信息新增 `build_id`、`version_label`、`bank_version`、`bank_source`（`pointer:<版本>` / `env` / `default`）、
`study_ready`（基本学习可用）与 `ai_ready`（AI 队列可用），可直接判断「哪一版应用读的是哪一版题库」。

## 2. 存活、就绪与依赖定位
```bash
# 存活检查：进程能响应即返回 ok，供容器探针使用
curl -fsS http://127.0.0.1:8765/api/health/live

# 就绪检查：题库版本、应用库、对象存储、Redis 与 Worker 心跳逐个核对
curl -fsS http://127.0.0.1:8765/api/health/ready
```

`/api/health/ready` 返回 `status`（`ok` / `degraded`）、逐项 `checks`、`degraded` 依赖列表和
`affected_features`（受影响功能）。逐个断开依赖时可据此定位：题库不可读或版本不符影响检索与作答；
Redis 或 Worker 心跳超 90 秒影响 AI 任务排队、续跑与取消确认。Redis 检查执行 `AUTH`/`SELECT`/`PING`
协议握手（支持 `rediss://` TLS、URL 编码密码、Redis 6+ ACL `用户名:密码` 与 `?socket_timeout=`），
普通 TCP 监听器不会被判为可用。日志中以 `zhixu.health` 记录降级原因。

**未鉴权访问只返回精简结构**：`status`、`version`、`build_id`、`study_ready`、`ai_ready`、
`degraded`、`affected_features` 与各依赖的 `ok` 布尔值；宿主绝对路径（`bank_dir`）、精确题量、
Redis/Worker 诊断细节只对管理员或携带 `X-Health-Token`（部署探针口令）的请求开放。
探针口令通过环境变量 `STUDY_HEALTH_TOKEN` 配置，比较使用常量时间实现：

```bash
curl -fsS -H "X-Health-Token: $STUDY_HEALTH_TOKEN" https://<域名>/api/health/ready
```

## 3. 公网账号初始化与注册开关
公网部署默认关闭注册，且不再让「首个注册者」自动成为管理员。初始化流程：

1. 部署时设置 `STUDY_ADMIN_INIT_TOKEN`（一次性初始化口令，至少 8 位，仅运营持有）。
2. 调用初始化接口创建唯一的初始管理员（成功后口令即失效，重复调用返回 409）：
   ```bash
   curl -X POST https://<域名>/api/auth/init-admin \
     -H 'Content-Type: application/json' \
     -d '{"username":"运营管理员","password":"<强密码>","token":"<STUDY_ADMIN_INIT_TOKEN>"}'
   ```
3. 管理员在「内容管理 → 账号与注册策略」显式开放注册。新账号默认为学生；关闭注册不影响已有账号。
4. 登录、注册和恢复密码的限流记录在数据库中（`auth_rate_events`），多个 Web 进程共享同一窗口与恢复计时。
   登录/重置密码同时按「IP + 用户名」与「用户名全局」双重限流（15 分钟 20 次，跨 IP 生效）；
   修改密码与生成恢复码按账号维度限流（10 分钟 10 次）。用户名不存在时也会执行一次等成本的
   `scrypt` 假核验，避免用响应耗时枚举账号。

| 环境变量 | 行为 |
|---|---|
| `STUDY_PUBLIC_MODE=1` | 公网模式：注册默认关闭，首个注册者不会自动成为管理员；会话 Cookie 强制 `Secure`。 |
| `STUDY_REGISTRATION_MODE=open|closed` | 强制注册开关（优先于数据库设置，便于部署脚本固定策略）。 |
| `STUDY_ADMIN_INIT_TOKEN` | 一次性管理员初始化口令；未配置时初始化接口返回 503。 |
| `STUDY_HEALTH_TOKEN` | 部署探针口令：携带 `X-Health-Token` 读取 `/api/health/ready` 完整详情；未配置则不开放。 |
| `REDIS_URL` | Redis 连接串，支持 `redis://` / `rediss://`、`redis://:密码@host:6379/0`、ACL `redis://用户名:密码@host:6379/0`；可选 `?socket_timeout=3`。密码含保留字符时需百分号编码。 |
| `REDIS_PASSWORD` | Compose 传给 `redis-server --requirepass` 的密码，并自动拼进 Web/Worker 的 `REDIS_URL`。**公网部署必须覆盖默认值。** |

## 4. 在线热备份与恢复演练
```bash
# 一致性 WAL 热备份：默认写入 <数据目录>/backups/（Docker 即 /data/backups，随数据卷持久化）
python -m app.ops backup

# 需要把应用级加密密钥也带进备份包时显式包含（默认排除 api-key.key）
python -m app.ops backup --include-key

# 校验备份包 SHA-256 与 SQLite PRAGMA integrity_check
python -m app.ops verify data/backups/zhixu-backup-xxxx.zip

# 恢复演练到独立目录（先完整解压到暂存目录校验，再逐个原子替换）
python -m app.ops restore data/backups/zhixu-backup-xxxx.zip --target ./restore-drill
```

备份包包含账号表、恢复码、本地附件与 SHA-256 清单。**默认不包含 `api-key.key`**：
它是应用级加密密钥，泄露即可解密所有已保存的模型 API Key。ZIP 本身不加密（标准库 `zipfile`
没有加密能力，本项目不引入额外加密依赖），因此安全边界是「排除敏感文件 + 清单校验 + 离机保管」：
需要完整离线恢复时用 `--include-key`，并把 ZIP 与密钥分别存放在受控介质；普通备份把
`api-key.key` 单独按密钥规范备份。恢复后若缺少密钥，已有模型连接的 Key 需要重新录入。

归档成员路径在解压前按 POSIX + Windows 双语义校验：拒绝绝对路径、`..` 穿越、驱动器号、
NTFS 备用数据流（`a::b`）与 Windows 保留设备名；`verify`/`restore` 都不会把危险成员写到目标目录。
PostgreSQL 部署需另外用 `pg_dump` / `pg_restore` 导出账号与学习记录，并单独备份 S3 桶；
快照里发现 `attempts.image_path`、`ai_uploads.extra_files_json` 等字段引用 `s3:` 时会拒绝本地 ZIP 备份。

## 5. Docker Compose 公网部署

复制 `deploy/.env.example` 为 `deploy/.env`，配置公网域名 `STUDY_DOMAIN`、允许访问的域名
`STUDY_ALLOWED_HOSTS`、数据库密码 `PG_PASSWORD`、Redis 密码 `REDIS_PASSWORD`、宿主机题库路径
`HOST_BANK_DIR`、`STUDY_PUBLIC_MODE=1`、`STUDY_ADMIN_INIT_TOKEN` 与部署探针口令
`STUDY_HEALTH_TOKEN`。含 URL 保留字符的密码需要在 `DATABASE_URL` / `REDIS_PASSWORD` 中使用百分号编码。
Caddy 签发公网证书需要把域名解析到服务器，并开放 TCP 80 和 443。

Redis 服务默认启用密码认证（`requirepass ${REDIS_PASSWORD:-changeme}`），Web/Worker 的连接串自动带上
该密码；**公网部署必须覆盖默认值 changeme**。如需 TLS 或 Redis 6+ ACL，把 `REDIS_URL` 改成
`rediss://用户名:密码@redis:6379/0` 形式（就绪检查与 arq 均按 DSN 解析）。

```bash
cd deploy
docker compose up -d --build
docker compose ps
```

Compose 启动 Web、arq Worker、PostgreSQL、Redis 和 Caddy。Web 和 Worker 使用同一数据库、Redis、题库目录和数据卷。
数据卷保存 API 密钥加密文件和本地文件。题库源文件只读，PostgreSQL 会自动建立查询镜像和 `pg_trgm` 索引。首次公网部署
请使用空 PostgreSQL 数据库，原 SQLite 账号和学习记录需单独迁移；完成账号初始化后再开放注册。

独立部署时，先安装 `pip install -r requirements.txt -r deploy/requirements.txt`。配置 `DATABASE_URL` 即启用 PostgreSQL，
配置 `REDIS_URL` 即启用 Redis 投递。Worker 在 Linux 上运行 `arq deploy.worker.WorkerSettings`，并使用与 Web 相同的运行配置。
Worker 每 30 秒写入 `service_heartbeat` 心跳，供就绪检查判断执行进程是否可用；超过 90 秒没有心跳会标记为异常。
Worker 支持 `STUDY_WORKER_CONCURRENCY`（默认 8）和 `STUDY_WORKER_JOB_TIMEOUT`（秒，默认 1800）。
已进入执行状态的任务若因 Worker 中断而退出，可从任务列表续跑，笔记和打标任务恢复已完成的检查点；同一任务重试按 `job_id`
幂等写入，只保留一份最终笔记或讲解产物。

## 5.1 校勘修订进入题库版本

1. 管理员在「内容管理 → 校勘」审核题目的修订，状态推进到「已发布」。
2. 点击「导出已发布修订」，把下载的 NDJSON 保存到题库根目录的 `releases\revisions\<时间戳>.jsonl`。
   该目录只存放已审核修订导出，不参与快照内容摘要。
3. 构建下一版题库时，构建器缺省按修改时间升序读取 `releases\revisions\` 下全部导出并**逐条重放**，
   因此连续发版会继承上一版已生效的修订；也可显式传 `--revisions a.jsonl,b.jsonl` 指定顺序。
4. 构建产物中的 `revision_report.json` 记录 applied / conflict / pending 三类结果与修订链；
   应用侧 `/api/admin/revisions` 与校勘队列的「生效于版本 / 待进入构建」徽章读取该报告。
5. 修订被判定为冲突（`content_revision`/`answer_revision` 对不上）时应人工核对：可能是该题已被
   后续修订覆盖，或题库在两次导出之间被重建。

容器部署时 `releases\revisions\` 需要可写挂载（或构建在宿主机执行）；应用容器内的题库挂载保持只读。

## 5.2 志愿数据（可选）

「志愿查询」使用 Hugging Face 数据集 `choucsan/Gaokao-Compass-11M`（MIT 许可）导入的只读库
`admission.sqlite3`，默认位于 `STUDY_DATA_DIR`（容器内 `/data`）。该文件不参与热备份，可随时重建：

1. 在宿主机导入（推荐，避免容器内长期占用下载带宽）：

   ```powershell
   cd study-platform
   python scripts/import_admission.py            # 下载约 1.35GB，首次约 10–20 分钟
   ```

2. 把生成的 `data/admission.sqlite3` 放进容器数据卷（`zhixu_data`）：

   ```powershell
   docker compose cp data/admission.sqlite3 app:/data/admission.sqlite3
   docker compose restart app
   ```

   也可以在容器内执行（镜像已包含 `scripts/`，需要容器可访问 Hugging Face）：

   ```powershell
   docker compose exec app python scripts/import_admission.py
   ```

3. 未放置该文件时「志愿查询」页显示导入指引，接口 `/api/admission/meta` 返回 `ready:false`，
   其余功能不受影响；不要把 2.6GB 的录取库打进镜像。
4. 数据为 MIT 许可（Copyright (c) 2025 choucisan），分发镜像或派生数据时保留项目根目录的
   `THIRD_PARTY_NOTICES.md`；页面已标注来源与非官方免责声明。

## 6. 可选功能

| 配置 | 行为 |
|---|---|
| `STUDY_AI_URL_POLICY` | AI 自定义地址策略：留空随 `STUDY_PUBLIC_MODE` 自动决策（本地 `local_open` / 公网 `public_https`），可显式设为 `local_open`、`public_https` 或 `allowlist`（严格托管，配合 `STUDY_AI_ALLOWED_HOSTS` 与管理页白名单）。 |
| `STUDY_AI_MAX_ACTIVE_PER_USER` | 每用户同时运行任务数上限（默认 2；取消中的任务也占配额）。 |
| `STUDY_AI_GLOBAL_CONCURRENCY` | 全站同时执行 AI 任务数上限（默认 8）。 |
| `STUDY_OCR_SCALE` | PDF 渲染清晰度倍率（默认 2.0，范围 0.5–4.0；越高识别率越好、越慢）。 |
| `STUDY_OCR_PAGE_TIMEOUT` | 单页 OCR 模型调用超时秒数（默认 120）。 |
| `STUDY_SYMPY_POOL` | 是否启用 SymPy 常驻子进程池（默认启用；`0` 关闭）。 |
| `STUDY_SYMPY_POOL_SIZE` | 常驻 SymPy 进程数（1–2，默认 1；并发评测可设 2）。 |
| `STUDY_SYMPY_POOL_MAX_REQUESTS` | 单进程处理多少次请求后回收重建（默认 200）。 |
| `STUDY_AI_ALLOWED_HOSTS` | `allowlist` 策略下的额外放行域名（逗号分隔，支持通配符如 `*.openai.azure.com`）。 |
| `S3_BUCKET` | 新上传文件写入私有 S3 桶。通过 AWS 凭据链或 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_SESSION_TOKEN` 授权，可设 `AWS_REGION` 与 `S3_PREFIX`。MinIO 使用 `S3_ENDPOINT_URL`，例如容器可达的 `http://minio:9000`。先创建桶，Web 与 Worker 使用一致的桶配置。历史本地文件仍从共享数据目录读取。 |
| `STUDY_EMBEDDING_BASE_URL`、`STUDY_EMBEDDING_MODEL`、`STUDY_EMBEDDING_API_KEY` | 启用 OpenAI 兼容 Embedding 接口，例如 API base `https://api.openai.com/v1`。新上传及 OCR 校对后的资料分块文本会发送给配置的服务。未配置或接口失败时回退到本地 TF-IDF。已有资料可重新上传或校对 OCR 以建立稠密向量。 |
| `COMMUNITY_EXPLANATIONS_ENABLED=1` | 开启社区讲解。满足分享条件的题库讲解由生成者主动分享，可撤回。管理员可设为平台精选。部署默认关闭社区讲解。 |
| `STUDY_REVIEW_SCHEDULER=fsrs-lite` | 达到全站 2,000 条由原卷或 AI 交叉核验判定的题库作答后启用 FSRS-lite。默认使用掌握度规则。 |

## 7. 依赖与镜像

- 生产依赖锁定在 `requirements.txt`，开发/验收依赖拆到 `requirements-dev.txt`（不进入镜像）。
- PostgreSQL 使用官方 `postgres:16-alpine` 镜像：应用未使用向量扩展，`pg_trgm` 为官方镜像自带 contrib；
  首次连接时由应用创建库表，`pg_schema.sql` 只负责 `CREATE EXTENSION`（可在初始化脚本中执行）。
- 搜索索引（`bank_fts`、`bank_leak_flags`、`bank_search_meta`）在应用库中按题库内容摘要自动构建，
  题库版本切换后首次请求会重建一次（约 0.5–1.5s），之后检索稳定在 50ms 内。

笔记逐块 JSON 抽取由用户在每次生成时勾选，界面会显示预估调用次数。提示词模板可从「我的模板」主动分享，管理员可将公开模板设为推荐。

PostgreSQL 部署使用 `pg_dump` / `pg_restore`。部署启用对象存储时，备份还应包含 S3 桶、`api-key.key` 和仍在数据卷中的历史文件。
