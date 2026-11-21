/* 评审台（广场评审员）：按学科领取「社区待核验」任务、独立作答/说明论证后提交结构化评审。
 *
 * 接口（前缀 /api/plaza，见 app/plaza/review.py、roles.py）：
 * - GET  /plaza/me                     能力探测（capabilities.review、roles 授权学科）
 * - GET  /plaza/review/queue?subject=  按学科队列（作者回避、已评过的版本不再出现）
 * - GET  /plaza/submissions/{id}       评审视角详情（提交前 review_summary 隐藏他人投票）
 * - POST /plaza/submissions/{id}/reviews {version, verdict, reasons, independent_answer, conflict_declared}
 * - GET  /plaza/review/mine            我的评审历史（WS14 新增，可能尚未就绪 → 本地兜底）
 *
 * 公平性约束：一人一版本一票；「通过」需要先独立作答或说明论证；「需修订/无法判断/驳回」
 * 必须填写简短原因；提交前不展示也不伪造他人投票与意见（服务端已隐藏）。
 */
import { api } from '../core/api.js';
import { current, go } from '../core/router.js';
import { onSessionReset } from '../core/state.js';
import {
  el, heading, button, input, field, badge, notice, empty, loading, busy,
  toast, number, dateText, query, icon, wireTabKeyboard,
} from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE_SIZE = 10;
const VERDICTS = ['approve', 'revise', 'unsure', 'reject'];
const VERDICT_VARIANTS = { approve: 'teal', revise: 'amber', reject: 'red', unsure: 'blue' };
const RECORDS_KEY = 'zhixu.plaza.reviewRecords';
const RECORD_LIMIT = 30;

let queue = { subject: '', page: 1, items: [], total: 0 };
let currentRun = 0;
let activeTab = 'queue';

onSessionReset(() => {
  queue = { subject: '', page: 1, items: [], total: 0 };
  currentRun = 0;
  activeTab = 'queue';
});

function verdictLabel(verdict) {
  const key = `plaza.review.verdict.${verdict}`;
  const translated = t(key);
  return translated === key ? String(verdict || '') : translated;
}

function verdictBadge(verdict) {
  return badge(verdictLabel(verdict), VERDICT_VARIANTS[verdict] || '');
}

function statusLabel(code, fallback = '') {
  if (!code) return fallback || '';
  const key = `plaza.submission.status.${code}`;
  const translated = t(key);
  return translated === key ? (fallback || code) : translated;
}

/* 本地评审记录：/plaza/review/mine 未就绪时的兜底，且按用户隔离避免串号。 */
function readLocalRecords(me) {
  try {
    const raw = window.sessionStorage.getItem(RECORDS_KEY);
    const list = JSON.parse(raw || '[]');
    if (!Array.isArray(list)) return [];
    const userId = Number(me?.user?.id || 0);
    return list.filter(item => item && Number(item.user_id || 0) === userId);
  } catch {
    return [];
  }
}

function pushLocalRecord(me, record) {
  try {
    const userId = Number(me?.user?.id || 0);
    if (!userId) return;
    const list = readLocalRecords(me);
    const next = [{ ...record, user_id: userId, saved_at: new Date().toISOString() }]
      .concat(list.filter(item => Number(item.submission_id) !== Number(record.submission_id)))
      .slice(0, RECORD_LIMIT);
    window.sessionStorage.setItem(RECORDS_KEY, JSON.stringify(next));
  } catch {
    /* sessionStorage 不可用时只影响「我的评审记录」缓存，不影响评审提交。 */
  }
}

function normalizeHistory(data) {
  const items = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
  return items.map(item => {
    const review = item.my_review || item.review || {};
    return {
      submission_id: Number(item.submission_id || item.id || item.submission?.id || 0),
      version: Number(item.version || review.version || 0) || null,
      verdict: item.verdict || review.verdict || '',
      reasons: item.reasons || review.reasons || '',
      independent_answer: item.independent_answer || review.independent_answer || '',
      created_at: item.created_at || item.reviewed_at || review.created_at || '',
      transition: item.transition || '',
      status: item.status || item.submission?.status || '',
      status_label: item.status_label || item.submission?.status_label || '',
      subject: item.subject || item.submission?.subject || '',
      stem_preview: item.stem_preview || item.submission?.stem_preview || '',
      source: 'server',
    };
  }).filter(item => item.submission_id);
}

