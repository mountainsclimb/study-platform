# Shared implementation contract

All JSON routes below use `/api`. Frontend uses `fetch` same-origin with credentials; error format is FastAPI `{detail: string}`. Do not embed data using innerHTML without escaping. Python modules are imported as `app.*`. Source bank is read-only; do not modify it. No new dependencies are to be installed.

## Infrastructure (root owns)

`app.db` exports `DATA_DIR: Path`, `BANK_DIR: Path` (source bank directory), `connection()` (context manager yielding SQLite connection, row_factory=sqlite3.Row; commit on success), `utcnow()` (ISO UTC string), `init_db()`. DATA_DIR may be overridden by `STUDY_DATA_DIR`; BANK_DIR by `STUDY_BANK_DIR`. App db filename `app.sqlite3`.

`app.auth.current_user(request)` is a FastAPI dependency returning `dict(id:int, username:str, role:'admin'|'student')`. `require_admin` also exported. All private routes depend on current_user. `GET /api/auth/me` → user; `POST /api/auth/register` {username,password} → user; `POST /api/auth/login` same → user; `POST /api/auth/logout`; HttpOnly cookie. `GET /api/health`. `GET /api/admin/overview` returns counts. First registered user is admin (local bootstrap), documented in UI/README.

Root `main.py` calls `practice.init_practice()` and `ai.init_ai()` in lifespan and includes `practice.router`, `ai.router`; serves web/index.html and assets at `/static`. `/` returns index. Source bank never mounted as a directory. Each owner initializes their own tables. Share schema changes via this file and message.

## Practice owner: app/practice.py

Routes: `GET /api/catalog` → {subjects:[], types:[], provinces:[], years:[], total:int, difficulties:['待标注','简单','中等','困难']}; `GET /api/questions` query subject,type,province,year,difficulty,q,page,page_size → {items,total,page,page_size}; `GET /api/questions/{qid:path}` (qid URL encoded) → safe question with group material, options, images, has_answer, has_solution, unlocked; answer/solution excluded unless unlocked. Avoid greedy path collisions: action routes use body qid.

`POST /api/attempts` multipart Form qid, answer(optional string JSON array or text), file(optional UploadFile) → {attempt,question,result}; reject empty submit, validate choice labels. `GET /api/reveal?qid=...` only after attempt. `POST /api/mastery` JSON {qid,level:'未掌握'|'初步掌握'|'比较掌握'|'熟练掌握'}; high levels need latest attempt result='correct'. `GET /api/wrongbook` query subject, due_only → {items,total}; `POST /api/wrongbook` {qid,reason?} manual add; `GET /api/reviews` query subject,type,province,year,difficulty,q → {items,total} limited due recommendation separate from search list; `GET /api/stats` → overview.

`GET /api/bank-image?subject=...&file=...` checks read path and serves only bank image assets. `GET /api/attempt-image/{id}` owner only.

r5 source-bank read compatibility (2026-11-21): the published r5 package stores every questions/groups column as TEXT (`objective='0'`, `order_in_paper='10'`, `year='2010'`). The read layer normalizes instead of trusting Python truthiness or lexical ordering: `practice._flag()` maps flags to real bool (`'0'/''/None` → False, `'1'/'true'/numbers` → True); `year`, `order_in_paper`, `pua_remaining` become ints (`_int_or_none`); ordering and range filters use `CAST(NULLIF(TRIM(col),'') AS INTEGER)` on SQLite and the INTEGER column on the PostgreSQL mirror (`_int_sql`). Choice grading requires `objective` true AND non-empty options — `objective='0'` questions that still carry options are not graded as choice (they stay `pending`). `search_similar_questions` ranks candidates by FTS rank (SQLite) / hit score (PostgreSQL) before `LIMIT 40`, so LaTeX-like stems are not pushed out. `app/db.py` mirrors r5 numeric columns as INTEGER (`PG_BANK_INTEGER_COLUMNS`, explicit `_mirror_int` conversion before COPY) and treats an all-TEXT mirror as stale when `PG_BANK_MIRROR_VERSION` changes, rebuilding it on next attach.

Unified question references and idempotent attempts (2026-11-21 Phase 1 remainder): `app/question_ref.py` parses `bank:<qid>` / bare qid (treated as bank, legacy-compatible) / `app:<pl_questions.id>` / `official:<id>` (Phase 1: parse-only; reads returned 404 「该来源题目暂未开放」——**Phase 3 起 official 读取已开放，见下方「Platform-selected questions」段落，替换此处的 parse-only 限制**). Every practice route that takes a `qid`/`ref` (`GET /api/questions/{qid:path}`, `GET /api/reveal`, `GET /api/attempts/history`, `POST /api/attempts`, `POST /api/mastery`, `POST /api/wrongbook`, `GET /api/bank-image?qid=app:<id>`) accepts these refs: bank reads stay read-only, `app:` reads go through paperlab ownership (own question, `deleted_at IS NULL`; foreign/deleted = **404**, never a 403 existence leak), malformed refs = **422**. Storage keys stay backward compatible — `attempts`/`wrong_questions`/`mastery` keep the bare qid for bank questions and `app:<id>` for personal ones, and question projections add `ref`/`source`/`source_label` without renaming `qid`. `POST /api/attempts` also accepts optional form `submission_id` (≤160 chars of `A-Za-z0-9._:+-`); `submission_ledger(user_id, kind='attempt', ledger_key = <storage key>|<submission_id>)` is the uniqueness source, so retries and concurrent duplicates replay the recorded attempt with `duplicate:true` and never insert a second row (no `submission_id` keeps the old non-idempotent behaviour). `GET /api/review/queue` merges bank and personal questions into one review queue (`items` graded, `pending_items` ungraded and excluded from wrong-question scheduling; entries carry `ref/source/source_label/graded`), and `GET /api/stats` adds `app_attempts/app_practiced/app_wrong_count` while keeping existing fields.

