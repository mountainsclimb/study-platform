import { routes, state, isPublicRoute } from './state.js';
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
  // 登录用户与访客壳都算「当前渲染有效」；仅靠 state.user 会把访客页的异步结果全部丢弃。
  return version === state.renderVersion && Boolean(state.user || state.guest);
}

export async function renderRoute() {
  // 访客壳（state.guest）只渲染公开路由；白名单集中在 core/state.js 的 PUBLIC_ROUTES。
  // 非公开路由或未进入访客壳时直接返回：由 main.js 的 hashchange / 登录流程接管，
  // 保证访客不可能通过手改 hash 渲染个人页面。
  const { route: parsedRoute, params } = parseHash();
  const guest = !state.user;
  if (!$('#main-content')) return;
  if (guest && (!state.guest || !isPublicRoute(parsedRoute))) return;
  let route = parsedRoute;
  if (!(route in routes)) route = 'home';
  if (guest && !isPublicRoute(route)) return;
  if (!guest && route === 'admin' && state.user.role !== 'admin') route = 'home';
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
    if (version !== state.renderVersion || (!state.user && !state.guest)) return;
    container.replaceChildren(
      heading('LEARNING SPACE', routes[route][0], '请检查连接后重试。'),
      el('div', { class: 'error-panel' },
        error.message || '页面加载失败',
        el('div', { class: 'row', style: 'margin-top:16px' },
          button('重新加载', () => renderRoute(), 'secondary', 'repeat')))
    );
  }
}
