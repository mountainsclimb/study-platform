/* sigma.js + graphology 知识图谱（方案 §6.6）。
 *
 * 用法：
 *   const controller = renderKnowledgeGraph(container, graphData, {onSelect});
 *   await controller.ready;
 *   controller.destroy();
 *
 * 数据来自 `GET /api/insight/kp-graph`（节点=知识点，边=同题共现/从属）；
 * 布局用知识点层级做确定性三级布局，不引入额外力导向库。
 * 纪律：脚本同源懒加载、无 eval/内联脚本；节点标题按纯文本写入 sigma 标签。
 */
import { el } from '../core/dom.js';
import { t } from '../core/i18n.js';

const GRAPHOLOGY_URL = '/static/vendor/graph/graphology.umd.min.js';
const SIGMA_URL = '/static/vendor/graph/sigma.min.js';
const LOAD_TIMEOUT_MS = 15000;
const LEVEL_GAP = 180;
const ROW_GAP = 64;

let loaderPromise = null;

function ossText(key, fallback) {
  const value = t(key);
  return value !== key ? value : fallback;
}

function loadScript(url, mark) {
  const existing = document.querySelector(`script[data-oss="${mark}"]`);
  if (existing && existing.dataset.loaded === '1') return Promise.resolve();
  return new Promise((resolve, reject) => {
    let script = existing;
    if (!script) {
      script = document.createElement('script');
      script.src = url;
      script.async = true;
      script.dataset.oss = mark;
      document.head.append(script);
    }
    const timer = setTimeout(() => {
      if (script) script.remove();
      reject(new Error(ossText('insight.graph.loadFailed', '知识图谱加载失败')));
    }, LOAD_TIMEOUT_MS);
    script.addEventListener('load', () => {
      clearTimeout(timer);
      script.dataset.loaded = '1';
      resolve();
    }, { once: true });
    script.addEventListener('error', () => {
      clearTimeout(timer);
      script.remove();
      reject(new Error(ossText('insight.graph.loadFailed', '知识图谱加载失败')));
    }, { once: true });
  });
}

/** 首次调用时注入 graphology 与 sigma（UMD 全局）；失败可重试。 */
export function loadGraphLibraries() {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return Promise.reject(new Error(ossText('insight.graph.unavailable', '当前环境不支持知识图谱')));
  }
  if (window.graphology && window.Sigma) return Promise.resolve(window);
  if (loaderPromise) return loaderPromise;
  loaderPromise = (async () => {
    if (!window.graphology) await loadScript(GRAPHOLOGY_URL, 'ossGraphologyScript');
    if (!window.Sigma) await loadScript(SIGMA_URL, 'ossSigmaScript');
    if (!window.graphology || !window.Sigma) {
      throw new Error(ossText('insight.graph.loadFailed', '知识图谱加载失败'));
    }
    return window;
  })().catch(error => {
    loaderPromise = null;
    throw error;
  });
  return loaderPromise;
}

/** 规范化 `/api/insight/kp-graph` 响应；不可用/结构非法返回 null。 */
export function normalizeKnowledgeGraph(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (raw.available === false) return null;
  const nodesRaw = Array.isArray(raw.nodes) ? raw.nodes : [];
  const edgesRaw = Array.isArray(raw.edges) ? raw.edges : [];
  if (!nodesRaw.length) return null;
  const nodes = [];
  const seen = new Set();
  for (const item of nodesRaw) {
    if (!item || typeof item !== 'object') return null;
    const kpId = Number(item.kp_id);
    if (!Number.isInteger(kpId) || seen.has(kpId)) continue;
    seen.add(kpId);
    nodes.push({
      kp_id: kpId,
      title: String(item.title || ''),
      level: Number.isInteger(item.level) ? item.level : 1,
      path: String(item.path || ''),
      attempts: Number(item.attempts) || 0,
      accuracy: Number.isFinite(Number(item.accuracy)) ? Number(item.accuracy) : null,
      weak: item.weak === true,
      weight: Math.max(1, Number(item.weight) || 1),
    });
  }
  if (!nodes.length) return null;
  const edges = [];
  for (const item of edgesRaw) {
    if (!item || typeof item !== 'object') continue;
    const source = Number(item.source);
    const target = Number(item.target);
    if (!seen.has(source) || !seen.has(target) || source === target) continue;
    edges.push({
      source,
      target,
      type: item.type === 'hierarchy' ? 'hierarchy' : 'cooccur',
      weight: Math.max(1, Number(item.weight) || 1),
    });
  }
  return {
    available: true,
    subject: raw.subject || null,
    days: raw.days || null,
    note: raw.note || null,
    nodes,
    edges,
  };
}

/** 按 level 分层、层内按名称排序的确定性布局（不依赖随机数/力导向）。 */
function layoutNodes(nodes) {
  const byLevel = new Map();
  for (const node of nodes) {
    const level = Math.max(1, Math.min(6, node.level || 1));
    if (!byLevel.has(level)) byLevel.set(level, []);
    byLevel.get(level).push(node);
  }
  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  const positions = new Map();
  levels.forEach((level, column) => {
    const row = byLevel.get(level).sort((a, b) => (
      a.title < b.title ? -1 : a.title > b.title ? 1 : a.kp_id - b.kp_id
    ));
    const offset = ((row.length - 1) * ROW_GAP) / 2;
    row.forEach((node, index) => {
      positions.set(node.kp_id, {
        x: column * LEVEL_GAP,
        y: index * ROW_GAP - offset,
      });
    });
  });
  return positions;
}