/* ------------------------------------------------------------------------- */
/* 页面入口                                                                   */
/* ------------------------------------------------------------------------- */

export async function renderReview(container, version, params = null) {
  const run = ++currentRun;
  activeTab = params?.get?.('tab') === 'history' ? 'history' : 'queue';
  container.replaceChildren(
    heading('PLAZA · REVIEW', t('plaza.review.pageTitle'), t('plaza.review.pageSubtitle')),
    loading(t('plaza.review.loading')));

  let me;
  try {
    me = await api('/plaza/me');
  } catch (error) {
    if (run !== currentRun || !current(version)) return;
    container.replaceChildren(
      heading('PLAZA · REVIEW', t('plaza.review.pageTitle'), t('plaza.review.pageSubtitle')),
      el('div', { class: 'error-panel' },
        error.message || t('plaza.review.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:16px' },
          button(t('common.retry'), () => renderReview(container, version, params), '', 'repeat'))));
    return;
  }
  if (run !== currentRun || !current(version)) return;

  const shell = el('div', { class: 'plzw-page', 'data-page': 'plaza-review' });
  container.replaceChildren(
    heading('PLAZA · REVIEW', t('plaza.review.pageTitle'), t('plaza.review.pageSubtitle')),
    shell);

  if (!me.authenticated) {
    shell.append(notice(t('plaza.review.loginRequired'), 'warning'),
      el('div', { class: 'row' }, button(t('plaza.submission.goLogin'), () => go('home'), 'secondary', 'lock')));
    return;
  }
  if (!me.capabilities?.review) {
    shell.append(
      notice(t('plaza.review.noPermission'), 'warning'),
      el('section', { class: 'pg-card plzw-card' },
        el('h2', {}, t('plaza.review.noPermissionTitle')),
        el('p', { class: 'help' }, t('plaza.review.noPermissionHint'))));
    return;
  }

  const panelHost = el('div');
  const tabs = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': t('plaza.review.tabsLabel') });
  const tabDefs = [
    ['queue', t('plaza.review.tab.queue'), 'layers'],
    ['history', t('plaza.review.tab.history'), 'history'],
  ];
  const tabButtons = new Map();
  for (const [key, label, iconName] of tabDefs) {
    const tab = el('button', {
      type: 'button',
      class: 'pg-tab',
      role: 'tab',
      id: `plaza-review-tab-${key}`,
      'aria-selected': 'false',
      onClick: () => selectTab(key),
    }, icon(iconName), label);
    tabButtons.set(key, tab);
    tabs.append(tab);
  }
  wireTabKeyboard(tabs);
  shell.append(tabs, panelHost);

  function selectTab(key) {
    activeTab = key;
    for (const [name, tab] of tabButtons) tab.setAttribute('aria-selected', String(name === key));
    panelHost.replaceChildren();
    if (key === 'history') renderHistoryPanel(panelHost, version, me);
    else renderQueuePanel(panelHost, version, me);
  }
  selectTab(activeTab);
}

/* ------------------------------------------------------------------------- */
/* 评审队列                                                                   */
/* ------------------------------------------------------------------------- */

