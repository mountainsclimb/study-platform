/* 投稿台（广场所作者端）：我的投稿列表 + 投稿详情/预览/声明/送审/改版/撤回，
 * 以及 Phase 3 的通知入口与作者申诉（rejected/withdrawn/disputed）。
 *
 * 接口（前缀 /api/plaza，见 app/plaza/submissions.py、release.py 与 WS21 质量运营模块）：
 * - GET  /plaza/me                       能力探测（capabilities.submit）
 * - GET  /plaza/submissions/mine         我的投稿（状态筛选 + 分页）
 * - POST /plaza/submissions              从个人题创建不可变投稿快照（{question_id, source, license_confirmed}）
 * - GET  /plaza/submissions/{id}         详情（不可变快照、来源声明、逐项校验、评审进度、can.*、my_appeal）
 * - GET  /plaza/submissions/{id}/events  事件链（作者看节点）
 * - GET  /plaza/submissions/{id}/appeals 我的申诉记录（作者/编辑；未就绪静默降级）
 * - POST /plaza/submissions/{id}/appeals 作者申诉 {reason}（仅 rejected/withdrawn/disputed；409 表示状态不允许）
 * - PATCH /plaza/submissions/{id}        草稿改声明（{version, source, license_confirmed}，CAS）
 * - POST /plaza/submissions/{id}/revise  送审后改版（创建新版本并从待评开始）
 * - POST /plaza/submissions/{id}/submit  送审（{version, confirm_preview:true, license_confirmed}）
 * - POST /plaza/submissions/{id}/withdraw 撤回（{reason}）
 *
 * 页面只负责展示与服务端返回的 can.* 能力对齐；投稿内容是不可变快照，作者要改题干须先
 * 编辑个人题再「改版」，这里不伪造任何审核结论。
 */
import { api } from '../core/api.js';
import { current, go } from '../core/router.js';
import { onSessionReset } from '../core/state.js';
import {
  el, heading, button, input, field, badge, notice, empty, loading, busy,
  toast, number, dateText, query, icon,
} from '../core/dom.js';
import { t } from '../core/i18n.js';
// 通知中心面板（与顶栏铃铛同一实现；投稿台提供「通知中心」入口）。
import { openPlazaNotificationPanel } from '../components/plazaNotifications.js';

const PAGE_SIZE = 10;
/* 与 app/plaza/db.py 的 SUBMISSION_STATUSES / STATUS_LABELS 对齐（顺序即状态机顺序）。 */
const STATUS_ORDER = [
  'draft', 'pending_initial', 'community_review', 'verified',
  'disputed', 'pending_release', 'published', 'withdrawn', 'rejected',
];
const STATUS_VARIANTS = {
  draft: '',
  pending_initial: 'blue',
  community_review: 'amber',
  verified: 'teal',
  disputed: 'red',
  pending_release: 'amber',
  published: 'teal',
  withdrawn: '',
  rejected: 'red',
};
/* 与服务端 SOURCE_TEXT_KEYS 对齐（note 仅作者/编辑可见）。 */
const SOURCE_FIELDS = [
  'url', 'original_author', 'license_basis', 'rights_scope',
  'attribution', 'reference', 'page_no', 'question_no',
];
const EVENT_ACTIONS = [
  'created', 'updated', 'submitted', 'revised', 'author_withdrawn', 'withdrawn',
  'initial_review_accepted', 'initial_review_rejected', 'review_submitted',
  'threshold_met', 'disputed', 'published', 'release_withdrawn',
];

let listState = { items: [], total: 0, page: 1, page_size: PAGE_SIZE, status: '' };
let currentRun = 0;
let activeId = null;

onSessionReset(() => {
  listState = { items: [], total: 0, page: 1, page_size: PAGE_SIZE, status: '' };
  currentRun = 0;
  activeId = null;
});

function statusLabel(code, fallback = '') {
  if (!code) return fallback || '';
  const key = `plaza.submission.status.${code}`;
  const translated = t(key);
  if (translated === key) return fallback || code;
  return translated;
}

function statusBadge(code, fallback = '') {
  return badge(statusLabel(code, fallback), STATUS_VARIANTS[code] ?? '');
}

function statusOptions(includeAll = true) {
  const options = includeAll ? [{ value: '', label: t('plaza.submission.filter.all') }] : [];
  for (const code of STATUS_ORDER) options.push({ value: code, label: statusLabel(code) });
  return options;
}

function reviewSummaryText(summary) {
  if (!summary || summary.hidden) return '';
  const thresholds = summary.thresholds || {};
  return t('plaza.submission.review.summaryLine', {
    reviews: number(summary.independent_reviews || 0),
    minReviews: number(thresholds.min_reviews || 5),
    approves: number(summary.approves || 0),
    minApproves: number(thresholds.min_approves || 4),
    subjectApproves: number(summary.subject_reviewer_approves || 0),
    minSubjectApproves: number(thresholds.min_subject_reviewer_approves || 2),
  });
}