Public Python helpers consumed by AI owner: `load_question(qid: str) -> dict` full question (trusted backend, includes answer/solution); `ensure_unlocked(user_id:int,qid:str) -> None` raises HTTPException 403 unless valid attempt; `latest_attempt(user_id:int,qid:str) -> dict|None`; `set_ai_grade(user_id:int,attempt_id:int,verdict:str,report:dict) -> None` verdict 'correct'|'incorrect'|'pending', only updates if proper owner/current attempt, evidence stored; `get_attempt_image(user_id:int,attempt_id:int) -> Path|None`. Practice JSON question includes qid, subject, year, province, type, stem_plain, options:[{label,text}], images:[{file,role,...}], group:{material_text,material_images,parent_stem,...}|null. The group projection MUST strip children answer fields. No answer in unsubmitted responses.

Platform-selected questions, Phase 3 (2026-11-21): `official:<public_id>` is now a first-class read source via the read-only adapter `app/official.py` (`load_official_question` / `search_official` / `inspect_official`); it never writes plaza tables. Readable statuses are exactly `app.plaza.db.PUBLIC_STATUSES` (`community_review`/`verified`/`published`) with a non-empty `public_id`, and `published` additionally requires an **active** `plaza_releases` pointer — evaluated at query time with no process cache, so withdrawal/dispute takes effect immediately for detail, list, attempts and paper snapshots (all **404**). `GET /api/questions` gains `source=bank|official|all` (default `bank` keeps the old contract unchanged; `official` supports only subject/type/keyword/paging and returns `source/bank_total/official_total/notice`; `all` merges bank-first with a strict combined offset; invalid source = **422**). `POST /api/attempts` and the other ref-taking practice routes accept `official:` refs with the same gates as bank/app questions; the storage key in `attempts`/`wrong_questions`/`mastery` is `official:<public_id>`, wrongbook and `GET /api/review/queue` merge the source, and withdrawn questions degrade to a placeholder card (history kept, no restricted body). `GET /api/stats` adds `official_attempts/official_practiced/official_wrong_count` without changing existing fields. `GET /api/bank-image` does **not** serve official images; official public images are served by the plaza image endpoints (§18).

## AI owner: app/ai.py

`GET /api/providers` → {items:[{id,name,kind,base_url,models:[str],key_hint}],presets:[...]}; `POST /api/providers` JSON {name,kind:'openai'|'anthropic'|'gemini',base_url,api_key,models:[str]} → redacted saved provider; `DELETE /api/providers/{id}`. One Key supports many models. Strict ownership. Encrypt saved key with local Fernet key (not sent to frontend). Public HTTPS preset hosts or env STUDY_AI_ALLOWED_HOSTS; no arbitrary private/loopback endpoints. API adapters use httpx explicit timeouts and no redirects. Model IDs user-supplied, no fabricated discovery. `POST /api/providers/{id}/test` {model} explicit user action returns connectivity result, never automatic paid requests.

`POST /api/ai/jobs` JSON {task:'explain'|'grade'|'question-analysis'|'notes'|'recognize'|'tagging'|'ocr',qid?,attempt_id?,document_id?,upload_id?,provider_id,model,verify_provider_id?,verify_model?,cross_verify:bool,template?,requirements?} → job {id,status}; `GET /api/ai/jobs` → {items}; `GET /api/ai/jobs/{id}` → {id,status,stage,result,error,...}. Tasks nonblocking with durable job status in SQLite; stale running jobs marked interrupted at startup. Do not claim queue supports multi-process resumability. For grade, cross verification required; only different provider/model pair allowed, same identity not independent. Both independent graders receive same evidence/rubric, neither sees other output. Strict JSON grading schema and agreement on correctness AND scores; pending on errors/disagreement/absent rubric or ungradable image. Question bank AI endpoints ensure_unlocked. Jobs store models/prompt version/usage; no secret logging. AI result includes Markdown teaching text plus verification details when requested.

`POST /api/uploads/question` multipart file → {id,filename,media_type,preview_url,extra_previews,extracted_text,status}; supports image or text (explicit error for unsupported; ≤5 files, ≤12MB/file → 413, ≤25MP and ≤40MB total → 422, server EXIF-normalizes and compresses to 2048). `GET /api/uploads/{id}` private response (owner only).

OCR-first intake (2026-11-21): `GET /api/uploads/{id}/intake` returns the full intake state (`status, revision, confirmed, draft, pending_draft, ai_suggestion, confirmed_at, saved_question_id, saved_at`); `POST /api/uploads/{id}/ocr-draft` builds/refreshes a local-OCR draft with no vision key (OCR unavailable returns HTTP 200 with `ocr_available=false` and keeps the original image; draft creation never bumps `intake_revision`); `POST /api/uploads/{id}/ai-answer` stores a separate AI answer suggestion (`ai_suggestion_json`, never merged into the confirmed draft or question content); `PATCH /api/uploads/{id}/confirm` writes the human-confirmed draft (`confirmed_json`) and bumps `intake_revision`; `POST /api/uploads/{id}/save-to-bank` creates a `pl_questions` row and registers the original image as a `pl_assets` asset. `confirm`/`ai-answer`/`save-to-bank` accept optional `expected_version` (= current `intake_revision`) and return **409** on mismatch; save-to-bank requires a confirmed draft (**409** otherwise), is idempotent via `saved_question_id`, and returns **422** for an empty stem / **502** when registering the original image or writing the `pl_questions` row fails. A `recognize` job (submitted via `POST /api/ai/jobs`) may only overwrite `recognized_json`/`draft_json` while the revision is unchanged and `confirmed_json` is empty; otherwise it writes `pending_draft_json` and reports `draft_applied=false` — it never overwrites human confirmation.

