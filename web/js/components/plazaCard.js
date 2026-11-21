/**
 * 广场公开页的共享卡片与标签投影（访客壳与广场页共用，纯 DOM 渲染、无框架）。
 *
 * 后端 payload 约定（WS13 已实现，WS14 并行补齐 community 可见性/公开图）：
 *   { public_id, status, status_label, source, subject, type, grade, difficulty,
 *     knowledge_tags, stem_preview, has_answer, has_figures, review:{...}, notice }
 * 前端只按 status/source/notice 渲染；字段缺失时降级为通用文案，不因后端演进崩页。
 */
import { el, badge, button, readableSummary } from '../core/dom.js';
import { t } from '../core/i18n.js';

// 广场快照的题型枚举（paperlab schema 归一化后的值）复用练习页已有题型文案。
const TYPE_KEY_MAP = {
  SINGLE: 'single_choice',
  MULTIPLE: 'multi_choice',
  BLANK: 'fill_blank',
  SUBJECTIVE: 'short_answer',
  MIXED: 'material_group',
};

/** 题型标签：优先复用 question.type.* 语言包，未知值回退后端原文/「其他」。 */
export function plazaTypeLabel(type) {
  const raw = String(type || '').trim();
  const key = TYPE_KEY_MAP[raw.toUpperCase()];
  if (key) {
    const label = t(`question.type.${key}`);
    if (label !== `question.type.${key}`) return label;
  }
  return raw || t('plaza.type.unknown');
}

/**
 * 审核状态投影：统一输出 { key, label, tone }。
 * key 用于页面判断「社区待核验」等分支，label/tone 用于徽标。
 * 后端（WS14 契约）：列表/详情新增 review_status = community | verified | featured，
 * community 表示初审通过、评审中的「社区待核验」；旧 payload 则回退按 status 映射。
 * - withdrawn / disputed → 已撤回 / 有争议（red，优先于 review_status）；
 * - review_status=community 或 status=community_review → 社区待核验（amber）；
 * - review_status=verified 或 status=verified → 社区核验通过（teal）；
 * - review_status=featured 或 status=published → 平台精选（blue）；
 * - 其它（含未来新增状态）→ 显示后端 status_label 原文。
 */
export function plazaStatusMeta(record = {}) {
  const status = String(record.status || '');
  const reviewStatus = String(record.review_status || '').trim().toLowerCase();
  if (status === 'withdrawn') return { key: 'withdrawn', label: t('plaza.status.withdrawn'), tone: 'red' };
  if (status === 'disputed') return { key: 'disputed', label: t('plaza.status.disputed'), tone: 'red' };
  if (reviewStatus === 'featured' || status === 'published') {
    return { key: 'featured', label: t('plaza.status.featured'), tone: 'blue' };
  }
  if (reviewStatus === 'community' || status === 'community_review') {
    return { key: 'pending', label: t('plaza.status.communityPending'), tone: 'amber' };
  }
  if (reviewStatus === 'verified' || status === 'verified') {
    return { key: 'verified', label: t('plaza.status.verified'), tone: 'teal' };
  }
  const label = String(record.status_label || '').trim() || t('plaza.status.unknown');
  return { key: 'other', label, tone: '' };
}

/** 来源投影：featured=平台精选，其余按社区来源展示。 */
export function plazaSourceMeta(record = {}) {
  const featured = String(record.source || '') === 'featured'
    || String(record.status || '') === 'published'
    || String(record.review_status || '') === 'featured';
  return featured
    ? { key: 'featured', label: t('plaza.source.featured'), tone: 'blue' }
    : { key: 'community', label: t('plaza.source.community'), tone: 'teal' };
}

/** 列表卡片的筛选关键词高亮（可选中列表摘要里的命中词）。 */
function highlighted(text, keyword) {
  const value = String(text || '');
  const term = String(keyword || '').trim();
  if (!term) return document.createTextNode(value);
  const fragment = document.createDocumentFragment();
  const lower = value.toLowerCase();
  const needle = term.toLowerCase();
  let cursor = 0;
  let index = lower.indexOf(needle);
  while (index >= 0) {
    if (index > cursor) fragment.append(document.createTextNode(value.slice(cursor, index)));
    fragment.append(el('mark', { class: 'kw-mark' }, value.slice(index, index + term.length)));
    cursor = index + term.length;
    index = lower.indexOf(needle, cursor);
  }
  if (cursor < value.length) fragment.append(document.createTextNode(value.slice(cursor)));
  return fragment;
}

/**
 * 公开题列表卡片。`item` 为 GET /api/plaza/questions 的 items[] 元素；
 * `onOpen` 为打开详情的回调（页面传 go('plaza', null, {id})）。
 * 返回 null 表示这一条没有可用的 public_id（调用方跳过即可）。
 */
