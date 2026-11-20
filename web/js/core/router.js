import { routes, state } from './state.js';
import { $, loading, heading, button, el } from './dom.js';
import { closeJobStreams } from './jobs.js';

let routeRenderers = {};

export function registerRenderers(map) {
  routeRenderers = map;
}

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [routePart, queryPart] = raw.split('?');
  const params = new URLSearchParams(queryPart || '');
  return { route: routePart || 'home', params };
}

export function setHashParams(route, paramsObj = {}, replace = true) {
  const clean = new URLSearchParams();
  for (const [k, v] of Object.entries(paramsObj)) {
    if (v !== '' && v !== null && v !== undefined && v !== false) clean.set(k, String(v));
  }
  const qs = clean.toString();
  const next = `#/${route}${qs ? '?' + qs : ''}`;
  if (location.hash === next) return;
  if (replace && history.replaceState) {
    history.replaceState(null, '', next);
  } else {
    location.hash = next;
  }
}

export function go(route, qid = null, paramsObj = null) {
  if (qid) {
    state.selectedQid = qid;
    state.questionJobId = null;
  }
  const params = { ...(paramsObj || {}) };
  // 跨页跳题把 qid 一并写进 #/practice?...&qid=：刷新或分享链接后仍能打开目标题。
  if (qid && route === 'practice') params.qid = qid;
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined)
  ).toString();
  const target = `#/${route}${qs ? '?' + qs : ''}`;
  if (location.hash === target) {
    renderRoute();
  } else {
    location.hash = target;
  }
}

export function current(version) {
  return version === state.renderVersion && Boolean(state.user);
}

export async function renderRoute() {
  if (!state.user || !$('#main-content')) return;
  const { route: parsedRoute, params } = parseHash();
  let route = parsedRoute;
  if (!(route in routes) || (route === 'admin' && state.user.role !== 'admin')) route = 'home';
  state.route = route;
  const version = ++state.renderVersion;
  document.title = `${routes[route][0]} · 知序`;
  // 路由切换时关闭上一页遗留的非全局 SSE，避免旧任务流继续占用连接池。
  closeJobStreams();
  const crumb = $('#breadcrumb-title');
  if (crumb) crumb.textContent = routes[route][0];
  document.querySelectorAll('.nav-item').forEach(n => {
    const active = n.dataset.route === route;
    n.classList.toggle('active', active);
    n.setAttribute('aria-current', active ? 'page' : 'false');
  });
  const container = $('#main-content');
  container.replaceChildren(loading());
  try {
    const fn = routeRenderers[route];
    if (fn) await fn(container, version, params);
    if (current(version)) container.focus({ preventScroll: true });
  } catch (error) {
    if (version !== state.renderVersion || !state.user) return;
    container.replaceChildren(
      heading('LEARNING SPACE', routes[route][0], '请检查连接后重试。'),
      el('div', { class: 'error-panel' },
        error.message || '页面加载失败',
        el('div', { class: 'row', style: 'margin-top:16px' },
          button('重新加载', () => renderRoute(), 'secondary', 'repeat')))
    );
  }
}