/* ------------------------------------------------------------------------- */
/* 页面入口                                                                   */
/* ------------------------------------------------------------------------- */

export async function renderSubmission(container, version, params = null) {
  const run = ++currentRun;
  activeId = Number(params?.get?.('id') || 0) || null;
  container.replaceChildren(
    heading('PLAZA · SUBMISSIONS', t('plaza.submission.pageTitle'), t('plaza.submission.pageSubtitle')),
    loading(t('plaza.submission.loading')));

  let me;
  try {
    me = await api('/plaza/me');
  } catch (error) {
    if (run !== currentRun || !current(version)) return;
    container.replaceChildren(
      heading('PLAZA · SUBMISSIONS', t('plaza.submission.pageTitle'), t('plaza.submission.pageSubtitle')),
      el('div', { class: 'error-panel' },
        error.message || t('plaza.submission.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:16px' },
          button(t('common.retry'), () => renderSubmission(container, version, params), '', 'repeat'))));
    return;
  }
  if (run !== currentRun || !current(version)) return;

  const shell = el('div', { class: 'plzw-page', 'data-page': 'plaza-submission' });
  container.replaceChildren(
    heading('PLAZA · SUBMISSIONS', t('plaza.submission.pageTitle'), t('plaza.submission.pageSubtitle')),
    shell);

  if (!me.authenticated) {
    shell.append(notice(t('plaza.submission.loginRequired'), 'warning'),
      el('div', { class: 'row' },
        button(t('plaza.submission.goLogin'), () => go('home'), 'secondary', 'lock')));
    return;
  }
  if (!me.capabilities?.submit) {
    shell.append(notice(t('plaza.submission.noPermission'), 'warning'));
    return;
  }

  if (activeId) await renderDetail(shell, version, activeId, me);
  else await renderList(shell, version, me);
}

/* ------------------------------------------------------------------------- */
/* 我的投稿列表                                                               */
/* ------------------------------------------------------------------------- */

