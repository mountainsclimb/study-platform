/* 试卷工坊 · 切题与答题区标注编辑器。
 *
 * 对接 `app/paperlab/ANNOTATE-API.md`（task-11 后端）：
 * - 页级 edit.json：GET/PUT /api/paperlab/imports/{id}/pages/{n}/annotation（version CAS）；
 * - 页列表：GET /api/paperlab/imports/{id}/pages；
 * - 重切：POST /api/paperlab/imports/{id}/resegment（≤20 页同步）；
 * - 答题区：POST /api/paperlab/imports/{id}/items/{itemId}/answer-box；
 * - 发布前校验：POST /api/paperlab/imports/{id}/publish（200 ready / 422 detail.ready=false+errors）。
 *
 * 交互：空白处拖拽新建题目框，Shift+拖拽新建答题区；选中后可整体拖动、8 向缩放、
 * 方向键微调（1px / Shift 10px）、Delete 删除、Esc 取消选中。所有坐标归一化 0-1，
 * 与服务端页图像素（naturalWidth/Height）互换。无第三方库、无内联脚本/外链。
 */
import { errorText } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, button, badge, notice, loading, empty, busy, toast, field, input, dateText,
} from '../core/dom.js';
import { current } from '../core/router.js';
import { t } from '../core/i18n.js';

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 2.5;
const ZOOM_STEP = 0.25;
const MIN_DRAW_SIZE = 0.012;
const MIN_BOX_SIDE = 0.002;
const HANDLE_DIRS = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const KIND_LABEL = {
  get question() { return t('paper.annotate.questionBox'); },
  get answer() { return t('paper.annotate.answerArea'); },
};
const KIND_BADGE = { question: 'blue', answer: 'amber' };
const PAGE_NATURAL_FALLBACK = { width: 1000, height: 1400 };

/* ---------------------------------------------------------------------------
 * 基础工具
 * ------------------------------------------------------------------------- */

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function round4(value) {
  return Number(Math.round(value * 10000) / 10000);
}

function normalizeBox(raw) {
  const x = clamp(num(raw?.x), 0, 1);
  const y = clamp(num(raw?.y), 0, 1);
  const w = clamp(num(raw?.w), MIN_BOX_SIDE, Math.max(MIN_BOX_SIDE, 1 - x));
  const h = clamp(num(raw?.h), MIN_BOX_SIDE, Math.max(MIN_BOX_SIDE, 1 - y));
  return {
    x: round4(clamp(x, 0, 1 - MIN_BOX_SIDE)),
    y: round4(clamp(y, 0, 1 - MIN_BOX_SIDE)),
    w: round4(clamp(w, MIN_BOX_SIDE, 1 - x)),
    h: round4(clamp(h, MIN_BOX_SIDE, 1 - y)),
  };
}

function boxStyle(box) {
  return `left:${(box.x * 100).toFixed(3)}%;top:${(box.y * 100).toFixed(3)}%;`
    + `width:${(box.w * 100).toFixed(3)}%;height:${(box.h * 100).toFixed(3)}%`;
}

function rectBetween(a, b) {
  return normalizeBox({
    x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
    w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y),
  });
}

function overlapRatio(a, b) {
  const left = Math.max(a.x, b.x);
  const top = Math.max(a.y, b.y);
  const right = Math.min(a.x + a.w, b.x + b.w);
  const bottom = Math.min(a.y + a.h, b.y + b.h);
  if (right <= left || bottom <= top) return 0;
  const inter = (right - left) * (bottom - top);
  const smaller = Math.min(a.w * a.h, b.w * b.h) || 1e-9;
  return inter / smaller;
}

function itemBbox(item) {
  const draft = item && item.draft && typeof item.draft === 'object' ? item.draft : {};
  const bbox = draft.bbox || (item && item.bbox) || null;
  if (!bbox || !(num(bbox.w) > 0) || !(num(bbox.h) > 0)) return null;
  return normalizeBox(bbox);
}

function itemIsStale(item) {
  if (!item) return false;
  const reason = String(item.review_reason || '');
  if (reason.includes('stale')) return true;
  const warnings = Array.isArray(item.warnings) ? item.warnings : [];
  return warnings.some(text => String(text).startsWith('stale'));
}

function itemStatusLabel(item) {
  if (!item) return '';
  if (item.question_id) return t('home.importStatus.committed');
  if (item.status === 'accepted') return t('paper.import.accepted');
  if (item.status === 'skipped') return t('paper.import.skipped_2');
  return t('assistant.pendingConfirmation');
}

function safeText(value, limit = 160) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/* 直接 fetch：保留 HTTP 状态码与 detail（core/api.js 会把 409/422 的 detail 压成字符串，
 * 而本编辑器需要展示冲突的当前 version/updated_at 与发布校验的 errors 列表）。 */
async function rawRequest(path, { method = 'GET', data } = {}) {
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  };
  if (data !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw Object.assign(new Error(t('paper.annotate.networkConnectionFailedMakeSureTheLocal')), { status: 0 });
  }
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const detail = payload.detail ?? payload.error ?? '';
    const error = new Error(errorText(detail) || t('settings.wizard.requestFailed', { v1: response.status }));
    error.status = response.status;
    error.detail = detail;
    error.headers = response.headers;
    throw error;
  }
  return payload;
}

/* ---------------------------------------------------------------------------
 * 弹层：数字输入 / 确认
 * ------------------------------------------------------------------------- */

function modalTokens(title, description, body, actions, label) {
  const dialog = el('dialog', { class: 'modal-dialog pl-ann-dialog', 'aria-label': label || title });
  const head = el('div', { class: 'card-header pl-ann-dialog-head' },
    el('div', {}, el('h3', {}, title), description ? el('p', { class: 'help' }, description) : null));
  dialog.append(head, body, el('div', { class: 'row end pl-ann-dialog-actions' }, ...actions));
  document.body.append(dialog);
  dialog.showModal();
  const close = () => { try { dialog.close(); } catch { /* ignore */ } dialog.remove(); };
  dialog.addEventListener('cancel', event => { event.preventDefault(); });
  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
  return { dialog, close };
}

function askNumberDialog({ title, description, initial = 5, min = 1, max = 20 } = {}) {
  return new Promise(resolve => {
    const valueInput = el('input', {
      type: 'number', class: 'input', min: String(min), max: String(max), value: String(initial),
      'aria-label': title,
    });
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      dialog.close();
      resolve(value);
    };
    const { dialog } = modalTokens(title, description,
      el('div', { class: 'field' }, el('label', {}, t('paper.annotate.count')), valueInput), [
        button(t('common.cancel'), () => finish(null), 'secondary'),
        button(t('paper.annotate.generate'), () => {
          const value = Math.round(num(valueInput.value, Number.NaN));
          if (!Number.isFinite(value) || value < min || value > max) {
            toast(t('paper.annotate.enterAnIntegerBetweenAnd', { v1: min, v2: max }), true);
            return;
          }
          finish(value);
        }, '', 'check'),
      ]);
    valueInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); dialog.querySelector('.button:last-child').click(); }
    });
    window.setTimeout(() => { try { valueInput.focus(); valueInput.select(); } catch { /* ignore */ } }, 30);
  });
}

function confirmDialog({ title, description, confirmText = t('paper.annotate.confirm'), cancelText = t('common.cancel'), danger = false } = {}) {
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      dialog.close();
      resolve(value);
    };
    const { dialog } = modalTokens(title, description, el('div'),
      [button(cancelText, () => finish(false), 'secondary'),
        button(confirmText, () => finish(true), danger ? 'danger' : '', danger ? 'trash' : 'check')]);
    dialog.addEventListener('cancel', () => finish(false));
  });
}

/* ---------------------------------------------------------------------------
 * 主组件
 * ------------------------------------------------------------------------- */

