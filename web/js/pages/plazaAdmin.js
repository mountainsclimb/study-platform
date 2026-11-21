/* 编辑台（广场平台编辑/管理员）：初审队列、待发布签发、发布历史与撤回/回滚、抽检、
 * 纠错/举报/申诉队列、质量指标、身份审核与角色授权。
 *
 * 接口（前缀 /api/plaza，见 app/plaza/review.py、release.py、roles.py 与 WS21 质量运营模块）：
 * - GET  /plaza/me                             能力探测（capabilities.initial_review / publish /
 *                                              grant_roles / quality / ops / metrics）
 * - GET  /plaza/admin/submissions?status=     编辑台队列（WS14 新增；未就绪时降级为「输入投稿 ID」）。
 * - GET  /plaza/submissions/{id}              详情（快照/来源/校验/评审阈值/can.*）
 * - POST /plaza/submissions/{id}/initial-review
 *       {version, decision:accept|reject, reason, source_checked, privacy_checked, completeness_checked}
 * - POST /plaza/submissions/{id}/publish      {version, reason}
 * - GET  /plaza/releases?status=&page=        发布历史
 * - POST /plaza/releases/{public_id}/withdraw {reason, mode:normal|emergency}
 * - POST /plaza/submissions/{id}/rollback     {reason}（回滚到上一已发布版本；无历史 409）
 * - GET  /plaza/admin/quality/queue?status=   抽检队列（pending/passed/issues/all，含 sampling 覆盖）
 * - POST /plaza/admin/quality/checks          {submission_id?|public_id?, decision:pass|issue,
 *                                              severity:low|medium|high, findings:[], note}
 *                                              high 问题由服务端自动紧急撤回（mode=emergency）
 * - GET  /plaza/admin/issues|reports|appeals?status=  纠错/举报/申诉队列（含 counts）
 * - PATCH /plaza/admin/issues|reports|appeals/{id} {action:resolve|reject, reason}
 * - GET  /plaza/admin/metrics                 质量指标（学科/题型、可判分率、图可用率、评审时长、问题率）
 * - GET  /plaza/admin/identity/queue          身份审核队列（WS14 新增，可能尚未就绪）
 * - POST /plaza/admin/identity/{id}/review    身份审核（WS14 新增，可能尚未就绪）
 * - POST /plaza/admin/roles                   {action:grant|revoke, role, subject, username?, user_id?, expires_at?}
 *
 * 能力探测原则：所有 WS14/WS21 新增队列接口都 try/catch，404/403 时展示「后端未就绪」占位与
 * 手工投稿 ID 入口，页面不崩；已存在的初审/签发/撤回 POST 接口在拿到投稿 ID 后直接可用。
 * 回避提示：作者不能初审/签发本人投稿（服务端强制），页面只做提示。
 */
import { api } from '../core/api.js';
import { current, go } from '../core/router.js';
import { onSessionReset } from '../core/state.js';
import {
  el, heading, button, input, field, select, badge, notice, empty, loading, busy,
  toast, number, dateText, query, icon, wireTabKeyboard,
} from '../core/dom.js';
import { t } from '../core/i18n.js';
import { plazaTypeLabel } from '../components/plazaCard.js';

const PAGE_SIZE = 20;
const EDITOR_IDS_KEY = 'zhixu.plaza.editorIds';
const EDITOR_ID_LIMIT = 12;
const SOURCE_FIELDS = [
  'url', 'original_author', 'license_basis', 'rights_scope',
  'attribution', 'reference', 'page_no', 'question_no', 'note',
];
const STATUS_KEYS = [
  'draft', 'pending_initial', 'community_review', 'verified',
  'disputed', 'pending_release', 'published', 'withdrawn', 'rejected',
];

let currentRun = 0;
let activeTab = 'initial';

onSessionReset(() => {
  currentRun = 0;
  activeTab = 'initial';
});

function statusLabel(code, fallback = '') {
  if (!code) return fallback || '';
  const key = `plaza.admin.status.${code}`;
  const translated = t(key);
  return translated === key ? (fallback || code) : translated;
}

function statusVariants(code) {
  return ({
    draft: '', pending_initial: 'blue', community_review: 'amber', verified: 'teal',
    disputed: 'red', pending_release: 'amber', published: 'teal', withdrawn: '', rejected: 'red',
  })[code] || '';
}

function statusBadge(code, fallback = '') {
  return badge(statusLabel(code, fallback), statusVariants(code));
}

function verdictLabel(verdict) {
  const key = `plaza.submission.verdict.${verdict}`;
  const translated = t(key);
  return translated === key ? String(verdict || '') : translated;
}

function identityKindLabel(kind) {
  const key = `plaza.admin.identity.kind.${kind}`;
  const translated = t(key);
  return translated === key ? String(kind || '') : translated;
}

function identityStatusLabel(status) {
  const key = `plaza.admin.identity.status.${status}`;
  const translated = t(key);
  return translated === key ? String(status || '') : translated;
}

/* ------------------------------------------------------------------------- */
/* 最近处理的投稿（后端队列接口未就绪时的入口）                               */
/* ------------------------------------------------------------------------- */

function readRecentIds() {
  try {
    const list = JSON.parse(window.sessionStorage.getItem(EDITOR_IDS_KEY) || '[]');
    if (!Array.isArray(list)) return [];
    return list.filter(item => item && Number(item.id || 0) > 0);
  } catch {
    return [];
  }
}

function rememberId(id, mode) {
  try {
    const list = readRecentIds().filter(item => Number(item.id) !== Number(id));
    list.unshift({ id: Number(id), mode: String(mode || 'initial'), at: new Date().toISOString() });
    window.sessionStorage.setItem(EDITOR_IDS_KEY, JSON.stringify(list.slice(0, EDITOR_ID_LIMIT)));
  } catch {
    /* 仅在无法记录最近入口时降级，不影响主流程。 */
  }
}

/* ------------------------------------------------------------------------- */
/* 页面入口                                                                   */
/* ------------------------------------------------------------------------- */

export async function renderPlazaAdmin(container, version, params = null) {
  const run = ++currentRun;
  // 深链支持 ?tab= 与通知 link 的 ?section=（WS21 契约：#/plaza-admin?section=ops）。
  const requestedTab = String(params?.get?.('tab') || params?.get?.('section') || '');
  const deepLinkId = Number(params?.get?.('id') || 0) || 0;
  container.replaceChildren(
    heading('PLAZA · EDITOR', t('plaza.admin.pageTitle'), t('plaza.admin.pageSubtitle')),
    loading(t('plaza.admin.loading')));

  let me;
  try {
    me = await api('/plaza/me');
  } catch (error) {
    if (run !== currentRun || !current(version)) return;
    container.replaceChildren(
      heading('PLAZA · EDITOR', t('plaza.admin.pageTitle'), t('plaza.admin.pageSubtitle')),
      el('div', { class: 'error-panel' },
        error.message || t('plaza.admin.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:16px' },
          button(t('common.retry'), () => renderPlazaAdmin(container, version, params), '', 'repeat'))));
    return;
  }
  if (run !== currentRun || !current(version)) return;

  const shell = el('div', { class: 'plzw-page', 'data-page': 'plaza-admin' });
  container.replaceChildren(
    heading('PLAZA · EDITOR', t('plaza.admin.pageTitle'), t('plaza.admin.pageSubtitle')),
    shell);

  if (!me.authenticated) {
    shell.append(notice(t('plaza.admin.loginRequired'), 'warning'),
      el('div', { class: 'row' }, button(t('plaza.submission.goLogin'), () => go('home'), 'secondary', 'lock')));
    return;
  }
  const canInitial = Boolean(me.capabilities?.initial_review);
  const canPublish = Boolean(me.capabilities?.publish);
  const canGrant = Boolean(me.capabilities?.grant_roles);
  // WS21 质量运营能力：编辑/管理员（旧实例没有这些字段时按编辑台既有能力降级探测）。
  const canQuality = Boolean(me.capabilities?.quality || me.capabilities?.editor || canPublish || canGrant);
  const canOps = Boolean(me.capabilities?.ops || me.capabilities?.editor || canPublish || canGrant);
  const canMetrics = Boolean(me.capabilities?.metrics || me.capabilities?.editor || canPublish || canGrant);
  if (!canInitial && !canPublish && !canGrant) {
    shell.append(
      notice(t('plaza.admin.noPermission'), 'warning'),
      el('section', { class: 'pg-card plzw-card' },
        el('h2', {}, t('plaza.admin.noPermissionTitle')),
        el('p', { class: 'help' }, t('plaza.admin.noPermissionHint'))));
    return;
  }

  const tabs = [];
  if (canInitial) tabs.push(['initial', t('plaza.admin.tab.initial'), 'shield']);
  if (canPublish) tabs.push(['release', t('plaza.admin.tab.release'), 'upload']);
  if (canPublish) tabs.push(['history', t('plaza.admin.tab.history'), 'history']);
  if (canQuality) tabs.push(['quality', t('plaza.admin.tab.quality'), 'shield']);
  if (canOps) tabs.push(['ops', t('plaza.admin.tab.ops'), 'message']);
  if (canMetrics) tabs.push(['metrics', t('plaza.admin.tab.metrics'), 'chart']);
  if (canInitial || canPublish || canGrant) tabs.push(['identity', t('plaza.admin.tab.identity'), 'lock']);

  // 深链支持 ?tab= 与通知 link 的 ?section=（WS21 契约：编辑端通知链接 #/plaza-admin?section=ops）。
  activeTab = tabs.some(([key]) => key === requestedTab) ? requestedTab : tabs[0][0];
  const tabBar = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': t('plaza.admin.tabsLabel') });
  const tabButtons = new Map();
  for (const [key, label, iconName] of tabs) {
    const tab = el('button', {
      type: 'button',
      class: 'pg-tab',
      role: 'tab',
      id: `plaza-admin-tab-${key}`,
      'aria-selected': 'false',
      onClick: () => selectTab(key),
    }, icon(iconName), label);
    tabButtons.set(key, tab);
    tabBar.append(tab);
  }
  wireTabKeyboard(tabBar);
  const panelHost = el('div', { class: 'plzw-admin-panel' });
  shell.append(tabBar, panelHost);

  function renderSection(key) {
    panelHost.replaceChildren();
    if (key === 'initial') {
      renderQueueSection(panelHost, version, me, 'initial');
    } else if (key === 'release') {
      renderQueueSection(panelHost, version, me, 'release');
    } else if (key === 'history') {
      renderHistorySection(panelHost, version, me);
    } else if (key === 'quality') {
      renderQualitySection(panelHost, version);
    } else if (key === 'ops') {
      renderOpsSection(panelHost, version);
    } else if (key === 'metrics') {
      renderMetricsSection(panelHost, version);
    } else {
      renderIdentitySection(panelHost, version, me, canGrant);
    }
  }

  function selectTab(key) {
    activeTab = key;
    for (const [name, tab] of tabButtons) tab.setAttribute('aria-selected', String(name === key));
    renderSection(key);
  }
  selectTab(activeTab);
  if (deepLinkId) {
    // 深链 #/plaza-admin?id=<投稿ID>：直接在对应队列分区打开该投稿。
    openSubmission(panelHost, version, me, deepLinkId, {
      mode: activeTab === 'release' ? 'release' : 'initial',
      onBack: () => selectTab(activeTab),
    });
  }
}