function nodeColor(node, highlighted) {
  if (node.weak) return highlighted ? '#e05a4f' : '#c0392b';
  if (node.attempts === 0) return highlighted ? '#9aa8b5' : '#8a97a3';
  return highlighted ? '#4b8ef0' : '#2f6fed';
}

/**
 * 在容器内渲染知识图谱；返回 `{element, ready, destroy}`。
 * 点击节点回调 `onSelect(node)`（节点已含 kp_id/title/path 等）。
 */
export function renderKnowledgeGraph(container, rawGraph, options = {}) {
  const graphData = normalizeKnowledgeGraph(rawGraph);
  const host = el('div', {
    class: 'oss-kp-graph',
    role: 'region',
    'aria-label': ossText('insight.graph.title', '知识图谱'),
    'aria-busy': 'true',
  });
  let destroyed = false;
  let rendererRef = null;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  function fail(message, detail) {
    if (detail) console.warn('[knowledgeGraph]', detail);
    host.removeAttribute('aria-busy');
    host.replaceChildren(el('p', { class: 'help oss-kp-graph-unavailable', role: 'status' }, message));
    resolveReady({ ok: false, reason: 'failed' });
  }

  if (!graphData) {
    host.append(el('p', { class: 'help oss-kp-graph-unavailable', role: 'status' },
      ossText('insight.graph.empty', '暂无可展示的知识点关联。')));
    if (container) container.replaceChildren(host);
    resolveReady({ ok: false, reason: 'invalid' });
    return {
      element: host,
      ready,
      destroy: () => {
        host.remove();
        if (container) container.replaceChildren();
        resolveReady({ ok: false, reason: 'destroyed' });
      },
    };
  }

  if (container && typeof container.replaceChildren === 'function') container.replaceChildren(host);
  const canvas = el('div', { class: 'oss-kp-graph-canvas' });
  const legend = el('p', { class: 'help oss-kp-graph-legend' },
    ossText('insight.graph.legend', '红色=薄弱知识点，蓝色=已练，灰色=未练；点击节点进入对应练习。'));
  host.append(canvas, legend);

  let observer = null;
  function teardown() {
    try { if (rendererRef) rendererRef.kill(); } catch { /* 忽略 */ }
    rendererRef = null;
    if (observer) { observer.disconnect(); observer = null; }
  }
  if (typeof MutationObserver === 'function' && document.body) {
    // 页面/路由重渲染直接丢弃宿主 DOM 时，停掉 sigma 的渲染循环（review L3 同口径）。
    observer = new MutationObserver(() => {
      if (!host.isConnected) teardown();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  (async () => {
    let libs;
    try {
      libs = await loadGraphLibraries();
    } catch (error) {
      if (!destroyed) fail(error?.message || ossText('insight.graph.loadFailed', '知识图谱加载失败'), error);
      return;
    }
    if (destroyed) return;
    try {
      const graph = new libs.graphology.Graph({ multi: false, type: 'directed' });
      const positions = layoutNodes(graphData.nodes);
      const meta = new Map(graphData.nodes.map(node => [node.kp_id, node]));
      for (const node of graphData.nodes) {
        const position = positions.get(node.kp_id) || { x: 0, y: 0 };
        graph.addNode(String(node.kp_id), {
          x: position.x,
          y: position.y,
          label: node.title,
          size: 6 + Math.sqrt(node.weight) * 2,
          color: nodeColor(node, false),
          meta: node,
        });
      }
      for (const edge of graphData.edges) {
        const key = `${edge.source}->${edge.target}`;
        if (graph.hasEdge(key)) continue;
        try {
          graph.addDirectedEdgeWithKey(key, String(edge.source), String(edge.target), {
            type: edge.type,
            weight: edge.weight,
            size: edge.type === 'hierarchy' ? 1 : 1.5,
          });
        } catch { /* 重复边/自环由规范化阶段排除 */ }
      }
      let hovered = null;
      const renderer = new libs.Sigma(graph, canvas, {
        renderEdgeLabels: false,
        labelRenderedSizeThreshold: 6,
        defaultEdgeColor: '#c3ced8',
        defaultNodeColor: '#2f6fed',
        nodeReducer: (node, data) => {
          const info = data.meta || {};
          const highlighted = !hovered || node === hovered || graph.areNeighbors(node, hovered);
          return {
            ...data,
            color: nodeColor(info, highlighted),
            label: info.title || data.label,
            highlighted,
            zIndex: highlighted ? 2 : 1,
          };
        },
        edgeReducer: (edge, data) => {
          const highlighted = !hovered
            || graph.extremities(edge).includes(hovered);
          return { ...data, color: highlighted ? '#9fb3c5' : '#e0e6eb', hidden: !highlighted };
        },
      });
      renderer.on('enterNode', ({ node }) => {
        hovered = node;
        renderer.refresh();
      });
      renderer.on('leaveNode', () => {
        hovered = null;
        renderer.refresh();
      });
      renderer.on('clickNode', ({ node }) => {
        const info = graph.getNodeAttribute(node, 'meta');
        if (info && typeof options.onSelect === 'function') {
          try { options.onSelect(info); } catch { /* 忽略回调失败 */ }
        }
      });
      if (destroyed) {
        renderer.kill();
        return;
      }
      rendererRef = renderer;
      host.removeAttribute('aria-busy');
      resolveReady({ ok: true });
    } catch (error) {
      if (!destroyed) fail(ossText('insight.graph.buildFailed', '知识图谱构建失败。'), error);
    }
  })();

  return {
    element: host,
    ready,
    get renderer() { return rendererRef; },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      teardown();
      host.remove();
      if (container && typeof container.replaceChildren === 'function') container.replaceChildren();
      resolveReady({ ok: false, reason: 'destroyed' });
    },
  };
}