async function renderList(shell, version, me) {
  const listHost = el('div', { class: 'plzw-list' });
  const pagerHost = el('div', { class: 'pagination', hidden: true });
  const statusSelect = el('select', { class: 'input', 'aria-label': t('plaza.submission.filter.status') });
  for (const option of statusOptions()) {
    statusSelect.append(el('option', { value: option.value }, option.label));
  }
  statusSelect.value = listState.status || '';
  statusSelect.addEventListener('change', () => {
    listState.status = statusSelect.value || '';
    listState.page = 1;
    loadList();
  });

  const newIdInput = input(t('plaza.submission.create.placeholder'), {
    type: 'number', min: '1', inputmode: 'numeric', 'aria-label': t('plaza.submission.create.placeholder'),
  });
  const createCard = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.create.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.create.hint'))),
      icon('plus')),
    el('div', { class: 'plzw-inline-field' },
      newIdInput,
      button(t('plaza.submission.create.button'), event => busy(event.currentTarget, async () => {
        const questionId = Number(newIdInput.value || 0);
        if (!questionId) throw new Error(t('plaza.submission.create.needId'));
        const result = await api('/plaza/submissions', {
          method: 'POST',
          data: { question_id: questionId, source: {}, license_confirmed: false },
        });
        const createdId = result?.submission?.id;
        toast(t('plaza.submission.create.created'));
        if (createdId) go('submission', null, { id: createdId });
        else await renderList(shell, version, me);
      }), '', 'plus'),
      button(t('plaza.submission.create.goMyBank'), () => go('mybank'), 'secondary', 'tag')));

  shell.append(
    createCard,
    el('section', { class: 'pg-card plzw-card' },
      el('div', { class: 'pg-card-head' },
        el('div', {},
          el('h2', {}, t('plaza.submission.list.title')),
          el('p', { class: 'pg-card-sub' }, t('plaza.submission.list.hint'))),
        el('div', { class: 'pg-actions' },
          statusSelect,
          button(t('common.refresh'), event => busy(event.currentTarget, () => loadList()), 'secondary', 'repeat'))),
      listHost,
      pagerHost));
  await loadList();

  async function loadList() {
    if (!current(version)) return;
    listHost.replaceChildren(loading(t('plaza.submission.list.loading')));
    pagerHost.hidden = true;
    let data;
    try {
      data = await api(`/plaza/submissions/mine?${query({
        status: listState.status || '',
        page: listState.page,
        page_size: PAGE_SIZE,
      })}`);
    } catch (error) {
      if (!current(version)) return;
      listHost.replaceChildren(el('div', { class: 'error-panel' },
        error.message || t('plaza.submission.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:12px' },
          button(t('common.retry'), event => busy(event.currentTarget, () => loadList()), '', 'repeat'))));
      return;
    }
    if (!current(version)) return;
    listState = {
      ...listState,
      items: Array.isArray(data.items) ? data.items : [],
      total: Number(data.total || 0),
      page: Number(data.page || 1),
    };
    renderItems();
  }

  function renderItems() {
    listHost.replaceChildren();
    if (!listState.items.length) {
      listHost.append(empty(
        t('plaza.submission.list.emptyTitle'),
        t('plaza.submission.list.emptyText'),
        el('div', { class: 'row wrap' },
          button(t('plaza.submission.create.goMyBank'), () => go('mybank'), '', 'tag'),
          listState.status
            ? button(t('plaza.submission.filter.clear'), () => {
              listState.status = '';
              statusSelect.value = '';
              listState.page = 1;
              loadList();
            }, 'secondary', 'repeat')
            : null),
        'file'));
    }
    for (const item of listState.items) {
      const summary = item.review_summary;
      listHost.append(el('article', {
        class: 'plzw-item',
        'data-submission-id': String(item.id || ''),
      },
      el('div', { class: 'plzw-item-head' },
        el('strong', {}, `#${number(item.id)}`),
        statusBadge(item.status, item.status_label),
        badge(t('plaza.submission.item.version', { v1: number(item.current_version || item.version || 1) }), 'blue'),
        item.public_visible ? badge(t('plaza.submission.item.public'), 'teal') : null,
        item.deadline_passed ? badge(t('plaza.submission.item.deadlinePassed'), 'red') : null),
      el('div', { class: 'plzw-meta', 'data-no-i18n': '' },
        el('span', {}, item.subject || t('plaza.submission.emptyValue')),
        el('span', {}, item.type || ''),
        item.grade ? el('span', {}, item.grade) : null,
        el('span', {}, t('plaza.submission.item.updatedAt', { time: dateText(item.updated_at) }))),
      el('p', { class: 'plzw-stem', 'data-no-i18n': '' }, item.stem_preview || t('plaza.submission.item.emptyStem')),
      !summary?.hidden && summary?.thresholds
        ? el('p', { class: 'help' }, reviewSummaryText(summary))
        : null,
      item.review_deadline_at
        ? el('p', { class: 'help' },
          t('plaza.submission.item.deadline', { time: dateText(item.review_deadline_at) }))
        : null,
      el('div', { class: 'plzw-item-actions' },
        button(t('plaza.submission.item.open'), () => go('submission', null, { id: item.id }), 'secondary small', 'chevron'))));
    }
    if (listState.total > PAGE_SIZE) {
      const pages = Math.max(1, Math.ceil(listState.total / PAGE_SIZE));
      pagerHost.hidden = false;
      pagerHost.replaceChildren(
        el('span', {}, t('plaza.submission.list.pageOf', {
          v1: number(listState.total), v2: number(listState.page), v3: number(pages),
        })),
        el('div', { class: 'row' },
          button(t('common.prevPage'), () => {
            if (listState.page <= 1) return;
            listState.page -= 1;
            loadList();
          }, 'secondary small', 'chevron', { disabled: listState.page <= 1 }),
          button(t('common.nextPage'), () => {
            if (listState.page >= pages) return;
            listState.page += 1;
            loadList();
          }, 'secondary small', 'chevron', { disabled: listState.page >= pages })));
    } else {
      pagerHost.hidden = true;
    }
  }
}

/* ------------------------------------------------------------------------- */
/* 投稿详情                                                                   */
/* ------------------------------------------------------------------------- */

async function renderDetail(shell, version, id, me) {
  shell.replaceChildren(loading(t('plaza.submission.detail.loading')));
  let payload;
  let events = null;
  try {
    [payload, events] = await Promise.all([
      api(`/plaza/submissions/${encodeURIComponent(id)}`),
      api(`/plaza/submissions/${encodeURIComponent(id)}/events`).catch(() => null),
    ]);
  } catch (error) {
    if (!current(version)) return;
    shell.replaceChildren(el('div', { class: 'error-panel' },
      error.message || t('plaza.submission.loadFailed'),
      el('div', { class: 'row', style: 'margin-top:12px' },
        button(t('plaza.submission.detail.back'), () => go('submission'), 'secondary', 'chevron'),
        button(t('common.retry'), event => busy(event.currentTarget, () => renderDetail(shell, version, id, me)), '', 'repeat'))));
    return;
  }
  if (!current(version)) return;
  renderDetailView(shell, version, id, me, payload, events);
}