/* ------------------------------------------------------------------------- */
/* 初审 / 待发布队列（能力探测 + 手工 ID 降级）                               */
/* ------------------------------------------------------------------------- */

function renderQueueSection(host, version, me, mode) {
  const statuses = mode === 'initial' ? ['pending_initial'] : ['verified', 'pending_release'];
  const title = mode === 'initial' ? t('plaza.admin.initial.title') : t('plaza.admin.release.title');
  const hint = mode === 'initial' ? t('plaza.admin.initial.hint') : t('plaza.admin.release.hint');

  const listHost = el('div', { class: 'plzw-list' });
  const probeHost = el('div');
  const manualCard = manualIdCard(host, version, me, mode);

  host.append(el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {}, el('h2', {}, title), el('p', { class: 'pg-card-sub' }, hint)),
      el('div', { class: 'pg-actions' },
        button(t('common.refresh'), event => busy(event.currentTarget, () => loadQueue()), 'secondary', 'repeat'))),
    probeHost,
    listHost));

  loadQueue();

  async function loadQueue() {
    if (!current(version)) return;
    probeHost.replaceChildren();
    listHost.replaceChildren(loading(t('plaza.admin.queue.loading')));
    let items = null;
    let lastError = null;
    for (const status of statuses) {
      try {
        const data = await api(`/plaza/admin/submissions?${query({
          status,
          page: 1,
          page_size: PAGE_SIZE,
        })}`);
        const pageItems = Array.isArray(data.items) ? data.items : [];
        items = (items || []).concat(pageItems);
      } catch (error) {
        lastError = error;
      }
    }
    if (!current(version)) return;
    if (items === null) {
      // 队列接口未就绪（404/403 等）：展示占位 + 手工投稿 ID 入口，不报错崩页。
      listHost.replaceChildren();
      probeHost.append(notReadyCard(me, mode));
      probeHost.append(manualCard);
      const recent = recentIdsCard(host, version, me, mode);
      if (recent) listHost.append(recent);
      void lastError;
      return;
    }
    probeHost.append(manualCard);
    renderQueueItems(items, mode);
  }

  function renderQueueItems(items, modeValue) {
    listHost.replaceChildren();
    if (!items.length) {
      listHost.append(empty(
        t('plaza.admin.queue.emptyTitle'),
        t('plaza.admin.queue.emptyText'),
        null, 'layers'));
      return;
    }
    for (const item of items) {
      const id = Number(item.id || item.submission_id || 0);
      if (!id) continue;
      const summary = item.review || item.review_summary || null;
      listHost.append(el('article', {
        class: 'plzw-item',
        'data-submission-id': String(id),
        'data-status': String(item.status || ''),
      },
      el('div', { class: 'plzw-item-head' },
        el('strong', {}, `#${number(id)}`),
        statusBadge(item.status, item.status_label),
        badge(t('plaza.admin.queue.version', { v1: number(item.current_version || item.version || 1) }), 'blue'),
        item.subject ? badge(item.subject, 'teal') : null,
        item.type ? badge(item.type, '') : null,
        item.deadline_passed ? badge(t('plaza.admin.queue.deadlinePassed'), 'red') : null,
        Number(item.blocking_count || 0) > 0
          ? badge(t('plaza.admin.queue.blockingCount', { v1: number(item.blocking_count) }), 'amber')
          : null),
      el('p', { class: 'plzw-stem', 'data-no-i18n': '' }, item.stem_preview || t('plaza.submission.item.emptyStem')),
      el('div', { class: 'plzw-meta' },
        item.author_display
          ? el('span', {}, t('plaza.admin.queue.author', { v1: item.author_display }))
          : null,
        item.submitted_at
          ? el('span', {}, t('plaza.admin.queue.submittedAt', { time: dateText(item.submitted_at) }))
          : null,
        summary && !summary.hidden && summary.thresholds
          ? el('span', {}, t('plaza.admin.queue.threshold', {
            v1: number(summary.approves || 0),
            v2: number(summary.thresholds.min_approves || 4),
            v3: number(summary.subject_reviewer_approves || 0),
            v4: number(summary.thresholds.min_subject_reviewer_approves || 2),
          }))
          : null),
      el('div', { class: 'plzw-item-actions' },
        button(modeValue === 'initial' ? t('plaza.admin.queue.openInitial') : t('plaza.admin.queue.openPublish'),
          () => openSubmission(host, version, me, id, {
            mode: modeValue,
            onBack: () => renderQueueSection(host, version, me, modeValue),
          }), 'secondary small', 'chevron'))));
    }
  }
}

function notReadyCard(me, mode) {
  return el('div', { class: 'plzw-not-ready', 'data-queue-not-ready': 'true' },
    notice(t('plaza.admin.queue.notReadyTitle'), 'warning'),
    el('p', { class: 'help' }, t('plaza.admin.queue.notReadyText')),
    el('p', { class: 'help' }, t('plaza.admin.queue.notReadyHint', {
      v1: mode === 'initial' ? '/plaza/admin/submissions?status=pending_initial' : '/plaza/admin/submissions?status=verified',
      v2: me?.user?.username || '',
    })));
}

function manualIdCard(host, version, me, mode) {
  const idInput = input(t('plaza.admin.queue.manualPlaceholder'), {
    type: 'number', min: '1', inputmode: 'numeric',
    'aria-label': t('plaza.admin.queue.manualLabel'),
  });
  return el('section', { class: 'pg-card plzw-card', 'data-manual-id': 'true' },
    el('div', { class: 'pg-card-head', style: 'margin-bottom:8px' },
      el('div', {},
        el('h3', { style: 'margin:0' }, t('plaza.admin.queue.manualTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.queue.manualHint')))),
    el('div', { class: 'plzw-inline-field' },
      idInput,
      button(t('plaza.admin.queue.manualLoad'), event => busy(event.currentTarget, async () => {
        const id = Number(idInput.value || 0);
        if (!id) throw new Error(t('plaza.admin.queue.manualNeedId'));
        await openSubmission(host, version, me, id, {
          mode,
          onBack: () => renderQueueSection(host, version, me, mode),
        });
      }), '', 'search')));
}

function recentIdsCard(host, version, me, mode) {
  const items = readRecentIds();
  if (!items.length) return null;
  const card = el('section', { class: 'pg-card plzw-card' });
  card.append(el('h3', { style: 'margin:0 0 8px' }, t('plaza.admin.queue.recentTitle')));
  const row = el('div', { class: 'plzw-tags' });
  for (const item of items) {
    row.append(button(`#${number(item.id)}`, () => openSubmission(host, version, me, item.id, {
      mode: item.mode || mode,
      onBack: () => renderQueueSection(host, version, me, mode),
    }), 'soft small'));
  }
  card.append(row);
  return card;
}