export async function renderPageAnnotator({
  mount, importId, meta = {}, version = state.renderVersion, onBack = null,
} = {}) {
  mount.replaceChildren(loading(t('paper.annotate.loadingTheSegmentationEditor')));
  let bundle;
  try {
    const [detail, pageList] = await Promise.all([
      rawRequest(`/paperlab/imports/${encodeURIComponent(importId)}`),
      rawRequest(`/paperlab/imports/${encodeURIComponent(importId)}/pages`),
    ]);
    bundle = { detail, pages: pageList.items || [] };
  } catch (error) {
    if (!current(version)) return;
    mount.replaceChildren(el('div', { class: 'pl-detail-error' },
      notice(error.message || t('paper.annotate.failedToLoadTheSegmentationEditorPlease'), 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderPageAnnotator({ mount, importId, meta, version, onBack }), '', 'repeat'),
        button(t('paper.annotate.backToImportDetails'), () => { if (onBack) onBack(); }, 'secondary'))));
    return;
  }
  if (!current(version)) return;
  if (!bundle.pages.length) {
    mount.replaceChildren(el('div', { class: 'pl-ann' },
      el('div', { class: 'card pl-ann-empty-card' },
        empty(t('paper.annotate.thisImportHasNoPages'), t('paper.annotate.uploadAPaperHandoutAndGeneratePage'), null, 'file'),
        el('div', { class: 'row' },
          button(t('paper.annotate.backToImportDetails'), () => { if (onBack) onBack(); }, 'secondary', 'chevron')))));
    return;
  }

  const ui = {
    detail: bundle.detail,
    pages: bundle.pages,
    items: Array.isArray(bundle.detail.items) ? bundle.detail.items : [],
    pageNo: (bundle.pages.find(page => page.image_url) || bundle.pages[0]).page_no,
    cache: new Map(),
    selectedBoxId: null,
    mode: 'question',
    zoom: 1,
    showDrafts: true,
    lastResegment: null,
    publish: null,
    highlightItemId: null,
    conflictOpen: false,
    saving: false,
    loadError: null,
  };

  function pageState(pageNo = ui.pageNo) {
    let state = ui.cache.get(pageNo);
    if (!state) {
      state = { loaded: false, loading: false, version: 0, boxes: [], dirty: false, page: null };
      ui.cache.set(pageNo, state);
    }
    return state;
  }

  function currentPage() {
    return ui.pages.find(page => Number(page.page_no) === Number(ui.pageNo)) || ui.pages[0] || null;
  }

  function currentState() {
    return pageState(ui.pageNo);
  }

  function naturalSize() {
    const state = currentState();
    const page = currentPage();
    const width = num(state.page?.naturalWidth, num(page?.naturalWidth, num(page?.width, PAGE_NATURAL_FALLBACK.width)));
    const height = num(state.page?.naturalHeight, num(page?.naturalHeight, num(page?.height, PAGE_NATURAL_FALLBACK.height)));
    return { width: width > 0 ? width : PAGE_NATURAL_FALLBACK.width, height: height > 0 ? height : PAGE_NATURAL_FALLBACK.height };
  }

  function pageItems(pageNo = ui.pageNo) {
    return ui.items.filter(item => Number(item.page_no) === Number(pageNo));
  }

  function findItem(itemId) {
    return ui.items.find(item => Number(item.id) === Number(itemId)) || null;
  }

  function dirtyPages() {
    return [...ui.cache.entries()].filter(([, state]) => state.dirty).map(([pageNo]) => pageNo);
  }

  function anyDirty() {
    return dirtyPages().length > 0;
  }

  function nextBoxId(kind, boxes) {
    const prefix = kind === 'question' ? 'q' : 'a';
    const used = new Set(boxes.map(box => String(box.id || '')));
    let index = boxes.filter(box => box.kind === kind).length + 1;
    let candidate = `${prefix}${index}`;
    while (used.has(candidate)) { index += 1; candidate = `${prefix}${index}`; }
    return candidate;
  }

  function markDirty() {
    currentState().dirty = true;
    syncUnloadGuard();
  }

  /* ---------------- 未保存离开保护 ---------------- */

  let unloadGuardOn = false;
  function beforeUnload(event) {
    if (!mount.isConnected || !anyDirty()) return undefined;
    event.preventDefault();
    event.returnValue = '';
    return '';
  }
  function syncUnloadGuard() {
    const need = anyDirty();
    if (need && !unloadGuardOn) {
      window.addEventListener('beforeunload', beforeUnload);
      unloadGuardOn = true;
    } else if (!need && unloadGuardOn) {
      window.removeEventListener('beforeunload', beforeUnload);
      unloadGuardOn = false;
    }
  }

  /* ---------------- 数据加载 ---------------- */

  async function loadPage(pageNo, { force = false } = {}) {
    const state = pageState(pageNo);
    if (state.loaded && !force) return state;
    if (state.loading) return state;
    state.loading = true;
    try {
      const result = await rawRequest(
        `/paperlab/imports/${encodeURIComponent(importId)}/pages/${encodeURIComponent(pageNo)}/annotation`);
      state.version = num(result.edit?.version, 0);
      state.boxes = (Array.isArray(result.edit?.boxes) ? result.edit.boxes : []).map(box => {
        const normalized = normalizeBox(box);
        return {
          id: String(box.id || ''),
          kind: box.kind === 'answer' ? 'answer' : 'question',
          ...normalized,
          label: box.label ? String(box.label) : '',
          item_id: box.item_id ? Number(box.item_id) : null,
          score: box.score === null || box.score === undefined ? null : Number(box.score),
        };
      });
      state.page = result.page || null;
      state.loaded = true;
      state.dirty = false;
      const page = ui.pages.find(entry => Number(entry.page_no) === Number(pageNo));
      if (page && result.page) {
        page.width = num(result.page.width, page.width);
        page.height = num(result.page.height, page.height);
        page.naturalWidth = num(result.page.naturalWidth, page.naturalWidth || page.width);
        page.naturalHeight = num(result.page.naturalHeight, page.naturalHeight || page.height);
      }
      ui.loadError = null;
    } catch (error) {
      state.loadError = error.message || t('paper.annotate.failedToLoadSegmentationAnnotations');
      throw error;
    } finally {
      state.loading = false;
    }
    syncUnloadGuard();
    return state;
  }

  async function refreshImportData() {
    const [detail, pageList] = await Promise.all([
      rawRequest(`/paperlab/imports/${encodeURIComponent(importId)}`),
      rawRequest(`/paperlab/imports/${encodeURIComponent(importId)}/pages`),
    ]);
    ui.detail = detail;
    ui.items = Array.isArray(detail.items) ? detail.items : [];
    ui.pages = pageList.items || ui.pages;
    if (!ui.pages.some(page => Number(page.page_no) === Number(ui.pageNo))) {
      ui.pageNo = ui.pages[0].page_no;
    }
  }

  /* ---------------- 保存 / 冲突 ---------------- */

  function publicBox(box) {
    const payload = {
      id: box.id, kind: box.kind, x: box.x, y: box.y, w: box.w, h: box.h,
    };
    if (box.label) payload.label = box.label;
    if (box.item_id) payload.item_id = Number(box.item_id);
    if (box.score !== null && box.score !== undefined) payload.score = Number(box.score);
    return payload;
  }

  function updateDirtyUi() {
    renderToolbar();
    renderPages();
    renderPanel();
  }

  async function savePage({ silent = false } = {}) {
    const state = currentState();
    if (!state.dirty || ui.saving) return true;
    if (ui.conflictOpen) return false;
    ui.saving = true;
    try {
      const result = await rawRequest(
        `/paperlab/imports/${encodeURIComponent(importId)}/pages/${encodeURIComponent(ui.pageNo)}/annotation`,
        { method: 'PUT', data: { version: state.version, boxes: state.boxes.map(publicBox) } });
      state.version = num(result.edit?.version, state.version + 1);
      if (Array.isArray(result.edit?.boxes)) {
        state.boxes = result.edit.boxes.map(box => ({
          id: String(box.id || ''), kind: box.kind === 'answer' ? 'answer' : 'question',
          ...normalizeBox(box), label: box.label || '',
          item_id: box.item_id ? Number(box.item_id) : null,
          score: box.score === null || box.score === undefined ? null : Number(box.score),
        }));
      }
      state.dirty = false;
      syncUnloadGuard();
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      if (!silent) toast(t('paper.annotate.segmentationBoxesForPageSavedVersion', { v1: ui.pageNo, v2: state.version }));
      if (warnings.length) toast(warnings.join(t('common.semicolonSeparator')), true);
      updateDirtyUi();
      renderViewer();
      return true;
    } catch (error) {
      if (error.status === 409) {
        openConflictDialog(error.detail);
        return false;
      }
      toast(error.message || t('paper.question.saveFailedPleaseTryAgain'), true);
      return false;
    } finally {
      ui.saving = false;
      renderToolbar();
    }
  }

  function openConflictDialog(detail) {
    const serverVersion = num(detail?.version, 0);
    const updatedAt = detail?.updated_at || '';
    const state = currentState();
    ui.conflictOpen = true;
    const dialog = el('dialog', { class: 'modal-dialog pl-ann-dialog', 'aria-label': t('paper.annotate.segmentationSaveConflict') });
    let settled = false;
    const close = () => {
      if (settled) return;
      settled = true;
      ui.conflictOpen = false;
      try { dialog.close(); } catch { /* ignore */ }
      dialog.remove();
      renderToolbar();
    };
    const info = el('div', { class: 'pl-ann-conflict-info' },
      el('div', { class: 'pl-ann-conflict-row' },
        el('span', {}, t('paper.annotate.currentServerVersion')), el('strong', {}, `v${serverVersion}`)),
      el('div', { class: 'pl-ann-conflict-row' },
        el('span', {}, t('paper.annotate.lastUpdated')), el('strong', {}, updatedAt ? dateText(updatedAt) : t('admin.unknown'))),
      el('div', { class: 'pl-ann-conflict-row' },
        el('span', {}, t('paper.annotate.versionYouBasedOn')), el('strong', {}, `v${state.version}`)),
      el('p', { class: 'help' }, t('paper.annotate.yourLocalChangesAreStillInThe')));
    dialog.append(
      el('div', { class: 'card-header pl-ann-dialog-head' },
        el('div', {}, el('h3', {}, t('paper.annotate.segmentationAnnotationsWereChangedBySomeoneElse')),
          el('p', { class: 'help' }, String(detail?.message || t('paper.annotate.versionConflictChooseHowToProceed'))))),
      info,
      el('div', { class: 'row end pl-ann-dialog-actions' },
        button(t('paper.annotate.keepLocalHandleLater'), () => close(), 'ghost'),
        button(t('paper.annotate.reloadLatest'), async () => {
          close();
          try {
            await loadPage(ui.pageNo, { force: true });
            ui.selectedBoxId = null;
            renderAll();
            toast(t('paper.annotate.loadedTheLatestServerVersionUnsavedLocal'));
          } catch (error) {
            toast(error.message || t('paper.annotate.failedToReloadPleaseTryAgain'), true);
          }
        }, 'secondary', 'repeat'),
        button(t('paper.annotate.overwriteSaveWithLocalChanges'), async () => {
          close();
          state.version = serverVersion;
          state.dirty = true;
          if (await savePage()) toast(t('paper.annotate.overwriteSavedWithLocalChanges'));
        }, '', 'check')));
    document.body.append(dialog);
    dialog.showModal();
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  }

  /* ---------------- 页面切换 ---------------- */

  async function switchPage(pageNo) {
    if (Number(pageNo) === Number(ui.pageNo)) return true;
    if (anyDirty()) {
      const saved = await savePage({ silent: true });
      if (!saved) return false;
    }
    ui.pageNo = Number(pageNo);
    ui.selectedBoxId = null;
    ui.highlightItemId = null;
    try {
      await loadPage(ui.pageNo);
    } catch (error) {
      toast(error.message || t('paper.annotate.failedToLoadSegmentationAnnotationsPleaseTry'), true);
    }
    if (ui.lastResegment) await autoLinkBoxes();
    renderAll();
    return true;
  }

  /* ---------------- 框编辑 ---------------- */

  function selectBox(boxId) {
    ui.selectedBoxId = boxId || null;
    renderOverlay();
    renderPanel();
  }

  function addBox(kind, rect) {
    const state = currentState();
    const normalized = normalizeBox(rect);
    if (normalized.w < MIN_DRAW_SIZE || normalized.h < MIN_DRAW_SIZE) return;
    const box = {
      id: nextBoxId(kind, state.boxes), kind, ...normalized, label: '', item_id: null, score: null,
    };
    state.boxes.push(box);
    markDirty();
    ui.selectedBoxId = box.id;
    renderAll();
  }

  function deleteBox(boxId) {
    const state = currentState();
    const index = state.boxes.findIndex(box => box.id === boxId);
    if (index < 0) return;
    state.boxes.splice(index, 1);
    if (ui.selectedBoxId === boxId) ui.selectedBoxId = null;
    markDirty();
    renderAll();
  }

  function moveBox(box, dx, dy) {
    Object.assign(box, normalizeBox({ x: box.x + dx, y: box.y + dy, w: box.w, h: box.h }));
  }

  function resizeBox(box, dir, dx, dy) {
    let { x, y, w, h } = box;
    if (dir.includes('w')) { x += dx; w -= dx; }
    if (dir.includes('e')) { w += dx; }
    if (dir.includes('n')) { y += dy; h -= dy; }
    if (dir.includes('s')) { h += dy; }
    if (w < MIN_BOX_SIDE) { if (dir.includes('w')) x = box.x + box.w - MIN_BOX_SIDE; w = MIN_BOX_SIDE; }
    if (h < MIN_BOX_SIDE) { if (dir.includes('n')) y = box.y + box.h - MIN_BOX_SIDE; h = MIN_BOX_SIDE; }
    Object.assign(box, normalizeBox({ x, y, w, h }));
  }

  function beginPointer(event, { onMove, onUp }) {
    event.preventDefault();
    const move = ev => onMove(ev);
    const finish = ev => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', finish);
      document.removeEventListener('pointercancel', finish);
      onUp(ev);
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', finish);
    document.addEventListener('pointercancel', finish);
  }

  /* ---------------- 渲染：壳 ---------------- */

  const titleEl = el('h2', { class: 'pl-detail-title', 'data-no-i18n': '' });
  const subtitleEl = el('p', { class: 'help' });
  const headStatus = el('div', { class: 'row' });
  const backBtn = button(t('paper.annotate.backToImportDetails'), async () => {
    if (anyDirty()) {
      const choice = await new Promise(resolve => {
        const { dialog, close } = modalTokens(t('paper.annotate.thereAreUnsavedSegmentationChanges'),
          t('paper.annotate.saveTheChangesToTheCurrentPage'), el('div'), [
            button(t('common.cancel'), () => { close(); resolve(null); }, 'secondary'),
            button(t('paper.annotate.leaveWithoutSaving'), () => { close(); resolve('discard'); }, 'ghost'),
            button(t('paper.annotate.saveAndLeave'), () => { close(); resolve('save'); }, '', 'check'),
          ], t('paper.annotate.unsavedSegmentationChanges'));
        dialog.addEventListener('cancel', () => resolve(null));
      });
      if (choice === null) return;
      if (choice === 'save' && !(await savePage())) return;
    }
    if (onBack) onBack();
  }, 'ghost small', 'chevron');
  const head = el('div', { class: 'pl-detail-head pl-ann-head' },
    el('div', { class: 'row pl-ann-head-main' }, backBtn,
      el('div', {}, titleEl, subtitleEl)),
    headStatus);

  const toolbar = el('div', { class: 'card pl-ann-toolbar', role: 'toolbar', 'aria-label': t('paper.annotate.segmentationToolbar') });
  const pageListBox = el('div', { class: 'pl-ann-pages' });
  const viewerMeta = el('div', { class: 'pl-ann-viewer-meta' });
  const viewport = el('div', { class: 'pl-ann-viewport' });
  const viewerHint = el('p', { class: 'help pl-ann-viewer-hint' });
  const viewerCard = el('section', { class: 'card pl-ann-viewer-card' },
    viewerMeta, viewport, viewerHint);
  const panelBox = el('aside', { class: 'pl-ann-panel', 'aria-label': t('paper.annotate.segmentationPropertiesAndActions') });
  const layout = el('div', { class: 'pl-ann-layout' },
    el('section', { class: 'card pl-ann-page-card' },
      el('div', { class: 'pl-ann-section-head' }, el('h3', {}, t('paper.annotate.page')),
        el('p', { class: 'help' }, t('paper.annotate.clickToSwitchPagesUnsavedChangesTo'))),
      pageListBox),
    el('div', { class: 'pl-ann-main' }, viewerCard),
    panelBox);

  const root = el('section', { class: 'pl-ann' }, head, toolbar, layout);
  mount.replaceChildren(root);

  /* ---------------- 渲染：工具条 ---------------- */

  function modeButton(kind) {
    return button(KIND_LABEL[kind], () => {
      ui.mode = kind;
      renderToolbar();
    }, `secondary pl-ann-mode${ui.mode === kind ? ' is-active' : ''}${kind === 'answer' ? ' pl-ann-mode-answer' : ''}`,
    kind === 'question' ? 'scan' : 'edit', {
      'aria-pressed': ui.mode === kind ? 'true' : 'false',
    });
  }

  function renderToolbar() {
    const page = currentPage();
    const state = currentState();
    const scale = viewerScalePercent();
    toolbar.replaceChildren(
      el('div', { class: 'pl-ann-toolgroup' },
        modeButton('question'),
        modeButton('answer'),
        el('span', { class: 'help pl-ann-tool-hint' }, t('paper.annotate.dragOnEmptySpaceToDrawShift'))),
      el('div', { class: 'pl-ann-toolgroup' },
        button(t('paper.annotate.distributeEvenly'), () => evenSplit(), 'ghost small', 'layers'),
        button(t('paper.annotate.clearThisPage'), () => clearPage(), 'ghost small', 'trash'),
        el('label', { class: 'pl-ann-check' },
          el('input', {
            type: 'checkbox', checked: ui.showDrafts,
            onChange: event => { ui.showDrafts = !!event.target.checked; renderOverlay(); },
          }),
          t('paper.annotate.showReSplitBoxes'))),
      el('div', { class: 'pl-ann-toolgroup pl-ann-toolgroup-end' },
        el('div', { class: 'pl-ann-zoom', role: 'group', 'aria-label': t('paper.annotate.zoom') },
          button('−', () => {
            ui.zoom = clamp(ui.zoom - ZOOM_STEP, ZOOM_MIN, ZOOM_MAX);
            renderViewer();
            renderToolbar();
          }, 'ghost small'),
          button(t('paper.card.fitWidth'), () => { ui.zoom = 1; renderViewer(); renderToolbar(); }, 'ghost small'),
          button('+', () => {
            ui.zoom = clamp(ui.zoom + ZOOM_STEP, ZOOM_MIN, ZOOM_MAX);
            renderViewer();
            renderToolbar();
          }, 'ghost small'),
          el('span', { class: 'pl-ann-zoom-value', 'aria-live': 'polite' }, `${scale}%`)),
        state.dirty ? badge(t('paper.annotate.unsaved'), 'amber') : badge(t('paper.annotate.saved'), 'teal'),
        button(t('paper.annotate.saveThisPage'), () => busy(document.activeElement || toolbar, () => savePage()), '', 'check', {
          disabled: !state.dirty || !page,
        })));
  }

  function viewerScalePercent() {
    const canvas = viewport.querySelector('.pl-ann-canvas');
    const { width } = naturalSize();
    if (!canvas || !width) return Math.round(ui.zoom * 100);
    const rendered = canvas.getBoundingClientRect().width || 0;
    return rendered > 0 ? Math.max(1, Math.round((rendered / width) * 100)) : Math.round(ui.zoom * 100);
  }

  /* ---------------- 渲染：页列表 ---------------- */

  function renderPages() {
    pageListBox.replaceChildren();
    if (!ui.pages.length) {
      pageListBox.append(el('p', { class: 'help' }, t('paper.annotate.noPages')));
      return;
    }
    for (const page of ui.pages) {
      const pageNo = Number(page.page_no);
      const state = ui.cache.get(pageNo);
      const local = state && state.loaded ? state.boxes : null;
      const summary = page.annotation || {};
      const boxCount = local ? local.length : num(summary.box_count);
      const questionCount = local
        ? local.filter(box => box.kind === 'question').length : num(summary.question_count);
      const answerCount = local
        ? local.filter(box => box.kind === 'answer').length : num(summary.answer_count);
      const items = pageItems(pageNo);
      const staleCount = items.filter(itemIsStale).length;
      const edited = state ? (state.dirty || state.loaded) : summary.edited;
      const card = el('button', {
        type: 'button',
        class: `pl-ann-page${pageNo === Number(ui.pageNo) ? ' is-active' : ''}${state && state.dirty ? ' is-dirty' : ''}`,
        'aria-pressed': pageNo === Number(ui.pageNo) ? 'true' : 'false',
        onClick: () => switchPage(pageNo),
      },
      el('span', { class: 'pl-ann-page-no' }, t('result.page', { v1: pageNo })),
      el('span', { class: 'pl-ann-page-badges' },
        badge(t('assistant.q', { v1: questionCount }), 'blue'),
        badge(t('paper.annotate.ans', { v1: answerCount }), 'amber'),
        items.length ? badge(t('paper.import.draft', { v1: items.length }), '') : null,
        staleCount ? badge(`stale ${staleCount}`, 'red') : null,
        page.has_text ? badge(t('paper.annotate.plainText'), '') : null,
        edited ? badge(t('paper.annotate.edited'), 'teal') : null),
      el('span', { class: 'pl-ann-page-state' },
        state && state.dirty ? t('paper.annotate.unsaved') : `${num(page.naturalWidth || page.width)}×${num(page.naturalHeight || page.height)}`));
      pageListBox.append(card);
    }
  }

  /* ---------------- 渲染：画布 / 覆盖层 ---------------- */

  let boundOverlay = null;

  function renderViewer() {
    const page = currentPage();
    const state = currentState();
    const { width, height } = naturalSize();
    const hasImage = Boolean(page && page.image_url);
    const viewportWidth = Math.max(160, viewport.clientWidth || 640);
    const renderWidth = hasImage
      ? Math.round(clamp(viewportWidth * ui.zoom - 2, 160, 5000))
      : 0;
    const scalePct = hasImage && width
      ? Math.max(1, Math.round((renderWidth / width) * 100))
      : Math.round(ui.zoom * 100);
    viewerMeta.replaceChildren(
      el('div', { class: 'pl-ann-viewer-title' },
        el('strong', {}, page ? t('common.pageOf', { page: page.page_no, pages: ui.pages.length }) : t('paper.import.noPages')),
        badge(`${width}×${height}`, 'blue'),
        page && page.has_text ? badge(t('paper.annotate.plainTextPage'), '') : null),
      el('div', { class: 'row pl-ann-viewer-tools' },
        el('span', { class: 'help' }, t('paper.annotate.zoomNaturalSize', { v1: scalePct, v2: width, v3: height }))));
    viewerHint.replaceChildren();

    if (!page) {
      viewport.replaceChildren(el('div', { class: 'pl-ann-textpage' }, t('paper.annotate.noPages')));
      return;
    }
    if (!hasImage) {
      viewport.replaceChildren(el('div', { class: 'pl-text-page pl-ann-textpage' },
        el('p', {}, t('paper.annotate.thisIsAPlainTextPageExtracted')),
        el('p', { class: 'help' }, t('paper.annotate.uploadAScanOrPdfPageTo'))));
      viewerHint.append(el('span', {}, t('paper.annotate.plainTextPagesDoNotSupportSegmentation')));
      return;
    }
    if (state.loadError) {
      viewport.replaceChildren(el('div', { class: 'pl-ann-loaderror' },
        notice(state.loadError, 'error'),
        button(t('paper.annotate.reloadThisPage'), () => busy(document.activeElement, async () => {
          await loadPage(ui.pageNo, { force: true });
          renderAll();
        }), '', 'repeat')));
      return;
    }

    const canvas = el('div', { class: 'pl-ann-canvas', style: `width:${renderWidth}px` });
    const image = el('img', {
      class: 'pl-ann-image', src: page.image_url, alt: t('paper.import.pageOriginalImage', { v1: page.page_no }),
      loading: 'eager', draggable: 'false',
    });
    const overlay = el('div', {
      class: 'pl-ann-overlay', tabindex: '0', role: 'application',
      'aria-label': t('paper.annotate.segmentationCanvasForPageDragOnEmpty', { v1: page.page_no }),
    });
    image.addEventListener('error', () => {
      viewport.replaceChildren(notice(t('paper.annotate.failedToLoadThePageImageRefresh'), 'error'));
    });
    canvas.append(image, overlay);
    viewport.replaceChildren(canvas);
    if (boundOverlay !== overlay) {
      boundOverlay = overlay;
      bindCanvas(overlay);
    }
    renderOverlay();
    viewerHint.append(el('span', {}, t('paper.annotate.dragOnEmptySpaceToCreateA')
      + t('paper.annotate.arrowKeysToNudgeShift10pxDelete')));
  }

  function bindCanvas(overlay) {
    overlay.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      const page = currentPage();
      if (!page || !page.image_url) return;
      if (event.target !== overlay) return;
      const start = pointOf(event, overlay);
      const kind = event.shiftKey ? 'answer' : ui.mode;
      const ghost = el('div', { class: `pl-ann-ghost pl-ann-ghost-${kind}` });
      ghost.setAttribute('style', boxStyle(normalizeBox({ ...start, w: MIN_BOX_SIDE, h: MIN_BOX_SIDE })));
      overlay.append(ghost);
      let last = { ...start };
      let moved = false;
      beginPointer(event, {
        onMove: ev => {
          last = pointOf(ev, overlay);
          moved = true;
          ghost.setAttribute('style', boxStyle(rectBetween(start, last)));
        },
        onUp: () => {
          ghost.remove();
          const rect = rectBetween(start, last);
          if (moved && rect.w >= MIN_DRAW_SIZE && rect.h >= MIN_DRAW_SIZE) {
            addBox(kind, rect);
          } else {
            selectBox(null);
          }
        },
      });
    });

    overlay.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        selectBox(null);
      }
    });
  }

  function pointOf(event, overlay) {
    const rect = overlay.getBoundingClientRect();
    if (!rect.width || !rect.height) return { x: 0, y: 0 };
    return {
      x: clamp((event.clientX - rect.left) / rect.width, 0, 1),
      y: clamp((event.clientY - rect.top) / rect.height, 0, 1),
    };
  }

  function renderOverlay() {
    const overlay = viewport.querySelector('.pl-ann-overlay');
    if (!overlay) return;
    overlay.replaceChildren();
    const state = currentState();
    const page = currentPage();
    if (!page || !page.image_url) return;

    if (ui.showDrafts) {
      for (const item of pageItems(ui.pageNo)) {
        const bbox = itemBbox(item);
        if (!bbox) continue;
        const stale = itemIsStale(item);
        const node = el('div', {
          class: `pl-ann-draft${item.question_id ? ' is-committed' : ''}${stale ? ' is-stale' : ''}`
            + `${ui.highlightItemId && Number(ui.highlightItemId) === Number(item.id) ? ' is-highlight' : ''}`,
          style: boxStyle(bbox),
          'data-item-id': String(item.id),
        }, el('span', { class: 'pl-ann-draft-label' },
          `#${item.item_no} ${itemStatusLabel(item)}${stale ? ' · stale' : ''}`));
        overlay.append(node);
      }
    }

    for (const box of state.boxes) {
      const selected = box.id === ui.selectedBoxId;
      const item = box.item_id ? findItem(box.item_id) : null;
      const stale = itemIsStale(item);
      const classes = [
        'pl-ann-box', `pl-ann-box-${box.kind}`,
        selected ? 'is-selected' : '',
        item ? 'is-linked' : '',
        stale ? 'is-stale' : '',
      ].filter(Boolean).join(' ');
      const node = el('div', {
        class: classes, style: boxStyle(box), 'data-box-id': box.id, tabindex: '0',
        'aria-label': `${KIND_LABEL[box.kind]} ${box.id}${box.label ? ` ${box.label}` : ''}`,
      });
      node.append(el('span', { class: 'pl-ann-box-label' },
        `${box.kind === 'question' ? 'Q' : 'A'}·`,
        el('span', { 'data-no-i18n': '' }, box.label || box.id),
        item ? t('paper.annotate.question', { v1: item.item_no }) : ''));
      node.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        if (event.target.classList.contains('pl-ann-handle')) return;
        event.stopPropagation();
        selectBox(box.id);
        startMove(event, box, node, overlay);
      });
      node.addEventListener('click', event => event.stopPropagation());
      node.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectBox(box.id);
        }
      });
      if (selected) {
        for (const dir of HANDLE_DIRS) {
          const handle = el('span', {
            class: `pl-ann-handle pl-ann-handle-${dir}`, 'data-handle': dir,
            'aria-hidden': 'true', title: t('paper.annotate.dragToResize'),
          });
          handle.addEventListener('pointerdown', event => {
            if (event.button !== 0) return;
            event.preventDefault();
            event.stopPropagation();
            startResize(event, box, node, overlay, dir);
          });
          node.append(handle);
        }
      }
      overlay.append(node);
    }
  }

  function startMove(event, box, node, overlay) {
    const rect = overlay.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const origin = { x: box.x, y: box.y };
    const start = { clientX: event.clientX, clientY: event.clientY };
    let moved = false;
    beginPointer(event, {
      onMove: ev => {
        moved = true;
        const dx = (ev.clientX - start.clientX) / rect.width;
        const dy = (ev.clientY - start.clientY) / rect.height;
        Object.assign(box, normalizeBox({ x: origin.x + dx, y: origin.y + dy, w: box.w, h: box.h }));
        node.setAttribute('style', boxStyle(box));
      },
      onUp: () => {
        if (moved) {
          markDirty();
          renderAll();
        }
      },
    });
  }

  function startResize(event, box, node, overlay, dir) {
    const rect = overlay.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const origin = { ...box };
    const start = { clientX: event.clientX, clientY: event.clientY };
    let moved = false;
    beginPointer(event, {
      onMove: ev => {
        moved = true;
        const dx = (ev.clientX - start.clientX) / rect.width;
        const dy = (ev.clientY - start.clientY) / rect.height;
        const next = { ...origin };
        resizeBox(next, dir, dx, dy);
        Object.assign(box, next);
        node.setAttribute('style', boxStyle(box));
      },
      onUp: () => {
        if (moved) {
          markDirty();
          renderAll();
        }
      },
    });
  }

  /* ---------------- 渲染：右侧面板 ---------------- */

  function renderPanel() {
    panelBox.replaceChildren();
    panelBox.append(boxListSection(), draftSection(), resegmentSection(), publishSection());
    syncPanelHighlight();
  }

  function boxListSection() {
    const state = currentState();
    const boxes = state.boxes;
    const section = el('section', { class: 'card pl-ann-panel-card' },
      el('div', { class: 'pl-ann-section-head' },
        el('h3', {}, t('paper.annotate.segmentationBoxesOnThisPage', { v1: boxes.length })),
        el('p', { class: 'help' }, t('paper.annotate.questionBoxBlueCropsTheQuestionImage'))),
      );
    if (!boxes.length) {
      section.append(el('p', { class: 'help pl-ann-empty-hint' },
        t('paper.annotate.noSegmentationBoxesYetDragOnEmpty')));
      return section;
    }
    const list = el('div', { class: 'pl-ann-rows' });
    for (const box of boxes) {
      list.append(boxRow(box));
    }
    section.append(list);
    return section;
  }

  function boxRow(box) {
    const selected = box.id === ui.selectedBoxId;
    const item = box.item_id ? findItem(box.item_id) : null;
    const labelInput = input(t('paper.annotate.labelOptional'), { value: box.label || '', maxlength: '80', 'aria-label': t('paper.annotate.segmentationBoxLabel', { v1: box.id }) });
    labelInput.addEventListener('change', () => {
      box.label = labelInput.value.trim().slice(0, 80);
      markDirty();
      renderOverlay();
      renderToolbar();
      renderPages();
    });
    const fields = [field(t('paper.annotate.label'), labelInput)];
    let scoreInput = null;
    if (box.kind === 'question') {
      scoreInput = el('input', {
        type: 'number', class: 'input', min: '0', max: '1000', step: '0.5',
        value: box.score === null || box.score === undefined ? '' : String(box.score),
        'aria-label': t('paper.annotate.segmentationBoxPoints', { v1: box.id }),
      });
      scoreInput.addEventListener('change', () => {
        const value = Number(scoreInput.value);
        box.score = Number.isFinite(value) && value >= 0 && value <= 1000 ? value : null;
        markDirty();
        renderOverlay();
        renderToolbar();
        renderPages();
      });
      fields.push(field(t('paper.compose.points'), scoreInput));
    }
    const row = el('article', {
      class: `pl-ann-row${selected ? ' is-selected' : ''}${itemIsStale(item) ? ' is-stale' : ''}`,
      'data-box-row': box.id, tabindex: '0',
    },
    el('div', { class: 'pl-ann-row-head' },
      badge(KIND_LABEL[box.kind], KIND_BADGE[box.kind]),
      el('strong', {}, box.id),
      item ? badge(t('paper.annotate.linkedToQuestion', { v1: item.item_no }), itemIsStale(item) ? 'red' : 'teal') : badge(t('paper.annotate.notLinkedToADraft'), ''),
      box.kind === 'question' && item ? itemStatusBadgeText(item) : null),
    el('div', { class: 'pl-ann-row-fields' }, ...fields),
    el('div', { class: 'row pl-ann-row-actions' },
      button(t('paper.annotate.locate'), () => { selectBox(box.id); flashBox(box.id); }, 'ghost small', 'target'),
      box.kind === 'answer'
        ? answerLinkControl(box)
        : questionLinkControl(box),
      button(t('common.delete'), () => deleteBox(box.id), 'ghost small', 'trash')));
    row.addEventListener('click', event => {
      if (event.target.closest('button') || event.target.closest('input') || event.target.closest('select')) return;
      selectBox(box.id);
    });
    return row;
  }

  function itemStatusBadgeText(item) {
    return badge(itemStatusLabel(item), item.question_id ? 'blue' : item.status === 'accepted' ? 'teal' : '');
  }

  function linkSelect(box) {
    const select = el('select', { class: 'input pl-ann-link-select', 'aria-label': t('paper.annotate.linkDraft', { v1: box.id }) });
    select.append(el('option', { value: '' }, t('paper.annotate.chooseADraft')));
    for (const item of pageItems(ui.pageNo)) {
      select.append(el('option', { value: String(item.id) },
        t('paper.annotate.question_2', {
          v1: item.item_no,
          v2: item.question_id ? t('paper.annotate.inBankSuffix') : '',
          v3: itemIsStale(item) ? ' (stale)' : '',
        })));
    }
    select.value = box.item_id ? String(box.item_id) : '';
    return select;
  }

  function questionLinkControl(box) {
    const select = linkSelect(box);
    select.addEventListener('change', () => {
      box.item_id = select.value ? Number(select.value) : null;
      markDirty();
      renderAll();
    });
    return select;
  }

  function answerLinkControl(box) {
    const select = linkSelect(box);
    select.addEventListener('change', async () => {
      const itemId = select.value ? Number(select.value) : 0;
      box.item_id = itemId || null;
      markDirty();
      renderAll();
      if (!itemId) return;
      if (currentState().dirty && !(await savePage({ silent: true }))) return;
      try {
        const result = await rawRequest(
          `/paperlab/imports/${encodeURIComponent(importId)}/items/${encodeURIComponent(itemId)}/answer-box`,
          { method: 'POST', data: { box: { x: box.x, y: box.y, w: box.w, h: box.h } } });
        const state = currentState();
        state.version = num(result.edit?.version, state.version);
        state.boxes = (result.edit?.boxes || state.boxes).map(entry => ({
          id: String(entry.id || ''), kind: entry.kind === 'answer' ? 'answer' : 'question',
          ...normalizeBox(entry), label: entry.label || '',
          item_id: entry.item_id ? Number(entry.item_id) : null,
          score: entry.score === null || entry.score === undefined ? null : Number(entry.score),
        }));
        state.dirty = false;
        syncUnloadGuard();
        const updatedItem = result.item;
        if (updatedItem && updatedItem.id) {
          ui.items = ui.items.map(item => (Number(item.id) === Number(updatedItem.id) ? updatedItem : item));
        }
        toast(t('paper.annotate.answerAreaCoordinatesWrittenIntoThisQuestion'));
        renderAll();
      } catch (error) {
        if (error.status === 409) {
          toast(error.message || t('paper.annotate.thisQuestionIsAlreadyInTheBank'), true);
        } else {
          toast(error.message || t('paper.annotate.failedToLinkTheAnswerAreaPlease'), true);
        }
      }
    });
    return select;
  }

  function draftSection() {
    const items = pageItems(ui.pageNo);
    const section = el('section', { class: 'card pl-ann-panel-card' },
      el('div', { class: 'pl-ann-section-head' },
        el('h3', {}, t('paper.annotate.draftComparisonForThisPage', { v1: items.length })),
        el('p', { class: 'help' }, t('paper.annotate.questionDraftsCreatedUpdatedByReSplitting'))));
    if (!items.length) {
      section.append(el('p', { class: 'help pl-ann-empty-hint' },
        t('paper.annotate.noRecognitionReSplitDraftsOnThis')));
      return section;
    }
    const list = el('div', { class: 'pl-ann-draft-list' });
    for (const item of items) {
      const bbox = itemBbox(item);
      const stale = itemIsStale(item);
      const draft = item.draft || {};
      list.append(el('article', {
        class: `pl-ann-draft-row${stale ? ' is-stale' : ''}`
          + `${ui.highlightItemId && Number(ui.highlightItemId) === Number(item.id) ? ' is-highlight' : ''}`,
        'data-draft-row': String(item.id),
      },
      el('div', { class: 'pl-ann-row-head' },
        el('strong', {}, `#${item.item_no}`),
        badge(itemStatusLabel(item), item.question_id ? 'blue' : item.status === 'accepted' ? 'teal' : 'blue'),
        stale ? badge('stale', 'red') : null,
        bbox ? badge(`${Math.round(bbox.w * 100)}%×${Math.round(bbox.h * 100)}%`, '') : badge(t('paper.annotate.noBbox'), 'amber')),
      el('p', { class: 'pl-ann-draft-stem', 'data-no-i18n': '' }, safeText(draft.stem || t('paper.annotate.stemToBeFilledIn'), 90)),
      el('div', { class: 'row pl-ann-row-actions' },
        button(t('paper.annotate.locate'), () => locateItem(item), 'ghost small', 'search'))));
    }
    section.append(list);
    return section;
  }

  function flashBox(boxId) {
    const node = viewport.querySelector(`[data-box-id="${boxId}"]`);
    if (!node) return;
    node.classList.add('is-flash');
    window.setTimeout(() => node.classList.remove('is-flash'), 900);
    try { node.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch { /* ignore */ }
  }

  async function locateItem(item) {
    if (!item) return;
    const pageNo = Number(item.page_no) || ui.pageNo;
    if (pageNo !== Number(ui.pageNo)) {
      const switched = await switchPage(pageNo);
      if (!switched) return;
    }
    ui.highlightItemId = item.id;
    renderOverlay();
    renderPanel();
    window.setTimeout(() => {
      const row = panelBox.querySelector(`[data-draft-row="${item.id}"]`);
      if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 40);
  }

  function syncPanelHighlight() {
    const rows = panelBox.querySelectorAll('[data-draft-row]');
    for (const row of rows) {
      const itemId = Number(row.getAttribute('data-draft-row'));
      if (itemId === Number(ui.highlightItemId)) {
        try { row.scrollIntoView({ block: 'nearest' }); } catch { /* ignore */ }
        break;
      }
    }
  }

  /* ---------------- 自动布局 / 清空 ---------------- */

  async function evenSplit() {
    const state = currentState();
    const count = await askNumberDialog({
      title: t('paper.annotate.distributeQuestionBoxesEvenly'),
      description: t('paper.annotate.splitThePageVerticallyIntoNQuestion'),
      initial: state.boxes.filter(box => box.kind === 'question').length || 5,
      min: 1, max: 30,
    });
    if (!count) return;
    const hasBoxes = state.boxes.length > 0;
    if (hasBoxes) {
      const ok = await confirmDialog({
        title: t('paper.annotate.regenerateCandidateBoxes'),
        description: t('paper.annotate.thisPageAlreadyHasSegmentationBoxesDistributing', { v1: state.boxes.length, v2: count }),
        confirmText: t('paper.annotate.clearAndGenerate'), danger: true,
      });
      if (!ok) return;
    }
    const top = 0.04;
    const bottom = 0.04;
    const gap = 0.01;
    const usable = 1 - top - bottom - gap * (count - 1);
    const height = usable / count;
    state.boxes = Array.from({ length: count }, (_, index) => ({
      id: nextBoxId('question', []), kind: 'question',
      x: 0.06, y: round4(top + index * (height + gap)), w: 0.88, h: round4(height),
      label: '', item_id: null, score: null,
    }));
    // 连续生成时重新编号，避免 nextBoxId 在空数组上重复。
    state.boxes.forEach((box, index) => { box.id = `q${index + 1}`; });
    markDirty();
    ui.selectedBoxId = state.boxes[0]?.id || null;
    renderAll();
    toast(t('paper.annotate.generatedQuestionBoxesDragResizeThemAnd', { v1: count }));
  }

  async function clearPage() {
    const state = currentState();
    if (!state.boxes.length) return;
    const ok = await confirmDialog({
      title: t('paper.annotate.clearThisPageSSegmentationBoxes'),
      description: t('paper.annotate.thisDeletesAllSegmentationBoxesOnPage', { v1: ui.pageNo, v2: state.boxes.length }),
      confirmText: t('paper.annotate.clearThisPage'), danger: true,
    });
    if (!ok) return;
    state.boxes = [];
    ui.selectedBoxId = null;
    markDirty();
    renderAll();
    toast(t('paper.annotate.clearedThisPageSSegmentationBoxesRemember'));
  }

  /* ---------------- 重切 ---------------- */

  function resegmentSection() {
    const section = el('section', { class: 'card pl-ann-panel-card' },
      el('div', { class: 'pl-ann-section-head' },
        el('h3', {}, t('paper.annotate.reSplitAndAnswerAreas')),
        el('p', { class: 'help' }, t('paper.annotate.cropQuestionImagesFromThePageBy'))));
    section.append(el('div', { class: 'row pl-ann-action-row' },
      button(t('paper.annotate.reSplitThisPage'), event => busy(event.currentTarget, () => runResegment(false)), '', 'scan'),
      button(t('paper.annotate.reSplitAllPages'), event => busy(event.currentTarget, () => runResegment(true)), 'secondary', 'layers')));
    if (ui.lastResegment) {
      const result = ui.lastResegment;
      const body = el('div', { class: 'pl-ann-result' },
        el('div', { class: 'pl-ann-summary-row' },
          badge(t('paper.annotate.new', { v1: num(result.created) }), 'teal'),
          badge(t('paper.annotate.updated', { v1: num(result.updated) }), 'blue'),
          badge(t('paper.import.skipped_3', { v1: num(result.skipped) }), num(result.skipped) ? 'amber' : ''),
          badge(`stale ${num(result.stale)}`, num(result.stale) ? 'red' : '')),
        el('p', { class: 'help' }, result.summary || t('paper.annotate.reSplitComplete')));
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      if (warnings.length) body.append(notice(warnings.slice(0, 8).join(t('common.semicolonSeparator')), 'warning'));
      if (Array.isArray(result.stale_items) && result.stale_items.length) {
        body.append(el('p', { class: 'help' },
          t('paper.annotate.staleInBankQuestionsContentNotOverwritten', { v1: result.stale_items.map(entry => `#${entry.item_id}`).join(t('common.listSeparator')) })));
      }
      section.append(body);
    }
    return section;
  }

  async function runResegment(allPages) {
    if (anyDirty()) {
      if (!(await savePage({ silent: true }))) return;
      const remaining = dirtyPages();
      if (remaining.length) {
        const ok = await confirmDialog({
          title: t('paper.annotate.otherPagesStillHaveUnsavedChanges'),
          description: t('paper.annotate.pageHasUnsavedChangesReSplittingUses', {
      v1: remaining.join(t('common.listSeparator')),
    }),
          confirmText: t('paper.annotate.continueReSplitting'),
        });
        if (!ok) return;
      }
    }
    try {
      const result = await rawRequest(`/paperlab/imports/${encodeURIComponent(importId)}/resegment`, {
        method: 'POST', data: allPages ? {} : { pages: [Number(ui.pageNo)] },
      });
      ui.lastResegment = { ...result, allPages };
      toast(result.summary || t('paper.annotate.reSplitComplete'));
      await refreshImportData();
      await autoLinkBoxes();
      renderAll();
    } catch (error) {
      toast(error.message || t('paper.annotate.reSplitFailedPleaseTryAgain'), true);
    }
  }

  async function autoLinkBoxes() {
    const state = currentState();
    const items = pageItems(ui.pageNo).filter(item => itemBbox(item));
    let linked = 0;
    for (const box of state.boxes) {
      if (box.kind !== 'question' || box.item_id) continue;
      let best = null;
      for (const item of items) {
        const bbox = itemBbox(item);
        if (!bbox) continue;
        const ratio = overlapRatio(box, bbox);
        if (ratio >= 0.9 && (!best || ratio > best.ratio)) best = { ratio, item };
      }
      if (best) {
        box.item_id = best.item.id;
        linked += 1;
      }
    }
    if (linked) {
      markDirty();
      toast(t('paper.annotate.linkedSegmentationBoxesToReSplitDrafts', { v1: linked }));
    }
  }

  /* ---------------- 发布校验 ---------------- */

  function publishSection() {
    const section = el('section', { class: 'card pl-ann-panel-card' },
      el('div', { class: 'pl-ann-section-head' },
        el('h3', {}, t('paper.annotate.prePublishValidation')),
        el('p', { class: 'help' }, t('paper.annotate.statisticsAndOrderChecksOnlyCommittingTo'))),
      el('div', { class: 'row pl-ann-action-row' },
        button(t('paper.annotate.validatePublishReadiness'), event => busy(event.currentTarget, runPublish), '', 'shield')));
    const publish = ui.publish;
    if (!publish) return section;
    if (publish.ok) {
      const data = publish.data || {};
      const summary = data.summary || {};
      section.append(el('div', { class: 'pl-ann-result pl-ann-publish-result' },
        notice(t('paper.annotate.publishValidationPassedStemsAndBboxesAre'), 'success'),
        el('div', { class: 'pl-ann-summary-row' },
          badge(t('paper.annotate.page_2', { v1: num(summary.page_count) }), 'blue'),
          badge(t('paper.import.draft', { v1: num(summary.item_count) }), ''),
          badge(t('paper.annotate.toConfirm', { v1: num(summary.pending) }), num(summary.pending) ? 'amber' : ''),
          badge(t('paper.import.accepted_2', { v1: num(summary.accepted) }), 'teal'),
          badge(t('paper.import.inBank', { v1: num(summary.committed) }), 'blue')),
        Array.isArray(data.order) && data.order.length
          ? el('p', { class: 'help' },
            t('paper.annotate.publishOrder', {
              v1: data.order.slice(0, 12)
                .map(entry => `${t('result.page', { v1: entry.page_no })} #${entry.item_no}`)
                .join(t('common.semicolonSeparator')),
              v2: data.order.length > 12 ? '…' : '',
            }))
          : null,
        Array.isArray(data.warnings) && data.warnings.length
          ? notice(data.warnings.join(t('common.semicolonSeparator')), 'warning') : null));
      return section;
    }
    const detail = publish.detail && typeof publish.detail === 'object' ? publish.detail : {};
    const summary = detail.summary || {};
    const errors = Array.isArray(detail.errors) ? detail.errors : [];
    const box = el('div', { class: 'pl-ann-result pl-ann-result-error pl-ann-publish-result' },
      notice(detail.message || publish.message || t('paper.annotate.publishValidationFailedFixTheIssuesAnd'), 'error'),
      el('div', { class: 'pl-ann-summary-row' },
        badge(t('paper.annotate.page_2', { v1: num(summary.page_count) }), 'blue'),
        badge(t('paper.annotate.candidates', { v1: num(summary.checks?.candidates) }), ''),
        badge(t('paper.annotate.errors', { v1: errors.length }), 'red')),
      );
    if (errors.length) {
      const list = el('div', { class: 'pl-ann-error-list' });
      for (const entry of errors.slice(0, 30)) {
        const item = findItem(entry.item_id);
        const label = item
          ? t('paper.annotate.pageQuestion', { v1: item.page_no, v2: item.item_no })
          : (entry.item_no ? t('paper.annotate.pageQuestion', { v1: entry.page_no, v2: entry.item_no }) : '');
        const messages = Array.isArray(entry.errors) ? entry.errors : [entry.message || t('paper.annotate.publishValidationFailed')];
        list.append(el('article', { class: 'pl-ann-error-row' },
          el('div', { class: 'pl-ann-row-head' },
            el('strong', {}, label || t('paper.annotate.issue')),
            item ? badge(itemStatusLabel(item), 'blue') : null),
          el('p', {}, messages.join(t('common.semicolonSeparator'))),
          item
            ? el('div', { class: 'row pl-ann-row-actions' },
              button(t('paper.annotate.locateQuestion'), () => locateItem(item), 'ghost small', 'search'))
            : null));
      }
      box.append(list);
    }
    if (Array.isArray(detail.warnings) && detail.warnings.length) {
      box.append(notice(detail.warnings.slice(0, 8).join(t('common.semicolonSeparator')), 'warning'));
    }
    section.append(box);
    return section;
  }

  async function runPublish() {
    if (anyDirty()) {
      if (!(await savePage({ silent: true }))) return;
      const remaining = dirtyPages();
      if (remaining.length) {
        const ok = await confirmDialog({
          title: t('paper.annotate.otherPagesStillHaveUnsavedChanges'),
          description: t('paper.annotate.pageHasUnsavedChangesPublishValidationUses', {
      v1: remaining.join(t('common.listSeparator')),
    }),
          confirmText: t('paper.annotate.continueValidation'),
        });
        if (!ok) return;
      }
    }
    try {
      const data = await rawRequest(`/paperlab/imports/${encodeURIComponent(importId)}/publish`, { method: 'POST' });
      ui.publish = { ok: true, data };
      toast(t('paper.annotate.publishValidationPassed'));
    } catch (error) {
      ui.publish = { ok: false, detail: error.detail, message: error.message };
      toast(error.message || t('paper.annotate.publishValidationFailed_2'), true);
    }
    renderPanel();
  }

  /* ---------------- 键盘 ---------------- */

  mount.addEventListener('keydown', event => {
    if (!mount.isConnected) return;
    const target = event.target;
    const editing = target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
    const saveKey = (event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === 's';
    if (saveKey) {
      event.preventDefault();
      if (!ui.saving) savePage();
      return;
    }
    if (editing) return;
    const state = currentState();
    const box = state.boxes.find(entry => entry.id === ui.selectedBoxId);
    if (!box) return;
    const { width, height } = naturalSize();
    const stepPx = event.shiftKey ? 10 : 1;
    const dx = stepPx / width;
    const dy = stepPx / height;
    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault();
      deleteBox(box.id);
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      selectBox(null);
      return;
    }
    const moves = {
      ArrowLeft: [-dx, 0], ArrowRight: [dx, 0], ArrowUp: [0, -dy], ArrowDown: [0, dy],
    };
    if (moves[event.key]) {
      event.preventDefault();
      moveBox(box, ...moves[event.key]);
      markDirty();
      renderOverlay();
      renderToolbar();
      renderPages();
    }
  });

  /* ---------------- 首屏 ---------------- */

  function renderHead() {
    titleEl.textContent = t('paper.annotate.segmentation', {
      v1: ui.detail?.import?.title || t('paper.import.import', { v1: importId }),
    });
    const importRow = ui.detail?.import || {};
    subtitleEl.textContent = t('paper.import.pagesStatus', { v1: ui.pages.length, v2: importRow.status || '—' })
      + t('paper.annotate.pageListOnTheLeftCanvasIn');
    headStatus.replaceChildren(
      badge(t('paper.annotate.page_3', { v1: ui.pages.length }), 'blue'),
      anyDirty() ? badge(t('paper.annotate.pagesUnsaved', { v1: dirtyPages().length }), 'amber') : badge(t('paper.annotate.allSaved'), 'teal'),
      button(t('tasks.refresh'), () => busy(document.activeElement, async () => {
        await refreshImportData();
        await loadPage(ui.pageNo, { force: true });
        renderAll();
      }), 'secondary small', 'repeat'));
  }

  function renderAll() {
    renderHead();
    renderPages();
    renderViewer();
    renderToolbar();
    renderPanel();
    syncUnloadGuard();
  }

  /* 首次加载当前页标注后再渲染，避免画布骨架闪动。 */
  try {
    await loadPage(ui.pageNo);
  } catch (error) {
    if (!current(version)) return;
    toast(error.message || t('paper.annotate.failedToLoadSegmentationAnnotationsPleaseTry'), true);
  }
  if (!current(version)) return;
  renderAll();
  window.addEventListener('resize', () => { if (mount.isConnected) renderViewer(); });
}