function renderDetailView(shell, version, id, me, payload, events) {
  const sub = payload?.submission || {};
  const checks = payload?.checks?.items || sub.checks || [];
  const blocking = payload?.checks?.blocking || checks.filter(check => check.blocking && !check.ok);
  shell.replaceChildren();

  const actionsRow = el('div', { class: 'plzw-actions-bar' },
    button(t('plaza.submission.detail.back'), () => go('submission'), 'secondary', 'chevron'),
    button(t('common.refresh'), event => busy(event.currentTarget, () => renderDetail(shell, version, id, me)), 'secondary', 'repeat'));

  const headCard = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.detail.title', { v1: number(sub.id || id) })),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.detail.subtitle'))),
      actionsRow),
    el('div', { class: 'plzw-meta' },
      statusBadge(sub.status, sub.status_label),
      badge(t('plaza.submission.item.version', { v1: number(sub.current_version || sub.version || 1) }), 'blue'),
      sub.question_id ? badge(t('plaza.submission.detail.fromQuestion', { v1: number(sub.question_id) }), '') : null,
      sub.public_visible ? badge(t('plaza.submission.item.public'), 'teal') : null,
      sub.deadline_passed ? badge(t('plaza.submission.item.deadlinePassed'), 'red') : null),
    el('p', { class: 'help' }, t('plaza.submission.detail.immutableHint')));
  shell.append(headCard);

  if (sub.public_id) {
    shell.append(notice(t('plaza.submission.detail.publicId', { v1: sub.public_id }), ''));
  }
  if (sub.review_deadline_at) {
    shell.append(el('p', { class: 'help' },
      t('plaza.submission.item.deadline', { time: dateText(sub.review_deadline_at) })));
  }

  const grid = el('div', { class: 'plzw-grid' });
  const left = el('div', { class: 'plzw-col' });
  const right = el('div', { class: 'plzw-col' });
  grid.append(left, right);
  shell.append(grid);

  left.append(snapshotCard(sub));
  left.append(checksCard(checks, blocking));
  left.append(reviewCard(sub));
  right.append(sourceCard(sub, version, id, shell, me, payload, events));
  right.append(actionsCard(sub, payload, version, id, shell, me, events));
  // 通知入口 + 作者申诉（rejected/withdrawn/disputed）；事件时间线保留在最后。
  right.append(notifyAppealCard(sub, payload, version, id, shell, me));
  right.append(eventsCard(events));
}

function snapshotCard(sub) {
  const snapshot = sub.snapshot || {};
  const content = snapshot.content || {};
  const card = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.snapshot.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.snapshot.hint'))),
      icon('book')));
  card.append(el('div', { class: 'plzw-meta', 'data-no-i18n': '' },
    badge(snapshot.subject || t('plaza.submission.emptyValue'), 'teal'),
    badge(snapshot.type || t('plaza.submission.emptyValue'), 'blue'),
    snapshot.grade ? badge(snapshot.grade, '') : null,
    snapshot.difficulty !== null && snapshot.difficulty !== undefined
      ? badge(t('plaza.submission.snapshot.difficulty', { v1: number(snapshot.difficulty) }), '')
      : null,
    snapshot.question_version
      ? badge(t('plaza.submission.snapshot.questionVersion', { v1: number(snapshot.question_version) }), '')
      : null));
  const tags = Array.isArray(snapshot.knowledge_tags) ? snapshot.knowledge_tags : [];
  if (tags.length) {
    card.append(el('div', { class: 'plzw-tags', 'data-no-i18n': '' }, tags.map(tag => badge(tag, ''))));
  }
  card.append(contentBlock(t('plaza.submission.snapshot.stem'), content.stem || ''));
  if (Array.isArray(content.options) && content.options.length) {
    const list = el('ul', { class: 'plzw-options', 'data-no-i18n': '' });
    for (const option of content.options) {
      list.append(el('li', {},
        el('span', { class: 'plzw-option-label' }, option.label || ''),
        option.text || ''));
    }
    card.append(el('div', { class: 'plzw-field' },
      el('div', { class: 'plzw-field-label' }, t('plaza.submission.snapshot.options')), list));
  }
  const answers = Array.isArray(content.answers) ? content.answers.filter(Boolean) : [];
  card.append(contentBlock(
    t('plaza.submission.snapshot.answers'),
    answers.length ? answers.join('；') : t('plaza.submission.snapshot.noAnswer'),
    !answers.length));
  card.append(contentBlock(t('plaza.submission.snapshot.explanation'), content.explanation || ''));
  const parts = Array.isArray(content.parts) ? content.parts : [];
  if (parts.length) {
    const partList = el('ol', { class: 'plzw-parts' });
    for (const part of parts) {
      const partAnswers = Array.isArray(part.answers) ? part.answers.filter(Boolean) : [];
      partList.append(el('li', { 'data-no-i18n': '' },
        el('div', { class: 'plzw-stem' }, `${part.index ? part.index + '. ' : ''}${part.stem || ''}`),
        partAnswers.length
          ? el('div', { class: 'help' }, t('plaza.submission.snapshot.answers') + '：' + partAnswers.join('；'))
          : null));
    }
    card.append(el('div', { class: 'plzw-field' },
      el('div', { class: 'plzw-field-label' }, t('plaza.submission.snapshot.parts')), partList));
  }
  const images = Array.isArray(content.images) ? content.images : [];
  if (images.length) {
    card.append(notice(t('plaza.submission.snapshot.imagesNotice', { v1: number(images.length) }), ''));
    card.append(figureGrid(sub.public_id, images));
  }
  return card;
}

