import { el, button, downloadText } from '../core/dom.js';
import { t } from '../core/i18n.js';

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