/* ------------------------------------------------------------------------- */
/* 投稿详情 + 初审 / 签发                                                     */
/* ------------------------------------------------------------------------- */

async function openSubmission(host, version, me, submissionId, options = {}) {
  if (!current(version)) return;
  const mode = options.mode === 'release' ? 'release' : 'initial';
  host.replaceChildren(loading(t('plaza.admin.detail.loading')));
  let payload;
  try {
    payload = await api(`/plaza/submissions/${encodeURIComponent(submissionId)}`);
  } catch (error) {
    if (!current(version)) return;
    host.replaceChildren(el('section', { class: 'pg-card plzw-card' },
      el('div', { class: 'pg-card-head' },
        el('div', {}, el('h2', {}, t('plaza.admin.detail.title', { v1: number(submissionId) }))),
        button(t('common.back'), options.onBack || (() => go('plaza-admin')), 'secondary', 'chevron')),
      el('div', { class: 'error-panel' }, error.message || t('plaza.admin.detail.loadFailed'))));
    return;
  }
  if (!current(version)) return;
  rememberId(submissionId, mode);
  renderSubmissionDetail(host, version, me, payload, mode, options);
}

function renderSubmissionDetail(host, version, me, payload, mode, options) {
  const sub = payload?.submission || {};
  const checks = payload?.checks?.items || sub.checks || [];
  const blocking = payload?.checks?.blocking || checks.filter(check => check.blocking && !check.ok);
  const summary = sub.review_summary || {};
  host.replaceChildren();

  const head = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.detail.title', { v1: number(sub.id || 0) })),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.detail.subtitle'))),
      el('div', { class: 'pg-actions' },
        button(t('common.back'), options.onBack || (() => go('plaza-admin')), 'secondary', 'chevron'),
        button(t('common.refresh'), event => busy(event.currentTarget, async () => {
          await openSubmission(host, version, me, sub.id, options);
        }), 'secondary', 'repeat'))),
    el('div', { class: 'plzw-meta' },
      statusBadge(sub.status, sub.status_label),
      badge(t('plaza.admin.queue.version', { v1: number(sub.current_version || sub.version || 1) }), 'blue'),
      sub.subject ? badge(sub.subject, 'teal') : null,
      sub.type ? badge(sub.type, '') : null,
      sub.grade ? badge(sub.grade, '') : null,
      sub.public_visible && sub.public_id ? badge(sub.public_id, 'teal') : null,
      sub.deadline_passed ? badge(t('plaza.admin.queue.deadlinePassed'), 'red') : null));
  if (sub.viewer?.is_author) {
    head.append(notice(t('plaza.admin.detail.selfNotice'), 'warning'));
  }
  host.append(head);

  // 内容与来源（只读快照）
  const snapshot = sub.snapshot || {};
  const content = snapshot.content || {};
  const infoCard = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.detail.contentTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.detail.contentHint'))),
      icon('book')));
  infoCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.admin.detail.stem')),
    el('div', { class: 'plzw-stem', 'data-no-i18n': '' }, content.stem || t('plaza.submission.emptyValue'))));
  const answers = Array.isArray(content.answers) ? content.answers.filter(Boolean) : [];
  infoCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.admin.detail.answers')),
    el('div', { class: 'plzw-stem', 'data-no-i18n': '' },
      answers.length ? answers.join('；') : t('plaza.submission.emptyValue'))));
  const images = Array.isArray(content.images) ? content.images : [];
  if (images.length) {
    infoCard.append(el('div', { class: 'plzw-field' },
      el('div', { class: 'plzw-field-label' }, t('plaza.admin.detail.figures')),
      figureGrid(sub.public_id, images)));
  }
  infoCard.append(el('div', { class: 'plzw-field' },
    el('div', { class: 'plzw-field-label' }, t('plaza.admin.detail.source')),
    el('div', { class: 'plzw-source-lines', 'data-no-i18n': '' },
      SOURCE_FIELDS.map(key => (sub.source?.[key]
        ? el('div', {}, `${key}: ${sub.source[key]}`)
        : null)))));
  if (Array.isArray(sub.blocking) && sub.blocking.length) {
    infoCard.append(notice(t('plaza.admin.detail.blocking', { v1: sub.blocking.join('；') }), 'warning'));
  } else if (blocking.length) {
    infoCard.append(notice(t('plaza.admin.detail.blocking', {
      v1: blocking.map(check => check.label || check.key).join('；'),
    }), 'warning'));
  }
  host.append(infoCard);

  // 评审阈值与意见
  const reviewCard = el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.detail.reviewTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.detail.reviewHint'))),
      icon('check')));
  if (summary.hidden) {
    reviewCard.append(notice(summary.notice || t('plaza.admin.detail.reviewHidden'), ''));
  } else {
    const thresholds = summary.thresholds || {};
    reviewCard.append(el('div', { class: 'plzw-metrics' },
      metric(t('plaza.admin.detail.independent'), `${number(summary.independent_reviews || 0)} / ${number(thresholds.min_reviews || 5)}`),
      metric(t('plaza.admin.detail.approves'), `${number(summary.approves || 0)} / ${number(thresholds.min_approves || 4)}`),
      metric(t('plaza.admin.detail.subjectApproves'), `${number(summary.subject_reviewer_approves || 0)} / ${number(thresholds.min_subject_reviewer_approves || 2)}`),
      metric(t('plaza.admin.detail.rejects'), number(summary.rejects || 0))));
    reviewCard.append(el('div', { class: 'plzw-meta' },
      summary.threshold_met
        ? badge(t('plaza.admin.detail.thresholdMet'), 'teal')
        : badge(t('plaza.admin.detail.thresholdPending'), 'amber'),
      summary.blocking ? badge(t('plaza.admin.detail.blockingBadge'), 'red') : null,
      summary.conflict_declared
        ? badge(t('plaza.admin.detail.conflicts', { v1: number(summary.conflict_declared) }), 'amber')
        : null));
    const reviews = Array.isArray(sub.reviews) ? sub.reviews : [];
    if (reviews.length) {
      const list = el('div', { class: 'plzw-list' });
      for (const review of reviews) {
        list.append(el('article', { class: 'plzw-item' },
          el('div', { class: 'plzw-item-head' },
            badge(verdictLabel(review.verdict), ({ approve: 'teal', revise: 'amber', reject: 'red', unsure: 'blue' })[review.verdict] || ''),
            review.reviewer_role === 'subject_reviewer'
              ? badge(t('plaza.admin.detail.subjectReviewer'), 'blue') : null,
            review.conflict_declared ? badge(t('plaza.admin.detail.conflictDeclared'), 'amber') : null,
            el('span', { class: 'help' }, dateText(review.created_at))),
          review.reasons ? el('p', { class: 'help', style: 'margin:0' }, review.reasons) : null));
      }
      reviewCard.append(el('div', { class: 'plzw-field' },
        el('div', { class: 'plzw-field-label' }, t('plaza.admin.detail.reviews')), list));
    }
  }
  host.append(reviewCard);

  if (mode === 'initial') host.append(initialReviewCard(host, version, me, payload, options));
  else host.append(publishCard(host, version, me, payload, options));

  // 事件链（作者看节点；编辑看完整审计字段）
  host.append(eventsCard(sub.id));
}

function metric(label, value) {
  return el('div', { class: 'plzw-metric' },
    el('div', { class: 'plzw-metric-label' }, label),
    el('div', { class: 'plzw-metric-value' }, value));
}

/* 公开衍生图（WS14）：编辑按「公开题号 + 配图序号」核对可公开配图；不可公开时给出占位。 */
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

