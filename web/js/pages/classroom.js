/* AI 课堂（整合自 OpenMAIC）：课程列表 → 创建课堂 → 生成工作台（大纲确认）→ 课堂播放器 →
 * 幻灯片结构化编辑器（P6a）。
 *
 * 路由（hash 参数）：
 *   #/classroom                      课程列表
 *   #/classroom?new=1                创建课堂
 *   #/classroom?course=ID&view=generate[&job=JOBID]   生成工作台（大纲确认 / 进度 / 单场景重生成）
 *   #/classroom?course=ID&view=play  课堂播放器（进入时自动创建/复用学习会话）
 *   #/classroom?course=ID&view=edit[&scene=SID]       幻灯片结构化编辑器
 *
 * 播放器、生成工作台与编辑器分别拆到 components/classroomPlayer.js / classroomWizard.js /
 * classroomEditor.js；本页只负责路由、列表、创建表单与「编辑幻灯片」入口。
 */
import { api } from '../core/api.js';
import { onSessionReset } from '../core/state.js';
import {
  el, heading, button, badge, notice, empty, loading, busy, toast,
  field, input, select, dateText, number,
} from '../core/dom.js';
import { t } from '../core/i18n.js';
import { current, go, renderRoute } from '../core/router.js';
import { createWizardView } from '../components/classroomWizard.js';
import { createPlayerView } from '../components/classroomPlayer.js';
import { createEditorView } from '../components/classroomEditor.js';
import {
  closeCanvasEditor,
  focusPlayerScene,
  openCanvasEditor,
} from '../components/classroomCanvas.js';
import { COURSE_STATUS, MODE_LABELS, statusBadgeParts } from '../components/classroomShared.js';

const PAGE_SIZE = 12;
let activeCleanup = null;
let classroomMetaPromise = null;

function destroyActive() {
  if (typeof activeCleanup === 'function') {
    try { activeCleanup(); } catch { /* 清理失败不影响新页面 */ }
  }
  activeCleanup = null;
  try { closeCanvasEditor(); } catch { /* 自由画布模态未打开时忽略 */ }
}

/** GET /classroom/meta 的能力位（interactive_mode / images / limits），只在会话内缓存一次。 */
function loadClassroomMeta() {
  if (!classroomMetaPromise) {
    classroomMetaPromise = api('/classroom/meta').catch(() => null);
  }
  return classroomMetaPromise;
}

function courseIsInteractive(course) {
  return course?.interactive_mode === true || String(course?.mode || '') === 'interactive';
}

// 路由切换（hashchange）与登出/401 时都要销毁播放器/工作台的监听与定时器。
window.addEventListener('hashchange', () => destroyActive());
onSessionReset(() => {
  destroyActive();
  classroomMetaPromise = null;
});

function statusBadge(course) {
  const status = statusBadgeParts(course.status, COURSE_STATUS);
  return badge(status.label, status.variant);
}

function courseModeLabel(course) {
  return MODE_LABELS[course.mode] || '标准课堂';
}

function courseMetaLine(course) {
  return [
    course.subject || null,
    course.grade || null,
    courseModeLabel(course),
    `${number(course.scene_count)} 个场景`,
    course.updated_at ? `更新于 ${dateText(course.updated_at)}` : null,
  ].filter(Boolean).join(' · ');
}

