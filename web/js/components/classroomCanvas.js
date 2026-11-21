/* 课堂「自由画布编辑」入口（P6b）：父页侧 iframe 模态与 postMessage 桥。
 *
 * 背景：编辑器的构建产物在 `/static/editor/index.html`（`editor/` 目录 React + Vite 构建，
 * 产物随仓库入库）。知序平台的全局安全响应头是
 * `Content-Security-Policy: frame-ancestors 'none'` + `X-Frame-Options: DENY`
 * （见 app/main.py 的 security_headers 中间件，非本模块可改范围），
 * 因此浏览器会直接拦截「iframe 加载 /static/editor/index.html」。
 *
 * 解决办法（不改后端、不降级安全策略）：父页先 `fetch('/static/editor/index.html')`
 * 读出构建入口，把其中相对的 `<script src>` / `<link href>` 绝对化成
 * `/static/editor/assets/...`，再通过 **iframe `srcdoc`** 挂载；srcdoc 文档继承父页 origin
 * （cookie / localStorage / fetch('/api/...') 都按同源处理），脚本/CSS 仍从站内静态目录加载。
 * 若部署环境允许同源 iframe，`openCanvasEditor` 在 fetch 失败时会回退成直接 `src=` 加载。
 *
 * 参数传递：srcdoc 没有查询串，父页把 `{course,scene}` 写进 iframe `name`
 * （`zhixu-canvas:{...}`），编辑器读取 `window.name`；直接打开 URL 时仍支持 `?course=&scene=`。
 *
 * iframe 内编辑器通过 postMessage 通知父页（同源；父页同时校验 origin 与 event.source）：
 *   { type: 'zhixu-slide-ready' }                     编辑器挂载完成
 *   { type: 'zhixu-slide-saved', version, close }     保存成功（close=true 表示保存并关闭）
 *   { type: 'zhixu-slide-dirty', dirty }              未保存变更状态
 *   { type: 'zhixu-slide-close' }                     用户关闭编辑器
 * 保存后调用 onSaved(version)（调用方刷新播放器/场景数据）；刷新前会把学习会话进度
 * 指向刚编辑的场景，确保关闭后播放器直接展示修改后的内容。
 * 移动端/窄屏给出「建议使用电脑」提示（仍允许继续）；不引入任何外链资源。
 */
import { el, toast } from '../core/dom.js';

/** postMessage 协议常量；必须与 editor/src/protocol.ts 保持一致（tests/test_classroom_canvas.py 校验）。 */
export const CANVAS_MESSAGE = Object.freeze({
  ready: 'zhixu-slide-ready',
  saved: 'zhixu-slide-saved',
  dirty: 'zhixu-slide-dirty',
  close: 'zhixu-slide-close',
});

/** 构建产物在静态目录下的固定入口（app.main 把 web/ 挂到 /static）。 */
export const CANVAS_EDITOR_PATH = '/static/editor/index.html';

/** 构建产物资源前缀（用于把 index.html 里的相对 URL 绝对化）。 */
export const CANVAS_EDITOR_BASE = '/static/editor/';

/** iframe name 前缀：srcdoc 挂载时用它把 course/scene 传给编辑器。 */
export const CANVAS_IFRAME_NAME_PREFIX = 'zhixu-canvas:';

/** 播放器会话的 localStorage 前缀（与 classroomPlayer.js 保持一致，用于刷新后定位场景）。 */
const PLAYER_SESSION_KEY_PREFIX = 'zhixu.classroom.session.';

let activeEditor = null;

/** 生成编辑器 iframe 的规范地址（直接打开 / 部署允许同源 iframe 时使用）。 */
export function canvasEditorUrl(courseId, sceneId) {
  const params = new URLSearchParams();
  params.set('course', String(Number(courseId) || 0));
  params.set('scene', String(Number(sceneId) || 0));
  return `${CANVAS_EDITOR_PATH}?${params.toString()}`;
}

/** iframe 的 name：srcdoc 文档没有 location.search，用 window.name 携带参数。 */
export function canvasIframeName(courseId, sceneId) {
  return `${CANVAS_IFRAME_NAME_PREFIX}${JSON.stringify({
    course: Number(courseId) || 0,
    scene: Number(sceneId) || 0,
  })}`;
}