function renderQueuePanel(host, version, me) {
  host.replaceChildren();
  const subjects = [...new Set((me.roles || [])
    .filter(item => ['reviewer', 'subject_reviewer'].includes(String(item.role || '')))
    .map(item => String(item.subject || '').trim())
    .filter(Boolean))];

  let subjectControl;
  if (subjects.length) {
    subjectControl = el('select', { class: 'input', 'aria-label': t('plaza.review.queue.subject') });
    subjectControl.append(el('option', { value: '' }, t('plaza.review.queue.subjectAll')));
    for (const subject of subjects) subjectControl.append(el('option', { value: subject }, subject));
    subjectControl.value = subjects.includes(queue.subject) ? queue.subject : '';
  } else {
    // 管理员/编辑不受学科分派限制，可手填过滤。
    subjectControl = input(t('plaza.review.queue.subjectPlaceholder'), {
      value: queue.subject || '', maxlength: '40', 'aria-label': t('plaza.review.queue.subject'),
    });
  }
  subjectControl.addEventListener('change', () => {
    queue.subject = String(subjectControl.value || '').trim();
    queue.page = 1;
    loadQueue();
  });

  const controls = el('div', { class: 'pg-card-head', style: 'margin:0 0 12px' },
    el('div', {},
      el('h2', { style: 'margin:0' }, t('plaza.review.queue.title')),
      el('p', { class: 'pg-card-sub' }, t('plaza.review.queue.hint'))),
    el('div', { class: 'pg-actions' },
      subjectControl,
      button(t('common.refresh'), event => busy(event.currentTarget, () => loadQueue()), 'secondary', 'repeat')));
  const listHost = el('div', { class: 'plzw-list' });
  const pagerHost = el('div', { class: 'pagination', hidden: true });
  host.append(el('section', { class: 'pg-card plzw-card' }, controls, listHost, pagerHost));
  loadQueue();

  async function loadQueue() {
    if (!current(version)) return;
    listHost.replaceChildren(loading(t('plaza.review.queue.loading')));
    pagerHost.hidden = true;
    let data;
    try {
      data = await api(`/plaza/review/queue?${query({
        subject: queue.subject || '',
        page: queue.page,
        page_size: PAGE_SIZE,
      })}`);
    } catch (error) {
      if (!current(version)) return;
      listHost.replaceChildren(el('div', { class: 'error-panel' },
        error.message || t('plaza.review.queue.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:12px' },
          button(t('common.retry'), event => busy(event.currentTarget, () => loadQueue()), '', 'repeat'))));
      return;
    }
    if (!current(version)) return;
    queue.items = Array.isArray(data.items) ? data.items : [];
    queue.total = Number(data.total || 0);
    queue.page = Number(data.page || 1);
    renderQueueList();
  }

  function renderQueueList() {
    listHost.replaceChildren();
    if (!queue.items.length) {
      listHost.append(empty(
        t('plaza.review.queue.emptyTitle'),
        t('plaza.review.queue.emptyText'),
        null, 'layers'));
      return;
    }
    for (const item of queue.items) {
      listHost.append(el('article', {
        class: 'plzw-item',
        'data-submission-id': String(item.submission_id || ''),
      },
      el('div', { class: 'plzw-item-head' },
        el('strong', {}, `#${number(item.submission_id)}`),
        badge(t('plaza.review.queue.version', { v1: number(item.version || 1) }), 'blue'),
        badge(item.subject || t('plaza.submission.emptyValue'), 'teal'),
        item.type ? badge(item.type, '') : null,
        item.grade ? badge(item.grade, '') : null,
        item.deadline_passed ? badge(t('plaza.review.queue.deadlinePassed'), 'red') : null),
      el('p', { class: 'plzw-stem', 'data-no-i18n': '' }, item.stem_preview || ''),
      (Array.isArray(item.knowledge_tags) && item.knowledge_tags.length)
        ? el('div', { class: 'plzw-tags', 'data-no-i18n': '' }, item.knowledge_tags.map(tag => badge(tag, '')))
        : null,
      item.review_deadline_at
        ? el('p', { class: 'help' }, t('plaza.review.queue.deadline', { time: dateText(item.review_deadline_at) }))
        : null,
      el('p', { class: 'help' }, item.notice || t('plaza.review.queue.independentNotice')),
      el('div', { class: 'plzw-item-actions' },
        button(t('plaza.review.queue.review'), () => openReview(item.submission_id), '', 'edit'))));
    }
    if (queue.total > PAGE_SIZE) {
      const pages = Math.max(1, Math.ceil(queue.total / PAGE_SIZE));
      pagerHost.hidden = false;
      pagerHost.replaceChildren(
        el('span', {}, t('plaza.review.queue.pageOf', {
          v1: number(queue.total), v2: number(queue.page), v3: number(pages),
        })),
        el('div', { class: 'row' },
          button(t('common.prevPage'), () => {
            if (queue.page <= 1) return;
            queue.page -= 1;
            loadQueue();
          }, 'secondary small', 'chevron', { disabled: queue.page <= 1 }),
          button(t('common.nextPage'), () => {
            if (queue.page >= pages) return;
            queue.page += 1;
            loadQueue();
          }, 'secondary small', 'chevron', { disabled: queue.page >= pages })));
    }
  }

  async function openReview(submissionId) {
    if (!current(version)) return;
    listHost.replaceChildren();
    pagerHost.hidden = true;
    host.replaceChildren(loading(t('plaza.review.detail.loading')));
    await renderReviewDetail(host, version, me, submissionId, () => {
      host.replaceChildren();
      renderQueuePanel(host, version, me);
    });
  }
}

