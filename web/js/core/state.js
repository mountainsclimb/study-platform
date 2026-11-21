import { t } from './i18n.js';

/**
 * P8 i18n：路由标签不再硬编码中文。
 * `routes[key]` 仍是 `[label, icon]` 二元组（`core/router.js` 只读 `routes[key][0]`），
 * 但索引 0 用 getter 动态取 `t('nav.'+key)`：切语言后再次读取即为新语言，
 * 不需要改 router.js，也不破坏 `Object.entries(routes)` 等既有调用。
 * 中文原文保留为兜底：语言包尚未加载/缺键时仍显示中文。
 *
 * 导航按「三个用户动作」分组（方案 §1）：学习（拍题/我的题库/错题复习的日常入口）、
 * 内容（拍题入库与内容生产）、工具（学情/笔记/助手/任务/志愿/设置）。分组只决定
 * 侧栏渲染顺序，不新增未实现的路由；移动端侧栏与桌面共用同一份分组。
 */
const ROUTE_DEFS = [
  // 学习
  ['home', 'home', '学习概览'],
  ['practice', 'book', '题库练习'],
  ['wrongbook', 'repeat', '错题复习'],
  ['mybank', 'tag', '我的题库'],
  ['submission', 'file', '我的投稿'],
  // 内容
  ['upload', 'scan', '拍题入库'],
  ['paperlab', 'printer', '试卷工坊'],
  ['classroom', 'presentation', 'AI 课堂'],
  ['physics', 'atom', '物理演示'],
  // 广场（Phase 2）：公开只读页，访客与登录用户都可见。
  // 投稿/评审/编辑台为登录后工作台路由，不属于 PUBLIC_ROUTES（访客不可渲染）。
  ['plaza', 'layers', '广场'],
  // 工具
  ['insight', 'chart', '学情分析'],
  ['review', 'check', '评审台'],
  ['plaza-admin', 'shield', '编辑台'],
  ['notes', 'notes', '知识清单'],
  ['assistant', 'message', 'AI 助手'],
  ['tasks', 'history', '任务中心'],
  ['admission', 'compass', '志愿查询'],
  ['settings', 'sliders', '模型设置'],
  // 管理（仅管理员可见，渲染时按角色过滤）
  ['admin', 'shield', '内容管理'],
];

const ROUTE_FALLBACK_LABELS = Object.fromEntries(ROUTE_DEFS.map(([key, , fallback]) => [key, fallback]));
const ROUTE_ICONS = Object.fromEntries(ROUTE_DEFS.map(([key, icon]) => [key, icon]));

/** 导航分组（顺序 = 桌面/移动侧栏渲染顺序）；`label` 是 i18n 键，渲染时取 t()。 */
const NAV_GROUP_DEFS = [
  {
    key: 'learn',
    labelKey: 'nav.group.learn',
    routes: ['home', 'practice', 'wrongbook', 'mybank', 'submission'],
  },
  {
    key: 'content',
    labelKey: 'nav.group.content',
    routes: ['upload', 'paperlab', 'classroom', 'physics', 'plaza'],
  },
  {
    key: 'tools',
    labelKey: 'nav.group.tools',
    routes: ['insight', 'review', 'plaza-admin', 'notes', 'assistant', 'tasks', 'admission', 'settings', 'admin'],
  },
];

export const navGroups = NAV_GROUP_DEFS.map(group => ({
  key: group.key,
  labelKey: group.labelKey,
  // 只保留确实注册过的路由；管理员项由 main.js 按角色过滤。
  routes: group.routes.filter(key => key in ROUTE_FALLBACK_LABELS),
}));

/** 路由标题（导航/面包屑/文档标题共用）：优先语言包，缺键回退中文原文。 */
export function routeLabel(key) {
  const fallback = ROUTE_FALLBACK_LABELS[key] ?? key;
  const translated = t(`nav.${key}`);
  return translated === `nav.${key}` ? fallback : translated;
}

/** 路由图标名（`routes[key][1]` 保持不变，供侧栏图标使用）。 */
export function routeIcon(key) {
  return ROUTE_ICONS[key] ?? 'book';
}

function makeRouteEntry(key) {
  // 注意：数组长度必须保留 2，否则 `for (const [label, name] of Object.entries(routes))`
  // 的数组迭代器不会读到访问器属性。先放占位元素再定义为 getter。
  const entry = [ROUTE_FALLBACK_LABELS[key] ?? key, ROUTE_ICONS[key] ?? 'book'];
  Object.defineProperty(entry, 0, {
    configurable: true,
    enumerable: true,
    get: () => routeLabel(key),
  });
  return entry;
}

export const routes = Object.fromEntries(ROUTE_DEFS.map(([key]) => [key, makeRouteEntry(key)]));

/**
 * 访客（未登录）可只读打开的路由白名单。
 * 未登录访客只能渲染这里的公开页；其它路由在访客态一律回落到登录页，
 * 登录成功后仍按当前 hash 回到用户原本要去的页面。
 * 公开路由集合集中定义在这里，router / main / 页面模块不要各自维护白名单。
 */
export const PUBLIC_ROUTES = new Set(['plaza']);

/** 判断某个路由是否允许匿名访客只读渲染。 */
export function isPublicRoute(key) {
  return PUBLIC_ROUTES.has(String(key || ''));
}

export function readLocal(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

export function saveLocal(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Storage may be disabled. */
  }
}