async function renderList(container, version, params) {
  const page = Math.max(1, Number(params.get('page')) || 1);
  const pageRoot = el('div', { class: 'cls-page' });
  container.replaceChildren(pageRoot);
  const importInput = el('input', { type: 'file', accept: '.zip,application/zip', hidden: true });
  const importButton = button('导入课堂 ZIP', () => importInput.click(), 'secondary', 'upload');
  importInput.addEventListener('change', async () => {
    const file = importInput.files && importInput.files[0];
    importInput.value = '';
    if (!file) return;
    if (file.size > 200 * 1024 * 1024) { toast('课程包不能超过 200MB', true); return; }
    const form = new FormData();
    form.append('file', file);
    importButton.disabled = true;
    try {
      const result = await api('/classroom/import', { method: 'POST', body: form });
      const imported = result.course || {};
      const sceneCount = Number(result.scene_count) || 0;
      toast(`导入完成：${imported.title || '课程'}（${sceneCount} 个场景）`);
      go('classroom', null, {
        course: imported.id,
        view: sceneCount > 0 ? 'play' : 'generate',
      });
    } catch (error) {
      toast(error.message || '课堂 ZIP 导入失败', true);
    } finally {
      if (importButton.isConnected) importButton.disabled = false;
    }
  });
  pageRoot.append(importInput);
  pageRoot.append(heading('AI CLASSROOM', 'AI 课堂', '把任何主题或资料，变成可播放、可互动的课堂。',
    el('div', { class: 'row cls-list-heading-actions' },
      importButton,
      button('新建课堂', () => go('classroom', null, { new: 1 }), '', 'plus'))));
  const host = el('div', { class: 'cls-list-host' });
  pageRoot.append(host);
  host.append(loading('正在加载课程列表…'));

  let data;
  try {
    data = await api(`/classroom/courses?page=${page}&page_size=${PAGE_SIZE}`);
  } catch (error) {
    if (!current(version)) return;
    host.replaceChildren(
      notice(error.message || '课程列表加载失败，请重试。', 'error'),
      el('div', { class: 'row' },
        button('重新加载', event => busy(event.currentTarget, () => renderList(container, version, params)), '', 'repeat'),
        button('新建课堂', () => go('classroom', null, { new: 1 }), 'secondary', 'plus')));
    return;
  }
  if (!current(version)) return;

  const items = Array.isArray(data.items) ? data.items : [];
  const total = Number(data.total) || 0;
  if (!items.length) {
    host.replaceChildren(empty(
      '还没有创建课堂',
      '输入一个主题（或选择已有资料），AI 会先生成课程大纲，确认后逐场景生成幻灯片、测验与互动内容。',
      button('创建第一门课堂', () => go('classroom', null, { new: 1 }), '', 'presentation'),
      'presentation'));
    return;
  }

  const toolbar = el('div', { class: 'cls-list-toolbar row between' },
    el('p', { class: 'help' }, `共 ${number(total)} 门课程 · 第 ${page} 页`),
    el('div', { class: 'row' },
      button('刷新', event => busy(event.currentTarget, () => renderList(container, version, params)), 'secondary small', 'repeat')));

  const grid = el('div', { class: 'cls-course-grid' });
  for (const course of items) {
    const busyDelete = async (trigger) => {
      if (!window.confirm(`删除课程「${course.title || course.topic}」？场景与学习记录会一并删除。`)) return;
      try {
        trigger.disabled = true;
        await api(`/classroom/courses/${course.id}`, { method: 'DELETE' });
        toast('课程已删除');
        renderList(container, version, params);
      } catch (error) {
        toast(error.message || '删除失败', true);
      } finally {
        if (trigger.isConnected) trigger.disabled = false;
      }
    };
    const ready = course.status === 'ready' && Number(course.scene_count) > 0;
    grid.append(el('article', { class: 'card cls-course-card', 'data-course-id': String(course.id) },
      el('div', { class: 'cls-course-card-head' },
        el('div', { class: 'cls-course-card-title' },
          el('h3', {}, course.title || course.topic || '未命名课程'),
          statusBadge(course),
          courseIsInteractive(course) ? badge('深度交互', 'blue') : null,
          Number(course.source_count) > 0 ? badge(`联网来源 ${number(course.source_count)}`, 'teal') : null),
        el('p', { class: 'cls-course-topic' }, course.topic || '')),
      el('p', { class: 'help cls-course-meta' }, courseMetaLine(course)),
      course.description ? el('p', { class: 'cls-course-desc' }, course.description) : null,
      el('div', { class: 'row cls-course-actions' },
        ready
          ? button('进入课堂', () => go('classroom', null, { course: course.id, view: 'play' }), '', 'presentation')
          : button('继续生成', () => go('classroom', null, { course: course.id, view: 'generate' }), '', 'sparkle'),
        button('大纲与场景', () => go('classroom', null, { course: course.id, view: 'generate' }), 'secondary small', 'layers'),
        button('删除', event => busyDelete(event.currentTarget), 'ghost small', 'trash'))));
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pager = totalPages > 1
    ? el('div', { class: 'pagination cls-pagination' },
      el('span', {}, `第 ${page} / ${totalPages} 页`),
      el('div', { class: 'row' },
        button('上一页', () => go('classroom', null, { page: page - 1 }), 'secondary small', 'chevron', { disabled: page <= 1 }),
        button('下一页', () => go('classroom', null, { page: page + 1 }), 'secondary small', 'chevron', { disabled: page >= totalPages })))
    : null;

  host.replaceChildren(toolbar, grid, pager || el('span'));
  pageRoot.append(el('p', { class: 'help cls-export-note' },
    '课程生成后可导出 PPTX / 离线单文件 HTML / 课程 ZIP；ZIP 可随时在本页重新导入。'
    + '使用联网资料生成的课程会在导出文件中附带「参考来源」（PPTX 末页 / HTML 文末 / ZIP sources.json）。'));
}

// ---------------------------------------------------------------------------
// 创建课堂
// ---------------------------------------------------------------------------

const SUBJECT_OPTIONS = ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理'];
const GRADE_OPTIONS = [
  '小学一年级', '小学二年级', '小学三年级', '小学四年级', '小学五年级', '小学六年级',
  '初中一年级', '初中二年级', '初中三年级',
  '高中一年级', '高中二年级', '高中三年级',
];

async function renderCreate(container, version) {
  const pageRoot = el('div', { class: 'cls-page' });
  container.replaceChildren(pageRoot);
  pageRoot.append(heading('AI CLASSROOM', 'AI 课堂', '创建一门新课堂：先写清主题与要求，生成时可选择参考资料。',
    button('返回课程列表', () => go('classroom'), 'secondary small', 'chevron')));

  const meta = (await loadClassroomMeta()) || {};
  if (!current(version)) return;
  const interactiveInfo = meta.interactive_mode || {};
  const imagesInfo = meta.images || {};
  const searchInfo = meta.search || {};
  const imageMax = Number(meta.limits?.images_per_scene_max) || Number(imagesInfo.max_per_scene) || 2;

  const topicInput = input('例如：光合作用的原理与影响因素', { maxlength: 500, required: true });
  const titleInput = input('可选，默认取主题前 30 字', { maxlength: 60 });
  const subjectSelect = select(SUBJECT_OPTIONS.map(item => ({ value: item, label: item })), '生物');
  const gradeSelect = select(GRADE_OPTIONS.map(item => ({ value: item, label: item })), '初中二年级');
  const modeSelect = select([
    { value: 'standard', label: '标准课堂（幻灯片 + 测验）' },
    { value: 'interactive', label: '深度交互（含交互场景）' },
  ], 'standard');
  modeSelect.id = 'classroom-mode-select';
  const modeHint = el('p', { class: 'help cls-mode-hint', 'aria-live': 'polite' });
  const modeField = el('div', { class: 'field' },
    el('label', { for: 'classroom-mode-select' }, '课堂模式'),
    modeSelect,
    modeHint);
  const capabilityRow = el('div', { class: 'cls-capability-row' },
    el('span', { class: 'help' }, '当前能力：'),
    el('span', { class: `cls-capability-chip${interactiveInfo.enabled ? ' is-on' : ' is-off'}` },
      interactiveInfo.enabled
        ? `深度交互可用（${interactiveInfo.interactive_scenes_min ?? 1}–${interactiveInfo.interactive_scenes_max ?? 2} 个交互场景）`
        : '深度交互未启用'),
    el('span', { class: `cls-capability-chip${imagesInfo.enabled ? ' is-on' : ' is-off'}` },
      imagesInfo.enabled
        ? `AI 配图可用（${imagesInfo.provider || imagesInfo.model || '已配置连接'}，≤${imageMax} 张/页）`
        : 'AI 配图暂无图片连接'),
    el('span', { class: `cls-capability-chip${searchInfo.available ? ' is-on' : ' is-off'}` },
      searchInfo.available
        ? `联网资料可用（${searchInfo.provider || 'DeepSeek'} 搜索）`
        : '联网资料：未配置 DeepSeek 连接'));

  function refreshModeHint() {
    if (modeSelect.value === 'interactive') {
      if (!interactiveInfo.enabled) {
        modeHint.textContent = '当前实例未启用深度交互模式，将按标准模式生成。';
        return;
      }
      modeHint.textContent = `深度交互模式：全课安排 ${interactiveInfo.interactive_scenes_min ?? 1}–${interactiveInfo.interactive_scenes_max ?? 2} 个交互场景（优先中段），幻灯片自动追加「动手试一试」提示；超出上限的场景会被服务端改为幻灯片并在生成结果里给出 warnings。`;
      return;
    }
    modeHint.textContent = '标准课堂：以幻灯片 + 测验为主，不生成「动手试一试」提示块。';
  }
  modeSelect.addEventListener('change', refreshModeHint);
  refreshModeHint();

  const requirementsArea = el('textarea', { rows: 4, maxlength: 4000, placeholder: '补充教学要求，例如：面向初中生、重点讲清原料与产物、多举生活例子。' });

  const docsBox = el('div', { class: 'cls-doc-picker' }, el('p', { class: 'help' }, '正在加载资料库…'));
  const selectedDocs = new Set();

  async function loadDocs() {
    try {
      const data = await api('/documents');
      const docs = (data.items || []).filter(doc => Number(doc.chunks) > 0 && doc.status === 'ready');
      docsBox.replaceChildren();
      if (!docs.length) {
        docsBox.append(el('p', { class: 'help' }, '没有可用资料。可在「知识清单」页上传 PDF / Markdown / 扫描件（OCR 后）作为大纲参考。'));
        return;
      }
      for (const doc of docs) {
        const checkbox = el('input', {
          type: 'checkbox',
          onChange: () => {
            if (checkbox.checked) selectedDocs.add(doc.id);
            else selectedDocs.delete(doc.id);
          },
        });
        docsBox.append(el('label', { class: 'cls-doc-option' },
          checkbox,
          el('span', {}, doc.title || `资料 ${doc.id}`),
          el('span', { class: 'help' }, `${number(doc.chunks)} 个内容块`)));
      }
    } catch {
      docsBox.replaceChildren(el('p', { class: 'help' }, '资料库读取失败，可先不选资料直接创建。'));
    }
  }

  const submit = button('创建并进入生成', null, '', 'sparkle');
  submit.addEventListener('click', async (event) => {
    const trigger = event.currentTarget;
    const topic = String(topicInput.value || '').trim();
    if (topic.length < 2) { toast('课程主题至少 2 个字', true); topicInput.focus(); return; }
    try {
      trigger.disabled = true;
      const created = await api('/classroom/courses', {
        method: 'POST',
        data: {
          title: String(titleInput.value || '').trim() || undefined,
          topic,
          subject: subjectSelect.value,
          grade: gradeSelect.value,
          mode: modeSelect.value,
          requirements: String(requirementsArea.value || '').trim(),
          document_ids: [...selectedDocs],
        },
      });
      const courseId = created.course?.id;
      if (!courseId) throw new Error('课程创建返回缺少 id');
      toast('课堂已创建，请选择模型后生成大纲。');
      go('classroom', null, { course: courseId, view: 'generate' });
    } catch (error) {
      toast(error.message || '创建课堂失败', true);
    } finally {
      if (trigger.isConnected) trigger.disabled = false;
    }
  });

  pageRoot.append(el('div', { class: 'card cls-create-card' },
    el('div', { class: 'cls-create-grid' },
      field('课程主题（必填）', topicInput, '2–500 字；AI 会据此推断受众与教学风格。'),
      field('课程标题（可选）', titleInput),
      field('学科', subjectSelect),
      field('年级', gradeSelect),
      modeField,
      el('div', { class: 'full' }, capabilityRow),
      el('div', { class: 'full' }, field('补充要求（可选）', requirementsArea)),
      el('div', { class: 'full' }, field('参考已有资料（可选，最多 8 份）', docsBox,
        '资料会作为大纲生成的事实依据；不上传资料时按课题常识设计。'))),
    el('div', { class: 'row cls-create-actions' },
      submit,
      button('取消', () => go('classroom'), 'secondary'))));

  loadDocs();
}

// ---------------------------------------------------------------------------
// 生成工作台 / 播放器 / 幻灯片编辑器
// ---------------------------------------------------------------------------

/**
 * 「编辑幻灯片」入口：课程 ready 且存在 slide 场景时，在播放器/工作台页头插入一条入口栏
 * （含场景选择 + 编辑按钮）。课程详情接口是唯一真实来源，无法编辑时不渲染任何假按钮。
 * 播放器骨架是异步构建的，这里用短轮询等待 `.cls-player-top` 出现后再插入，避免被 replaceChildren 清掉。
 */
function mountEditorEntry(node, courseId, version) {
  let entry = null;
  let cancelled = false;
  let tries = 0;
  let timer = null;
  const stop = () => {
    cancelled = true;
    if (timer) { clearTimeout(timer); timer = null; }
  };
  api(`/classroom/courses/${courseId}`)
    .then((detail) => {
      if (cancelled || !current(version)) return;
      const course = detail?.course || {};
      const scenes = Array.isArray(detail?.scenes) ? detail.scenes : [];
      const slideScenes = scenes.filter(scene => String(scene.scene_type || scene.type || '') === 'slide');
      if (course.status !== 'ready' || !slideScenes.length) return;
      const allScenes = Array.isArray(detail?.scenes) ? detail.scenes : [];
      entry = buildEditorEntryBar(course, slideScenes, allScenes);
      const place = () => {
        if (cancelled || !current(version) || !entry) return;
        const anchor = node.querySelector('.cls-player-top, .cls-wizard-toolbar');
        if (!anchor) {
          if (tries < 40 && node.isConnected) {
            tries += 1;
            timer = setTimeout(place, 100);
          }
          return;
        }
        if (entry.parentNode !== node) node.insertBefore(entry, anchor);
      };
      place();
    })
    .catch(() => { /* 入口加载失败不影响播放/工作台的其它功能 */ });
  return () => {
    stop();
    if (entry) entry.remove();
    entry = null;
  };
}

// ---------------------------------------------------------------------------
// M4 课堂演示场景：把已保存的学习附件以「固定版本」插入课堂 / 删除演示场景
// ---------------------------------------------------------------------------
// - 附件列表 GET /api/artifacts?kind=<kind>&page_size=50（仅公开元数据，不含 data）；
// - 插入 POST /api/classroom/courses/{id}/scenes/demo（带上当前版本号 → 后端固定版本）；
// - 删除 DELETE /api/classroom/courses/{id}/scenes/{scene_id}（仅 demo 场景）；
// - 接口缺失/失败时只做中文降级提示，不伪造列表或插入结果。

const DEMO_ARTIFACT_KINDS = Object.freeze([
  'function_graph', 'whiteboard', 'flow_animation', 'isometric', 'science_demo', 'demo_3d',
]);

/** kind → 文案键 + 中文兜底（kind 是后端枚举，不直接拼进界面文案）。 */
const DEMO_KIND_META = Object.freeze({
  function_graph: { key: 'study.m4.kind.functionGraph', fallback: '函数图' },
  whiteboard: { key: 'study.m4.kind.whiteboard', fallback: '解题白板' },
  flow_animation: { key: 'study.m4.kind.flowAnimation', fallback: '流程动画' },
  isometric: { key: 'study.m4.kind.isometric', fallback: '等距结构' },
  science_demo: { key: 'study.m4.kind.scienceDemo', fallback: '科学演示' },
  demo_3d: { key: 'study.m4.kind.demo3d', fallback: '3D 演示' },
  mech_3d: { key: 'study.m5.kind.mech3d', fallback: '机械 3D' },
  factory_sim: { key: 'study.m5.kind.factorySim', fallback: '工厂仿真' },
  molecule_3d: { key: 'study.oss.kind.molecule', fallback: '分子模型' },
});

/** 取 `study.m4.*` 文案：locale 缺键时回退中文常量并做 `{name}` 插值。 */
function m4Text(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function demoKindLabel(kind) {
  const meta = DEMO_KIND_META[String(kind || '')];
  return meta ? m4Text(meta.key, meta.fallback) : m4Text('study.m4.kind.other', '学习附件');
}

/** 后端 meta.demo_artifact_kinds 优先（`{demo:[kind...]}` 或数组两种形状）；
 *  未声明（后端未就绪）时按 6 类演示附件探测。 */
function demoArtifactKinds(meta) {
  const declared = meta?.demo_artifact_kinds;
  let fromMeta = [];
  if (Array.isArray(declared)) fromMeta = declared;
  else if (declared && typeof declared === 'object') {
    fromMeta = Object.values(declared)
      .filter(Array.isArray)
      .flat();
  }
  const kinds = fromMeta.map(kind => String(kind || '').trim()).filter(Boolean);
  return kinds.length ? [...new Set(kinds)] : [...DEMO_ARTIFACT_KINDS];
}

function sceneKind(scene) {
  return String(scene?.scene_type || scene?.type || '');
}

function demoSceneTitle(scene) {
  const content = scene?.content && typeof scene.content === 'object' ? scene.content : {};
  const artifact = content.artifact && typeof content.artifact === 'object' ? content.artifact : {};
  return String(artifact.title || scene?.title || '').trim()
    || m4Text('study.m4.demoStage.untitled', '未命名演示');
}

function demoSceneRevision(scene) {
  const content = scene?.content && typeof scene.content === 'object' ? scene.content : {};
  const artifact = content.artifact && typeof content.artifact === 'object' ? content.artifact : {};
  const revision = Number(artifact.revision ?? content.fixed_revision);
  return Number.isFinite(revision) && revision > 0 ? Math.round(revision) : 0;
}

/** 并行读取演示类学习附件 → 去重合并（列表只含公开元数据：标题/kind/当前版本）。 */
async function loadDemoArtifactOptions() {
  const meta = await loadClassroomMeta();
  const kinds = demoArtifactKinds(meta);
  const results = await Promise.allSettled(
    kinds.map(kind => api(`/artifacts?kind=${encodeURIComponent(kind)}&page_size=50`)),
  );
  const seen = new Set();
  const failedKinds = [];
  const items = [];
  results.forEach((result, index) => {
    if (result.status !== 'fulfilled') { failedKinds.push(kinds[index]); return; }
    const list = Array.isArray(result.value?.items) ? result.value.items : [];
    for (const item of list) {
      if (!item || typeof item !== 'object') continue;
      const artifactId = String(item.artifact_id || '');
      if (!artifactId || seen.has(artifactId)) continue;
      seen.add(artifactId);
      items.push(item);
    }
  });
  items.sort((left, right) => String(right.updated_at || '').localeCompare(String(left.updated_at || '')));
  return { items, failedKinds, totalKinds: kinds.length };
}

/**
 * 「插入演示」弹层：选中一个学习附件 → 固定当前版本插入课堂。
 * @param {{courseId:number, onInserted?:(result:object)=>void}} options
 * @returns {HTMLDialogElement}
 */
function openDemoInsertDialog({ courseId, onInserted } = {}) {
  const dialog = el('dialog', {
    class: 'modal-dialog cls-demo-insert-dialog',
    'aria-label': m4Text('study.m4.classroom.insertDialogTitle', '插入学习附件演示'),
  });
  const listHost = el('div', { class: 'cls-demo-insert-list' },
    loading(m4Text('study.m4.classroom.insertLoading', '正在读取学习附件…')));
  const status = el('p', { class: 'help cls-demo-insert-status', role: 'status', 'aria-live': 'polite' });
  const titleInput = input(m4Text('study.m4.classroom.titlePlaceholder', '默认使用附件标题'), { maxlength: '120' });
  const noteInput = el('textarea', {
    class: 'cls-demo-insert-note',
    rows: 2,
    maxlength: '300',
    placeholder: m4Text('study.m4.classroom.notePlaceholder', '会显示在课堂演示底部，说明演示出处或使用建议'),
  });
  const confirmButton = button(m4Text('study.m4.classroom.insertConfirm', '插入课堂'), null, '', 'plus', {
    disabled: true,
    'data-demo-insert-confirm': 'true',
  });
  let selected = null;
  let loadSeq = 0;
  let submitting = false;
  const close = () => { try { dialog.close(); } catch { /* 已关闭时忽略 */ } };

  function setStatus(text, isError = false) {
    status.textContent = text || '';
    status.classList.toggle('is-error', Boolean(isError));
  }

  function renderOptions({ items, failedKinds, totalKinds }) {
    if (!items.length) {
      listHost.replaceChildren(
        el('p', { class: 'cls-demo-insert-empty' },
          m4Text('study.m4.classroom.insertEmpty', '请先在题目页把函数图/演示保存为学习附件，再插入课堂')),
        el('p', { class: 'help' },
          m4Text('study.m4.classroom.insertEmptyHint', '支持流程动画、等距结构、3D 演示、科学演示、函数图与解题白板。')),
        button(t('common.reload'), () => load(), 'secondary small', 'rotate'));
      if (failedKinds.length && failedKinds.length >= totalKinds) {
        setStatus(m4Text('study.m4.classroom.insertUnavailable', '学习附件暂不可用，请稍后重试。'), true);
      } else if (failedKinds.length) {
        setStatus(m4Text('study.m4.classroom.insertPartial', '部分类型读取失败，已列出可用附件。'));
      }
    } else {
      listHost.replaceChildren(
        el('p', { class: 'help cls-demo-insert-select-hint' },
          m4Text('study.m4.classroom.selectHint', '选择一个学习附件插入课堂；插入后会固定当前版本。')));
      const nodes = [];
      const options = el('div', {
        class: 'cls-demo-insert-items',
        role: 'group',
        'aria-label': m4Text('study.m4.classroom.insertDialogTitle', '插入学习附件演示'),
      });
      for (const item of items) {
        const artifactId = String(item.artifact_id || '');
        const revision = Number(item.current_revision) || 1;
        const metaParts = [
          demoKindLabel(item.kind),
          m4Text('study.m4.common.version', '版本 v{version}', { version: revision }),
          item.updated_at
            ? m4Text('study.m4.common.updatedAt', '更新于 {time}', { time: dateText(item.updated_at) })
            : null,
        ].filter(Boolean);
        const node = el('button', {
          type: 'button',
          class: 'cls-demo-insert-item',
          'aria-pressed': 'false',
          'data-artifact-id': artifactId,
          onClick: () => {
            selected = item;
            for (const other of nodes) {
              const active = other === node;
              other.classList.toggle('is-selected', active);
              other.setAttribute('aria-pressed', active ? 'true' : 'false');
            }
            confirmButton.disabled = false;
            setStatus('');
          },
        },
        el('span', { class: 'cls-demo-insert-item-main' },
          el('strong', { class: 'cls-demo-insert-item-title', 'data-no-i18n': 'true' },
            String(item.title || m4Text('study.m4.kind.other', '学习附件'))),
          el('span', { class: 'help cls-demo-insert-item-meta' }, metaParts.join(' · '))),
        el('span', { class: 'cls-demo-insert-check', 'aria-hidden': 'true' }, ''));
        nodes.push(node);
        options.append(node);
      }
      listHost.append(options);
    }
    if (items.length && failedKinds.length) {
      setStatus(m4Text('study.m4.classroom.insertPartial', '部分类型读取失败，已列出可用附件。'));
    }
  }

  async function load() {
    const seq = ++loadSeq;
    selected = null;
    confirmButton.disabled = true;
    listHost.replaceChildren(loading(m4Text('study.m4.classroom.insertLoading', '正在读取学习附件…')));
    setStatus('');
    try {
      const result = await loadDemoArtifactOptions();
      if (seq !== loadSeq || !dialog.isConnected) return;
      renderOptions(result);
    } catch (error) {
      if (seq !== loadSeq || !dialog.isConnected) return;
      listHost.replaceChildren(
        el('p', { class: 'cls-demo-insert-empty' },
          m4Text('study.m4.classroom.insertUnavailable', '学习附件暂不可用，请稍后重试。')),
        button(t('common.reload'), () => load(), 'secondary small', 'rotate'));
      setStatus(error?.message || '', true);
    }
  }

  confirmButton.addEventListener('click', async () => {
    if (!selected || submitting) return;
    const revision = Number(selected.current_revision) || 1;
    submitting = true;
    confirmButton.disabled = true;
    setStatus(m4Text('study.m4.classroom.inserting', '正在插入演示…'));
    try {
      const result = await api(`/classroom/courses/${courseId}/scenes/demo`, {
        method: 'POST',
        data: {
          artifact_id: String(selected.artifact_id),
          revision,
          title: String(titleInput.value || '').trim() || undefined,
          source_note: String(noteInput.value || '').trim() || undefined,
        },
      });
      const pinned = Number(result?.scene?.content?.artifact?.revision) || revision;
      submitting = false;
      toast(m4Text('study.m4.classroom.insertSuccess', '已插入演示（固定版本 v{revision}）', { revision: pinned }));
      close();
      await onInserted?.(result);
    } catch (error) {
      submitting = false;
      confirmButton.disabled = !selected;
      setStatus(error?.message || m4Text('study.m4.classroom.insertFailed', '插入演示失败，请重试'), true);
    }
  });

  dialog.append(
    el('div', { class: 'cls-demo-insert-head' },
      el('div', { class: 'cls-demo-insert-head-text' },
        el('h3', {}, m4Text('study.m4.classroom.insertDialogTitle', '插入学习附件演示')),
        el('p', { class: 'help' },
          m4Text('study.m4.classroom.insertHint', '从已保存的演示/函数图/白板附件中选择一个，插入课堂后会固定当前版本；之后附件升版不影响这一页。'))),
      button(t('common.close'), close, 'ghost small', 'close', { 'aria-label': t('common.close') })),
    listHost,
    el('div', { class: 'cls-demo-insert-form' },
      field(m4Text('study.m4.classroom.titleField', '场景标题（可选）'), titleInput),
      field(m4Text('study.m4.classroom.noteField', '插入说明（可选）'), noteInput)),
    status,
    el('div', { class: 'row end cls-demo-insert-actions' },
      button(t('common.cancel'), close, 'secondary'),
      confirmButton));
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
  dialog.showModal();
  load();
  return dialog;
}

/**
 * 在播放器/生成工作台工具区挂载「插入演示」入口与演示场景管理列表。
 * 播放器骨架是异步构建的，这里用短轮询等待锚点出现后再插入（与幻灯片编辑入口同一策略）。
 * @returns {() => void} 清理函数（路由切换时调用）
 */
function mountDemoTools(node, courseId, version) {
  let cancelled = false;
  let tries = 0;
  let timer = null;
  let panel = null;
  let courseScenes = [];

  const cleanup = () => {
    cancelled = true;
    if (timer) { clearTimeout(timer); timer = null; }
    if (panel) panel.remove();
    panel = null;
  };

  /** 插入/删除后刷新课程数据；targetSceneId 用于把播放器进度指向新场景/相邻场景。 */
  const refreshCourse = async (targetSceneId = 0) => {
    if (!current(version)) return;
    if (targetSceneId) {
      try {
        const detail = await api(`/classroom/courses/${courseId}`);
        if (!current(version)) return;
        await focusPlayerScene(courseId, targetSceneId, Array.isArray(detail?.scenes) ? detail.scenes : []);
      } catch { /* 定位失败不阻塞刷新 */ }
    }
    if (!current(version)) return;
    try { renderRoute(); } catch { /* 路由已变化时忽略 */ }
  };

  const deleteDemoScene = async (scene, scenesSnapshot) => {
    const title = demoSceneTitle(scene);
    if (!window.confirm(m4Text('study.m4.classroom.deleteConfirm',
      '删除课堂演示场景「{title}」？只移除这个场景，不会删除学习附件。', { title }))) return;
    try {
      await api(`/classroom/courses/${courseId}/scenes/${scene.id}`, { method: 'DELETE' });
      toast(m4Text('study.m4.classroom.deleteSuccess', '演示场景已删除'));
      const index = scenesSnapshot.findIndex(item => String(item.id) === String(scene.id));
      const remaining = scenesSnapshot.filter(item => String(item.id) !== String(scene.id));
      const target = remaining[Math.min(Math.max(index, 0), remaining.length - 1)];
      await refreshCourse(target?.id || 0);
    } catch (error) {
      toast(error?.message || m4Text('study.m4.classroom.deleteFailed', '删除演示场景失败，请重试'), true);
    }
  };

  const renderPanelList = (demos) => {
    const listHost = panel?.querySelector('.cls-demo-manage-list');
    if (!listHost) return;
    if (!demos.length) {
      listHost.replaceChildren(el('p', { class: 'help cls-demo-manage-empty' },
        m4Text('study.m4.classroom.demoManageEmpty',
          '还没有插入演示场景：点击「插入演示」把已保存的学习附件固定到课堂。')));
      return;
    }
    listHost.replaceChildren(...demos.map((scene) => {
      const revision = demoSceneRevision(scene);
      const metaParts = [
        m4Text('study.m4.scene.demo', '原理演示'),
        revision > 0
          ? m4Text('study.m4.demoStage.fixedVersion', '固定版本 v{version}', { version: revision })
          : null,
      ].filter(Boolean);
      return el('div', { class: 'cls-demo-manage-item', 'data-scene-id': String(scene.id) },
        el('div', { class: 'cls-demo-manage-main' },
          el('strong', { 'data-no-i18n': 'true' }, demoSceneTitle(scene)),
          el('span', { class: 'help' }, metaParts.join(' · '))),
        button(m4Text('study.m4.classroom.deleteDemo', '删除'),
          event => busy(event.currentTarget, () => deleteDemoScene(scene, courseScenes)),
          'ghost small', 'trash'));
    }));
  };

  const place = () => {
    if (cancelled || !current(version)) return;
    const anchor = node.querySelector('.cls-player-top, .cls-wizard-toolbar');
    if (!anchor) {
      if (tries < 40 && node.isConnected) {
        tries += 1;
        timer = setTimeout(place, 100);
      }
      return;
    }
    panel = el('section', {
      class: 'cls-demo-manage',
      'data-demo-tools': '1',
      'aria-label': m4Text('study.m4.classroom.panelTitle', '课堂演示场景'),
    },
    el('div', { class: 'cls-demo-manage-head' },
      el('div', { class: 'cls-demo-manage-text' },
        el('strong', {}, m4Text('study.m4.classroom.panelTitle', '课堂演示场景')),
        el('p', { class: 'help' },
          m4Text('study.m4.classroom.panelHint',
            '演示场景以固定版本渲染；附件后续升版不会改变课堂内容。删除只移除课堂场景，不删除学习附件。'))),
      el('div', { class: 'row cls-demo-manage-actions' },
        button(m4Text('study.m4.classroom.insert', '插入演示'), () => openDemoInsertDialog({
          courseId,
          onInserted: result => refreshCourse(Number(result?.scene?.id) || 0),
        }), '', 'layers', { 'data-demo-insert': 'true' }))),
    el('div', { class: 'cls-demo-manage-list' }, loading()));
    anchor.insertAdjacentElement('afterend', panel);
    api(`/classroom/courses/${courseId}`)
      .then((detail) => {
        if (cancelled || !current(version)) return;
        courseScenes = Array.isArray(detail?.scenes) ? detail.scenes : [];
        renderPanelList(courseScenes.filter(scene => sceneKind(scene) === 'demo'));
      })
      .catch(() => {
        if (cancelled || !current(version)) return;
        const listHost = panel?.querySelector('.cls-demo-manage-list');
        if (listHost) {
          listHost.replaceChildren(el('p', { class: 'help' },
            m4Text('study.m4.classroom.insertUnavailable', '学习附件暂不可用，请稍后重试。')));
        }
      });
  };
  place();
  return cleanup;
}

function buildEditorEntryBar(course, slideScenes, allScenes = []) {
  const sceneSelect = select(
    slideScenes.map((scene) => {
      const order = Number(scene.order || scene.order_index) || 0;
      return {
        value: String(scene.id),
        label: `${order ? `第 ${order} 页 · ` : ''}${scene.title || '未命名场景'}`,
      };
    }),
    String(slideScenes[0].id),
  );
  sceneSelect.classList.add('cls-edit-entry-select');
  sceneSelect.setAttribute('aria-label', '选择要编辑的幻灯片场景');
  const openCanvas = () => {
    const sceneId = Number(sceneSelect.value) || Number(slideScenes[0].id);
    const scene = slideScenes.find(item => String(item.id) === String(sceneId)) || slideScenes[0];
    openCanvasEditor({
      courseId: course.id,
      sceneId: scene.id,
      sceneTitle: scene.title || '未命名幻灯片',
      onSaved: () => {
        // 刷新前把学习会话进度指向刚编辑的场景，播放器刷新后直接展示新内容。
        focusPlayerScene(course.id, scene.id, allScenes)
          .catch(() => false)
          .finally(() => { try { renderRoute(); } catch { /* 路由已变化时忽略 */ } });
      },
    });
  };
  return el('div', { class: 'cls-edit-entry', role: 'note', 'aria-label': '幻灯片编辑入口' },
    el('div', { class: 'cls-edit-entry-text' },
      el('strong', {}, '幻灯片编辑'),
      el('span', { class: 'help' }, '「编辑幻灯片」为结构化编辑；「自由画布编辑」可任意拖拽/缩放排版，保存后播放器与导出立即生效。')),
    el('div', { class: 'cls-edit-entry-actions' },
      el('label', { class: 'cls-edit-entry-field' },
        el('span', { class: 'help' }, '编辑场景'),
        sceneSelect),
      button('编辑幻灯片', () => go('classroom', null, {
        course: course.id,
        view: 'edit',
        scene: sceneSelect.value,
      }), 'secondary', 'edit'),
      button('自由画布编辑', openCanvas, '', 'layers')));
}

function renderGenerate(container, version, courseId, jobId) {
  container.replaceChildren(heading('AI CLASSROOM', 'AI 课堂', '课程生成工作台：确认大纲后逐场景生成，可取消、可续跑、可单场景重生成。'));
  const wizard = createWizardView({ container, version, courseId, jobId });
  const detachEntry = mountEditorEntry(wizard.node, courseId, version);
  const detachDemo = mountDemoTools(wizard.node, courseId, version);
  activeCleanup = () => {
    try { detachEntry(); } catch { /* 清理失败不影响新页面 */ }
    try { detachDemo(); } catch { /* 清理失败不影响新页面 */ }
    try { wizard.destroy(); } catch { /* 清理失败不影响新页面 */ }
  };
}

function renderPlayer(container, version, courseId) {
  container.replaceChildren();
  const player = createPlayerView({ container, version, courseId });
  const detachEntry = mountEditorEntry(player.node, courseId, version);
  const detachDemo = mountDemoTools(player.node, courseId, version);
  activeCleanup = () => {
    try { detachEntry(); } catch { /* 清理失败不影响新页面 */ }
    try { detachDemo(); } catch { /* 清理失败不影响新页面 */ }
    try { player.destroy(); } catch { /* 清理失败不影响新页面 */ }
  };
}

function renderEditor(container, version, courseId, sceneId) {
  container.replaceChildren();
  const editor = createEditorView({ container, version, courseId, sceneId });
  activeCleanup = editor.destroy;
}

export async function renderClassroom(container, version, params) {
  destroyActive();
  const courseId = Number(params.get('course')) || 0;
  const jobId = Number(params.get('job')) || 0;
  const view = String(params.get('view') || '');
  if (courseId && view === 'edit') {
    renderEditor(container, version, courseId, Number(params.get('scene')) || 0);
    return;
  }
  if (courseId && view === 'generate') {
    renderGenerate(container, version, courseId, jobId || null);
    return;
  }
  if (courseId) {
    renderPlayer(container, version, courseId);
    return;
  }
  if (params.get('new')) {
    await renderCreate(container, version);
    return;
  }
  await renderList(container, version, params);
}