/* ------------------------------------------------------------------------- */
/* 评审详情 + 表单                                                            */
/* ------------------------------------------------------------------------- */

async function renderReviewDetail(host, version, me, submissionId, onBack) {
  let payload;
  try {
    payload = await api(`/plaza/submissions/${encodeURIComponent(submissionId)}`);
  } catch (error) {
    if (!current(version)) return;
    host.replaceChildren(el('section', { class: 'pg-card plzw-card' },
      el('div', { class: 'pg-card-head' },
        el('div', {}, el('h2', {}, t('plaza.review.detail.title', { v1: number(submissionId) }))),
        button(t('plaza.review.detail.back'), onBack, 'secondary', 'chevron')),
      el('div', { class: 'error-panel' }, error.message || t('plaza.review.detail.loadFailed'))));
    return;
  }
  if (!current(version)) return;
  const sub = payload.submission || {};
  const snapshot = sub.snapshot || {};
  const content = snapshot.content || {};
  const checks = sub.checks || payload.checks?.items || [];
  const blocking = checks.filter(check => check.blocking && !check.ok);
  host.replaceChildren();

  const head = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.review.detail.title', { v1: number(sub.id || submissionId) })),
        el('p', { class: 'pg-card-sub' }, t('plaza.review.detail.subtitle'))),
      el('div', { class: 'pg-actions' },
        button(t('plaza.review.detail.back'), onBack, 'secondary', 'chevron'),
        button(t('common.refresh'), event => busy(event.currentTarget, async () => {
          host.replaceChildren(loading(t('plaza.review.detail.loading')));
          await renderReviewDetail(host, version, me, submissionId, onBack);
        }), 'secondary', 'repeat'))),
    el('div', { class: 'plzw-meta' },
      badge(statusLabel(sub.status, sub.status_label), sub.status === 'community_review' ? 'amber' : ''),
      badge(t('plaza.review.detail.version', { v1: number(sub.current_version || sub.version || 1) }), 'blue'),
      badge(snapshot.subject || t('plaza.submission.emptyValue'), 'teal'),
      snapshot.type ? badge(snapshot.type, '') : null,
      snapshot.grade ? badge(snapshot.grade, '') : null,
      sub.deadline_passed ? badge(t('plaza.review.queue.deadlinePassed'), 'red') : null));
  host.append(head);

  if (sub.review_deadline_at) {
    host.append(el('p', { class: 'help' },
      t('plaza.review.queue.deadline', { time: dateText(sub.review_deadline_at) })));
  }

  // 待核验内容：题干/选项/答案/解析/来源/阻断项。评审员需要核验答案，因此完整展示。
  const contentCard = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.review.detail.contentTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.review.detail.contentHint'))),
      icon('book')));
  contentCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.review.detail.stem')),
    el('div', { class: 'plzw-stem', 'data-no-i18n': '' }, content.stem || t('plaza.submission.emptyValue'))));
  if (Array.isArray(content.options) && content.options.length) {
    const list = el('ul', { class: 'plzw-options', 'data-no-i18n': '' });
    for (const option of content.options) {
      list.append(el('li', {},
        el('span', { class: 'plzw-option-label' }, option.label || ''),
        option.text || ''));
    }
    contentCard.append(el('div', { class: 'plzw-field' },
      el('div', { class: 'plzw-field-label' }, t('plaza.review.detail.options')), list));
  }
  const answers = Array.isArray(content.answers) ? content.answers.filter(Boolean) : [];
  contentCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.review.detail.claimedAnswers')),
    el('div', { class: 'plzw-stem', 'data-no-i18n': '' },
      answers.length ? answers.join('；') : t('plaza.submission.emptyValue'))));
  contentCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.review.detail.explanation')),
    el('div', { class: 'plzw-stem', 'data-no-i18n': '' }, content.explanation || t('plaza.submission.emptyValue'))));
  const tags = Array.isArray(snapshot.knowledge_tags) ? snapshot.knowledge_tags : [];
  if (tags.length) {
    contentCard.append(el('div', { class: 'plzw-tags', 'data-no-i18n': '' }, tags.map(tag => badge(tag, ''))));
  }
  const images = Array.isArray(content.images) ? content.images : [];
  if (images.length) {
    contentCard.append(el('div', { class: 'plzw-field' },
      el('div', { class: 'plzw-field-label' }, t('plaza.review.detail.figures')),
      figureGrid(sub.public_id, images)));
  }
  contentCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.review.detail.source')),
    el('div', { class: 'plzw-source-lines', 'data-no-i18n': '' },
      Object.entries(sub.source || {})
        .filter(([key, value]) => key !== 'note' && key !== 'confirmed_by_user_id' && key !== 'confirmed_at' && value)
        .map(([key, value]) => el('div', {}, `${key}: ${value}`)))));
  if (blocking.length) {
    contentCard.append(notice(t('plaza.review.detail.blocking', {
      v1: blocking.map(check => check.label || check.key).join('；'),
    }), 'warning'));
  }
  host.append(contentCard);

  // 投票隐藏说明：提交前服务端不返回他人意见，前端也不伪造。
  const summary = sub.review_summary || {};
  host.append(el('section', { class: 'pg-card plzw-card' },
    el('h2', {}, t('plaza.review.detail.summaryTitle')),
    summary.hidden
      ? notice(summary.notice || t('plaza.review.detail.hidden'), '')
      : el('p', { class: 'help' }, t('plaza.review.detail.visibleAfterReview'))));

  if (sub.my_review) {
    host.append(myReviewCard(sub.my_review));
  } else if (sub.can?.review) {
    host.append(reviewForm(host, version, me, sub, () => {
      host.replaceChildren(loading(t('plaza.review.detail.loading')));
      renderReviewDetail(host, version, me, submissionId, onBack);
    }));
  } else {
    host.append(notice(t('plaza.review.detail.closed'), 'warning'));
  }
}

