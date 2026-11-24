import { el, button, downloadText } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { escapeHtml } from '../core/markdown.js';

/** 取 `mindmap.*` 文案：locale 缺键时回退中文常量（第二参在 `t()` 里是插值变量）。 */
function ossText(key, fallback) {
  const value = t(key);
  return value !== key ? value : fallback;
}

const BRANCH_COLORS = ['#147d70', '#48689e', '#b56e2d', '#7a5195', '#2c7a5b', '#a84a54', '#2e7d9b'];

function cleanNodeLabel(raw) {
  let s = String(raw || '').trim();
  s = s.replace(/^root\s*\(\((.*)\)\)$/i, '$1')
    .replace(/^\(\((.*)\)\)$/, '$1')
    .replace(/^\{\{(.*)\}\}$/, '$1')
    .replace(/^\((.*)\)$/, '$1')
    .replace(/^\[(.*)\]$/, '$1')
    .replace(/^["']|["']$/g, '')
    .trim();
  return s || t('mindmap.node');
}

export function parseMermaidMindmap(source) {
  const lines = String(source || '').split(/\r?\n/);
  const items = [];
  for (const raw of lines) {
    if (!raw.trim() || raw.trim().startsWith('%%') || raw.trim() === 'mindmap') continue;
    const indent = raw.match(/^(\s*)/)[1].replace(/\t/g, '  ').length;
    items.push({ indent, label: cleanNodeLabel(raw), children: [] });
  }
  if (!items.length) return { label: t('mindmap.knowledgeMap'), children: [] };
  const root = items[0];
  const stack = [root];
  for (let i = 1; i < items.length; i++) {
    const cur = items[i];
    while (stack.length > 1 && stack[stack.length - 1].indent >= cur.indent) {
      stack.pop();
    }
    stack[stack.length - 1].children.push(cur);
    stack.push(cur);
  }
  return root;
}

export function markdownToMindmapTree(title, mdText) {
  const root = { label: cleanNodeLabel(title || t('nav.notes')), level: 0, children: [] };
  const stack = [root];
  for (const rawLine of String(mdText || '').split(/\r?\n/)) {
    const hm = rawLine.trim().match(/^(#{1,4})\s+(.+)$/);
    if (!hm) continue;
    const level = hm[1].length;
    const label = hm[2].replace(/\[D\d+:[^\]\s]+\]/g, '').replace(/[*`]/g, '').trim().slice(0, 42);
    if (!label) continue;
    const node = { label, level, children: [] };
    while (stack.length > 1 && stack[stack.length - 1].level >= level) stack.pop();
    stack[stack.length - 1].children.push(node);
    stack.push(node);
  }
  return root;
}

function measureTextWidth(text, isRoot = false) {
  let units = 0;
  for (const ch of String(text)) units += ch.charCodeAt(0) > 127 ? 13.5 : 7.5;
  return Math.max(isRoot ? 92 : 68, Math.min(260, Math.round(units + (isRoot ? 28 : 22))));
}

function layoutTree(root) {
  const rowH = 38;
  const colGap = 46;
  let nextLeafY = 0;

  function assignDepthAndWidth(node, depth, branchIdx) {
    node.depth = depth;
    node.branchIdx = branchIdx;
    node.w = measureTextWidth(node.label, depth === 0);
    node.h = depth === 0 ? 36 : 28;
    if (!node.children?.length) {
      node.y = nextLeafY * rowH + 30;
      nextLeafY++;
      return;
    }
    node.children.forEach((ch, idx) => {
      assignDepthAndWidth(ch, depth + 1, depth === 0 ? idx : branchIdx);
    });
    const first = node.children[0].y;
    const last = node.children[node.children.length - 1].y;
    node.y = (first + last) / 2;
  }

  function assignX(node, x) {
    node.x = x;
    let maxRight = x + node.w;
    for (const ch of node.children || []) {
      const r = assignX(ch, x + node.w + colGap);
      if (r > maxRight) maxRight = r;
    }
    return maxRight;
  }

  assignDepthAndWidth(root, 0, 0);
  const totalW = assignX(root, 24) + 32;
  const totalH = Math.max(160, nextLeafY * rowH + 36);
  return { root, width: totalW, height: totalH };
}

export function renderMindmapWidget(treeOrMermaid, { title = t('mindmap.mindMap') } = {}) {
  const tree = typeof treeOrMermaid === 'string' ? parseMermaidMindmap(treeOrMermaid) : treeOrMermaid;
  const { root, width, height } = layoutTree(tree);
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('xmlns', ns);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.setAttribute('class', 'mindmap-svg');

  const edgesG = document.createElementNS(ns, 'g');
  const nodesG = document.createElementNS(ns, 'g');
  svg.append(edgesG, nodesG);

  function draw(node) {
    const color = node.depth === 0 ? '#102b35' : BRANCH_COLORS[node.branchIdx % BRANCH_COLORS.length];
    for (const ch of node.children || []) {
      const x1 = node.x + node.w;
      const y1 = node.y;
      const x2 = ch.x;
      const y2 = ch.y;
      const cx = (x1 + x2) / 2;
      const chColor = BRANCH_COLORS[ch.branchIdx % BRANCH_COLORS.length];
      const path = document.createElementNS(ns, 'path');
      path.setAttribute('d', `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', chColor);
      path.setAttribute('stroke-width', node.depth === 0 ? '2.2' : '1.6');
      path.setAttribute('stroke-opacity', '0.65');
      edgesG.append(path);
      draw(ch);
    }

    const g = document.createElementNS(ns, 'g');
    const rect = document.createElementNS(ns, 'rect');
    rect.setAttribute('x', String(node.x));
    rect.setAttribute('y', String(node.y - node.h / 2));
    rect.setAttribute('width', String(node.w));
    rect.setAttribute('height', String(node.h));
    rect.setAttribute('rx', node.depth === 0 ? '18' : '8');
    rect.setAttribute('fill', node.depth === 0 ? '#102b35' : (node.depth === 1 ? color : '#ffffff'));
    rect.setAttribute('stroke', color);
    rect.setAttribute('stroke-width', node.depth <= 1 ? '0' : '1.4');

    const text = document.createElementNS(ns, 'text');
    text.setAttribute('x', String(node.x + node.w / 2));
    text.setAttribute('y', String(node.y + 4.5));
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('font-size', node.depth === 0 ? '13.5' : '12');
    text.setAttribute('font-family', '"PingFang SC","Microsoft YaHei",system-ui,sans-serif');
    text.setAttribute('font-weight', node.depth <= 1 ? '600' : '400');
    text.setAttribute('fill', node.depth <= 1 ? '#ffffff' : '#1d3742');
    const label = node.label.length > 22 ? node.label.slice(0, 21) + '…' : node.label;
    text.textContent = label;
    if (node.label.length > 22) {
      const tip = document.createElementNS(ns, 'title');
      tip.textContent = node.label;
      g.append(tip);
    }
    g.append(rect, text);
    nodesG.append(g);
  }

  draw(root);

  let scale = 1;
  const viewport = el('div', { class: 'mindmap-viewport' }, svg);
  const applyScale = () => {
    svg.style.width = `${Math.round(width * scale)}px`;
    svg.style.height = `${Math.round(height * scale)}px`;
  };
  applyScale();

  return el('div', { class: 'mindmap-card' },
    el('div', { class: 'mindmap-toolbar' },
      el('strong', { class: 'small-text' }, `🗺 ${title}`),
      el('div', { class: 'row' },
        button(t('mindmap.zoomOut'), () => { scale = Math.max(0.5, +(scale - 0.15).toFixed(2)); applyScale(); }, 'ghost small'),
        button(t('physics.reset'), () => { scale = 1; applyScale(); }, 'ghost small'),
        button(t('mindmap.zoomIn'), () => { scale = Math.min(2.0, +(scale + 0.15).toFixed(2)); applyScale(); }, 'ghost small'),
        button(t('mindmap.downloadSvg'), () => {
          const serialized = new XMLSerializer().serializeToString(svg);
          downloadText(`${title}.svg`, serialized, 'image/svg+xml;charset=utf-8');
        }, 'secondary small', 'download'))),
    viewport);
}

/* ---------------------------------------------------------------------------
 * 批次五（§6.4）：markmap 交互脑图。
 * 打包产物 web/vendor/markmap/markmap-view.iife.min.js 由
 * scripts/build_markmap_bundle.mjs 生成（版本锁定，重建字节一致）；
 * 节点由既有树结构 {label, children} 转成 markmap 的 {content, children}，
 * 样式经 markmap 的 embedGlobalCSS 内嵌，无需外部 CSS。
 * ------------------------------------------------------------------------- */

const MARKMAP_SCRIPT_URL = '/static/vendor/markmap/markmap-view.iife.min.js';
const MARKMAP_MARK = 'ossMarkmapScript';
const MARKMAP_LOAD_TIMEOUT_MS = 15000;
let markmapLoader = null;

/**
 * markmap 的节点 content 按 HTML 写入页面；笔记标题/正文是用户内容，
 * 复用核心转义函数（与 DOMPurify fail-closed 的 `markdown()` 同一安全口径），
 * 按纯文本渲染（§6.4 先做纯文本节点），避免注入并保证真实 CSP 零违规。
 */
export const escapeMindmapLabel = escapeHtml;

/** 首次调用时注入打包产物（IIFE 全局 Markmap）；失败可重试。 */
export function loadMarkmap() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error(ossText('mindmap.interactiveFailed', '交互脑图加载失败，可使用静态视图。')));
  }
  const existing = window.Markmap;
  if (existing && typeof existing.Markmap === 'function' && typeof existing.deriveOptions === 'function') {
    return Promise.resolve(existing);
  }
  if (markmapLoader) return markmapLoader;
  markmapLoader = new Promise((resolve, reject) => {
    let script = document.querySelector(`script[data-oss="${MARKMAP_MARK}"]`);
    const finish = () => {
      const library = window.Markmap;
      if (library && typeof library.deriveOptions === 'function' && library.Markmap) {
        resolve(library);
        return;
      }
      if (script) script.remove();
      reject(new Error(ossText('mindmap.interactiveFailed', '交互脑图加载失败，可使用静态视图。')));
    };
    if (script && script.dataset.loaded === '1') {
      finish();
      return;
    }
    if (!script) {
      script = document.createElement('script');
      script.src = MARKMAP_SCRIPT_URL;
      script.async = true;
      script.dataset.oss = MARKMAP_MARK;
      document.head.append(script);
    }
    const timer = setTimeout(() => {
      if (script) script.remove();
      reject(new Error(ossText('mindmap.interactiveFailed', '交互脑图加载失败，可使用静态视图。')));
    }, MARKMAP_LOAD_TIMEOUT_MS);
    script.addEventListener('load', () => {
      clearTimeout(timer);
      script.dataset.loaded = '1';
      finish();
    }, { once: true });
    script.addEventListener('error', () => {
      clearTimeout(timer);
      script.remove();
      reject(new Error(ossText('mindmap.interactiveFailed', '交互脑图加载失败，可使用静态视图。')));
    }, { once: true });
  }).catch(error => {
    markmapLoader = null;
    throw error;
  });
  return markmapLoader;
}

/**
 * 交互脑图（可折叠、可缩放）；返回 {element, destroy}。
 * 输入是既有 parseMermaidMindmap / markdownToMindmapTree 的 {label, children} 树。
 */
export function renderInteractiveMindmap(tree, { title = t('mindmap.mindMap') } = {}) {
  const host = el('div', {
    class: 'mindmap-interactive',
    role: 'region',
    'aria-busy': 'true',
    'aria-label': title || t('mindmap.mindMap'),
  });
  let instance = null;
  let destroyed = false;
  (async () => {
    try {
      const library = await loadMarkmap();
      if (destroyed) return;
      const ns = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(ns, 'svg');
      svg.setAttribute('class', 'mindmap-interactive-svg');
      host.replaceChildren(svg);
      // markmap 节点:{content, children};fit 等首帧后再调,初始渲染是异步过渡。
      // content 是 HTML：笔记文本先转义，按纯文本渲染。
      const toNode = node => ({
        content: escapeMindmapLabel(node.label),
        children: (node.children || []).map(toNode),
      });
      instance = library.Markmap.create(svg, library.deriveOptions({ maxWidth: 260 }), toNode(tree));
      host.removeAttribute('aria-busy');
      requestAnimationFrame(() => {
        // fit() 是 async：显式 catch，且 destroy 后不再执行。
        if (destroyed || !instance) return;
        Promise.resolve(instance.fit()).catch(() => { /* 首帧未就绪时忽略 */ });
      });
    } catch (error) {
      if (destroyed) return;
      host.removeAttribute('aria-busy');
      console.warn('[mindmap] 交互视图加载失败：', error);
      host.replaceChildren(el('p', { class: 'help', role: 'status' },
        ossText('mindmap.interactiveFailed', '交互脑图加载失败，可使用静态视图。')));
    }
  })();
  return {
    element: host,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      try { if (instance) instance.destroy(); } catch { /* 忽略 */ }
      host.remove();
    },
  };
}
