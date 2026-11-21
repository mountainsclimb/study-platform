/**
 * 广场通知中心（Phase 3 · WS21 契约）：登录用户顶栏铃铛 + 通知面板。
 *
 * 接口（前缀 /api/plaza，见 WS21 交付的 app/plaza 通知模块）：
 * - GET  /notifications?unread_only=&kind=&page=&page_size=
 *        → { items:[{id,kind,title,body,link,submission_id,public_id,payload,read,
 *                    read_at,created_at}], total, unread_count, page, page_size }
 * - POST /notifications/read {ids?:[int], all?:bool}
 *        → {updated, unread_count, message}（至少一个参数；只作用本人）
 *
 * 能力探测：接口未就绪（404/403/网络错误）时隐藏铃铛，不打扰用户、不阻塞壳层；
 * 访客壳不挂载（main.js 只在登录主壳 renderShell 中调用 mount）。
 * 「投稿台」等页面可通过 openPlazaNotificationPanel() 复用同一个通知面板。
 */
import { api } from '../core/api.js';
import {
  el, button, busy, dateText, loading, query, toast,
} from '../core/dom.js';
import { t } from '../core/i18n.js';

export const NOTIFICATIONS_PATH = '/plaza/notifications';
export const NOTIFICATIONS_READ_PATH = '/plaza/notifications/read';
const PAGE_SIZE = 20;
// 未读数轮询：60s；页面隐藏时跳过，避免后台标签页持续请求。
const POLL_MS = 60 * 1000;

// 同一页面可能同时挂载铃铛并通过页面按钮打开面板：已读结果广播给所有铃铛。
const unreadSyncs = new Set();
let panel = null;

/** 铃铛图标：dom.js 的共享图标表没有 bell，这里用同一套线性风格本地绘制。 */
function bellIcon() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const d of ['M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9', 'M13.7 21a2 2 0 0 1-3.4 0']) {
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

/** 兼容 {items:[...]} / 裸数组两种返回（契约固定 items，前端仍防御旧实例）。 */
function itemsOf(data) {
  if (Array.isArray(data)) return data;
  return Array.isArray(data?.items) ? data.items : [];
}

/** 未读数：优先服务端 unread_count；缺失时按未读条数兜底。 */
function unreadOf(data, items = []) {
  for (const key of ['unread_count', 'unread', 'count']) {
    if (data && data[key] !== undefined && data[key] !== null) {
      const value = Number(data[key]);
      if (Number.isFinite(value)) return Math.max(0, value);
    }
  }
  return items.filter(item => item && !item.read && !item.read_at).length;
}

function notificationIdOf(item) {
  return Number(item?.id ?? item?.notification_id ?? 0) || 0;
}

/**
 * 通知跳转目标：优先服务端给出的 hash 路由（契约：link 可直接赋给 location.hash），
 * 缺失时按 public_id / submission_id 派生，最后回广场列表。
 */
export function notificationTargetOf(item) {
  const direct = String(item?.link || item?.hash || '').trim();
  if (direct.startsWith('#/')) return direct;
  if (item?.public_id) return `#/plaza?id=${encodeURIComponent(item.public_id)}`;
  if (item?.submission_id) return `#/submission?id=${encodeURIComponent(item.submission_id)}`;
  return '#/plaza';
}

export function unreadLabel(count) {
  const value = Number(count) || 0;
  return value > 0
    ? t('plaza.notify.bellLabelUnread', { v1: value })
    : t('plaza.notify.bellLabel');
}

function broadcastUnread(count) {
  for (const sync of [...unreadSyncs]) {
    try { sync(count); } catch { /* 单个铃铛同步失败不影响其它入口 */ }
  }
}

/**
 * 在壳层槽位挂载通知铃铛。返回清理函数（登出/切壳时必须调用，停掉轮询与监听）。
 * 接口未就绪时铃铛保持隐藏（data-unavailable），轮询会继续重试。
 */
export function mountPlazaNotifications(slot) {
  if (!slot || typeof slot.append !== 'function') return () => {};
  const countBadge = el('span', {
    class: 'badge red plaza-notify-count hide',
    'data-unread-count': '0',
    // 绝对定位覆盖在铃铛右上角：只占铃铛自身的 36px 宽度，移动端顶栏不被未读数撑宽。
    style: 'position:absolute;top:-3px;right:-7px;font-size:10px;line-height:14px;'
      + 'padding:0 4px;min-width:14px;justify-content:center;pointer-events:none',
  });
  const bell = el('button', {
    type: 'button',
    class: 'icon-button plaza-notify-bell',
    'data-plaza-notification-bell': '',
    'aria-haspopup': 'dialog',
  }, bellIcon());
  const wrap = el('span', {
    class: 'plaza-notify-wrap hide',
    style: 'position:relative;display:inline-flex;align-items:center',
  }, bell, countBadge);
  slot.append(wrap);

  let disposed = false;
  let unread = 0;
  let ready = false;
  let timer = null;

  const syncLabel = () => {
    const label = unreadLabel(unread);
    bell.setAttribute('aria-label', label);
    bell.title = label;
  };
  const setUnread = count => {
    unread = Math.max(0, Number(count) || 0);
    countBadge.textContent = unread > 99 ? '99+' : String(unread);
    countBadge.classList.toggle('hide', unread <= 0);
    wrap.dataset.unread = String(unread);
    syncLabel();
  };
  unreadSyncs.add(setUnread);
  setUnread(0);

  bell.addEventListener('click', () => openPlazaNotificationPanel({ onChanged: setUnread }));

  async function refresh() {
    try {
      const data = await api(`${NOTIFICATIONS_PATH}?${query({ unread_only: 1, page: 1, page_size: 1 })}`);
      if (disposed) return;
      ready = true;
      wrap.classList.remove('hide');
      wrap.removeAttribute('data-unavailable');
      setUnread(unreadOf(data));
    } catch {
      if (disposed) return;
      // 未就绪：隐藏铃铛（不弹错）；已就绪后的瞬时失败保留上一次计数。
      if (!ready) {
        wrap.classList.add('hide');
        wrap.setAttribute('data-unavailable', 'true');
      }
    }
  }

  const onLocaleChanged = () => syncLabel();
  document.addEventListener('zhixu:locale-changed', onLocaleChanged);
  // 首次探测完成前保持隐藏，避免登录瞬间出现空铃铛又被填充的闪烁。
  void refresh();
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'hidden') void refresh();
  }, POLL_MS);
  syncLabel();

  return () => {
    disposed = true;
    unreadSyncs.delete(setUnread);
    if (timer) window.clearInterval(timer);
    document.removeEventListener('zhixu:locale-changed', onLocaleChanged);
    wrap.remove();
  };
}