/* 公开衍生图（WS14）：评审员按「公开题号 + 配图序号」查看可公开配图；
 * 不可公开/未公开的配图显示占位原因，不伪造图片或存储路径。 */
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

function myReviewCard(review) {
  return el('section', { class: 'pg-card plzw-card', 'data-my-review': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.review.my.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.review.my.hint'))),
      verdictBadge(review.verdict)),
    review.independent_answer
      ? el('div', { class: 'plzw-field' },
        el('div', { class: 'plzw-field-label' }, t('plaza.review.form.independentAnswer')),
        el('div', { class: 'plzw-stem', 'data-no-i18n': '' }, review.independent_answer))
      : null,
    review.reasons
      ? el('div', { class: 'plzw-field' },
        el('div', { class: 'plzw-field-label' }, t('plaza.review.form.reasons')),
        el('div', { class: 'plzw-stem', 'data-no-i18n': '' }, review.reasons))
      : null,
    el('p', { class: 'help' },
      review.conflict_declared ? t('plaza.review.my.conflict') : t('plaza.review.my.independent')),
    el('p', { class: 'help' }, t('plaza.review.my.oneVote')));
}

function reviewForm(host, version, me, sub, onDone) {
  const form = el('section', { class: 'pg-card plzw-card', 'data-review-form': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.review.form.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.review.form.hint'))),
      icon('edit')));

  let verdict = '';
  const verdictOptions = el('div', { class: 'plzw-verdict-options', role: 'radiogroup', 'aria-label': t('plaza.review.form.verdict') });
  for (const value of VERDICTS) {
    const radio = el('input', { type: 'radio', name: 'plaza-review-verdict', value });
    radio.addEventListener('change', () => { if (radio.checked) verdict = value; });
    verdictOptions.append(el('label', { class: `plzw-verdict${value === 'reject' ? ' is-danger' : ''}` },
      radio, el('span', {}, verdictLabel(value))));
  }
  form.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.review.form.verdict')), verdictOptions));

  const independentInput = el('textarea', {
    class: 'input', rows: 3,
    placeholder: t('plaza.review.form.independentPlaceholder'),
  });
  form.append(field(t('plaza.review.form.independentAnswer'), independentInput,
    t('plaza.review.form.independentHint')));

  const reasonsInput = el('textarea', {
    class: 'input', rows: 3,
    placeholder: t('plaza.review.form.reasonsPlaceholder'),
  });
  form.append(field(t('plaza.review.form.reasons'), reasonsInput, t('plaza.review.form.reasonsHint')));

  const conflictBox = el('input', { type: 'checkbox' });
  form.append(el('label', { class: 'plzw-checkbox help' }, conflictBox,
    el('span', {}, t('plaza.review.form.conflict'))));

  const submitBtn = button(t('plaza.review.form.submit'), event => busy(event.currentTarget, async () => {
    if (!verdict) throw new Error(t('plaza.review.form.verdictRequired'));
    const independent = String(independentInput.value || '').trim();
    const reasons = String(reasonsInput.value || '').trim();
    if (verdict === 'approve' && independent.length < 2) {
      throw new Error(t('plaza.review.form.independentRequired'));
    }
    if (verdict !== 'approve' && reasons.length < 2) {
      throw new Error(t('plaza.review.form.reasonsRequired'));
    }
    const result = await api(`/plaza/submissions/${encodeURIComponent(sub.id)}/reviews`, {
      method: 'POST',
      data: {
        version: Number(sub.current_version || sub.version || 1),
        verdict,
        reasons,
        independent_answer: independent,
        conflict_declared: Boolean(conflictBox.checked),
      },
    });
    pushLocalRecord(me, {
      submission_id: Number(sub.id),
      version: Number(sub.current_version || sub.version || 1),
      verdict,
      reasons,
      independent_answer: independent,
      created_at: new Date().toISOString(),
      transition: result?.transition || '',
      status: result?.submission?.status || '',
      status_label: result?.submission?.status_label || '',
      subject: sub.subject || '',
      stem_preview: '',
    });
    const transition = String(result?.transition || 'pending');
    const transitionKey = `plaza.review.transition.${transition}`;
    const transitionText = t(transitionKey);
    toast(transitionText === transitionKey
      ? t('plaza.review.transition.pending')
      : transitionText);
    onDone();
  }), '', 'check');
  form.append(el('div', { class: 'pg-actions' }, submitBtn,
    el('span', { class: 'help' }, t('plaza.review.form.oneVoteHint'))));
  return form;
}