/* 公开衍生图（WS14）：有 public_id 时按「公开题号 + 配图序号」读取；
 * 不可公开/尚未公开的配图显示原因占位，不伪造图片。 */
function figureGrid(publicId, images) {
  const grid = el('div', { class: 'plzw-figure-grid' });
  images.forEach((image, index) => {
    const label = String(image.asset || '');
    if (image.unavailable || !publicId || !label) {
      grid.append(el('span', { class: 'plzw-figure-fallback' },
        image.unavailable
          ? t('plaza.submission.snapshot.figureUnavailable')
          : t('plaza.submission.snapshot.figurePending')));
      return;
    }
    const src = `/api/plaza/questions/${encodeURIComponent(publicId)}/images/${index}`;
    const figure = el('figure', { class: 'plzw-figure' });
    const img = el('img', {
      src,
      alt: image.alt || t('plaza.submission.snapshot.figure'),
      loading: 'lazy',
      'data-no-i18n': '',
    });
    img.addEventListener('error', () => {
      figure.replaceChildren(el('span', { class: 'plzw-figure-fallback' },
        t('plaza.submission.snapshot.figureUnavailable')));
    });
    figure.append(img);
    grid.append(figure);
  });
  return grid;
}

function contentBlock(label, text, muted = false) {
  return el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, label),
    el('div', { class: `plzw-stem${muted ? ' is-muted' : ''}`, 'data-no-i18n': '' },
      String(text || '').trim() || t('plaza.submission.emptyValue')));
}

function checkText(check, kind) {
  const key = `plaza.submission.check.${check.key}.${kind}`;
  const translated = t(key);
  if (translated === key) return kind === 'label' ? (check.label || check.key || '') : '';
  return translated;
}

function checksCard(checks, blocking) {
  const card = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.checks.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.checks.hint'))),
      icon('shield')));
  card.append(notice(
    blocking.length
      ? t('plaza.submission.checks.blockingCount', { v1: number(blocking.length) })
      : t('plaza.submission.checks.noBlocking'),
    blocking.length ? 'warning' : ''));
  const rows = el('div', { class: 'plzw-checks' });
  for (const check of checks) {
    const ok = Boolean(check.ok);
    const variant = ok ? 'teal' : (check.blocking ? 'red' : 'amber');
    const badgeText = ok
      ? t('plaza.submission.checks.ok')
      : (check.blocking ? t('plaza.submission.checks.blocking') : t('plaza.submission.checks.warning'));
    rows.append(el('div', { class: 'plzw-check-row' },
      badge(badgeText, variant),
      el('div', { class: 'plzw-check-body' },
        el('div', { class: 'plzw-check-label' }, checkText(check, 'label') || check.label || check.key || ''),
        checkText(check, 'hint')
          ? el('p', { class: 'help', style: 'margin:4px 0 0' }, checkText(check, 'hint'))
          : null)));
  }
  card.append(rows);
  return card;
}

function reviewCard(sub) {
  const summary = sub.review_summary || {};
  const card = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.review.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.review.hint'))),
      icon('check')));
  if (summary.hidden) {
    card.append(notice(summary.notice || t('plaza.submission.review.hidden'), ''));
    return card;
  }
  const thresholds = summary.thresholds || {};
  const metrics = el('div', { class: 'plzw-metrics' },
    metric(t('plaza.submission.review.independent'), `${number(summary.independent_reviews || 0)} / ${number(thresholds.min_reviews || 5)}`),
    metric(t('plaza.submission.review.approves'), `${number(summary.approves || 0)} / ${number(thresholds.min_approves || 4)}`),
    metric(t('plaza.submission.review.subjectApproves'), `${number(summary.subject_reviewer_approves || 0)} / ${number(thresholds.min_subject_reviewer_approves || 2)}`),
    metric(t('plaza.submission.review.rejectBlocking'), number(summary.rejects || 0)));
  card.append(metrics);
  const tags = el('div', { class: 'plzw-meta' },
    summary.threshold_met ? badge(t('plaza.submission.review.thresholdMet'), 'teal') : badge(t('plaza.submission.review.thresholdPending'), 'amber'),
    summary.blocking ? badge(t('plaza.submission.review.blocking'), 'red') : null);
  card.append(tags);
  const reviews = Array.isArray(sub.reviews) ? sub.reviews : [];
  if (reviews.length) {
    const list = el('div', { class: 'plzw-list' });
    for (const review of reviews) {
      list.append(el('article', { class: 'plzw-item' },
        el('div', { class: 'plzw-item-head' },
          badge(verdictLabel(review.verdict), verdictVariant(review.verdict)),
          review.reviewer_role === 'subject_reviewer'
            ? badge(t('plaza.submission.review.subjectReviewer'), 'blue') : null,
          review.conflict_declared ? badge(t('plaza.submission.review.conflictDeclared'), 'amber') : null,
          el('span', { class: 'help' }, dateText(review.created_at))),
        review.reasons ? el('p', { class: 'help', style: 'margin:0' }, review.reasons) : null));
    }
    card.append(el('div', { class: 'plzw-field' },
      el('div', { class: 'plzw-field-label' }, t('plaza.submission.review.records')), list));
  } else {
    card.append(el('p', { class: 'help' }, t('plaza.submission.review.empty')));
  }
  return card;
}

