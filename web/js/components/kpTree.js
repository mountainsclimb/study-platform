import { el, badge } from '../core/dom.js';

export function buildKpHierarchy(flatItems, subject = null) {
  const filtered = subject ? flatItems.filter(k => k.subject === subject) : flatItems;
  const byId = new Map();
  const roots = [];
  for (const item of filtered) {
    byId.set(item.id, { ...item, children: [] });
  }
  for (const node of byId.values()) {
    if (node.parent_id && byId.has(node.parent_id)) {
      byId.get(node.parent_id).children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

export function renderKpTree(flatItems, { subject = '', selectedIds = [], multi = false, onSelect } = {}) {
  const selected = new Set((selectedIds || []).map(String));
  const roots = buildKpHierarchy(flatItems, subject || null);
  const wrap = el('div', { class: 'kp-tree' });
  if (!roots.length) {
    wrap.append(el('p', { class: 'help' }, subject ? `学科「${subject}」暂无知识点。` : '选择学科后可展开知识点目录。'));
    return wrap;
  }

  function toggleId(id) {
    const sid = String(id);
    if (multi) {
      if (selected.has(sid)) selected.delete(sid);
      else selected.add(sid);
    } else {
      if (selected.has(sid)) selected.clear();
      else { selected.clear(); selected.add(sid); }
    }
    if (onSelect) onSelect([...selected]);
  }

  function renderNode(node) {
    const isSel = selected.has(String(node.id));
    if (node.children?.length) {
      const det = el('details', { class: `kp-branch level-${node.level}`, open: node.level === 1 && roots.length <= 4 });
      const sum = el('summary', {},
        el('span', {
          class: `kp-node-label${isSel ? ' active' : ''}`,
          onClick: e => {
            if (e.target.classList.contains('kp-pick-btn')) return;
          },
        }, node.name),
        el('span', { class: 'kp-counts' },
          node.question_count ? badge(`${node.question_count}题`, 'blue') : null,
          node.wrong_count ? badge(`错${node.wrong_count}`, 'red') : null,
          el('button', {
            type: 'button',
            class: `kp-pick-btn${isSel ? ' active' : ''}`,
            onClick: e => {
              e.preventDefault();
              e.stopPropagation();
              toggleId(node.id);
            },
          }, isSel ? '已选' : '筛选')));
      det.append(sum);
      const childWrap = el('div', { class: 'kp-children' });
      for (const ch of node.children) childWrap.append(renderNode(ch));
      det.append(childWrap);
      return det;
    }
    return el('button', {
      type: 'button',
      class: `kp-leaf${isSel ? ' active' : ''}`,
      onClick: () => toggleId(node.id),
    },
      el('span', {}, node.name),
      el('span', { class: 'kp-counts' },
        node.question_count ? badge(`${node.question_count}`, 'blue') : null,
        node.wrong_count ? badge(`错${node.wrong_count}`, 'red') : null));
  }

  for (const r of roots) wrap.append(renderNode(r));
  return wrap;
}
