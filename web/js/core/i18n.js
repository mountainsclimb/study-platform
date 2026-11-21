/**
 * P8 i18n 基础设施（原生 ESM、无构建、无内联脚本）。
 *
 * 设计要点：
 * - 语言包是静态 JSON（`/static/locales/zh-CN.json`、`/static/locales/en-US.json`），
 *   由 `initI18n()` 在首屏渲染前 `fetch` 进来；两个语言包同时加载，便于缺键时
 *   回退到中文原文（`t()` 只对同一个 key warn 一次）。
 * - 持久化键 `zhixu.locale`（裸字符串，与 `zhixu.theme` 的约定一致）；缺省读取
 *   `navigator.language`，zh* → zh-CN、en* → en-US，其它一律 zh-CN。
 * - `setLocale()` 持久化 + 同步 `<html lang>` + 广播 `zhixu:locale-changed`；
 *   页面重渲染由监听方处理（主壳在 main.js 里统一 `renderRoute()`）。
 * - `applyI18n(root)` 批量替换 `[data-i18n]` / `[data-i18n-placeholder]` /
 *   `[data-i18n-title]` / `[data-i18n-aria-label]`。
 * - 题目正文、AI 生成内容、用户名、题库数据一律不翻译（由调用方保留原值）。
 */

export const LOCALE_STORAGE_KEY = 'zhixu.locale';
export const LOCALE_EVENT = 'zhixu:locale-changed';
export const DEFAULT_LOCALE = 'zh-CN';
export const SUPPORTED_LOCALES = ['zh-CN', 'en-US'];

const MESSAGES = {
  'zh-CN': Object.create(null),
  'en-US': Object.create(null),
};

const warnedKeys = new Set();
let currentLocale = DEFAULT_LOCALE;
let initPromise = null;

/** 把任意语言标记归一化为支持的语言；不支持返回 null。 */
export function normalizeLocale(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  if (lower.startsWith('zh')) return 'zh-CN';
  if (lower.startsWith('en')) return 'en-US';
  return null;
}

/** 读取持久化的语言（可能是 null）。 */
export function readStoredLocale() {
  try {
    return normalizeLocale(window.localStorage.getItem(LOCALE_STORAGE_KEY));
  } catch {
    return null;
  }
}

/** 缺省语言：持久化 > 浏览器语言 > zh-CN。 */
export function detectLocale() {
  return readStoredLocale() || normalizeLocale(window.navigator?.language) || DEFAULT_LOCALE;
}

/** 当前语言（zh-CN | en-US）。 */
export function getLocale() {
  return currentLocale;
}

/** 当前是否中文环境（少数需要条件拼接的调用方使用）。 */
export function isZhLocale() {
  return currentLocale === 'zh-CN';
}

/** 只允许 zh-CN / en-US；其它值保持原语言不变并返回 false。 */
export function isSupportedLocale(value) {
  return normalizeLocale(value) !== null;
}

async function loadMessages(locale) {
  const response = await fetch(`/static/locales/${locale}.json`, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`语言包加载失败（${locale} · HTTP ${response.status}）`);
  const data = await response.json();
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error(`语言包格式异常（${locale}）`);
  }
  const table = Object.create(null);
  for (const [key, value] of Object.entries(data)) {
    if (typeof value === 'string' && value !== '') table[key] = value;
  }
  MESSAGES[locale] = table;
  return table;
}

/**
 * 首屏前初始化：加载中英文语言包、确定语言、同步 `<html lang>`。
 * 重复调用返回同一个 Promise；语言包加载失败不阻塞页面（t() 回退 key/中文）。
 */
export function initI18n() {
  if (initPromise) return initPromise;
  currentLocale = detectLocale();
  document.documentElement.setAttribute('lang', currentLocale);
  initPromise = (async () => {
    const results = await Promise.allSettled(SUPPORTED_LOCALES.map(locale => loadMessages(locale)));
    const failed = results
      .map((result, index) => (result.status === 'rejected' ? SUPPORTED_LOCALES[index] : null))
      .filter(Boolean);
    if (failed.length) {
      // 语言包缺失是部署问题：只报一次 warning，页面仍可用（回显 key 或中文兜底）。
      console.warn(`[i18n] 语言包加载失败：${failed.join(', ')}`);
    }
    return currentLocale;
  })();
  return initPromise;
}

