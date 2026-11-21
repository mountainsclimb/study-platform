/* 知序 · 主题状态模块（P8 暗色主题）
 *
 * 与 web/js/theme-boot.js（普通 script，在样式表之前同步执行）配对：
 * - boot 负责「首屏前应用」，避免暗色刷新闪白；
 * - 本模块（ESM）负责运行期的状态读写、循环切换与跨组件广播。
 *
 * 两者共用 localStorage 键 `zhixu.theme`（auto | light | dark，裸字符串），
 * 读取时同时兼容 JSON 字符串写法；写入统一走本模块，保证刷新后保持。
 * 系统主题变化与多标签页同步由 boot 统一监听，本模块只在 boot 缺失时兜底，
 * 避免同一份状态被注册两次监听。
 */

export const THEME_STORAGE_KEY = 'zhixu.theme';
export const THEME_MODES = ['auto', 'light', 'dark'];
export const THEME_EVENT = 'zhixu:theme-changed';
const MODE_LABELS = { auto: '跟随系统', light: '浅色', dark: '暗色' };

function boot() {
  return typeof window !== 'undefined' ? window.__zhixuTheme : null;
}

function systemQuery() {
  if (typeof window === 'undefined' || !window.matchMedia) return null;
  return window.matchMedia('(prefers-color-scheme: dark)');
}

export function normalizeThemeMode(value) {
  return THEME_MODES.includes(value) ? value : 'auto';
}

/** 当前持久化的主题模式：'auto' | 'light' | 'dark'。 */
export function getThemeMode() {
  const api = boot();
  if (api && typeof api.readMode === 'function') return normalizeThemeMode(api.readMode());
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (!raw) return 'auto';
    let value = raw;
    if (raw.charAt(0) === '"') {
      try { value = JSON.parse(raw); } catch { value = raw; }
    }
    return normalizeThemeMode(value);
  } catch {
    return 'auto';
  }
}

/** 当前实际生效的主题：'light' | 'dark'（auto 时由系统偏好决定）。 */
export function getResolvedTheme() {
  const mode = getThemeMode();
  if (mode === 'light' || mode === 'dark') return mode;
  const query = systemQuery();
  return query && query.matches ? 'dark' : 'light';
}

/** 顶栏按钮的循环顺序：auto → light → dark → auto。 */
export function getNextThemeMode(mode = getThemeMode()) {
  const current = normalizeThemeMode(mode);
  return THEME_MODES[(THEME_MODES.indexOf(current) + 1) % THEME_MODES.length];
}

/** 「跟随系统」「浅色」「暗色」的可读名称。 */
export function themeModeLabel(mode = getThemeMode()) {
  return MODE_LABELS[normalizeThemeMode(mode)];
}

/** 顶栏按钮的 aria-label / title 文案：说明当前模式、当前生效主题与下一步动作。 */
export function describeTheme(mode = getThemeMode()) {
  const current = normalizeThemeMode(mode);
  const resolved = getResolvedTheme();
  const actual = `${themeModeLabel(current)}${current === 'auto' ? `（当前${resolved === 'dark' ? '暗色' : '浅色'}）` : ''}`;
  const next = getNextThemeMode(current);
  const nextAction = next === 'auto' ? '点击改为跟随系统' : `点击切换为${themeModeLabel(next)}（固定）`;
  return `主题切换。当前：${actual}。${nextAction}`;
}

function dispatchThemeChanged(mode, resolved, source) {
  try {
    document.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { mode, resolved, source } }));
  } catch {
    /* 广播失败不影响主题本身。 */
  }
}

function persistThemeMode(mode) {
  try {
    // 与 boot 脚本的零依赖读取对齐：存裸字符串 token。
    window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    /* 存储被禁用时仍应用本次切换，只是刷新后不保持。 */
  }
}

function applyThemeMode(mode, source) {
  const normalized = normalizeThemeMode(mode);
  const api = boot();
  if (api && typeof api.apply === 'function') {
    return api.apply(normalized, source); // boot 负责 dataset/colorScheme/meta/广播
  }
  const resolved = normalized === 'auto' ? (getResolvedThemeFromSystem() ? 'dark' : 'light') : normalized;
  const root = document.documentElement;
  root.setAttribute('data-theme', resolved);
  root.setAttribute('data-theme-mode', normalized);
  try { root.style.colorScheme = resolved; } catch { /* ignore */ }
  dispatchThemeChanged(normalized, resolved, source);
  return resolved;
}

function getResolvedThemeFromSystem() {
  const query = systemQuery();
  return !!(query && query.matches);
}

/**
 * 设置主题模式并持久化：'auto' | 'light' | 'dark'。
 * @returns {string} 归一化后的模式
 */
export function setThemeMode(mode) {
  const next = normalizeThemeMode(mode);
  persistThemeMode(next);
  applyThemeMode(next, 'user');
  return next;
}

/** auto → light → dark → auto 循环切换，返回切换后的模式。 */
export function cycleThemeMode() {
  return setThemeMode(getNextThemeMode());
}

/**
 * 初始化主题：把 boot 已应用的状态与存储对齐，并确保运行期有系统主题监听。
 * boot 正常加载时它已注册监听，这里不重复注册；boot 缺失（例如缓存/代理
 * 吞掉了脚本）时退化为本模块自行监听系统变化。
 * @returns {Function} 清理函数（移除兜底监听）
 */
export function initTheme() {
  const api = boot();
  if (api && typeof api.refresh === 'function') {
    api.refresh();
  } else {
    applyThemeMode(getThemeMode(), 'init');
  }

  const cleanups = [];
  const query = systemQuery();
  if (!api && query) {
    const onSystemChange = () => {
      if (getThemeMode() === 'auto') applyThemeMode('auto', 'system');
    };
    if (query.addEventListener) query.addEventListener('change', onSystemChange);
    else if (query.addListener) query.addListener(onSystemChange);
    cleanups.push(() => {
      if (query.removeEventListener) query.removeEventListener('change', onSystemChange);
      else if (query.removeListener) query.removeListener(onSystemChange);
    });
    const onStorage = event => {
      if (event && event.key !== null && event.key !== THEME_STORAGE_KEY) return;
      applyThemeMode(getThemeMode(), 'storage');
    };
    window.addEventListener('storage', onStorage);
    cleanups.push(() => window.removeEventListener('storage', onStorage));
  }

  return () => {
    for (const cleanup of cleanups) {
      try { cleanup(); } catch { /* ignore */ }
    }
    cleanups.length = 0;
  };
}