function absolutizeEntryUrl(value) {
  const raw = String(value || '');
  if (raw.startsWith('./')) return CANVAS_EDITOR_BASE + raw.slice(2);
  if (raw.startsWith('../')) return CANVAS_EDITOR_BASE + raw.slice(3);
  return raw;
}

/**
 * 把构建入口 index.html 改写成可放进 srcdoc 的 HTML：
 * - `<script src>` / `<link href>` 的相对路径绝对化为 /static/editor/...；
 * - 去掉 `<base>`（知序平台 CSP `base-uri 'none'` 会拦截）。
 */
export function absolutizeEditorHtml(html) {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  for (const node of doc.querySelectorAll('script[src]')) {
    node.setAttribute('src', absolutizeEntryUrl(node.getAttribute('src')));
  }
  for (const node of doc.querySelectorAll('link[href]')) {
    node.setAttribute('href', absolutizeEntryUrl(node.getAttribute('href')));
  }
  for (const node of Array.from(doc.querySelectorAll('base'))) node.remove();
  return `<!doctype html>${doc.documentElement.outerHTML}`;
}

/** 读取构建入口并生成 srcdoc 内容；失败时抛错（调用方回退直连）。 */
export async function buildEditorSrcdoc() {
  const response = await fetch(CANVAS_EDITOR_PATH, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  });
  if (!response.ok) throw new Error(`编辑器入口读取失败（HTTP ${response.status}）`);
  const html = await response.text();
  if (!/<script[^>]+src=/i.test(html)) throw new Error('编辑器入口缺少脚本资源（构建产物不完整）');
  return absolutizeEditorHtml(html);
}

function isNarrowOrTouchDevice() {
  try {
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return true;
    return window.innerWidth < 900;
  } catch {
    return false;
  }
}

/**
 * 把播放器学习会话进度指向某场景，保证保存后刷新能直接看到修改后的幻灯片。
 * 失败时静默忽略（播放器仍会从上次进度恢复）。
 */
