export const routes = {
  home: ['学习概览', 'home'],
  practice: ['题库练习', 'book'],
  admission: ['志愿查询', 'compass'],
  wrongbook: ['错题复习', 'repeat'],
  upload: ['拍题讲解', 'scan'],
  notes: ['知识清单', 'notes'],
  settings: ['模型设置', 'sliders'],
  admin: ['内容管理', 'shield'],
};

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
  noteJobId: null,
  uploadJobId: null,
  // 跨页面/跨账号的视图状态：登出或 401 过期时必须清空，避免下一个账号
  // 直接落到上一个账号停留的「题目视图 / 后台分区 / 笔记分区」。
  practiceView: null,
  resultsScrollTop: 0,
  activeEventSources: new Set(),
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
  state.currentQuestion = null;
  state.selectedQid = null;
  state.questionUpload = null;
  state.selectedNote = null;
  state.noteJobId = null;
  state.uploadJobId = null;
  state.questionJobId = null;
  state.filters = {};
  state.noteTab = 'all';
  state.notePreselectKp = null;
  state.practiceView = null;
  state.resultsScrollTop = 0;
  state.search = null;
  state.page = 1;
  state.seenReviewQids = new Set();
  knownBankVersion = null;
  for (const fn of [...sessionResetHooks]) {
    try { fn(); } catch { /* 单个页面清理失败不影响登出 */ }
  }
  try { sessionStorage.removeItem('zhixu.uploadState'); } catch { /* ignore */ }
}