/**
 * 打开通知面板（顶栏铃铛与投稿台「通知中心」入口共用）。
 * 选项：{onChanged?: (unreadCount:number)=>void} 用于回写铃铛未读数。
 */
export function openPlazaNotificationPanel(options = {}) {
  if (panel) {
    try { panel.close(); } catch { /* ignore */ }
    panel = null;
  }
  const listHost = el('div', { class: 'plzw-list plaza-notify-list' });
  const summary = el('p', { class: 'help', role: 'status', 'aria-live': 'polite' });
  const readAllButton = button(t('plaza.notify.markAllRead'), event => busy(
    event.currentTarget,
    () => markAllRead(),
  ), 'secondary small', 'check');
  const dialog = el('dialog', {
    class: 'modal-dialog plzw-dialog plaza-notify-dialog',
    'data-plaza-notification-panel': '',
    'aria-label': t('plaza.notify.title'),
  },
  el('div', { class: 'pg-card-head', style: 'margin:0' },
    el('div', {},
      el('h3', { style: 'margin:0' }, t('plaza.notify.title')),
      summary),
    button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
  el('div', { class: 'row', style: 'margin:12px 0' },
    readAllButton,
    el('span', { class: 'help' }, t('plaza.notify.hint'))),
  listHost);

  function publishUnread(count) {
    const value = Math.max(0, Number(count) || 0);
    options.onChanged?.(value);
    broadcastUnread(value);
    readAllButton.disabled = value <= 0;
    summary.textContent = value > 0
      ? t('plaza.notify.unreadSummary', { v1: value })
      : t('plaza.notify.allRead');
  }

  async function readNotifications(data) {
    return api(NOTIFICATIONS_READ_PATH, { method: 'POST', data });
  }

  async function markAllRead() {
    const result = await readNotifications({ all: true });
    publishUnread(unreadOf(result));
    toast(t('plaza.notify.markAllDone'));
    await load();
  }

  function renderItem(item) {
    const id = notificationIdOf(item);
    const unread = !item.read && !item.read_at;
    const target = notificationTargetOf(item);
    return el('article', {
      class: `plzw-item plaza-notify-item${unread ? ' unread' : ''}`,
      'data-notification-id': String(id),
      'data-read': unread ? 'false' : 'true',
    },
    el('div', { class: 'plzw-item-head' },
      el('strong', { 'data-no-i18n': '' }, String(item.title || t('plaza.notify.itemTitle'))),
      unread ? el('span', { class: 'badge amber' }, t('plaza.notify.unreadBadge')) : null,
      item.created_at ? el('span', { class: 'help' }, dateText(item.created_at)) : null),
    item.body ? el('p', { class: 'plzw-stem', 'data-no-i18n': '' }, String(item.body)) : null,
    el('div', { class: 'plzw-item-actions' },
      button(t('plaza.notify.open'), event => busy(event.currentTarget, async () => {
        const count = await openNotificationItem(id, target);
        if (count !== null) publishUnread(count);
      }), 'secondary small', 'chevron')));
  }

  async function openNotificationItem(id, target) {
    let count = null;
    if (id > 0) {
      try {
        const result = await readNotifications({ ids: [id] });
        count = unreadOf(result);
      } catch {
        // 标记已读失败不阻断跳转（服务端会在下次打开时返回真实未读状态）。
      }
    }
    dialog.close();
    if (target.startsWith('#/')) location.hash = target;
    return count;
  }

  async function load() {
    listHost.replaceChildren(loading(t('common.loading')));
    let data;
    try {
      data = await api(`${NOTIFICATIONS_PATH}?${query({ page: 1, page_size: PAGE_SIZE })}`);
    } catch (error) {
      listHost.replaceChildren(el('div', { class: 'error-panel' },
        error.message || t('plaza.notify.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:12px' },
          button(t('common.retry'), event => busy(event.currentTarget, () => load()), '', 'repeat'))));
      return;
    }
    const items = itemsOf(data);
    publishUnread(unreadOf(data, items));
    if (!items.length) {
      listHost.replaceChildren(el('div', { class: 'empty plaza-notify-empty' },
        el('h3', {}, t('plaza.notify.emptyTitle')),
        el('p', {}, t('plaza.notify.emptyText'))));
      return;
    }
    listHost.replaceChildren(...items.map(renderItem));
  }

  dialog.addEventListener('close', () => {
    dialog.remove();
    if (panel === dialog) panel = null;
  });
  document.body.append(dialog);
  panel = dialog;
  readAllButton.disabled = true;
  summary.textContent = t('plaza.notify.hint');
  dialog.showModal();
  void load();
  return dialog;
}