function interpolate(text, vars) {
  if (!vars || typeof text !== 'string') return text;
  return text.replace(/\{(\w+)\}/g, (match, name) => {
    const value = vars[name];
    return value === undefined || value === null ? match : String(value);
  });
}

function warnMissing(key) {
  if (warnedKeys.has(key)) return;
  warnedKeys.add(key);
  console.warn(`[i18n] 缺少文案 key：${key}（已回退中文原文）`);
}

/**
 * 取文案。缺失时回退到中文；中文也缺失时返回 key（并只 warn 一次）。
 * 支持 `{name}` 变量插值：`t('home.greeting', { name: '小明' })`。
 */
export function t(key, vars) {
  if (key === undefined || key === null || key === '') return '';
  const resolvedKey = String(key);
  const table = MESSAGES[currentLocale] || {};
  let value = table[resolvedKey];
  if (value === undefined || value === null || value === '') {
    const fallback = MESSAGES[DEFAULT_LOCALE]?.[resolvedKey];
    warnMissing(resolvedKey);
    value = fallback === undefined || fallback === null || fallback === '' ? resolvedKey : fallback;
  }
  return interpolate(value, vars);
}

/**
 * 切换语言：持久化 + 更新 `<html lang>` + 广播 `zhixu:locale-changed`。
 * 返回最终生效的语言；传入不支持的值时不切换，返回当前语言。
 */
export function setLocale(locale) {
  const next = normalizeLocale(locale);
  if (!next) return currentLocale;
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
  } catch {
    /* localStorage 被禁用时仍允许本次会话切换 */
  }
  if (next === currentLocale) {
    document.documentElement.setAttribute('lang', next);
    return currentLocale;
  }
  currentLocale = next;
  document.documentElement.setAttribute('lang', next);
  document.dispatchEvent(new CustomEvent(LOCALE_EVENT, { detail: { locale: next } }));
  return next;
}

/** 在 zh-CN / en-US 之间切换，返回生效语言。 */
export function toggleLocale() {
  return setLocale(currentLocale === 'zh-CN' ? 'en-US' : 'zh-CN');
}

/**
 * 批量翻译 DOM 子树（默认整页）。
 * - `[data-i18n]` → textContent
 * - `[data-i18n-placeholder]` → placeholder
 * - `[data-i18n-title]` → title
 * - `[data-i18n-aria-label]` → aria-label
 */
export function applyI18n(root = document) {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  const scopes = [root, ...root.querySelectorAll('[data-i18n],[data-i18n-placeholder],[data-i18n-title],[data-i18n-aria-label]')];
  for (const node of scopes) {
    if (!node || node.nodeType !== 1) continue;
    const textKey = node.getAttribute('data-i18n');
    if (textKey) node.textContent = t(textKey);
    const placeholderKey = node.getAttribute('data-i18n-placeholder');
    if (placeholderKey) node.setAttribute('placeholder', t(placeholderKey));
    const titleKey = node.getAttribute('data-i18n-title');
    if (titleKey) node.setAttribute('title', t(titleKey));
    const ariaKey = node.getAttribute('data-i18n-aria-label');
    if (ariaKey) node.setAttribute('aria-label', t(ariaKey));
  }
}

/** 数字格式化：跟随当前语言（locale 可显式覆盖）。 */
export function formatNumber(value, options = undefined) {
  const number = Number(value);
  if (!Number.isFinite(number)) return value === undefined || value === null ? '' : String(value);
  try {
    return new Intl.NumberFormat(currentLocale, options).format(number);
  } catch {
    return String(number);
  }
}

/** 日期/时间格式化：跟随当前语言；非法值回退原字符串。 */
export function formatDate(value, options = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) {
  if (value === undefined || value === null || value === '') return '';
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return new Intl.DateTimeFormat(currentLocale, options).format(date);
  } catch {
    return date.toLocaleString(currentLocale);
  }
}

/** 仅日期（年-月-日），用于「加入于 / 更新于」等只展示日期的位置。 */
export function formatDay(value) {
  return formatDate(value, { year: 'numeric', month: '2-digit', day: '2-digit' });
}

/** 测试/调试用：当前已加载的文案条目数。 */
export function getMessageCount(locale = currentLocale) {
  return Object.keys(MESSAGES[locale] || {}).length;
}