/* ------------------------------------------------------------------------- */
/* 我的评审记录                                                               */
/* ------------------------------------------------------------------------- */

function renderHistoryPanel(host, version, me) {
  host.replaceChildren();
  const listHost = el('div', { class: 'plzw-list' });
  const sourceHint = el('p', { class: 'help' });
  host.append(el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.review.history.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.review.history.hint'))),
      el('div', { class: 'pg-actions' },
        button(t('common.refresh'), event => busy(event.currentTarget, () => loadHistory()), 'secondary', 'repeat'))),
    sourceHint,
    listHost));
  loadHistory();

  async function loadHistory() {
    if (!current(version)) return;
    listHost.replaceChildren(loading(t('plaza.review.history.loading')));
    let records = [];
    let fromServer = false;
    try {
      const data = await api('/plaza/review/mine');
      records = normalizeHistory(data);
      fromServer = true;
    } catch {
      // 后端接口未就绪（404 等）时用本浏览器会话记录兜底，不报错崩页。
      records = readLocalRecords(me).map(item => ({ ...item, source: 'local' }));
    }
    if (!current(version)) return;
    sourceHint.textContent = fromServer
      ? t('plaza.review.history.fromServer')
      : t('plaza.review.history.fromLocal');
    if (!records.length) {
      listHost.replaceChildren(empty(
        t('plaza.review.history.emptyTitle'),
        t('plaza.review.history.emptyText'),
        null, 'history'));
      return;
    }
    listHost.replaceChildren();
    const enriched = await Promise.all(records.slice(0, 20).map(async record => {
      try {
        const detail = await api(`/plaza/submissions/${encodeURIComponent(record.submission_id)}`);
        const sub = detail.submission || {};
        return {
          ...record,
          status: record.status || sub.status || '',
          status_label: record.status_label || sub.status_label || '',
          subject: record.subject || sub.subject || '',
          version: record.version || sub.current_version || sub.version,
          my_review: record.verdict ? record : (sub.my_review || null),
          available: true,
        };
      } catch {
        return { ...record, available: false };
      }
    }));
    if (!current(version)) return;
    for (const record of enriched) {
      listHost.append(historyCard(record, version, me));
    }
  }

  function historyCard(record, versionRef, meRef) {
    return el('article', { class: 'plzw-item', 'data-submission-id': String(record.submission_id) },
      el('div', { class: 'plzw-item-head' },
        el('strong', {}, `#${number(record.submission_id)}`),
        record.verdict ? verdictBadge(record.verdict) : null,
        record.status ? badge(statusLabel(record.status, record.status_label), '') : null,
        record.version ? badge(t('plaza.review.queue.version', { v1: number(record.version) }), 'blue') : null,
        record.subject ? badge(record.subject, 'teal') : null,
        record.source === 'local' ? badge(t('plaza.review.history.localBadge'), 'amber') : null),
      record.reasons
        ? el('p', { class: 'help', style: 'margin:0', 'data-no-i18n': '' }, record.reasons)
        : null,
      el('div', { class: 'plzw-item-actions' },
        record.available === false
          ? el('span', { class: 'help' }, t('plaza.review.history.unavailable'))
          : button(t('plaza.review.history.open'), () => openFromHistory(record.submission_id), 'secondary small', 'chevron')));
  }

  function openFromHistory(submissionId) {
    if (!current(version)) return;
    // 用独立详情弹层承载历史查看，避免依赖队列 DOM 结构；仍走同一套隐藏/一人一票规则。
    openSubmissionPeek(submissionId);
  }

  function openSubmissionPeek(submissionId) {
    const dialog = el('dialog', { class: 'modal-dialog plzw-dialog', 'aria-label': t('plaza.review.history.open') });
    const body = el('div', { class: 'plzw-dialog-body' });
    dialog.append(
      el('div', { class: 'pg-card-head', style: 'margin:0' },
        el('div', {}, el('h3', { style: 'margin:0' }, t('plaza.review.detail.title', { v1: number(submissionId) }))),
        button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
      body);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
    body.append(loading(t('plaza.review.detail.loading')));
    renderReviewDetail(body, version, me, submissionId, () => dialog.close());
  }
}