export function plazaCard(item, { onOpen = null, keyword = '' } = {}) {
  if (!item || !item.public_id) return null;
  const status = plazaStatusMeta(item);
  const source = plazaSourceMeta(item);
  const tags = (Array.isArray(item.knowledge_tags) ? item.knowledge_tags : [])
    .map(tag => String(tag || '').trim())
    .filter(Boolean);
  const head = [item.subject, item.type ? plazaTypeLabel(item.type) : '', item.grade]
    .map(value => String(value || '').trim())
    .filter(Boolean)
    .join(' · ');
  const review = item.review && typeof item.review === 'object' ? item.review : {};
  const reviews = Number(review.independent_reviews) || 0;
  const stem = readableSummary(item.stem_preview || item.stem || '', 170) || t('plaza.card.noStem');

  const badges = el('div', { class: 'plaza-card-badges' },
    badge(status.label, status.tone),
    // 精选状态徽标已表达来源，不再重复来源标签；社区题同时显示「社区来源」。
    source.key === 'featured' ? null : badge(source.label, source.tone),
    item.has_answer ? badge(t('plaza.card.hasAnswer'), 'teal') : badge(t('plaza.card.noAnswer')),
    item.has_figures ? badge(t('plaza.card.hasFigure')) : null,
    // WS14 契约：有图但暂无匿名公开图时列表会带 figure_notice，卡片上提前提示。
    item.figure_notice ? badge(t('plaza.card.figureNotice'), 'amber') : null);

  const meta = [];
  if (reviews > 0) {
    meta.push(t('plaza.card.reviewProgress', {
      reviews,
      approves: Number(review.approves) || 0,
      subject: Number(review.subject_reviewer_approves) || 0,
    }));
  }

  return el('article', { class: 'card plaza-card', 'data-public-id': item.public_id },
    badges,
    el('h3', { class: 'plaza-card-title' }, head || t('plaza.card.untitled')),
    el('p', { class: 'plaza-card-stem' }, highlighted(stem, keyword)),
    tags.length
      ? el('div', { class: 'plaza-card-tags' },
        ...tags.slice(0, 6).map(tag => el('span', { class: 'kp-chip', 'data-no-i18n': '' }, tag)))
      : null,
    meta.length ? el('p', { class: 'plaza-card-meta' }, ...meta) : null,
    el('div', { class: 'plaza-card-actions' },
      el('span', { class: 'plaza-card-id', 'data-no-i18n': '' }, item.public_id),
      onOpen
        ? button(t('plaza.card.open'), onOpen, 'soft small', 'chevron', {
          'aria-label': t('plaza.card.openAria', { id: item.public_id }),
        })
        : null));
}

/** 详情页来源/授权声明字段 → 语言包标签（顺序即展示顺序）。 */
export const SOURCE_FIELD_DEFS = [
  ['url', 'plaza.detail.source.url'],
  ['original_author', 'plaza.detail.source.author'],
  ['license_basis', 'plaza.detail.source.license'],
  ['rights_scope', 'plaza.detail.source.rights'],
  ['attribution', 'plaza.detail.source.attribution'],
  ['reference', 'plaza.detail.source.reference'],
  ['page_no', 'plaza.detail.source.page'],
  ['question_no', 'plaza.detail.source.question'],
];

/** 只允许站内相对路径与 http(s) 链接作为来源地址，其它原样当文本展示。 */
export function safeSourceUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (raw.startsWith('/') && !raw.startsWith('//')) return raw;
  if (/^https?:\/\//i.test(raw)) return raw;
  return '';
}

/** 详情页的来源与授权声明摘要（空字段跳过；url 合法时渲染为外链）。 */
export function plazaSourceDeclaration(sourceDeclaration) {
  const source = sourceDeclaration && typeof sourceDeclaration === 'object' ? sourceDeclaration : {};
  const rows = [];
  for (const [field, labelKey] of SOURCE_FIELD_DEFS) {
    const value = String(source[field] || '').trim();
    if (!value) continue;
    const url = field === 'url' ? safeSourceUrl(value) : '';
    rows.push(el('div', { class: 'plaza-source-row' },
      el('span', { class: 'plaza-source-label' }, t(labelKey)),
      url
        ? el('a', { class: 'plaza-source-value', href: url, target: '_blank', rel: 'noopener noreferrer' }, value)
        : el('span', { class: 'plaza-source-value', 'data-no-i18n': '' }, value)));
  }
  if (!rows.length) return null;
  return el('section', { class: 'card plaza-source-card' },
    el('h2', { class: 'plaza-section-title' }, t('plaza.detail.sourceTitle')),
    ...rows);
}
