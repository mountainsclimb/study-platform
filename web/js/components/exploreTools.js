/* 题目页「探究工具」聚合入口。
 *
 * 目的：操作条不再一行平铺 8–10 个按钮，而是保留一个主入口 + 折叠菜单；
 * 菜单项仍是原有按钮节点，点击行为、深链与无障碍属性完全不变。
 *
 * 推荐规则只做「排序 + 推荐徽标」，不会隐藏其它工具；学科别名表把
 * 「科学 / 技术 / 机械」等归一到同一组规则，避免中文字面量精确匹配漏配。
 */
import { el, badge } from '../core/dom.js';
import { t } from '../core/i18n.js';

function exploreText(key, fallback) {
  const value = t(key);
  return value === key ? fallback : value;
}

/** 学科别名 → 规范学科（用于推荐规则，不改变后端字段）。 */
const SUBJECT_ALIASES = new Map([
  ['数学', '数学'],
  ['物理', '物理'],
  ['化学', '化学'],
  ['生物', '生物'],
  ['历史', '历史'],
  ['政治', '政治'],
  ['地理', '地理'],
  ['科学', '物理'],
  ['技术', '物理'],
  ['机械', '物理'],
  ['通用技术', '物理'],
]);

export function canonicalSubject(subject) {
  const value = String(subject || '').trim();
  return SUBJECT_ALIASES.get(value) || value;
}

const RULES = [
  { subject: '数学', pattern: /函数|导数|切线|三角|圆锥|椭圆|双曲线|向量|几何|不等式|线性规划|方程/, tool: 'geometry' },
  { subject: '数学', pattern: /函数|图像|图象/, tool: 'functionGraph' },
  { subject: '物理', pattern: /力|运动|机械|电|磁|光|波|能量|动量|压强|浮力/, tool: 'sandbox' },
  { subject: '物理', pattern: /机械|结构|传动|机构|工厂|流程/, tool: 'mech' },
  { subject: '化学', pattern: /反应|方程式|配平|平衡|氧化|还原|酸碱|燃烧/, tool: 'chemBalance' },
  { subject: '化学', pattern: /分子|原子|晶体|有机物|结构|官能团/, tool: 'molecule' },
  { subject: '历史', pattern: /.*/, tool: 'timeline' },
  { subject: '政治', pattern: /.*/, tool: 'timeline' },
];

/**
 * 根据学科与知识点标签选出推荐工具 id；没有匹配返回 null。
 * @param {string} subject
 * @param {string[]} tags
 * @returns {string|null}
 */
export function recommendedTool(subject, tags = []) {
  const canonical = canonicalSubject(subject);
  const text = (Array.isArray(tags) ? tags : []).map(tag => String(tag || '')).join(' ');
  for (const rule of RULES) {
    if (rule.subject !== canonical) continue;
    if (!text || rule.pattern.test(text)) return rule.tool;
  }
  return null;
}

/**
 * 创建「探究工具」折叠菜单。
 * @param {{entries: Array<{id:string,label:string,node:HTMLElement}>, subject:string, tags?:string[]}} options
 * @returns {HTMLElement|null}
 */
export function createExploreToolsMenu({ entries = [], subject = '', tags = [] } = {}) {
  const available = entries.filter(entry => entry && entry.node);
  if (!available.length) return null;
  const recommended = recommendedTool(subject, tags);
  const ordered = [...available].sort((left, right) => (
    (right.id === recommended ? 1 : 0) - (left.id === recommended ? 1 : 0)
  ));
  const list = el('div', { class: 'explore-tools-menu', role: 'menu' });
  for (const entry of ordered) {
    const row = el('div', { class: 'explore-tools-item', role: 'none' },
      entry.node,
      entry.id === recommended ? badge(exploreText('study.tools.recommended', '推荐'), 'teal') : null);
    list.append(row);
  }
  const details = el('details', { class: 'explore-tools', 'data-explore-tools': '1' },
    el('summary', { class: 'button soft small explore-tools-summary' },
      exploreText('study.tools.explore', '探究工具'),
      el('span', { class: 'explore-tools-count' }, `(${available.length})`)),
    list);
  return details;
}