export function removeLocal(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export const state = {
  user: null,
  // 访客态：未登录但正在浏览公开路由（广场）时为 true。登录/登出/会话过期都会清空。
  guest: false,
  route: 'home',
  catalog: null,
  knowledgePoints: null,
  explainModes: null,
  promptGrid: null,
  preferences: null,
  taskDefaults: {},
  providers: [],
  presets: [],
  // 后端 /api/providers 返回的地址策略：local_open / public_https / allowlist。
  urlPolicy: null,
  publicMode: null,
  selectedQid: null,
  currentQuestion: null,
  search: null,
  page: 1,
  pageSize: 12,
  renderVersion: 0,
  filters: {},
  seenReviewQids: new Set(),
  noteTab: 'all',
  selectedNote: null,
  notePreselectKp: null,
  questionJobId: null,
  questionUpload: null,
  // 拍题入库（OCR-first）当前草稿状态：识别草稿/人工确认/待确认模型草稿/入库结果。
  uploadIntake: null,
  noteJobId: null,
  uploadJobId: null,
  // 跨页面/跨账号的视图状态：登出或 401 过期时必须清空，避免下一个账号
  // 直接落到上一个账号停留的「题目视图 / 后台分区 / 笔记分区」。
  practiceView: null,
  resultsScrollTop: 0,
  activeEventSources: new Set(),
  // 整合模块（AI 课堂 / 试卷工坊 / 学情 / AI 助手 / 任务中心）的跨页面状态。
  classroomCourseId: null,
  classroomJobId: null,
  classroomSessionId: null,
  paperlabTab: 'imports',
  paperlabImportId: null,
  paperlabPaperId: null,
  paperlabJobId: null,
  mybankFilters: {},
  mybankSelected: new Set(),
  assistantConversationId: null,
  assistantJobId: null,
  insightSubject: '',
  tasksTaskFilter: '',
};

/* ------------------------------------------------------------------------- *
 * 会话清理与题库版本失效钩子
 * 页面模块把自己的模块级残留状态（如 revealedCache、adminTab、noteSection）
 * 注册进来：登出/401 时统一清空，换题库版本时让缓存失效。
 * ------------------------------------------------------------------------- */
const sessionResetHooks = new Set();
const bankChangeHooks = new Set();

export function onSessionReset(fn) {
  sessionResetHooks.add(fn);
  return () => sessionResetHooks.delete(fn);
}

export function onBankChange(fn) {
  bankChangeHooks.add(fn);
  return () => bankChangeHooks.delete(fn);
}

let knownBankVersion = null;
/**
 * 记录响应头 X-Zhixu-Bank 的题库版本；首次只建立基线，变化时让题库目录与
 * 已展开答案/解析缓存失效（后端热切换题库后旧目录不能继续使用）。
 */
export function noteBankVersion(version) {
  const next = String(version || '').trim();
  if (!next) return;
  if (!knownBankVersion) {
    knownBankVersion = next;
    return;
  }
  if (knownBankVersion === next) return;
  knownBankVersion = next;
  state.catalog = null;
  state.knowledgePoints = null;
  state.search = null;
  for (const fn of [...bankChangeHooks]) {
    try { fn(next); } catch { /* 缓存失效失败不影响请求主流程 */ }
  }
}

function filterStorageKey() {
  return state.user?.id ? `zhixu.filters.${state.user.id}` : null;
}

export function loadUserFilters() {
  const key = filterStorageKey();
  state.filters = key ? readLocal(key, {}) : {};
  return state.filters;
}

export function saveFilters() {
  const key = filterStorageKey();
  if (key) saveLocal(key, state.filters);
}

export function clearUserSession() {
  for (const es of state.activeEventSources) {
    try { es.close(); } catch { /* ignore */ }
  }
  state.activeEventSources.clear();
  state.user = null;
  // 会话被清空（登出/401 过期）后不再处于访客壳，避免旧公开页状态串到登录页。
  state.guest = false;
  state.catalog = null;
  state.knowledgePoints = null;
  state.explainModes = null;
  state.promptGrid = null;
  state.preferences = null;
  state.taskDefaults = {};
  state.providers = [];
  state.presets = [];
  state.urlPolicy = null;
  state.publicMode = null;
  // 广场能力（评审台/编辑台导航）随会话清空，避免退出后残留上一账号的入口。
  state.plazaCapabilities = {};
  state.currentQuestion = null;
  state.selectedQid = null;
  state.questionUpload = null;
  state.uploadIntake = null;
  state.selectedNote = null;
  state.noteJobId = null;
  state.uploadJobId = null;
  state.questionJobId = null;
  state.filters = {};
  state.noteTab = 'all';
  state.notePreselectKp = null;
  state.practiceView = null;
  state.resultsScrollTop = 0;
  // 整合模块的视图与运行中任务引用不能带到下一个账号。
  state.classroomCourseId = null;
  state.classroomJobId = null;
  state.classroomSessionId = null;
  state.paperlabTab = 'imports';
  state.paperlabImportId = null;
  state.paperlabPaperId = null;
  state.paperlabJobId = null;
  state.mybankFilters = {};
  state.mybankSelected = new Set();
  state.assistantConversationId = null;
  state.assistantJobId = null;
  state.insightSubject = '';
  state.tasksTaskFilter = '';
  state.search = null;
  state.page = 1;
  state.seenReviewQids = new Set();
  knownBankVersion = null;
  for (const fn of [...sessionResetHooks]) {
    try { fn(); } catch { /* 单个页面清理失败不影响登出 */ }
  }
  try { sessionStorage.removeItem('zhixu.uploadState'); } catch { /* ignore */ }
}