function metric(label, value) {
  return el('div', { class: 'plzw-metric' },
    el('div', { class: 'plzw-metric-label' }, label),
    el('div', { class: 'plzw-metric-value' }, value));
}

function verdictLabel(verdict) {
  const key = `plaza.submission.verdict.${verdict}`;
  const translated = t(key);
  return translated === key ? String(verdict || '') : translated;
}

function verdictVariant(verdict) {
  return ({ approve: 'teal', revise: 'amber', reject: 'red', unsure: 'blue' })[verdict] || '';
}

function sourceCard(sub, version, id, shell, me, payload, events) {
  const source = sub.source || {};
  const card = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.source.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.source.hint'))),
      icon('shield')));
  const editable = Boolean(sub.can?.patch || sub.can?.revise);
  const inputs = {};
  const grid = el('div', { class: 'pg-grid-2' });
  for (const key of SOURCE_FIELDS) {
    const control = input('', {
      maxlength: key === 'url' ? '500' : key === 'page_no' || key === 'question_no' ? '40' : '300',
      'aria-label': t(`plaza.submission.source.${key}`),
      disabled: !editable,
    });
    control.value = source[key] || '';
    inputs[key] = control;
    grid.append(field(t(`plaza.submission.source.${key}`), control));
  }
  const noteControl = el('textarea', { class: 'input', rows: 2, disabled: !editable });
  noteControl.value = source.note || '';
  inputs.note = noteControl;
  card.append(grid, field(t('plaza.submission.source.note'), noteControl, t('plaza.submission.source.noteHint')));

  const licenseBox = el('input', { type: 'checkbox', checked: Boolean(sub.license_confirmed), disabled: !editable });
  card.append(el('label', { class: 'plzw-checkbox help' }, licenseBox,
    el('span', {}, t('plaza.submission.source.licenseConfirm'))));

  function collected() {
    const value = {};
    for (const key of SOURCE_FIELDS) value[key] = String(inputs[key].value || '').trim();
    value.note = String(noteControl.value || '').trim();
    return { source: value, license: Boolean(licenseBox.checked) };
  }

  function dirty() {
    const currentSource = sub.source || {};
    for (const key of [...SOURCE_FIELDS, 'note']) {
      if (String(currentSource[key] || '').trim() !== String(inputs[key].value || '').trim()) return true;
    }
    return Boolean(licenseBox.checked) !== Boolean(sub.license_confirmed);
  }

  async function persist() {
    const { source: sourceValue, license } = collected();
    if (sub.can?.patch) {
      return api(`/plaza/submissions/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        data: { version: sub.version, source: sourceValue, license_confirmed: license },
      });
    }
    if (sub.can?.revise) {
      return api(`/plaza/submissions/${encodeURIComponent(id)}/revise`, {
        method: 'POST',
        data: { version: sub.version, source: sourceValue, license_confirmed: license },
      });
    }
    throw new Error(t('plaza.submission.source.locked'));
  }

  if (editable) {
    card.append(el('div', { class: 'pg-actions' },
      button(t('plaza.submission.source.save'), event => busy(event.currentTarget, async () => {
        const result = await persist();
        toast(result.message || t('plaza.submission.source.saved'));
        await renderDetail(shell, version, id, me);
      }), '', 'check')));
  } else {
    card.append(notice(t('plaza.submission.source.locked'), 'warning'));
  }

  // 供「送审」动作在声明有改动时先落库（同一套校验口径）。
  card.__persist = persist;
  card.__dirty = dirty;
  card.__collected = collected;
  return card;
}

function actionsCard(sub, payload, version, id, shell, me, events) {
  const card = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.actions.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.actions.hint'))),
      icon('play')));
  const can = sub.can || {};
  const status = String(sub.status || '');
  const statusHint = t(`plaza.submission.actions.statusHint.${status}`);
  if (statusHint !== `plaza.submission.actions.statusHint.${status}`) {
    card.append(el('p', { class: 'help' }, statusHint));
  }

  if (can.submit) {
    const previewBox = el('input', { type: 'checkbox' });
    card.append(el('label', { class: 'plzw-checkbox' }, previewBox,
      el('span', {}, t('plaza.submission.actions.previewConfirm'))));
    if (Array.isArray(sub.blocking) && sub.blocking.length) {
      card.append(notice(t('plaza.submission.actions.blockingFirst', { v1: number(sub.blocking.length) }), 'warning'));
    }
    const submitBtn = button(t('plaza.submission.actions.submit'), event => busy(event.currentTarget, async () => {
      if (!previewBox.checked) throw new Error(t('plaza.submission.actions.confirmFirst'));
      // 声明有改动时先保存，避免送审使用旧声明（改版会创建新版本）。
      const sourceNode = [...shell.querySelectorAll('.pg-card')].find(node => node.__persist);
      let latest = sub;
      if (sourceNode?.__dirty?.()) {
        const result = await sourceNode.__persist();
        latest = result?.submission || latest;
      }
      await api(`/plaza/submissions/${encodeURIComponent(id)}/submit`, {
        method: 'POST',
        data: {
          version: Number(latest.current_version || latest.version || sub.version || 1),
          confirm_preview: true,
          license_confirmed: Boolean(sourceNode?.__collected?.().license ?? sub.license_confirmed),
        },
      });
      toast(t('plaza.submission.actions.submitted'));
      await renderDetail(shell, version, id, me);
    }), '', 'upload');
    card.append(el('div', { class: 'pg-actions' }, submitBtn));
  }

  if (can.revise && !can.submit) {
    card.append(el('p', { class: 'help' }, t('plaza.submission.actions.reviseHint')));
  }

  if (can.withdraw) {
    const reasonInput = input(t('plaza.submission.actions.withdrawReason'), {
      maxlength: '200',
    });
    card.append(field(t('plaza.submission.actions.withdrawReason'), reasonInput));
    card.append(el('div', { class: 'pg-actions' },
      button(t('plaza.submission.actions.withdraw'), event => busy(event.currentTarget, async () => {
        if (!window.confirm(t('plaza.submission.actions.withdrawConfirm'))) return;
        await api(`/plaza/submissions/${encodeURIComponent(id)}/withdraw`, {
          method: 'POST',
          data: { reason: String(reasonInput.value || '').trim() },
        });
        toast(t('plaza.submission.actions.withdrawn'));
        await renderDetail(shell, version, id, me);
      }), 'danger', 'close')));
  }

  if (!can.submit && !can.revise && !can.withdraw) {
    card.append(el('p', { class: 'help' }, t('plaza.submission.actions.readonly')));
  }
  // 避免未使用参数告警：payload/events 保留供后续扩展（审核回执、事件跳转）。
  void payload;
  void events;
  return card;
}

/* ------------------------------------------------------------------------- */
/* 通知与作者申诉（Phase 3 · WS21）                                           */
/* ------------------------------------------------------------------------- */

// 只有被退回/撤回/有争议的投稿允许作者申诉（与服务端 409 规则一致）。
const APPEALABLE_STATUSES = ['rejected', 'withdrawn', 'disputed'];

function appealStatusBadge(appeal) {
  const status = String(appeal?.status || '');
  const key = `plaza.appeal.status.${status}`;
  const label = t(key);
  return badge(label === key ? status : label,
    status === 'resolved' ? 'teal' : status === 'rejected' ? 'red' : 'amber');
}

/** 合并详情 payload 的 my_appeal/appeals 与列表接口结果，按 id 去重。 */
function mergeAppeals(...groups) {
  const seen = new Set();
  const merged = [];
  for (const group of groups) {
    for (const item of Array.isArray(group) ? group : []) {
      if (!item || typeof item !== 'object') continue;
      const key = String(item.id ?? `${item.created_at}-${item.reason}`);
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(item);
    }
  }
  return merged;
}

function appealHistoryNode(appeals) {
  const host = el('div', { class: 'plzw-list', 'data-appeal-history': '' });
  if (!appeals.length) {
    host.append(el('p', { class: 'help' }, t('plaza.appeal.empty')));
    return host;
  }
  for (const appeal of appeals) {
    host.append(el('article', { class: 'plzw-item', 'data-appeal-id': String(appeal.id || '') },
      el('div', { class: 'plzw-item-head' },
        appealStatusBadge(appeal),
        appeal.created_at ? el('span', { class: 'help' }, dateText(appeal.created_at)) : null),
      el('p', { class: 'help', 'data-no-i18n': '', style: 'margin:0' }, String(appeal.reason || '')),
      appeal.resolution
        ? el('p', { class: 'help', 'data-no-i18n': '', style: 'margin:4px 0 0' }, String(appeal.resolution))
        : null));
  }
  return host;
}

/**
 * 通知入口 + 申诉卡：
 * - 「通知中心」按钮复用顶栏铃铛的面板（openPlazaNotificationPanel）；
 * - rejected/withdrawn/disputed 显示申诉表单，POST /plaza/submissions/{id}/appeals {reason}；
 * - 已有待处理申诉时只展示状态，不重复提交（服务端幂等 200）；
 * - 详情 payload 没有申诉记录时探测 GET /appeals（未就绪静默降级，不崩页）。
 */
function notifyAppealCard(sub, payload, version, id, shell, me) {
  const status = String(sub.status || '');
  const appealable = APPEALABLE_STATUSES.includes(status);
  const canAppeal = appealable
    && Boolean(me?.capabilities?.appeals ?? me?.capabilities?.submit);
  const initialAppeals = mergeAppeals(
    sub.my_appeal ? [sub.my_appeal] : [],
    Array.isArray(sub.appeals) ? sub.appeals : [],
    Array.isArray(payload?.appeals) ? payload.appeals : [],
  );
  const historyHost = el('div');
  let appeals = initialAppeals;
  let pendingAppeal = appeals.some(appeal => String(appeal.status || '') === 'pending');

  const card = el('section', { class: 'pg-card plzw-card', 'data-notify-appeal': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.appeal.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.appeal.hint'))),
      icon('message')));

  // 通知中心入口（顶栏铃铛面板共用；接口未就绪时面板内给出空态/失败提示）。
  card.append(el('div', { class: 'row', style: 'margin-bottom:12px' },
    button(t('plaza.notify.openCenter'), () => openPlazaNotificationPanel(), 'secondary small', 'message'),
    el('span', { class: 'help' }, t('plaza.submission.notify.hint'))));

  const formHost = el('div', { 'data-appeal-form-host': '' });
  card.append(historyHost, formHost);

  function renderHistory() {
    historyHost.replaceChildren(appealHistoryNode(appeals));
  }

  function renderForm() {
    formHost.replaceChildren();
    if (!appealable) {
      formHost.append(notice(t('plaza.appeal.notAvailable', {
        v1: statusLabel(status, sub.status_label),
      }), ''));
      return;
    }
    if (pendingAppeal) {
      formHost.append(notice(t('plaza.appeal.pendingNotice'), 'warning'));
      return;
    }
    if (!canAppeal) {
      formHost.append(notice(t('plaza.appeal.noPermission'), 'warning'));
      return;
    }
    const reasonInput = el('textarea', {
      class: 'input',
      rows: 3,
      maxlength: '500',
      placeholder: t('plaza.appeal.reasonPlaceholder'),
    });
    formHost.append(
      field(t('plaza.appeal.reason'), reasonInput, t('plaza.appeal.reasonHint')),
      el('div', { class: 'pg-actions' },
        button(t('plaza.appeal.submit'), event => busy(event.currentTarget, async () => {
          const reason = String(reasonInput.value || '').trim();
          if (reason.length < 4) throw new Error(t('plaza.appeal.reasonRequired'));
          const result = await api(`/plaza/submissions/${encodeURIComponent(id)}/appeals`, {
            method: 'POST',
            data: { reason },
          });
          toast(result?.message || t('plaza.appeal.submitted'));
          await renderDetail(shell, version, id, me);
        }), '', 'message')));
  }

  renderHistory();
  renderForm();

  // 记录接口探测：详情 payload 已含申诉记录时跳过；未就绪静默保持现有展示。
  if (canAppeal || appeals.length) {
    api(`/plaza/submissions/${encodeURIComponent(id)}/appeals`)
      .then(data => {
        if (!current(version)) return;
        const items = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
        if (!items.length) return;
        appeals = mergeAppeals(appeals, items);
        pendingAppeal = appeals.some(appeal => String(appeal.status || '') === 'pending');
        renderHistory();
        renderForm();
      })
      .catch(() => { /* 申诉记录接口未就绪：保留详情 payload 已有信息 */ });
  }
  return card;
}

function eventsCard(events) {
  const items = Array.isArray(events?.items) ? events.items : [];
  const card = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.submission.events.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.submission.events.hint'))),
      icon('history')));
  if (!items.length) {
    card.append(el('p', { class: 'help' }, t('plaza.submission.events.empty')));
    return card;
  }
  const timeline = el('ol', { class: 'plzw-timeline' });
  for (const item of items) {
    const actionKey = `plaza.submission.events.action.${item.action}`;
    const actionLabel = t(actionKey);
    timeline.append(el('li', {},
      el('div', { class: 'plzw-meta' },
        el('strong', {}, actionLabel === actionKey ? String(item.action || '') : actionLabel),
        item.to_status ? statusBadge(item.to_status) : null,
        el('span', { class: 'help' }, dateText(item.created_at))),
      item.reason ? el('p', { class: 'help', style: 'margin:4px 0 0' }, item.reason) : null));
  }
  card.append(timeline);
  return card;
}

/* 供测试与调试：页面使用的状态机顺序（与后端一致）。 */
export const SUBMISSION_STATUS_ORDER = STATUS_ORDER;
export const SUBMISSION_SOURCE_FIELDS = SOURCE_FIELDS;
export const SUBMISSION_EVENT_ACTIONS = EVENT_ACTIONS;
