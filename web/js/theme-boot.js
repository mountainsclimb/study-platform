/* 知序 · 主题首屏引导（P8 暗色主题）
 *
 * 这是一个「普通 script」（不是 module），必须由 web/index.html 的 <head>
 * 在所有样式表之前同步加载：在浏览器首次绘制前就把 data-theme 写到 <html> 上，
 * 避免暗色模式刷新时先闪一帧浅色背景（FOUC）。
 *
 * 主 CSP 为 script-src 'self'：本文件是独立同源静态文件，不含任何内联脚本。
 * localStorage 键 `zhixu.theme`，取值 auto | light | dark（裸字符串；为兼容
 * 早期/外部写入的 JSON 字符串，读取时同时接受 '"dark"' 这种形式）。
 *
 * 对外 API 挂在 window.__zhixuTheme 上，供 web/js/core/theme.js 复用，避免两处
 * 各写一套 dataset / colorScheme / 系统主题监听逻辑：
 *   readMode()      -> 'auto' | 'light' | 'dark'
 *   resolve(mode)   -> 'light' | 'dark'（auto 时按 prefers-color-scheme）
 *   apply(mode, src)-> 写入 data-theme / data-theme-mode / color-scheme，
 *                      更新 <meta name="theme-color">，广播 zhixu:theme-changed
 *   systemDark()    -> boolean
 *   refresh()       -> 重新读取存储并应用
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'zhixu.theme';
  var MODES = ['auto', 'light', 'dark'];
  var root = document.documentElement;
  var meta = document.querySelector('meta[name="theme-color"]');
  var originalThemeColor = meta ? meta.getAttribute('content') : null;
  var mql = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function normalize(value) {
    return MODES.indexOf(value) >= 0 ? value : 'auto';
  }

  function readMode() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return 'auto';
      var value = raw;
      // 兼容 JSON 字符串形式（'"dark"'）；裸 token 直接使用。
      if (raw.charAt(0) === '"') {
        try { value = JSON.parse(raw); } catch (error) { value = raw; }
      }
      return normalize(value);
    } catch (error) {
      // 隐私模式 / 存储被禁用时退回跟随系统。
      return 'auto';
    }
  }

  function systemDark() {
    return !!(mql && mql.matches);
  }

  function resolve(mode) {
    var normalized = normalize(mode);
    if (normalized === 'dark' || normalized === 'light') return normalized;
    return systemDark() ? 'dark' : 'light';
  }

  function setMetaThemeColor(resolved) {
    if (!meta) return;
    meta.setAttribute('content', resolved === 'dark' ? '#0f1615' : (originalThemeColor || '#102b35'));
  }

  function broadcast(mode, resolved, source) {
    try {
      document.dispatchEvent(new CustomEvent('zhixu:theme-changed', {
        detail: { mode: mode, resolved: resolved, source: source || 'apply' },
      }));
    } catch (error) {
      /* 老浏览器不支持 CustomEvent 构造器时静默降级，不影响主题本身。 */
    }
  }

  function apply(mode, source) {
    var normalized = normalize(mode);
    var resolved = resolve(normalized);
    root.setAttribute('data-theme', resolved);
    root.setAttribute('data-theme-mode', normalized);
    try { root.style.colorScheme = resolved; } catch (error) { /* ignore */ }
    setMetaThemeColor(resolved);
    broadcast(normalized, resolved, source);
    return resolved;
  }

  var api = {
    version: 1,
    key: STORAGE_KEY,
    modes: MODES.slice(),
    readMode: readMode,
    resolve: resolve,
    apply: apply,
    systemDark: systemDark,
    refresh: function () { return apply(readMode(), 'refresh'); },
  };
  window.__zhixuTheme = api;

  // 首屏前应用：此时 <head> 还没解析完样式表，不会出现浅色闪烁。
  apply(readMode(), 'boot');

  // 系统主题变化：仅 auto 模式跟随；固定 light/dark 时不打扰用户。
  function onSystemChange() {
    if (readMode() === 'auto') apply('auto', 'system');
  }
  if (mql) {
    if (mql.addEventListener) mql.addEventListener('change', onSystemChange);
    else if (mql.addListener) mql.addListener(onSystemChange);
  }

  // 多标签页同步：另一个标签页切换主题后，本页跟随（无需刷新）。
  window.addEventListener('storage', function (event) {
    if (event && event.key !== null && event.key !== STORAGE_KEY) return;
    apply(readMode(), 'storage');
  });
})();