export async function focusPlayerScene(courseId, sceneId, scenes) {
  const index = Array.isArray(scenes)
    ? scenes.findIndex(scene => String(scene?.id) === String(sceneId))
    : -1;
  if (index < 0) return false;
  let sessionId = 0;
  try {
    const raw = window.localStorage.getItem(PLAYER_SESSION_KEY_PREFIX + courseId);
    const parsed = raw ? JSON.parse(raw) : null;
    sessionId = Number(parsed?.id) || 0;
  } catch {
    sessionId = 0;
  }
  if (!sessionId) return false;
  try {
    const response = await fetch(`/api/classroom/sessions/${sessionId}`, {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      body: JSON.stringify({
        scene_id: Number(sceneId),
        progress: {
          scene_index: index,
          action_index: 0,
          scene_id: Number(sceneId),
          updated_at: new Date().toISOString(),
        },
      }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * 打开自由画布编辑器模态。
 * @param {{courseId:number, sceneId:number, sceneTitle?:string, onSaved?:(version:number)=>void}} options
 * @returns {{close:()=>void, iframe:HTMLIFrameElement}|null}
 */
export function openCanvasEditor(options = {}) {
  const courseId = Number(options.courseId) || 0;
  const sceneId = Number(options.sceneId) || 0;
  if (!courseId || !sceneId) {
    toast('缺少课程或场景信息，无法打开自由画布编辑', true);
    return null;
  }
  if (activeEditor) activeEditor.close();

  if (isNarrowOrTouchDevice()) {
    const proceed = window.confirm(
      '自由画布编辑面向电脑浏览器设计（平板建议横屏）。手机端操作空间有限，仍要打开吗？',
    );
    if (!proceed) return null;
  }

  const onSaved = typeof options.onSaved === 'function' ? options.onSaved : null;
  let closed = false;
  let ready = false;
  let dirty = false;
  let savedVersion = null;
  let cleanup = () => {};
  let loadTimer = null;

  const title = String(options.sceneTitle || '未命名幻灯片').slice(0, 80);
  const statusNode = el('span', { class: 'help cls-canvas-status', 'aria-live': 'polite' }, '正在载入编辑器…');
  const closeButton = el('button', {
    type: 'button',
    class: 'button ghost small',
    'aria-label': '关闭自由画布编辑',
    title: '关闭（Esc）',
  }, '关闭');

  const iframe = el('iframe', {
    class: 'cls-canvas-iframe',
    title: `自由画布编辑：${title}`,
    name: canvasIframeName(courseId, sceneId),
    referrerpolicy: 'no-referrer',
    allow: 'clipboard-read; clipboard-write',
  });

  const dialog = el('div', {
    class: 'cls-canvas-dialog',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': '自由画布编辑器',
  },
    el('header', { class: 'cls-canvas-dialog-head' },
      el('div', { class: 'cls-canvas-dialog-title' },
        el('strong', {}, '自由画布编辑'),
        el('span', { class: 'help' }, title)),
      el('div', { class: 'row cls-canvas-head-actions' }, statusNode, closeButton)),
    el('div', { class: 'cls-canvas-frame-host' }, iframe));

  const overlay = el('div', { class: 'cls-canvas-overlay' }, dialog);

  const finish = () => {
    const version = savedVersion;
    closeEditorModal();
    if (version !== null && onSaved) {
      try { onSaved(version); } catch { /* 刷新失败不影响关闭 */ }
    }
  };

  function closeEditorModal() {
    if (closed) return;
    closed = true;
    cleanup();
    overlay.remove();
    if (activeEditor === handle) activeEditor = null;
  }

  function requestClose() {
    if (dirty && !window.confirm('当前修改尚未保存，确定关闭自由画布编辑器吗？（未保存内容已自动存为本地草稿）')) return;
    finish();
  }

  function handleMessage(event) {
    // srcdoc 文档的 event.origin 序列化为 "null"；直接 src 模式则是站内 origin。
    if (event.origin && event.origin !== 'null' && event.origin !== window.location.origin) return;
    if (event.source !== iframe.contentWindow) return;
    const data = event.data;
    if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
    switch (data.type) {
      case CANVAS_MESSAGE.ready:
        ready = true;
        statusNode.textContent = '已就绪 · 修改后点「保存」生效';
        break;
      case CANVAS_MESSAGE.saved: {
        const version = Number(data.version) || null;
        savedVersion = version;
        dirty = false;
        statusNode.textContent = version ? `已保存（版本 ${version}）` : '已保存';
        toast(version ? `自由画布已保存（版本 ${version}）` : '自由画布已保存');
        if (data.close) finish();
        break;
      }
      case CANVAS_MESSAGE.dirty:
        dirty = Boolean(data.dirty);
        break;
      case CANVAS_MESSAGE.close:
        finish();
        break;
      default:
        break;
    }
  }

  function handleKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      requestClose();
    }
  }

  function handleRouteChange() {
    closeEditorModal();
  }

  cleanup = () => {
    window.removeEventListener('message', handleMessage);
    document.removeEventListener('keydown', handleKeydown);
    window.removeEventListener('hashchange', handleRouteChange);
    if (loadTimer) window.clearTimeout(loadTimer);
  };

  closeButton.addEventListener('click', requestClose);
  window.addEventListener('message', handleMessage);
  document.addEventListener('keydown', handleKeydown);
  window.addEventListener('hashchange', handleRouteChange);

  loadTimer = window.setTimeout(() => {
    if (!ready && !closed) statusNode.textContent = '编辑器加载较慢，请稍候…';
  }, 12000);

  document.body.append(overlay);
  try { closeButton.focus({ preventScroll: true }); } catch { /* 忽略 */ }

  // srcdoc 挂载（平台 CSP 禁止同源 URL 被 iframe 嵌入）；失败时回退直连 src。
  buildEditorSrcdoc()
    .then((html) => {
      if (closed) return;
      iframe.setAttribute('srcdoc', html);
    })
    .catch((error) => {
      if (closed) return;
      statusNode.textContent = '编辑器入口读取失败，正在尝试直连…';
      // eslint-disable-next-line no-console
      console.warn('[classroomCanvas] 读取编辑器入口失败：', error);
      iframe.removeAttribute('srcdoc');
      iframe.setAttribute('src', canvasEditorUrl(courseId, sceneId));
    });

  const handle = { close: requestClose, iframe };
  activeEditor = handle;
  return handle;
}

/** 关闭当前打开的自由画布模态（路由切换/销毁页面时调用）。 */
export function closeCanvasEditor() {
  if (activeEditor) activeEditor.close();
}