function initialReviewCard(host, version, me, payload, options = {}) {
  const sub = payload?.submission || {};
  const can = Boolean(sub.can?.initial_review);
  const card = el('section', { class: 'pg-card plzw-card', 'data-initial-review': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.initial.formTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.initial.formHint'))),
      icon('shield')));

  if (sub.viewer?.is_author) {
    card.append(notice(t('plaza.admin.detail.selfNotice'), 'warning'));
    return card;
  }
  if (!can) {
    card.append(notice(t('plaza.admin.initial.notAvailable', { v1: statusLabel(sub.status, sub.status_label) }), 'warning'));
    return card;
  }

  const sourceBox = el('input', { type: 'checkbox' });
  const privacyBox = el('input', { type: 'checkbox' });
  const completenessBox = el('input', { type: 'checkbox' });
  card.append(
    el('label', { class: 'plzw-checkbox' }, sourceBox, el('span', {}, t('plaza.admin.initial.sourceChecked'))),
    el('label', { class: 'plzw-checkbox' }, privacyBox, el('span', {}, t('plaza.admin.initial.privacyChecked'))),
    el('label', { class: 'plzw-checkbox' }, completenessBox, el('span', {}, t('plaza.admin.initial.completenessChecked'))));

  let decision = 'accept';
  const decisionSelect = select([
    { value: 'accept', label: t('plaza.admin.initial.accept') },
    { value: 'reject', label: t('plaza.admin.initial.reject') },
  ], decision, { 'aria-label': t('plaza.admin.initial.decision') });
  decisionSelect.addEventListener('change', () => { decision = decisionSelect.value; });
  card.append(field(t('plaza.admin.initial.decision'), decisionSelect, t('plaza.admin.initial.decisionHint')));

  const reasonInput = el('textarea', {
    class: 'input', rows: 2, placeholder: t('plaza.admin.initial.reasonPlaceholder'),
  });
  card.append(field(t('plaza.admin.initial.reason'), reasonInput, t('plaza.admin.initial.reasonHint')));

  card.append(el('div', { class: 'pg-actions' },
    button(t('plaza.admin.initial.submit'), event => busy(event.currentTarget, async () => {
      if (decision === 'reject' && String(reasonInput.value || '').trim().length < 2) {
        throw new Error(t('plaza.admin.initial.reasonRequired'));
      }
      if (decision === 'accept' && !(sourceBox.checked && privacyBox.checked && completenessBox.checked)) {
        throw new Error(t('plaza.admin.initial.checksRequired'));
      }
      const result = await api(`/plaza/submissions/${encodeURIComponent(sub.id)}/initial-review`, {
        method: 'POST',
        data: {
          version: Number(sub.current_version || sub.version || 1),
          decision,
          reason: String(reasonInput.value || '').trim(),
          source_checked: Boolean(sourceBox.checked),
          privacy_checked: Boolean(privacyBox.checked),
          completeness_checked: Boolean(completenessBox.checked),
        },
      });
      void result;
      toast(decision === 'accept'
        ? t('plaza.admin.initial.accepted')
        : t('plaza.admin.initial.rejected'));
      await openSubmission(host, version, me, sub.id, {
        mode: 'initial',
        onBack: options.onBack || (() => go('plaza-admin')),
      });
    }), '', 'check'),
    el('span', { class: 'help' }, t('plaza.admin.initial.avoidHint'))));
  return card;
}

function publishCard(host, version, me, payload, options = {}) {
  const sub = payload?.submission || {};
  const can = Boolean(sub.can?.publish);
  const summary = sub.review_summary || {};
  const card = el('section', { class: 'pg-card plzw-card', 'data-publish': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.release.formTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.release.formHint'))),
      icon('upload')));

  if (sub.viewer?.is_author) {
    card.append(notice(t('plaza.admin.detail.selfNotice'), 'warning'));
    return card;
  }
  if (!summary.hidden && summary.threshold_met === false) {
    card.append(notice(t('plaza.admin.release.thresholdPending'), 'warning'));
  }
  if (Array.isArray(sub.blocking) && sub.blocking.length) {
    card.append(notice(t('plaza.admin.release.blocking'), 'warning'));
  }
  if (!can) {
    const hint = statusLabel(sub.status, sub.status_label);
    card.append(notice(t('plaza.admin.release.notAvailable', { v1: hint }), 'warning'));
    card.append(el('p', { class: 'help' }, t('plaza.admin.release.avoidHint')));
    return card;
  }

  const reasonInput = input(t('plaza.admin.release.reasonPlaceholder'), { maxlength: '300' });
  card.append(field(t('plaza.admin.release.reason'), reasonInput, t('plaza.admin.release.reasonHint')));
  card.append(el('div', { class: 'pg-actions' },
    button(t('plaza.admin.release.submit'), event => busy(event.currentTarget, async () => {
      const result = await api(`/plaza/submissions/${encodeURIComponent(sub.id)}/publish`, {
        method: 'POST',
        data: {
          version: Number(sub.current_version || sub.version || 1),
          reason: String(reasonInput.value || '').trim(),
        },
      });
      const publicId = result?.release?.public_id || result?.public_id || sub.public_id || '';
      toast(publicId
        ? t('plaza.admin.release.publishedWithId', { v1: publicId })
        : t('plaza.admin.release.published'));
      await openSubmission(host, version, me, sub.id, {
        mode: 'release',
        onBack: options.onBack || (() => go('plaza-admin')),
      });
    }), '', 'check'),
    el('span', { class: 'help' }, t('plaza.admin.release.avoidHint'))));
  return card;
}

function eventsCard(submissionId) {
  const card = el('section', { class: 'pg-card plzw-card' });
  const host = el('div', { class: 'plzw-timeline-host' });
  card.append(
    el('div', { class: 'pg-card-head', style: 'margin-bottom:8px' },
      el('div', {},
        el('h2', {}, t('plaza.admin.detail.eventsTitle')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.detail.eventsHint'))),
      icon('history')),
    host);
  host.append(loading(t('common.loading')));
  api(`/plaza/submissions/${encodeURIComponent(submissionId)}/events`)
    .then(data => {
      const items = Array.isArray(data?.items) ? data.items : [];
      if (!items.length) {
        host.replaceChildren(el('p', { class: 'help' }, t('plaza.admin.detail.eventsEmpty')));
        return;
      }
      const timeline = el('ol', { class: 'plzw-timeline' });
      for (const item of items) {
        const key = `plaza.submission.events.action.${item.action}`;
        const translated = t(key);
        timeline.append(el('li', {},
          el('div', { class: 'plzw-meta' },
            el('strong', {}, translated === key ? String(item.action || '') : translated),
            item.to_status ? statusBadge(item.to_status) : null,
            item.actor_id ? el('span', { class: 'help' }, t('plaza.admin.detail.actor', { v1: number(item.actor_id) })) : null,
            el('span', { class: 'help' }, dateText(item.created_at))),
          item.reason ? el('p', { class: 'help', style: 'margin:4px 0 0' }, item.reason) : null));
      }
      host.replaceChildren(timeline);
    })
    .catch(() => {
      host.replaceChildren(el('p', { class: 'help' }, t('plaza.admin.detail.eventsUnavailable')));
    });
  return card;
}

/* ------------------------------------------------------------------------- */
/* 发布历史与撤回                                                             */
/* ------------------------------------------------------------------------- */

function renderHistorySection(host, version, me) {
  host.replaceChildren();
  let filter = '';
  const listHost = el('div', { class: 'plzw-list' });
  const pagerHost = el('div', { class: 'pagination', hidden: true });
  let page = 1;
  const filterSelect = select([
    { value: '', label: t('plaza.admin.history.filterAll') },
    { value: 'active', label: t('plaza.admin.history.filterActive') },
    { value: 'withdrawn', label: t('plaza.admin.history.filterWithdrawn') },
  ], filter, { 'aria-label': t('plaza.admin.history.filter') });
  filterSelect.addEventListener('change', () => {
    filter = filterSelect.value;
    page = 1;
    loadHistory();
  });
  host.append(el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.history.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.history.hint'))),
      el('div', { class: 'pg-actions' },
        filterSelect,
        button(t('common.refresh'), event => busy(event.currentTarget, () => loadHistory()), 'secondary', 'repeat'))),
    listHost,
    pagerHost));
  loadHistory();

  async function loadHistory() {
    if (!current(version)) return;
    listHost.replaceChildren(loading(t('plaza.admin.history.loading')));
    pagerHost.hidden = true;
    let data;
    try {
      data = await api(`/plaza/releases?${query({ status: filter, page, page_size: PAGE_SIZE })}`);
    } catch (error) {
      if (!current(version)) return;
      listHost.replaceChildren(el('div', { class: 'error-panel' },
        error.message || t('plaza.admin.history.loadFailed'),
        el('div', { class: 'row', style: 'margin-top:12px' },
          button(t('common.retry'), event => busy(event.currentTarget, () => loadHistory()), '', 'repeat'))));
      return;
    }
    if (!current(version)) return;
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      listHost.replaceChildren(empty(t('plaza.admin.history.emptyTitle'),
        t('plaza.admin.history.emptyText'), null, 'history'));
      return;
    }
    listHost.replaceChildren();
    for (const item of items) {
      listHost.append(el('article', {
        class: 'plzw-item',
        'data-public-id': String(item.public_id || ''),
        'data-release-status': String(item.status || ''),
      },
      el('div', { class: 'plzw-item-head' },
        el('strong', { 'data-no-i18n': '' }, String(item.public_id || '')),
        item.status === 'active'
          ? badge(t('plaza.admin.history.active'), 'teal')
          : badge(t('plaza.admin.history.withdrawn'), ''),
        badge(t('plaza.admin.queue.version', { v1: number(item.version || 1) }), 'blue'),
        item.subject ? badge(item.subject, 'teal') : null,
        item.withdraw_mode ? badge(item.withdraw_mode === 'emergency'
          ? t('plaza.admin.history.modeEmergency') : t('plaza.admin.history.modeNormal'), 'amber') : null),
      el('div', { class: 'plzw-meta' },
        el('span', {}, t('plaza.admin.history.submission', { v1: number(item.submission_id || 0) })),
        el('span', {}, t('plaza.admin.history.publishedAt', { time: dateText(item.published_at) })),
        item.withdrawn_at
          ? el('span', {}, t('plaza.admin.history.withdrawnAt', { time: dateText(item.withdrawn_at) }))
          : null),
      item.reason ? el('p', { class: 'help', style: 'margin:0' }, item.reason) : null,
      el('div', { class: 'plzw-item-actions' },
        item.status === 'active'
          ? button(t('plaza.admin.history.withdraw'), () => openWithdrawDialog(item, loadHistory), 'danger small', 'close')
          : null,
        item.status === 'active' && Number(item.submission_id || 0) > 0
          ? button(t('plaza.admin.history.rollback'), () => openRollbackDialog(item, loadHistory),
            'secondary small', 'rotate', {
              'data-rollback-submission': String(item.submission_id),
            })
          : null)));
    }
    if (Number(data.total || 0) > PAGE_SIZE) {
      const pages = Math.max(1, Math.ceil(Number(data.total) / PAGE_SIZE));
      pagerHost.hidden = false;
      pagerHost.replaceChildren(
        el('span', {}, t('plaza.admin.history.pageOf', { v1: number(data.total), v2: number(page), v3: number(pages) })),
        el('div', { class: 'row' },
          button(t('common.prevPage'), () => {
            if (page <= 1) return;
            page -= 1;
            loadHistory();
          }, 'secondary small', 'chevron', { disabled: page <= 1 }),
          button(t('common.nextPage'), () => {
            if (page >= pages) return;
            page += 1;
            loadHistory();
          }, 'secondary small', 'chevron', { disabled: page >= pages })));
    }
  }
}

