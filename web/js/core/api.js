import { state, clearUserSession, noteBankVersion } from './state.js';
import { el } from './dom.js';

let authExpiredHandler = null;
export function onAuthExpired(fn) {
  authExpiredHandler = fn;
}

/* ------------------------------------------------------------------------- *
 * 旧标签页提示（4.5）：启动时从 /api/health/live 记录 build_id，之后每个响应
 * 比对 X-Zhixu-Build。不一致说明服务端已经更新而当前页面资源还是旧的，
 * 在顶部插入可关闭的刷新提示条（不阻塞作答、不弹模态框）。
 *
 * 登录页 ↔ 主壳切换时 `#app` 会被整体替换，提示条 DOM 也会一起被移除。
 * 这里保留同一个节点引用并在壳层重渲染后重新挂载，避免「标志位死锁」：
 * buildBannerShown 已经是 true 却再也没有可见提示条。
 * ------------------------------------------------------------------------- */
let expectedBuildId = null;
let buildBannerShown = false;
let buildBannerDismissed = false;
let buildBannerNode = null;

function ensureBuildBannerNode() {
  if (buildBannerNode) return buildBannerNode;
  const banner = el('div', {
    class: 'build-stale-banner',
    role: 'status',
    'aria-live': 'polite',
  },
    el('span', { class: 'build-stale-banner-text' }, '应用已更新，请刷新页面以继续使用最新版本。'),
    el('button', {
      type: 'button', class: 'button small', onClick: () => window.location.reload(),
    }, '立即刷新'),
    el('button', {
      type: 'button', class: 'icon-button build-stale-banner-close', 'aria-label': '关闭更新提示',
      title: '暂不刷新',
      onClick: () => {
        buildBannerDismissed = true;
        banner.remove();
      },
    }, '×'));
  buildBannerNode = banner;
  return banner;
}

/** 把（已存在的）更新提示条挂到当前页面结构里；切换登录页/主壳后由 main.js 调用。 */
export function remountBuildBanner() {
  if (!buildBannerNode || buildBannerDismissed) return;
  const banner = buildBannerNode;
  if (banner.isConnected) return;
  const topbar = document.querySelector('.main > .topbar');
  if (topbar && topbar.parentElement) {
    // 主壳：顶栏之后、内容区之前，不会被固定侧栏遮住。
    topbar.insertAdjacentElement('afterend', banner);
    return;
  }
  // 登录/找回密码页：挂在 body 最前，作为独立一行的状态提示。
  document.body.prepend(banner);
}

function showBuildBanner() {
  if (buildBannerDismissed) return;
  buildBannerShown = true;
  ensureBuildBannerNode();
  remountBuildBanner();
}

function observeBuild(response) {
  let build = '';
  let bank = '';
  try {
    build = response.headers.get('X-Zhixu-Build') || '';
    bank = response.headers.get('X-Zhixu-Bank') || '';
  } catch {
    build = '';
  }
  // 题库热切换后让 state.catalog / 已展开的答案解析缓存失效（§2.4）。
  noteBankVersion(bank);
  if (!build) return;
  if (!expectedBuildId) {
    expectedBuildId = build;
    return;
  }
  if (build !== expectedBuildId) showBuildBanner();
}

/** 读取启动时对齐的构建号（验收脚本/调试可用来核对前后端版本）。 */
export function currentBuildId() {
  return expectedBuildId;
}

/** 启动时对齐构建号：以 /api/health/live 的 build_id 作为当前页面基线。 */
export async function initBuildGuard() {
  try {
    const live = await api('/health/live');
    if (live?.build_id) expectedBuildId = live.build_id;
  } catch {
    /* 健康检查不可用时不建立基线，避免误报更新提示；页面功能照常。 */
  }
}

export function errorText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(x => x.msg || String(x)).join('；');
  return detail?.message || JSON.stringify(detail) || '请求未完成，请重试。';
}

/* ------------------------------------------------------------------------- *
 * 筛选类 GET 的轻量取消：同一 latestKey 上的新请求会取消旧请求，避免慢响应
 * 覆盖新筛选结果。被取消的请求抛出带 aborted=true 的错误，调用方用 isAborted()
 * 判断后静默忽略即可（不弹 toast）。
 * ------------------------------------------------------------------------- */
const inflightGets = new Map();

function abortInflight(key) {
  const previous = inflightGets.get(key);
  if (!previous) return;
  inflightGets.delete(key);
  try { previous.abort(); } catch { /* ignore */ }
}

/** 判断错误是否来自「被更新的同类请求取消」。 */
export function isAborted(error) {
  return Boolean(error && error.aborted);
}

export async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  };
  if (options.data !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.data);
  }
  if (options.body) init.body = options.body;

  let dedupeKey = null;
  if (method === 'GET' && options.latest) {
    dedupeKey = options.latestKey || `${method} ${path}`;
    abortInflight(dedupeKey);
    const controller = new window.AbortController();
    init.signal = controller.signal;
    inflightGets.set(dedupeKey, controller);
  } else if (options.signal) {
    init.signal = options.signal;
  }

  let response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch (error) {
    if (dedupeKey) inflightGets.delete(dedupeKey);
    if (error && (error.name === 'AbortError' || error.code === 20)) {
      const aborted = new Error('请求已取消');
      aborted.aborted = true;
      throw aborted;
    }
    throw new Error('网络连接失败，请确认本机服务正常运行或检查网络连接。');
  }
  if (dedupeKey) inflightGets.delete(dedupeKey);
  observeBuild(response);

  if (options.rawText) {
    if (!response.ok) {
      const errJson = await response.json().catch(() => ({}));
      throw new Error(errorText(errJson.detail || `请求失败（${response.status}）`));
    }
    return response.text();
  }

  const json = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && state.user && !path.startsWith('/auth/')) {
      clearUserSession();
      if (authExpiredHandler) authExpiredHandler();
    }
    throw new Error(errorText(json.detail || json.error || `请求失败（${response.status}）`));
  }
  return json;
}

export async function ensureCatalog() {
  if (!state.catalog) state.catalog = await api('/catalog');
  return state.catalog;
}

export async function ensureKnowledgePoints(force = false) {
  if (!state.knowledgePoints || force) {
    const res = await api('/knowledge-points');
    state.knowledgePoints = res.items || [];
  }
  return state.knowledgePoints;
}

export async function ensureExplainModes() {
  if (!state.explainModes) {
    const res = await api('/ai/explain-modes');
    state.explainModes = res.items || [];
  }
  return state.explainModes;
}

export async function ensurePromptGrid() {
  if (!state.promptGrid) state.promptGrid = await api('/prompt-grid');
  return state.promptGrid;
}

export async function ensurePreferences(force = false) {
  if (!state.preferences || force) state.preferences = await api('/preferences');
  return state.preferences;
}

export async function refreshProviders() {
  const [prov, defaults] = await Promise.all([
    api('/providers'),
    api('/ai/task-defaults').catch(() => ({ items: {} })),
  ]);
  state.providers = prov.items || [];
  state.presets = prov.presets || [];
  state.taskDefaults = defaults.items || {};
  // 地址策略随连接接口一起返回，用于判断「本地模式」是否展示代理开关。
  state.urlPolicy = prov.url_policy || null;
  state.publicMode = typeof prov.public_mode === 'boolean' ? prov.public_mode : null;
  return state.providers;
}