AI usage ledger, quotas and concurrency, Phase 3 (2026-11-21): `app/ai/usage.py` owns two app-db tables — `ai_job_usage` (one idempotent row per **terminal** job: `job_id` unique, kind/model/status/duration/retries/token sums/optional `cost_micro`/`meta_json`; `meta_json` only stores classification/counts such as `input_kind ∈ text|image|pdf|audio|none`, models, verify flags, retry count and `prompt_version` — never question text, document bodies, file names or other user content) and `ai_quotas` (per-kind `daily_limit`/`concurrency_limit` overrides, 0 = unlimited). `app/ai/jobs.py` hooks: `check_new_job()` before the job INSERT in the same transaction (over quota/concurrency → **429** `{code:'ai_quota_exceeded', kind, message}`, nothing persisted and nothing consumed), `note_job_active()` after creation, `record_job_usage()` on every terminal path (completed/failed/cancelled/interrupted, including startup reaping), `check_resume()` before a resume and `clear_job_usage()` before re-dispatch. Quota priority is db override → `STUDY_AI_QUOTA_<KIND>` / `STUDY_AI_CONCURRENCY_<KIND>` env → built-in default → unconfigured (warn, no block); daily usage counts `ai_jobs` of the **UTC calendar day** whose status is not `failed/cancelled/interrupted`, so failed or cancelled work never consumes quota; concurrency counts queued/running/cancel-requested/awaiting-outline jobs from the database (in-process registry fallback). Routes: `GET /api/ai/usage/me` (own usage + today's quota; `current_user`), `GET /api/admin/ai/usage` (site-wide totals, P50/P95, by kind/model/day, pagination; `require_admin`), `GET|POST /api/admin/ai/quotas` (view/adjust/reset; `require_admin`). Ledger writes never affect the job state machine; ledger retention defaults to 180 days (`STUDY_AI_USAGE_RETENTION_DAYS`, ≤0 keeps forever). API detail: `INTEGRATION-API-INDEX.md` §19; invariants: Phase 3 section below.

`POST /api/documents` multipart file → {id,title,text_excerpt,pages,status}; PDF/DOCX/TXT/MD supported from installed libraries with limits and page/chunk provenance. `GET /api/documents` → {items}. `GET /api/notes` → {items} includes official published plus own; `GET /api/notes/{id}` private/official; `PATCH /api/notes/{id}` {title?,content?}; `POST /api/notes/{id}/publish` admin only (official origin explicit, audit); `DELETE /api/notes/{id}` owner/admin policy. `GET /api/templates` → {items:[{id,name,description}]}.

Notes tasks run extract → outline → generate → validate citations, save draft note linked to source pages/chunks. Show incomplete processing and factual unverified claims. Support user task styles (knowledge checklist, study notes, exam focus, mistakes) + custom requirements, without system override or exposing other users' sources. Uploaded data untrusted. Scanned PDF with no text returns needs OCR rather than fake success. Keep citations with document/page/chunk IDs; do not fabricate sources.

## Admission owner: app/admission.py

志愿查询 is read-only over `data/admission.sqlite3` (built by `scripts/import_admission.py` from Hugging Face dataset `choucsan/Gaokao-Compass-11M`, MIT). The file is created inside `db.DATA_DIR`; the app never writes it, and `data/` stays gitignored. Missing file: `GET /api/admission/meta` returns `{ready:false,detail:<import hint>}` and every data route raises 503 with the import command — never fabricate empty success. All routes depend on `current_user`.

Routes (prefix `/api/admission`): `GET /meta` → {ready,dataset,source_url,license,imported_at,schema_version,years,provinces:[{slug,name}],rows:{table:int},total_rows,overview_years,coverage:[{province,year,tables:{...}}]}; `GET /filters?province&year` → {categories,batches,rank_supported,control_score_supported,plan_supported,category_available,counts}; `GET /overview?province&year` → {counts,overview:原 meta.json payload}; `GET /score-rank?province&year&category&score` → {exact,nearest_score,batch,control_score,segment_count,cumulative_count,rank_range,total,matches,distribution}; `GET /schools?province&year&category&score|rank&band&keyword&page&page_size` → {mode,rank_supported,items:[{university_name,min_score,min_rank,delta|ratio,level:'冲'|'稳'|'保',is_985,is_211,school_province,school_nature}],total}; `GET /school?province&year&category&name` → {university,cutoffs,majors,plans,plan_summary,trend}; `GET /majors` and `GET /plans` accept keyword/university/category + page/page_size and return {items,total,total_people?}; `GET /suggest?q` → {universities,majors}; `GET /trend?province&university&category`; `GET /control-lines?province&year&category` → {items:[{category,batch,score,source_url}],official:{available,source,rows,years}}; `GET /gaps` → {available,summary,by_year,province_years:[{province,year,tables,missing_tables,rank,control,plan,category}],official:{...}}（读取 `data/admission-gaps.json`，由 `scripts/report_data_gaps.py` 生成；未生成时 available=false 并给出生成命令）。

官方补采批次线来自独立只读库 `data/admission-official.sqlite3`（`scripts/collect_official_data.py` + `scripts/import_official_data.py`，来源教育在线公开页面，保留 URL/抓取时间/SHA-256）。该库缺失时应用自动忽略。`/meta` 增加 `official:{available,rows,years,...}`；`/filters` 增加 `official_control_supported`，且 `control_score_supported = score-range 有批次线 or 官方补采有记录`；`/score-rank` 增加 `control_batch`、`control_source`（`score-range` / `official` / `official-inferred`）与 `official_batches`。当一分一段整表没有批次名（2017–2021 常见）时，按「该分数能过线的最严批次」补齐并标记 `official-inferred`，前端必须显示「官方补采，按分数匹配批次」，不得冒充原表批次。

Data caveats are product requirements: 2017–2021 多省缺 `min_rank` 与 `control_score`（`rank_supported=false` 时前端必须引导改用分数模式）；2023–2025 部分省份缺 `plan_count`（`plan_supported=false`，显示「未提供」，不得补零）；个别省年 `category` 整列为空（如河南 2024 录取表）时忽略科类筛选并返回 `category_filtered:false`。页面顶部必须按当前省年展示「位次 / 批次线 / 科类 / 计划人数」四项完整度。数据非官方；页面与 README 必须写明来源、MIT 许可（Copyright (c) 2025 choucisan）与「以官方公布为准」，完整声明见 `THIRD_PARTY_NOTICES.md`，分发时不得删除。`tests/test_admission.py` builds a tiny fixture DB instead of downloading 1.35GB. `scripts/admission_smoke.py` exercises a real imported `admission.sqlite3` through TestClient: it hardlinks the DB (and `admission-official.sqlite3` when present) into a sibling temp data dir (never writes the production `app.sqlite3`), asserts 72 checks by default, and `--sweep` covers all 232 province-year filter combinations. When `data/admission-gaps.json` exists it is copied in and the gap declaration is asserted too.

## Frontend owner web/*

Chinese responsive application, cohesive dark navy sidebar / light work surface with teal/indigo accents. Routes as tabs: 学习概览、题库练习、物理演示、AI 课堂、我的题库、试卷工坊、错题复习、学情分析、志愿查询、拍题讲解、知识清单、AI 助手、任务中心、模型设置、管理. Login/register modal or screen. Use real APIs, clear empty/errors/loading, no invented completion or sample official notes. Safe escaped text/Markdown. Visible task-specific buttons, answers only after successful submission, image upload option, multi-choice support. Question detail renders placeholder figures/formulas by role plus material, accessible labels. Difficulty unknown displayed 待标注. Persist chosen filters and AI model selection locally WITHOUT keys. API key entry password and reset after saving. Poll own jobs boundedly. Implement all requested flows and adapt errors without fake success.

Phase 1/2 frontend additions (2026-11-21): the sidebar groups routes as 学习 (`home/practice/wrongbook/mybank/submission`) / 内容 (`upload/paperlab/classroom/physics/plaza`) / 工具 (`insight/review/plaza-admin/notes/assistant/tasks/admission/settings/admin`), and `web/js/core/state.js` keeps the only public-route whitelist (`PUBLIC_ROUTES = {'plaza'}`). Public route `plaza` (广场 list/detail) renders for anonymous visitors inside a minimal guest shell (brand + plaza entry + language/theme + login/register; no personal nav, focus timer or daily quote), while `submission` (我的投稿), `review` (评审台) and `plaza-admin` (编辑台) are login-only workbenches whose sidebar entries are filtered by `/api/plaza/me` capabilities (`review`/`editor`). Logged-out access to a non-public route falls back to the login screen and returns to the original hash after login.

Phase 3 frontend additions (2026-11-21): the signed-in shell mounts a notification bell (`web/js/components/plazaNotifications.js`, unread badge, 60s polling skipped while the tab is hidden; hidden when the API is unavailable; cleaned up on logout and never mounted in the guest shell); the plaza detail page adds favorite/issue/report actions (login required; anonymous clicks route to login and return to the question); the editor workbench adds `quality` (sampling queue + check form + auto-withdraw notice), `ops` (issue/report/appeal queues) and `metrics` sections plus a publish-history rollback dialog, gated by `/api/plaza/me` `capabilities.quality|ops|metrics|rollback`; the submission page shows the author appeal form/records for `rejected/withdrawn/disputed`; the task center shows today's usage/quota panel, and the admin page adds an AI usage/quota section (14-day totals, by-kind table, editable quotas). The practice page exposes the question-source filter (`bank`/`official`/`all`), question detail shows official source/version/status and the 「收录到我的题库」 action for bank questions, paperlab compose offers the official source tab, and the home caliber card shows an official practice count only when the API probe succeeds — none of these may fabricate numbers when the backend is unavailable.

### Physics demos (web/js/pages/physics.js + components/physicsPlayer.js)

`#/physics` renders 10 deterministic physics demos from vendored assets `web/vendor/physics-in-motion/{physics-core.js,demos.js}` (upstream physics-in-motion, MIT, Copyright (c) 2026 xuxuezhou; commit recorded in the vendor README). `physics-core.js` is a framework-free ESM bundle (~38KB) exporting `buildSim`; **no React/Next runtime or build step**. The player computes `SimResult` in the browser and renders SVG scene, controls (play/pause/step/speed/timeline/event marks), toggle overlays, v-t/s-t/a-t charts, energy bars, quantities, phase equations (KaTeX), solution steps (click seeks the animation) and `sim.checks`. Question detail shows a 「物理演示」 entry only when `subject === '物理'`. Assets must stay same-origin (CSP `script-src 'self'`); do not load from CDN. Attribution is registered in `THIRD_PARTY_NOTICES.md` and the vendored LICENSE must be kept when distributing.

### Integration modules (AI 课堂 / 试卷工坊 / 学情 / AI 助手)

`INTEGRATION-CONTRACT.md` is the authoritative contract for the six integrated modules and is maintained
alongside this file. Summary: `app/classroom/**` (`cls_*` tables, `course` job, `/api/classroom`, interactive
scene HTML with its own CSP), `app/paperlab/**` (`pl_*` tables, `paper-import` / `paper-enrich` /
`paper-compose` / `paper-revision` / `paper-annotate` jobs, `/api/paperlab`), `app/insight.py`
(`/api/insight`, `insight-report` job, no own tables), `app/assistant.py` (`asst_*` tables,
`assistant-turn` job, `/api/assistant`, whitelisted tools only). Extension jobs register via
`app.ai.jobs.register_task_handler()`; `JobInput.params` carries per-task JSON parameters. All new private
tables carry `user_id` and are ownership-checked; DDL is idempotent via `con.executescript`.
Do not add module-specific table creation to `app/db.py` — each `init_<module>()` owns its schema.

Round additions (2026-11-21): paperlab attempts freeze the paper into `pl_attempt_items` at creation (legacy
attempts fall back with `frozen=false` + Chinese notice), `pl_assets` owns question images with unified 404 for
unauthorized reads, classroom deletion reclaims only unreferenced media and serves only content-sniffed inline
types, upload intake stores drafts/AI suggestions separately from human confirmation, and
`app/config_guard.py` fail-closes public-mode startup. These are spelled out in the new section below and in
`INTEGRATION-CONTRACT.md` §4.1/§4.2. The later Phase 1-remainder and Phase 2 additions (question references,
idempotent ledger, personal versions, home stats, `app/plaza/**`) are specified in the second invariant section
below and in `INTEGRATION-API-INDEX.md` §15–§16.

Communicate changes to API shapes before relying on them. Each agent owns only assigned files and tests; do not revert another agent's edits.

## Round invariants (2026-11-21 · r5 baseline + P0 hardening)

These invariants supplement the per-owner sections above; where an older statement is now narrower, the rules
here are authoritative. They are implemented and tested in the current working tree; the round record is
`docs/知序优化实施记录-20261121.md` and the integration detail lives in `INTEGRATION-CONTRACT.md` §4.1/§4.2.

1. **Question-image asset ownership (`pl_assets`).** New question images get a server-issued, unguessable `asset_id` (`app/paperlab/assets.py`); `content.images[i].asset` stores only that token. Reads (`GET /api/paperlab/questions/{id}/images/{index}`) verify in one step that the question belongs to the caller and is not deleted, that the asset row exists and is `active`/`published`, that the owner is the caller (or the asset is `public_derivative` and published), and that the reference appears in that question's content; any failure is **404**, so other users' assets are never confirmed to exist. Question create/edit validates refs (**422** for foreign/unknown/disabled ones); legacy filename refs pass only when editing old content unchanged and must be claimed with `python scripts/migrate_paperlab_assets.py --dry-run` then `--apply` (idempotent, content untouched; exit 1 on registration conflicts, 2 when `pl_assets` does not exist yet). Registered legacy refs stay readable.
2. **Whole-paper attempt freeze.** `POST /api/paperlab/papers/{id}/attempts` freezes, in the same write transaction, one `pl_attempt_items` row per position (question ref, `snapshot_json`, `score/max_score`, image refs) and the paper metadata into `pl_paper_attempts.paper_snapshot_json`. After that, edits/deletes/reorders/score changes to the paper — and deleting the paper — do not affect an in-progress or finished attempt: get/patch/submit/result/history/image routes all prefer the frozen rows, and grading uses the frozen reference answers and scores. Every attempt payload carries `frozen`, `paper_deleted` and a Chinese `notice`; legacy attempts without frozen rows fall back to the live paper with `frozen=false` and a 「历史题面不可追溯」 notice (never a 500). `DELETE /api/paperlab/papers/{id}` keeps attempts and returns `attempts_kept`; `pl_paper_attempts.paper_id` no longer has `ON DELETE CASCADE` (old SQLite databases are migrated once by `ensure_tables()`, PostgreSQL drops the constraint). New route `GET /api/paperlab/attempts/{attempt_id}/items/{item_position}/images/{image_index}` serves frozen attempt images.
3. **Classroom asset reclaim and media-type control.** Deleting a course (`DELETE /api/classroom/courses/{id}`) first resolves surviving references from the user's other courses/scenes, reassigns shared assets to a keeper course, keeps a file if another asset row uses the same `storage_name`, and only deletes rows/files with no surviving reference (post-commit re-check for concurrent writers); the response carries a Chinese audit `{examined, retained, reclaimed, shared_files_kept, files_removed, file_errors}`. `GET /api/classroom/assets/{id}` sniffs the real type from bytes: only the inline whitelist (png/jpeg/webp/gif, common audio, `video/mp4`) is served inline; everything else is `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` (active content downgraded to `application/octet-stream`) + `Content-Security-Policy: default-src 'none'; sandbox`, and missing files return a Chinese 404. Course ZIP import ignores declared MIME types, classifies by content, downgrades spoofed/active content to isolated downloads and reports `asset_warnings` (≤20). Course detail adds `missing_asset_ids`/`warnings` when referenced assets are gone. Reference images are read with a bounded reader (≤8MB, else **413**) and compressed to max side 1600 / JPEG 85; ASR audio is bounded at 25MB (**413**).
4. **Backup manifest v2.** `python -m app.ops backup` writes `manifest.format_version=2` plus `key_present`, `bank_version`, `bank_source`, `directories` (`{folder: {file_count, bytes}}` for `_BACKUP_FOLDERS` = `attempts`/`ai-files`/`issues`/`paperlab`/`classroom`/`official-sources`) and `excluded_entries` (actually present entries intentionally not backed up: admission databases/gaps/cache, `backups/`, WAL/SHM). `verify`/`restore` return these fields and still verify/restore format-v1 packages (`format_version` reported as 1, empty bank/directory info); `restore` also reports `restored_directories`. S3 references in `pl_pages`/`pl_questions`/`cls_assets` are still detected and fail the local ZIP backup explicitly (`ExternalStorageBackupRequired`), and `S3_BUCKET`/PostgreSQL deployments must use native snapshots — never a silent "backup succeeded".
5. **No public-mode default can take effect silently.** `app/main.py` lifespan runs `config_guard.enforce_public_config(admin_exists=auth._admin_exists)` right after `db.init_db()`: in public mode (`STUDY_PUBLIC_MODE` truthy) explicit errors raise `ConfigGuardError` and stop startup — wildcard/absent or local-only allowed hosts, `STUDY_DOMAIN` not in allowed hosts, `FORWARDED_ALLOW_IPS=*` or 0-prefix, non-PostgreSQL `DATABASE_URL`, example secrets, example/empty Redis password, no admin initialization path, and `STUDY_AI_URL_POLICY=local_open` (unknown policy values are a promoted warning that falls back to `public_https`). Missing `DATABASE_URL`/`REDIS_URL` is an error when a deployment layout is detected (`STUDY_DATA_DIR`/`STUDY_BANK_DIR` set) and a promoted warning otherwise; local mode triggers nothing. `python scripts/check_public_config.py --env-file deploy/.env` runs the same rules before deployment (strict by default; derives `DATABASE_URL`/`REDIS_URL` from `PG_PASSWORD`/`REDIS_PASSWORD` like Compose) and exits 0 pass / 1 public-config errors / 2 usage or local mode without `--allow-local`. Compose requires the public keys via `${VAR:?}`.
6. **Upload/intake versioning (pointer).** See the AI owner section: `intake_revision`, `pending_draft_json`, `expected_version` 409, confirmed-only save-to-bank, idempotent `saved_question_id`.
7. **r5 read-layer types (pointer).** See the Practice owner section: `objective` bool normalization, numeric `year/order`, `CAST(... AS INTEGER)` ordering, and the PostgreSQL mirror INTEGER rebuild.

Do not weaken these invariants in later modules: new bank-derived reads must go through the same normalization helpers, new private assets must be registered in `pl_assets`, and new long-running mutations must freeze what they grade.

## Round invariants (2026-11-21 · Phase 1 remainder + Phase 2 plaza/review)

These invariants extend the section above for the Phase 1-remainder and Phase 2 delivery. Where a statement in the
older sections is narrower, the rules here are authoritative. Implementation record:
`docs/知序优化实施记录-20261121-第二轮.md`; API detail: `INTEGRATION-API-INDEX.md` §15–§16; module contract:
`INTEGRATION-CONTRACT.md` §4.2/§4.3.

1. **Unified question identity and one-time booking.** `app/question_ref.py` is the single parser for
   `bank:<qid>` / bare qid (bank, legacy-compatible) / `app:<pl_questions.id>` / `official:<id>` (Phase 2:
   parse-only, reads 404; **superseded by the Phase 3 section below — official reads are now open**). Bank questions
   keep their bare qid in `attempts`/`wrong_questions`/`mastery` (old data and old
   clients keep working); personal questions use `app:<id>`. Practice reads of `app:` questions always resolve
   ownership through paperlab (own + not deleted), with **404** for foreign/deleted questions and **422** for
   malformed refs. `POST /api/attempts` accepts optional form `submission_id`; `submission_ledger`
   (`PRIMARY KEY(user_id, kind, ledger_key)`) guarantees one booking per `(user, question, submission_id)` for
   both single attempts (`kind='attempt'`) and whole-paper item booking (`kind='paper_item'`,
   key `paper:<attempt_id>:<item_position>`); duplicate/concurrent requests replay the recorded result with
   `duplicate:true` and never insert a second attempt or wrongbook increment. `GET /api/review/queue` merges
   bank and personal questions into one queue and separates ungraded (`pending_items`) from graded items.
2. **Personal collection versions and 「有新版」.** Collecting a bank question into `pl_questions`
   (`POST /api/paperlab/questions/collect`) snapshots the current content plus `source_ref/source_kind/
   source_question_id/source_bank_version/source_content_revision/source_answer_revision/source_meta_json/
   collected_at/content_sha256` and is idempotent per `(user, source_ref)` — duplicate collects return the
   existing item (`already_collected:true`) instead of overwriting. `pl_question_versions` is append-only
   history (`UNIQUE(question_id, version)`, `origin ∈ create|edit|collect|source_update|snapshot`) with no edit
   endpoint; reads of legacy rows append a `snapshot` version once. `GET .../source-status` reports
   `source_available`/`has_new_version`/collected-vs-current revisions and a field-level `diff` without echoing
   restricted bank text; `POST .../update-from-source` requires explicit `confirm:true`, creates `version+1`
   and leaves historical versions and existing practice attempts bound to the old content (unconfirmed **422**,
   version conflict **409**, already-latest `updated:false`). List/detail responses carry
   `has_new_version`/`source_available`.
3. **Plaza submission state machine, thresholds and fairness.** `plaza_submissions.status` moves
   `draft → pending_initial → community_review → verified → published` with `disputed/rejected/withdrawn` and
   `pending_release` branches. Only `community_review` is reviewable and only `verified`/`pending_release` is
   publishable. The pilot threshold is ≥5 independent reviews, ≥4 approves, ≥2 subject-reviewer approves and
   zero unresolved rejects; a subject-reviewer reject moves the submission to `disputed`; after 7 days without
   reaching the threshold the status simply stays unchanged (no auto pass/fail). One review per
   `(submission, version, reviewer)` (DB unique constraint); authors cannot review their own submissions, and
   editors cannot initial-review their own submission or publish a submission they authored or reviewed.
   Votes are hidden from a reviewer until they submit their own; `conflict_declared` votes are kept for audit but
   never counted as independent votes. Every status change writes a `plaza_events` row in the same transaction.
4. **Public visibility and public images (incl. community-pending).** Anonymous readers see only
   `community_review` (labelled 「社区待核验」/`review_status=community`, with aggregate review progress),
   `verified` and `published` (`review_status=featured`); list/detail exclude all other statuses, and
   `disputed`/`withdrawn` links return HTTP 200 with `visible:false` plus a Chinese notice (no silent 404 and no
   restricted body). Public payloads never expose internal `user_id`, usernames, reviewer identities, model
   configuration or storage paths. Public images are **derivatives only**: on entering a public status the
   service copies authorized personal assets into `pl_assets.public_derivative=1,status='published'` (sha256
   verified, ≤20MB) and maps them in `plaza_public_assets(submission_id,version,image_index,...)`; unshareable
   references are marked `unavailable` with a reason code and never block review. `GET /api/plaza/assets/{id}`
   and `GET /api/plaza/questions/{public_id}/images/{index}` require the submission to be publicly visible, the
   mapping to belong to the current version and be `active`, and the derivative to be readable; all failures
   are **404**, non-whitelisted media types **415**, responses are `Cache-Control: no-store`. Withdrawal
   immediately invalidates every public read (list/detail/images) while history and scores are kept.
5. **Guest shell, login return and capability-gated workbench.** `web/js/core/state.js` owns the only
   public-route whitelist (`PUBLIC_ROUTES`, currently just `plaza`); anonymous visitors render public routes in a
   minimal guest shell (brand/plaza entry/language/theme/login/register, no personal nav or widgets), and any
   non-public route falls back to login while remembering the original hash so login returns to it. Logged-in
   navigation is grouped (学习/内容/工具); `review` (评审台) and `plaza-admin` (编辑台) entries and pages are
   gated by `/api/plaza/me` capabilities, never by client-side identity labels. Plaza identities
   (`teacher` / `student_teacher` / `student` / `contributor`) are display-only and never grant permissions;
   permissions come from `plaza_roles` (`reviewer`/`subject_reviewer` with subject, `editor`) or the existing
   `admin` role.
6. **Home statistics caliber.** `GET /api/insight/home` (`app/home_stats.py`) is the only source for the
   home-page numbers: bank “可练/可自动判分/待核验” reuse practice's runtime gates (answer-leak flags, hard/temporary
   isolation, missing group material, missing/placeholder answer, data-quality flags), so 可自动判分 + 待核验 =
   可见题数 and excluded questions are reported separately; personal metrics use `attempts` as the single source
   (first-attempt accuracy counts only each question's first graded attempt; repeat practice/due/pending counts
   per question; today's count adds classroom quizzes and submitted whole-paper attempts once), weak knowledge
   points reuse the insight `<70%` caliber with real sample counts and `sample_limited` under 3 graded samples.
   Every number is returned with a Chinese `caliber` explanation; frontend pages must render these values rather
   than estimating or duplicating them locally.

## Round invariants (2026-11-21 · Phase 3 official sources + quality ops + AI usage)

These invariants extend the two sections above for the Phase 3 delivery (platform-selected questions, plaza quality
operations, AI usage/quotas). Where an older statement is narrower, the rules here are authoritative. Implementation
record: `docs/知序优化实施记录-20261121-第三轮.md`; API detail: `INTEGRATION-API-INDEX.md` §17–§19; module contract:
`INTEGRATION-CONTRACT.md` §3.3/§4.2/§4.6.

1. **Official question reads and visibility.** `app/official.py` is the only read adapter for platform-selected
   questions; it is read-only and never writes plaza tables. `official:<public_id>` is readable iff the submission status
   is one of `PUBLIC_STATUSES` (`community_review`/`verified`/`published`) **and** `public_id` is set, and `published`
   additionally requires an active `plaza_releases` pointer. Visibility is decided per query (no process cache), so a
   withdrawal or dispute immediately removes the question from list/detail/attempt/paper snapshot reads with **404** and
   never echoes restricted bodies; public images are invalidated the same way. Historical attempts/wrongbook/mastery rows
   keyed `official:<public_id>` are kept after withdrawal and rendered as placeholder cards with a Chinese notice
   (`inspect_official` returns status only). `published_version` in detail/snapshot payloads is the active release
   version, not a client-supplied value.
2. **Source-filter semantics on `GET /api/questions`.** `source=bank` remains the default and is byte-for-byte the old
   contract (structures, filters, `review_slots`). `source=official` lists only publicly readable selected questions and
   applies only `subject`/`type`/`q`/paging (bank-specific filters and sort semantics are explicitly not applicable);
   `source=all` merges bank rows first then official rows using a strict combined offset and reports `bank_total` and
   `official_total`. Unknown `source` is **422**, unknown `type` is **422**. Question-type filters accept both paperlab and
   practice type names. Frontends must capability-probe the parameter (invalid value → 422) before showing source filters,
   and must never relabel default bank results as official.
3. **Official attempts, wrongbook and stats.** `POST /api/attempts` (and mastery/wrongbook/manual-add) accept
   `official:` refs with the same grading gates as bank/app questions (objective choice only when `objective` and options
   and parsable answers agree; `BLANK` uses text rules; subjective stays `pending`). The storage key is
   `official:<public_id>`; idempotency is `submission_ledger(user_id,'attempt','official:<public_id>|<submission_id>')`.
   Wrongbook and `GET /api/review/queue` merge the source and carry `ref/source/source_label`; `GET /api/stats` adds
   `official_attempts/official_practiced/official_wrong_count` while existing counters keep their meaning (official work
   is also counted there). `GET /api/bank-image` must not be used for official images; official issue/report actions go
   through the plaza endpoints, not the bank content-issue endpoint.
4. **Official snapshots in paperlab.** A paper item may reference `official:<public_id>`; `split_ref` validates the public
   id (**422** malformed) and the snapshot freezes the published content plus `public_id`/`published_version`/
   `review_status`/`source_declaration` and public image references (never private paths). Frozen papers and whole-paper
   attempts keep their frozen content when the selected question is later revised or withdrawn; only public image reads
   re-check plaza visibility and return **404** after withdrawal. Grading uses the frozen answers; incorrect/pending
   official items join the unified `wrong_questions` and the submit response reports `official_bank_unlinked`/
   `official_wrong_added`. Paper item projections keep `source_label/public_id/review_status/published_version`.
5. **Quality sampling (pilot 100%) and automatic withdrawal.** Piloting uses `QUALITY_SAMPLING_MODE='initial_100'` with
   `QUALITY_SAMPLING_REQUIRED=True` (every published question must be independently sampled); only editors/admins may
   record checks, and a checker cannot sample their own submission (**403**). Only questions with `status='published'`
   and an active release are checkable (**409** otherwise). An `issue` decision carries a severity (`low/medium/high`,
   defaulting to `medium` when omitted) and requires at least one finding (**422** otherwise). A `high` severity issue
   **automatically withdraws** the active release in the same transaction (release pointer + submission status +
   `plaza_events` + author/favoriter notifications, emergency mode, `auto_withdrawn=true`); lower severities only notify
   the author. Sampling constants are product defaults and may be tuned by operations without changing the state machine.
6. **Notifications, favorites and feedback privacy/state.** Notifications are written only to the directly involved user
   (author, favoriter, reviewer themselves, reporter) and never expose another user's identity, review text or internal
   ids; writes happen in the same transaction as the state change and notification failure must not be silently
   interleaved with success. Favoriting is allowed only while the question is currently publicly visible (otherwise
   **409**), is idempotent per `(user, submission)`, and records are kept (with `available:false` + notice) after a
   withdrawal; public detail exposes `favorited` (anonymous false) and `favorite_count`. Issue/report targets must be
   publicly visible (unknown **404**, not visible **409**); appeals are author-only and only for
   `rejected/withdrawn/disputed` (pending appeal is idempotent); editor queues mask the reporter/author
   (`db.masked_display`) and processing is single-shot (`409` on repeat, reject requires a reason) with a notification to
   the reporter/author. `plaza_events` remains the audit trail for every transition.
7. **Rollback state machine.** `POST /api/plaza/submissions/{id}/rollback` may only be used by an editor/admin who is not
   the author (**403**). It requires a current active release, an earlier withdrawn release with a lower version and its
   version snapshot; otherwise **409** (with a JSON detail). In one transaction it withdraws the current release with
   `withdraw_mode='rollback'`, re-activates the previous release/pointer, restores the submission's version/snapshot/
   status to `published`, re-registers public figures, writes `release_rolled_back`, and notifies the author and
   favoriters (`favorite_restored`). Conditional UPDATEs guarantee only one winner under concurrency, and
   `plaza_releases.active_public_id` remains unique (one active release at a time). Withdrawal and rollback both affect
   only the pointer/status; public reads and the official source re-evaluate at query time, and history is retained.
8. **AI usage ledger, quota and concurrency.** Every terminal AI job writes one idempotent `ai_job_usage` row (unique
   `job_id`) covering completed/failed/cancelled/interrupted; ledger write failures never affect the job state machine or
   SSE results. The ledger stores model/status/duration/retries/token sums/optional cost estimate and classification-only
   metadata (`input_kind`, model list, verify/mode flags, prompt version) — **never** question text, document bodies,
   file names or other user content. Daily quotas and per-kind site concurrency are checked **before** inserting a job in
   the same transaction; a rejection returns **429** `{code:'ai_quota_exceeded', kind, message}` and consumes nothing.
   Only non-failed/non-cancelled/non-interrupted jobs from the UTC calendar day count against the quota, so failures and
   cancellations never consume quota; concurrency counts queued/running/cancel-requested/awaiting-outline jobs. Quota
   precedence is db override → env (`STUDY_AI_QUOTA_*`/`STUDY_AI_CONCURRENCY_*`) → built-in default → unconfigured
   (warn, do not block); `0` means unlimited. `GET /api/ai/usage/me` is self-only, `/api/admin/ai/*` requires admin, and
   cost is `null` unless both model prices are configured (never fabricated as zero). Ledger retention defaults to 180
   days; the legacy per-call `ai_usage` table keeps its own meaning and is not merged with the ledger.