function openWithdrawDialog(release, onDone) {
  const dialog = el('dialog', { class: 'modal-dialog plzw-dialog', 'aria-label': t('plaza.admin.history.withdrawTitle') });
  const modeSelect = select([
    { value: 'normal', label: t('plaza.admin.history.modeNormal') },
    { value: 'emergency', label: t('plaza.admin.history.modeEmergency') },
  ], 'normal', { 'aria-label': t('plaza.admin.history.withdrawMode') });
  const reasonInput = el('textarea', { class: 'input', rows: 2, placeholder: t('plaza.admin.history.withdrawReasonPlaceholder') });
  dialog.append(
    el('div', { class: 'pg-card-head', style: 'margin:0' },
      el('div', {}, el('h3', { style: 'margin:0' }, t('plaza.admin.history.withdrawTitle'))),
      button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
    el('p', { class: 'help', 'data-no-i18n': '' }, String(release.public_id || '')),
    field(t('plaza.admin.history.withdrawMode'), modeSelect, t('plaza.admin.history.withdrawModeHint')),
    field(t('plaza.admin.history.withdrawReason'), reasonInput, t('plaza.admin.history.withdrawReasonHint')),
    el('div', { class: 'pg-actions' },
      button(t('plaza.admin.history.withdrawConfirm'), event => busy(event.currentTarget, async () => {
        const reason = String(reasonInput.value || '').trim();
        if (reason.length < 2) throw new Error(t('plaza.admin.history.withdrawReasonRequired'));
        await api(`/plaza/releases/${encodeURIComponent(release.public_id)}/withdraw`, {
          method: 'POST',
          data: { reason, mode: modeSelect.value },
        });
        dialog.close();
        toast(t('plaza.admin.history.withdrawDone'));
        onDone();
      }), 'danger', 'close')));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

/**
 * 发布回滚（WS21 契约）：POST /plaza/submissions/{id}/rollback {reason}。
 * 服务端撤回当前活跃发布并恢复该投稿上一已发布版本；无历史版本返回 409，
 * 错误信息由 busy 统一 toast（不清空页面）。
 */
function openRollbackDialog(release, onDone) {
  const dialog = el('dialog', {
    class: 'modal-dialog plzw-dialog',
    'data-rollback-dialog': '',
    'aria-label': t('plaza.admin.history.rollbackTitle'),
  });
  const reasonInput = el('textarea', {
    class: 'input',
    rows: 2,
    placeholder: t('plaza.admin.history.rollbackReasonPlaceholder'),
  });
  dialog.append(
    el('div', { class: 'pg-card-head', style: 'margin:0' },
      el('div', {}, el('h3', { style: 'margin:0' }, t('plaza.admin.history.rollbackTitle'))),
      button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
    el('p', { class: 'help', 'data-no-i18n': '' }, String(release.public_id || '')),
    notice(t('plaza.admin.history.rollbackHint'), 'warning'),
    field(t('plaza.admin.history.rollbackReason'), reasonInput, t('plaza.admin.history.rollbackReasonHint')),
    el('div', { class: 'pg-actions' },
      button(t('plaza.admin.history.rollbackConfirm'), event => busy(event.currentTarget, async () => {
        const reason = String(reasonInput.value || '').trim();
        if (reason.length < 2) throw new Error(t('plaza.admin.history.rollbackReasonRequired'));
        const result = await api(`/plaza/submissions/${encodeURIComponent(release.submission_id)}/rollback`, {
          method: 'POST',
          data: { reason },
        });
        dialog.close();
        toast(result?.message || t('plaza.admin.history.rollbackDone'));
        onDone();
      }), 'danger', 'rotate')));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

/* ------------------------------------------------------------------------- */
/* 质量运营：抽检 / 纠错举报申诉队列 / 质量指标（WS21）                       */
/* ------------------------------------------------------------------------- */

/** 队列接口通用解包：契约固定 {items}，同时兼容裸数组（旧实例/降级实现）。 */
function listOf(value) {
  if (Array.isArray(value)) return value;
  return Array.isArray(value?.items) ? value.items : [];
}

/** 百分比归一化：rate 可能是 0~1 比例或 0~100 百分数，统一显示成百分数。 */
function percentValue(value) {
  if (value === null || value === undefined || value === '') return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  return numeric <= 1 ? numeric * 100 : numeric;
}

function percentText(value) {
  const percent = percentValue(value);
  if (percent === null) return t('common.noRecord');
  return `${Math.round(percent * 10) / 10}%`;
}

function hoursText(value) {
  if (value === null || value === undefined || value === '') return t('common.noRecord');
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return t('common.noRecord');
  return t('plaza.metrics.hoursValue', { v1: Math.round(numeric * 10) / 10 });
}

function severityLabel(value) {
  const key = `plaza.quality.severity.${value}`;
  const translated = t(key);
  return translated === key ? String(value || '') : translated;
}

function qualityStatusLabel(value) {
  const key = `plaza.quality.checkStatus.${value}`;
  const translated = t(key);
  return translated === key ? String(value || '') : translated;
}

function opsStatusLabel(value) {
  const key = `plaza.ops.status.${value}`;
  const translated = t(key);
  return translated === key ? String(value || '') : translated;
}

function issueFieldLabel(value) {
  const key = `plaza.ops.issue.field.${value}`;
  const translated = t(key);
  return translated === key ? String(value || '') : translated;
}

function reportReasonLabel(value) {
  const key = `plaza.report.reason.${value}`;
  const translated = t(key);
  return translated === key ? String(value || '') : translated;
}

/* ----------------------------- 抽检队列 ----------------------------- */

function renderQualitySection(host, version) {
  host.replaceChildren();
  let statusFilter = 'all';
  const summaryHost = el('div', { class: 'plzw-metrics', 'data-quality-summary': '' });
  const listHost = el('div', { class: 'plzw-list', 'data-quality-list': '' });
  const statusSelect = select([
    { value: 'all', label: t('plaza.quality.filter.all') },
    { value: 'pending', label: t('plaza.quality.filter.pending') },
    { value: 'issues', label: t('plaza.quality.filter.issues') },
    { value: 'passed', label: t('plaza.quality.filter.passed') },
  ], statusFilter, { 'aria-label': t('plaza.quality.filter.label') });
  statusSelect.addEventListener('change', () => {
    statusFilter = statusSelect.value;
    load();
  });
  host.append(el('section', { class: 'pg-card plzw-card', 'data-quality-section': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.quality.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.quality.hint'))),
      el('div', { class: 'pg-actions' },
        statusSelect,
        button(t('common.refresh'), event => busy(event.currentTarget, () => load()), 'secondary', 'repeat'))),
    summaryHost,
    listHost));
  load();

  async function load() {
    if (!current(version)) return;
    listHost.replaceChildren(loading(t('plaza.quality.loading')));
    let data;
    try {
      data = await api(`/plaza/admin/quality/queue?${query({
        status: statusFilter,
        page: 1,
        page_size: PAGE_SIZE,
      })}`);
    } catch {
      if (!current(version)) return;
      summaryHost.replaceChildren();
      listHost.replaceChildren(el('div', { 'data-quality-not-ready': 'true' },
        notice(t('plaza.quality.notReadyTitle'), 'warning'),
        el('p', { class: 'help' }, t('plaza.quality.notReadyText'))));
      return;
    }
    if (!current(version)) return;
    const items = listOf(data);
    renderSummary(data, items);
    renderItems(items);
  }

  function renderSummary(data, items) {
    const counts = data?.counts || {};
    const countOf = (key, fallback) => number(Number(counts[key] ?? fallback) || 0);
    const pending = items.filter(item => item.check_status === 'pending').length;
    const issues = items.filter(item => item.check_status === 'issue').length;
    const passed = items.filter(item => item.check_status === 'passed').length;
    const sampling = data?.sampling || {};
    const coverage = Number(sampling.coverage_rate);
    summaryHost.replaceChildren(
      metric(t('plaza.quality.summaryPending'), countOf('pending', pending)),
      metric(t('plaza.quality.summaryIssues'), countOf('issues', issues)),
      metric(t('plaza.quality.summaryPassed'), countOf('passed', passed)),
      metric(t('plaza.quality.summaryCoverage'),
        Number.isFinite(coverage) ? percentText(coverage) : t('common.noRecord')));
  }

  function renderItems(items) {
    listHost.replaceChildren();
    if (!items.length) {
      listHost.append(empty(t('plaza.quality.emptyTitle'), t('plaza.quality.emptyText'), null, 'shield'));
      return;
    }
    for (const item of items) listHost.append(qualityItem(item));
  }

  function qualityItem(item) {
    const submissionId = Number(item.submission_id || 0);
    const publicId = String(item.public_id || '');
    const latest = item.latest_check || {};
    const status = String(item.check_status || 'pending');
    const findings = Array.isArray(latest.findings) ? latest.findings.map(String).filter(Boolean) : [];
    return el('article', {
      class: 'plzw-item',
      'data-quality-item': submissionId ? String(submissionId) : publicId,
      'data-check-status': status,
    },
    el('div', { class: 'plzw-item-head' },
      el('strong', {}, submissionId ? `#${number(submissionId)}` : (publicId || t('plaza.quality.itemUnknown'))),
      badge(qualityStatusLabel(status), status === 'issue' ? 'red' : status === 'passed' ? 'teal' : 'amber'),
      publicId ? el('span', { class: 'help', 'data-no-i18n': '' }, publicId) : null,
      item.subject ? badge(item.subject, 'teal') : null,
      item.type ? badge(plazaTypeLabel(item.type), '') : null,
      item.auto_withdrawn ? badge(t('plaza.quality.autoWithdrawnBadge'), 'red') : null,
      Number(item.checks_count || 0) > 0
        ? badge(t('plaza.quality.checksCount', { v1: number(item.checks_count) }))
        : null),
    el('p', { class: 'plzw-stem', 'data-no-i18n': '' },
      item.stem_preview || t('plaza.submission.item.emptyStem')),
    el('div', { class: 'plzw-meta' },
      item.published_at
        ? el('span', {}, t('plaza.quality.publishedAt', { time: dateText(item.published_at) }))
        : null,
      latest.created_at
        ? el('span', {}, t('plaza.quality.lastCheckedAt', { time: dateText(latest.created_at) }))
        : null,
      latest.severity
        ? el('span', {}, t('plaza.quality.lastSeverity', { v1: severityLabel(latest.severity) }))
        : null),
    findings.length
      ? el('p', { class: 'help', 'data-no-i18n': '' }, findings.join('；'))
      : null,
    el('div', { class: 'plzw-item-actions' },
      String(item.status || '') === 'published'
        ? (status !== 'passed'
          ? button(t('plaza.quality.pass'), event => busy(event.currentTarget, async () => {
            await submitCheck(item, { decision: 'pass' });
          }), 'secondary small', 'check')
          : null)
        : el('span', { class: 'help' }, t('plaza.quality.notCheckable')),
      String(item.status || '') === 'published'
        ? button(t('plaza.quality.issue'), () => openQualityDialog(item), 'danger small', 'close')
        : null));
  }

  function payloadOf(item, extra) {
    const payload = { ...extra };
    const submissionId = Number(item.submission_id || 0);
    if (submissionId) payload.submission_id = submissionId;
    else if (item.public_id) payload.public_id = String(item.public_id);
    return payload;
  }

  async function submitCheck(item, extra) {
    const result = await api('/plaza/admin/quality/checks', {
      method: 'POST',
      data: payloadOf(item, extra),
    });
    // high 严重问题由服务端自动紧急撤回；显式提示编辑关注通知与作者/收藏者影响。
    if (result?.auto_withdrawn) toast(result?.message || t('plaza.quality.highWithdrawn'));
    else if (extra.decision === 'pass') toast(result?.message || t('plaza.quality.passToast'));
    else toast(result?.message || t('plaza.quality.issueToast'));
    await load();
  }

  function openQualityDialog(item) {
    const severitySelect = select([
      { value: 'low', label: t('plaza.quality.severity.low') },
      { value: 'medium', label: t('plaza.quality.severity.medium') },
      { value: 'high', label: t('plaza.quality.severity.high') },
    ], 'medium', { 'aria-label': t('plaza.quality.severityLabel') });
    const findingsInput = el('textarea', {
      class: 'input',
      rows: 3,
      placeholder: t('plaza.quality.findingsPlaceholder'),
    });
    const noteInput = input(t('plaza.quality.notePlaceholder'), { maxlength: '300' });
    const dialog = el('dialog', {
      class: 'modal-dialog plzw-dialog',
      'data-quality-dialog': '',
      'aria-label': t('plaza.quality.issueTitle'),
    },
    el('div', { class: 'pg-card-head', style: 'margin:0' },
      el('div', {},
        el('h3', { style: 'margin:0' }, t('plaza.quality.issueTitle')),
        el('p', { class: 'help' }, t('plaza.quality.issueHint'))),
      button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
    field(t('plaza.quality.severityLabel'), severitySelect, t('plaza.quality.severityHint')),
    field(t('plaza.quality.findings'), findingsInput, t('plaza.quality.findingsHint')),
    field(t('plaza.quality.note'), noteInput),
    notice(t('plaza.quality.highWarning'), 'warning'),
    el('div', { class: 'pg-actions' },
      button(t('plaza.quality.issueSubmit'), event => busy(event.currentTarget, async () => {
        const findings = String(findingsInput.value || '')
          .split(/[\n；;]+/)
          .map(value => value.trim())
          .filter(Boolean);
        if (!findings.length) throw new Error(t('plaza.quality.findingsRequired'));
        await submitCheck(item, {
          decision: 'issue',
          severity: severitySelect.value,
          findings,
          note: String(noteInput.value || '').trim(),
        });
        dialog.close();
      }), 'danger', 'close')));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }
}


/* --------------------------- 纠错/举报/申诉队列 --------------------------- */

function renderOpsSection(host, version) {
  host.replaceChildren();
  host.append(opsQueueCard(version, {
    kind: 'issues',
    path: '/plaza/admin/issues',
    title: t('plaza.ops.issues.title'),
    hint: t('plaza.ops.issues.hint'),
    emptyText: t('plaza.ops.issues.empty'),
  }));
  host.append(opsQueueCard(version, {
    kind: 'reports',
    path: '/plaza/admin/reports',
    title: t('plaza.ops.reports.title'),
    hint: t('plaza.ops.reports.hint'),
    emptyText: t('plaza.ops.reports.empty'),
  }));
  host.append(opsQueueCard(version, {
    kind: 'appeals',
    path: '/plaza/admin/appeals',
    title: t('plaza.ops.appeals.title'),
    hint: t('plaza.ops.appeals.hint'),
    emptyText: t('plaza.ops.appeals.empty'),
  }));
}

function opsQueueCard(version, { kind, path, title, hint, emptyText }) {
  let status = 'pending';
  const listHost = el('div', { class: 'plzw-list' });
  const countHost = el('p', { class: 'help', 'data-ops-counts': kind });
  const statusSelect = select([
    { value: 'pending', label: t('plaza.ops.filter.pending') },
    { value: 'resolved', label: t('plaza.ops.filter.resolved') },
    { value: 'rejected', label: t('plaza.ops.filter.rejected') },
    { value: 'all', label: t('plaza.ops.filter.all') },
  ], status, { 'aria-label': t('plaza.ops.filter.label') });
  statusSelect.addEventListener('change', () => {
    status = statusSelect.value;
    load();
  });
  const card = el('section', { class: 'pg-card plzw-card', 'data-ops-section': kind },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, title),
        el('p', { class: 'pg-card-sub' }, hint)),
      el('div', { class: 'pg-actions' },
        statusSelect,
        button(t('common.refresh'), event => busy(event.currentTarget, () => load()), 'secondary', 'repeat'))),
    countHost,
    listHost);
  load();
  return card;

  async function load() {
    if (!current(version)) return;
    listHost.replaceChildren(loading(t('plaza.ops.loading')));
    let data;
    try {
      data = await api(`${path}?${query({ status, page: 1, page_size: PAGE_SIZE })}`);
    } catch {
      if (!current(version)) return;
      countHost.textContent = '';
      listHost.replaceChildren(el('div', { 'data-ops-not-ready': kind },
        notice(t('plaza.ops.notReadyTitle'), 'warning'),
        el('p', { class: 'help' }, t('plaza.ops.notReadyText', { v1: `${path}?status=pending` }))));
      return;
    }
    if (!current(version)) return;
    const items = listOf(data);
    const counts = data?.counts || {};
    countHost.textContent = t('plaza.ops.counts', {
      v1: number(counts.pending || 0),
      v2: number(counts.resolved || 0),
      v3: number(counts.rejected || 0),
    });
    renderItems(items);
  }

  function renderItems(items) {
    listHost.replaceChildren();
    if (!items.length) {
      listHost.append(empty(t('plaza.ops.emptyTitle'), emptyText, null, 'message'));
      return;
    }
    for (const item of items) listHost.append(opsItem(item));
  }

  function detailTextOf(item) {
    if (kind === 'issues') {
      return [issueFieldLabel(item.field), String(item.description || '')].filter(Boolean).join(' · ');
    }
    if (kind === 'reports') {
      return [reportReasonLabel(item.reason), String(item.detail || '')].filter(Boolean).join(' · ');
    }
    return String(item.reason || '');
  }

  function opsItem(item) {
    const id = Number(item.id || 0);
    const question = item.question || {};
    const statusValue = String(item.status || 'pending');
    return el('article', {
      class: 'plzw-item',
      'data-ops-item': String(id),
      'data-ops-kind': kind,
      'data-ops-status': statusValue,
    },
    el('div', { class: 'plzw-item-head' },
      el('strong', {}, `#${number(id)}`),
      badge(opsStatusLabel(statusValue),
        statusValue === 'pending' ? 'amber' : statusValue === 'resolved' ? 'teal' : 'red'),
      item.public_id ? el('span', { class: 'help', 'data-no-i18n': '' }, String(item.public_id)) : null,
      Number(item.submission_id || 0) > 0
        ? badge(t('plaza.ops.submissionBadge', { v1: number(item.submission_id) }))
        : null,
      item.reporter_display
        ? el('span', { class: 'help', 'data-no-i18n': '' }, String(item.reporter_display))
        : null,
      item.author_display
        ? el('span', { class: 'help', 'data-no-i18n': '' }, String(item.author_display))
        : null,
      item.created_at ? el('span', { class: 'help' }, dateText(item.created_at)) : null),
    el('p', { class: 'plzw-stem', 'data-no-i18n': '' },
      question.stem_preview || t('plaza.submission.item.emptyStem')),
    el('p', { class: 'help', 'data-no-i18n': '' }, detailTextOf(item)),
    question.subject || question.type
      ? el('div', { class: 'plzw-meta' },
        question.subject ? badge(question.subject, 'teal') : null,
        question.type ? badge(plazaTypeLabel(question.type), '') : null)
      : null,
    item.resolution ? el('p', { class: 'help', 'data-no-i18n': '' }, String(item.resolution)) : null,
    el('div', { class: 'plzw-item-actions' },
      statusValue === 'pending'
        ? button(t('plaza.ops.process'), () => openOpsDialog(item), 'secondary small', 'edit')
        : null));
  }

  function openOpsDialog(item) {
    const actionSelect = select([
      { value: 'resolve', label: t('plaza.ops.action.resolve') },
      { value: 'reject', label: t('plaza.ops.action.reject') },
    ], 'resolve', { 'aria-label': t('plaza.ops.action.label') });
    const reasonInput = el('textarea', {
      class: 'input',
      rows: 2,
      placeholder: t('plaza.ops.reasonPlaceholder'),
    });
    const dialog = el('dialog', {
      class: 'modal-dialog plzw-dialog',
      'data-ops-dialog': kind,
      'aria-label': t('plaza.ops.process'),
    },
    el('div', { class: 'pg-card-head', style: 'margin:0' },
      el('div', {},
        el('h3', { style: 'margin:0' }, t('plaza.ops.processTitle', { v1: number(item.id || 0) })),
        el('p', { class: 'help' }, t('plaza.ops.processHint'))),
      button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
    el('p', { class: 'help', 'data-no-i18n': '' }, detailTextOf(item)),
    field(t('plaza.ops.action.label'), actionSelect),
    field(t('plaza.ops.reason'), reasonInput, t('plaza.ops.reasonHint')),
    el('div', { class: 'pg-actions' },
      button(t('plaza.ops.processSubmit'), event => busy(event.currentTarget, async () => {
        const action = actionSelect.value;
        const reason = String(reasonInput.value || '').trim();
        if (action === 'reject' && reason.length < 2) throw new Error(t('plaza.ops.reasonRequired'));
        const result = await api(`${path}/${encodeURIComponent(item.id)}`, {
          method: 'PATCH',
          data: reason ? { action, reason } : { action },
        });
        dialog.close();
        toast(result?.message || t('plaza.ops.processed'));
        await load();
      }), '', 'check')));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }
}

/* ----------------------------- 质量指标 ----------------------------- */

function renderMetricsSection(host, version) {
  host.replaceChildren();
  const grid = el('div', { class: 'plzw-metrics', 'data-metrics-grid': '' });
  const breakdownHost = el('div', { 'data-metrics-breakdown': '' });
  host.append(el('section', { class: 'pg-card plzw-card', 'data-metrics-section': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.metrics.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.metrics.hint'))),
      el('div', { class: 'pg-actions' },
        button(t('common.refresh'), event => busy(event.currentTarget, () => load()), 'secondary', 'repeat'))),
    grid,
    breakdownHost));
  load();

  async function load() {
    if (!current(version)) return;
    grid.replaceChildren(loading(t('plaza.metrics.loading')));
    breakdownHost.replaceChildren();
    let data;
    try {
      data = await api('/plaza/admin/metrics');
    } catch {
      if (!current(version)) return;
      grid.replaceChildren(el('div', { 'data-metrics-not-ready': 'true' },
        notice(t('plaza.metrics.notReadyTitle'), 'warning'),
        el('p', { class: 'help' }, t('plaza.metrics.notReadyText'))));
      return;
    }
    if (!current(version)) return;
    renderMetrics(data);
  }

  function renderMetrics(data) {
    const totals = data?.totals || {};
    const scorable = data?.scorable || {};
    const figures = data?.figures || {};
    const review = data?.review_duration || {};
    const quality = data?.quality || {};
    const feedback = data?.feedback || {};
    const withdrawals = data?.withdrawals || {};
    const medianHours = review.median_hours ?? review.submitted_to_published?.median_hours;
    const unresolved = (Number(feedback.issues_unresolved || 0)
      + Number(feedback.reports_unresolved || 0)
      + Number(feedback.appeals_unresolved || 0));
    grid.replaceChildren(
      metric(t('plaza.metrics.published'), number(totals.published || 0)),
      metric(t('plaza.metrics.publicVisible'), number(totals.public_visible || 0)),
      metric(t('plaza.metrics.scorableRate'), percentText(scorable.rate)),
      metric(t('plaza.metrics.figureRate'), percentText(figures.coverage_rate)),
      metric(t('plaza.metrics.reviewMedian'), hoursText(medianHours)),
      metric(t('plaza.metrics.qualityIssueRate'), percentText(quality.issue_rate)),
      metric(t('plaza.metrics.pendingChecks'), number(quality.pending_checks || 0)),
      metric(t('plaza.metrics.checksTotal'), number(quality.checks_total || 0)),
      metric(t('plaza.metrics.highIssues'), number(quality.high_issues || 0)),
      metric(t('plaza.metrics.feedbackPending'), number(unresolved)),
      metric(t('plaza.metrics.withdrawn'), number(withdrawals.submissions_withdrawn || totals.withdrawn || 0)),
      metric(t('plaza.metrics.rollbacks'), number(withdrawals.rollbacks || 0)));
    breakdownHost.replaceChildren();
    renderBreakdown(t('plaza.metrics.bySubject'), data?.by_subject || [], row => String(row.subject || t('plaza.metrics.unknownLabel')));
    renderBreakdown(t('plaza.metrics.byType'), data?.by_type || [], row => plazaTypeLabel(row.type));
    const caliber = data?.caliber && typeof data.caliber === 'object' ? Object.values(data.caliber) : [];
    if (caliber.length) {
      breakdownHost.append(
        el('h3', { style: 'margin:18px 0 8px' }, t('plaza.metrics.caliber')),
        el('ul', { class: 'help', 'data-no-i18n': '' },
          ...caliber.map(text => el('li', {}, String(text)))));
    }
  }

  function renderBreakdown(title, rows, labelOf) {
    const head = el('h3', { style: 'margin:18px 0 8px' }, title);
    const list = el('div', { class: 'plzw-list' });
    if (!Array.isArray(rows) || !rows.length) {
      list.append(el('p', { class: 'help' }, t('plaza.metrics.noData')));
    } else {
      for (const row of rows.slice(0, 24)) {
        list.append(el('div', { class: 'plzw-meta', 'data-metric-row': '' },
          el('span', { 'data-no-i18n': '' }, labelOf(row)),
          el('span', {}, t('plaza.metrics.rowLine', {
            v1: number(row.count || 0),
            v2: number(row.published || 0),
            v3: number(row.verified || 0),
          }))));
      }
    }
    breakdownHost.append(head, list);
  }
}

/* ------------------------------------------------------------------------- */
/* 身份审核 + 角色授权                                                        */
/* ------------------------------------------------------------------------- */

function renderIdentitySection(host, version, me, canGrant) {
  host.replaceChildren();

  // 身份审核（WS14 新增队列接口；未就绪时显示占位，不报错）
  const queueHost = el('div');
  host.append(el('section', { class: 'pg-card plzw-card' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.identity.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.identity.hint'))),
      el('div', { class: 'pg-actions' },
        button(t('common.refresh'), event => busy(event.currentTarget, () => loadIdentityQueue()), 'secondary', 'repeat'))),
    queueHost));
  loadIdentityQueue();

  async function loadIdentityQueue() {
    if (!current(version)) return;
    queueHost.replaceChildren(loading(t('plaza.admin.identity.loading')));
    let data;
    try {
      data = await api('/plaza/admin/identity/queue');
    } catch {
      queueHost.replaceChildren(
        el('div', { 'data-identity-not-ready': 'true' },
          notice(t('plaza.admin.identity.notReadyTitle'), 'warning'),
          el('p', { class: 'help' }, t('plaza.admin.identity.notReadyText'))));
      return;
    }
    if (!current(version)) return;
    const items = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
    if (!items.length) {
      queueHost.replaceChildren(el('p', { class: 'help' }, t('plaza.admin.identity.empty')));
      return;
    }
    const list = el('div', { class: 'plzw-list' });
    for (const item of items) {
      const id = Number(item.id || 0);
      const username = item.user?.username || item.username || item.applicant || '';
      list.append(el('article', { class: 'plzw-item', 'data-identity-id': String(id) },
        el('div', { class: 'plzw-item-head' },
          el('strong', {}, `#${number(id)}`),
          badge(identityKindLabel(item.kind), 'blue'),
          badge(identityStatusLabel(item.status), item.status === 'pending' ? 'amber' : ''),
          item.subject ? badge(item.subject, 'teal') : null,
          username ? el('span', { class: 'help', 'data-no-i18n': '' }, `@${username}`) : null,
          item.applied_at ? el('span', { class: 'help' }, dateText(item.applied_at)) : null),
        item.evidence
          ? el('p', { class: 'plzw-stem', 'data-no-i18n': '' }, String(item.evidence))
          : null,
        el('div', { class: 'plzw-item-actions' },
          item.status === 'pending'
            ? button(t('plaza.admin.identity.review'), () => openIdentityDialog(item, loadIdentityQueue), 'secondary small', 'edit')
            : null)));
    }
    queueHost.replaceChildren(list);
  }

  function openIdentityDialog(item, onDone) {
    const dialog = el('dialog', { class: 'modal-dialog plzw-dialog', 'aria-label': t('plaza.admin.identity.reviewTitle') });
    const reasonInput = el('textarea', { class: 'input', rows: 2, placeholder: t('plaza.admin.identity.reasonPlaceholder') });
    dialog.append(
      el('div', { class: 'pg-card-head', style: 'margin:0' },
        el('div', {}, el('h3', { style: 'margin:0' }, t('plaza.admin.identity.reviewTitle'))),
        button(t('common.close'), () => dialog.close(), 'ghost small', 'close')),
      el('p', { class: 'help', 'data-no-i18n': '' },
        `#${number(item.id)} · ${item.kind || ''} · ${item.subject || ''}`),
      field(t('plaza.admin.identity.reason'), reasonInput),
      el('div', { class: 'pg-actions' },
        button(t('plaza.admin.identity.approve'), event => busy(event.currentTarget, async () => {
          await submitIdentityReview(item.id, 'approve', reasonInput.value);
          dialog.close();
          toast(t('plaza.admin.identity.approved'));
          onDone();
        }), '', 'check'),
        button(t('plaza.admin.identity.reject'), event => busy(event.currentTarget, async () => {
          const reason = String(reasonInput.value || '').trim();
          if (reason.length < 2) throw new Error(t('plaza.admin.identity.reasonRequired'));
          await submitIdentityReview(item.id, 'reject', reason);
          dialog.close();
          toast(t('plaza.admin.identity.rejected'));
          onDone();
        }), 'danger', 'close')));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  async function submitIdentityReview(identityId, decision, reason) {
    return api(`/plaza/admin/identity/${encodeURIComponent(identityId)}/review`, {
      method: 'POST',
      data: { decision, reason: String(reason || '').trim() },
    });
  }

  // 角色授权（仅管理员）
  const rolesCard = el('section', { class: 'pg-card plzw-card', 'data-role-admin': 'true' },
    el('div', { class: 'pg-card-head' },
      el('div', {},
        el('h2', {}, t('plaza.admin.roles.title')),
        el('p', { class: 'pg-card-sub' }, t('plaza.admin.roles.hint'))),
      icon('key')));
  if (!canGrant) {
    rolesCard.append(notice(t('plaza.admin.roles.adminOnly'), 'warning'));
    host.append(rolesCard);
    return;
  }
  const usernameInput = input(t('plaza.admin.roles.usernamePlaceholder'), { maxlength: '40' });
  const userIdInput = input(t('plaza.admin.roles.userIdPlaceholder'), { type: 'number', min: '1', inputmode: 'numeric' });
  const roleSelect = select([
    { value: 'reviewer', label: t('plaza.admin.roles.role.reviewer') },
    { value: 'subject_reviewer', label: t('plaza.admin.roles.role.subject_reviewer') },
    { value: 'editor', label: t('plaza.admin.roles.role.editor') },
  ], 'reviewer', { 'aria-label': t('plaza.admin.roles.role') });
  const subjectInput = input(t('plaza.admin.roles.subjectPlaceholder'), { maxlength: '40' });
  const actionSelect = select([
    { value: 'grant', label: t('plaza.admin.roles.action.grant') },
    { value: 'revoke', label: t('plaza.admin.roles.action.revoke') },
  ], 'grant', { 'aria-label': t('plaza.admin.roles.action') });
  const expiresInput = input(t('plaza.admin.roles.expiresPlaceholder'), { maxlength: '40' });
  const resultHost = el('div');
  rolesCard.append(
    el('div', { class: 'pg-grid-2' },
      field(t('plaza.admin.roles.username'), usernameInput, t('plaza.admin.roles.usernameHint')),
      field(t('plaza.admin.roles.userId'), userIdInput, t('plaza.admin.roles.userIdHint')),
      field(t('plaza.admin.roles.roleLabel'), roleSelect, t('plaza.admin.roles.roleHint')),
      field(t('plaza.admin.roles.subject'), subjectInput, t('plaza.admin.roles.subjectHint')),
      field(t('plaza.admin.roles.action'), actionSelect),
      field(t('plaza.admin.roles.expiresAt'), expiresInput, t('plaza.admin.roles.expiresHint'))),
    el('div', { class: 'pg-actions' },
      button(t('plaza.admin.roles.submit'), event => busy(event.currentTarget, async () => {
        const username = String(usernameInput.value || '').trim();
        const userId = Number(userIdInput.value || 0);
        const role = roleSelect.value;
        const subject = String(subjectInput.value || '').trim();
        if (!username && !userId) throw new Error(t('plaza.admin.roles.userRequired'));
        if (role !== 'editor' && !subject) throw new Error(t('plaza.admin.roles.subjectRequired'));
        const payload = { action: actionSelect.value, role };
        if (username) payload.username = username;
        if (userId) payload.user_id = userId;
        if (role !== 'editor') payload.subject = subject;
        const expires = String(expiresInput.value || '').trim();
        if (expires) payload.expires_at = expires;
        const result = await api('/plaza/admin/roles', { method: 'POST', data: payload });
        const target = result?.user?.username ? `@${result.user.username}` : `#${number(result?.user?.id || userId || 0)}`;
        resultHost.replaceChildren(notice(
          payload.action === 'grant'
            ? t('plaza.admin.roles.grantDone', { v1: target, v2: t(`plaza.admin.roles.role.${role}`) })
            : t('plaza.admin.roles.revokeDone', { v1: target, v2: t(`plaza.admin.roles.role.${role}`) }),
          ''));
        const roles = Array.isArray(result?.roles) ? result.roles : [];
        if (roles.length) {
          resultHost.append(el('div', { class: 'plzw-tags', 'data-no-i18n': '' },
            roles.map(item => badge(`${item.role}${item.subject ? ' · ' + item.subject : ''}`, 'teal'))));
        }
      }), '', 'key'),
      el('span', { class: 'help' }, t('plaza.admin.roles.avoidHint'))),
    resultHost);
  host.append(rolesCard);
}

/* 供测试与调试使用：编辑台状态与队列探针路径（与后端契约对齐）。 */
export const ADMIN_STATUS_KEYS = STATUS_KEYS;
export const ADMIN_QUEUE_PATH = '/plaza/admin/submissions';
export const ADMIN_IDENTITY_QUEUE_PATH = '/plaza/admin/identity/queue';
