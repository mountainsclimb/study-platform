import { api, ensureCatalog, ensureKnowledgePoints, ensurePromptGrid } from '../core/api.js';
import { state, onSessionReset } from '../core/state.js';
import {
  el, icon, button, badge, heading, notice, field, input, select, statCard,
  number, loading, busy, toast, dateText, query, showImage, wireTabKeyboard,
} from '../core/dom.js';
import { go, current } from '../core/router.js';
import { mountJob } from '../core/jobs.js';
import { markdown } from '../core/markdown.js';
import { createModelPicker } from '../components/modelPicker.js';
import { renderKpTree } from '../components/kpTree.js';
import { t } from '../core/i18n.js';

// 内容管理三个分区：校勘 / 题库与知识点 / 运行与账号。模块级变量让
// renderRoute 重渲染（发布、导入、保存策略）后仍停留在当前分区。
let adminTab = 'issues';
// 登出/401 后清空分区记忆，避免下一个账号进入内容管理时停在上一账号的分区。
onSessionReset(() => { adminTab = 'issues'; });

function sharedReviewCard(title, path, initial, row, emptyText) {
  const content = el('div');
  const pageSize = 20;
  let offset = 0;
  let seq = 0;
  const refresh = async (nextOffset = offset) => {
    const mine = ++seq;
    try {
      const data = await api(`${path}?offset=${nextOffset}&limit=${pageSize}`, { latest: true, latestKey: path });
      if (mine !== seq) return;
      offset = nextOffset;
      render(data);
    } catch (error) {
      if (mine !== seq || error.aborted) return;
      content.replaceChildren(notice(error.message, 'warning'));
    }
  };
  function render(data) {
    const items = data.items || [];
    content.replaceChildren();
    if (data.error) {
      content.append(notice(data.error, 'warning'));
    } else if (items.length) {
      content.append(el('div', { class: 'mini-list' }, items.map(item => row(item, refresh))));
    } else {
      content.append(el('p', { class: 'help' }, emptyText));
    }
    if (offset > 0 || data.has_more) {
      content.append(el('div', { class: 'row end', style: 'margin-top:12px' },
        button(t('common.prevPage'), e => busy(e.currentTarget, () => refresh(offset - pageSize)), 'ghost small', null, { disabled: offset === 0 }),
        button(t('common.nextPage'), e => busy(e.currentTarget, () => refresh(offset + pageSize)), 'ghost small', null, { disabled: !data.has_more })));
    }
  }
  render(initial);
  return el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, title)), content);
}

function describeTemplateConfig(config, dimensions) {
  return Object.entries(config).map(([key, value]) => {
    const dim = dimensions[key];
    const names = (Array.isArray(value) ? value : [value]).map(id =>
      dim?.options.find(option => option.id === id)?.name || id);
    return names.length ? t('common.labelValue', { label: dim?.label || key, value: names.join(t('common.listSeparator')) }) : '';
  }).filter(Boolean).join(' · ');
}

// ---------------------------------------------------------------------------
// P1 「题目报错 → 待核对 → 待修订 / 确认无误 → 待发布 → 已发布」审核闭环
// ---------------------------------------------------------------------------

const ISSUE_STATUS_LABELS = {
  get pending_check() { return t('admin.issueStatus.pendingCheck'); },
  get to_revise() { return t('admin.issueStatus.toRevise'); },
  get confirmed() { return t('admin.issueStatus.confirmed'); },
  get to_publish() { return t('admin.issueStatus.toPublish'); },
  get published() { return t('admin.issueStatus.published'); },
};
const ISSUE_NEXT = {
  pending_check: ['to_revise', 'confirmed'],
  to_revise: ['to_publish', 'confirmed'],
  confirmed: ['to_publish'],
  to_publish: ['published', 'to_revise'],
  published: ['to_revise'],
};
const ISSUE_ACTION_LABELS = {
  get to_revise() { return t('admin.issueAction.toRevise'); },
  get confirmed() { return t('admin.issueAction.confirmed'); },
  get to_publish() { return t('admin.issueAction.toPublish'); },
  get published() { return t('admin.issueAction.published'); },
};
const ISSUE_RISK_LABELS = {
  get high() { return t('admin.issueRisk.high'); },
  get medium() { return t('admin.issueRisk.medium'); },
  get low() { return t('admin.issueRisk.low'); },
};

function statusBadge(status) {
  const type = status === 'published' ? 'teal'
    : status === 'confirmed' ? 'blue'
      : status === 'to_publish' ? 'amber'
        : status === 'to_revise' ? 'red' : '';
  return badge(ISSUE_STATUS_LABELS[status] || status, type);
}

function issueBeforeSummary(issue) {
  const before = issue.before || {};
  const parts = [];
  if (before.stem_excerpt) parts.push(el('p', { class: 'small-text' }, t('admin.currentlyShown', { v1: before.stem_excerpt })));
  if (before.options?.length) {
    parts.push(el('p', { class: 'small-text' },
      t('admin.options', { v1: before.options.map(o => `${o.label}. ${String(o.text || '').slice(0, 40)}`).join(' / ') })));
  }
  if (before.answer_text) parts.push(el('p', { class: 'small-text' }, t('physics.referenceAnswer', { v1: before.answer_text.slice(0, 120) })));
  if (before.images?.length) {
    const row = el('div', { class: 'row' });
    for (const image of before.images) {
      if (!image.file) continue;
      const url = `/api/bank-image?${query({ subject: issue.subject, file: image.file, preview: 'true' })}`;
      row.append(button(
        t('admin.originalPaperAssetPreview', { v1: String(image.file).split('/').pop() }),
        () => showImage(url, t('admin.originalPaperAssetAdminPreview')),
        'ghost small', 'eye',
      ));
    }
    if (row.childNodes.length) parts.push(row);
  }
  if (before.block_ids?.length) {
    parts.push(el('p', { class: 'help' }, t('admin.originalPaperBlocks', { v1: before.block_ids.slice(0, 12).join(t('common.listSeparator')) })));
  }
  return parts;
}

function openTransitionDialog(issue, target, onDone) {
  const decision = el('textarea', { rows: 2, value: issue.decision || '' });
  const evidence = el('textarea', { rows: 2, value: issue.evidence || '' });
  const after = el('textarea', {
    rows: 6,
    value: issue.after ? JSON.stringify(issue.after, null, 2) : '',
    placeholder: t('admin.stemPlainCorrectedStemOptionsLabelA'),
  });
  const dialog = el('dialog', { class: 'issue-dialog', 'aria-label': t('admin.fillInTheReviewDecision') },
    el('div', { class: 'row between' },
      el('h3', {}, `${ISSUE_STATUS_LABELS[issue.status] || issue.status} → ${ISSUE_STATUS_LABELS[target]}`),
      el('button', { type: 'button', class: 'close-dialog', 'aria-label': t('common.close'), onClick: () => dialog.close() }, '×')),
    el('p', { class: 'help' }, t('admin.questionBeforePublishingRecordTheDecisionReason', { v1: issue.qid, v2: issue.issue_type_label || '' })),
    field(t('admin.changeNoteDecisionReason'), decision),
    field(t('admin.originalPaperEvidencePageQuestionNumberOr'), evidence),
    field(t('admin.proposedChangeJsonObject'), after),
    el('div', { class: 'row end', style: 'margin-top:12px' },
      button(t('admin.submit'), event => busy(event.currentTarget, async () => {
        let afterValue = null;
        if (after.value.trim()) {
          try {
            afterValue = JSON.parse(after.value);
          } catch {
            throw new Error(t('admin.theProposedChangeMustBeAValid'));
          }
          if (typeof afterValue !== 'object' || Array.isArray(afterValue)) {
            throw new Error(t('admin.theProposedChangeMustBeAJson'));
          }
        }
        await api(`/admin/content-issues/${issue.id}/transition`, {
          method: 'POST',
          data: {
            status: target,
            decision: decision.value.trim(),
            evidence: evidence.value.trim(),
            after: afterValue,
          },
        });
        toast(t('admin.issueUpdatedTo', { v1: issue.id, v2: ISSUE_STATUS_LABELS[target] }));
        dialog.close();
        onDone();
      }), '', 'check')));
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
}

function contentIssuesCard(catalog) {
  const pageSize = 10;
  let offset = 0;
  let loadSeq = 0;
  const filters = { status: 'pending_check', subject: '', issue_type: '', risk: '' };
  const listMount = el('div');
  const totalBadge = el('span');

  const statusSel = select(
    [{ value: '', label: t('practice.allStatuses') }, ...Object.entries(ISSUE_STATUS_LABELS).map(([value, label]) => ({ value, label }))],
    filters.status,
  );
  const subjectSel = select(
    [{ value: '', label: t('insight.subjectAll') }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))],
    '',
  );
  const typeSel = select([
    { value: '', label: t('admin.allTypes') },
    { value: 'formula', label: t('question.formula') }, { value: 'stem_missing', label: t('admin.stemMissing') },
    { value: 'option', label: t('question.options') }, { value: 'image', label: t('question.image') },
    { value: 'answer', label: t('question.referenceAnswer') }, { value: 'other', label: t('insight.other') },
  ], '');
  const riskSel = select([
    { value: '', label: t('admin.allRisks') },
    { value: 'high', label: t('admin.highRisk') }, { value: 'medium', label: t('admin.mediumRisk') }, { value: 'low', label: t('admin.lowRisk') },
  ], '');

  async function toggleIsolation(issue, refresh) {
    const active = !issue.isolated;
    let reason = '';
    if (active) {
      reason = window.prompt(t('admin.describeTheIsolationBasisOriginalPaperLocation'), issue.evidence || issue.note || '');
      if (!reason || !reason.trim()) return;
    }
    await api(`/admin/questions/${encodeURIComponent(issue.qid)}/isolation`, {
      method: 'POST',
      data: { active, reason: reason.trim(), issue_id: issue.id },
    });
    toast(active ? t('admin.theQuestionIsTemporarilyIsolatedAnsweringAnd') : t('admin.isolationLifted'));
    await refresh(offset);
  }

  function render(data) {
    totalBadge.textContent = t('admin.records', { v1: data.total ?? 0 });
    const items = data.items || [];
    listMount.replaceChildren();
    if (!items.length) {
      listMount.append(el('p', { class: 'help' },
        t('admin.noQuestionIssuesUnderTheCurrentFilter')));
    }
    for (const issue of items) {
      const q = issue.question || {};
      const location = [q.year ? t('common.yearLabel', { year: q.year }) : null, q.province, q.number_label ? t('admin.originalPaperQ', { v1: q.number_label }) : null]
        .filter(Boolean).join(' · ');
      const numberText = q.number_label ? t('admin.originalPaperQ', { v1: q.number_label }) : t('admin.questionNumberPending');
      const evidenceText = String(issue.evidence || issue.note || '').trim();
      const actions = el('div', { class: 'pg-review-actions' },
        button(issue.isolated ? t('admin.liftIsolation') : t('admin.temporarilyIsolate'), event => busy(event.currentTarget, () => toggleIsolation(issue, load)),
          issue.isolated ? 'soft small' : 'danger small', issue.isolated ? 'rotate' : 'stop'),
        ...(ISSUE_NEXT[issue.status] || []).map(target => button(
          ISSUE_ACTION_LABELS[target] || ISSUE_STATUS_LABELS[target],
          () => openTransitionDialog(issue, target, () => load(offset)),
          target === 'published' ? '' : 'secondary small',
          target === 'published' ? 'check' : null,
        )),
        issue.screenshot_url
          ? button(t('admin.viewUserScreenshot'), () => showImage(issue.screenshot_url, t('admin.userIssueScreenshot')), 'ghost small', 'eye')
          : null);
      const snapshot = el('div', { class: 'pg-review-snapshot' },
        el('p', {}, t('admin.issueSnapshotMeta', { bank: issue.bank_version || t('admin.unknown'), field: issue.field_label || issue.field || '—', type: issue.issue_type_label || issue.issue_type || '—' })),
        location ? el('p', {}, t('admin.originalPaperLocation', { v1: location })) : el('p', {}, t('admin.originalPaperLocationNotFilled')),
        issue.revision
          ? el('p', {}, t('admin.revisionState', { v1: issue.revision.label })
            + (issue.revision.after_revision ? t('admin.contentRevisionProduced', { v1: issue.revision.after_revision }) : '')
            + (issue.revision.current_revision ? t('admin.currentBankContentRevision', { v1: issue.revision.current_revision }) : '')
            + (issue.revision.revision_matches === true ? t('admin.consistent')
              : issue.revision.revision_matches === false ? t('admin.inconsistentCheckWhetherALaterRevisionOverwrote') : ''))
          : null,
        ...issueBeforeSummary(issue));
      const body = el('div', { class: 'pg-review-body' },
        el('div', { class: 'pg-review-body-inner' },
          el('p', { class: 'pg-review-sub' }, t('admin.questionSnapshotEvidence')),
          snapshot,
          issue.note ? el('p', {}, issue.note) : null,
          issue.after
            ? el('details', { class: 'sources' },
              el('summary', {}, t('admin.proposedChangeConsumedByTheQuestionBank')),
              el('pre', { class: 'code-block' }, JSON.stringify(issue.after, null, 2)))
            : null,
          issue.decision ? el('p', { class: 'small-text' }, t('admin.decision', { v1: issue.decision })) : null,
          issue.evidence ? el('p', { class: 'help' }, t('admin.evidence', { v1: issue.evidence })) : null,
          el('p', { class: 'help' },
            t('admin.issueReporterLine', {
              reporter: issue.reporter ? t('admin.reporter', { v1: issue.reporter }) : t('admin.anonymousReporter'),
              reviewer: issue.reviewer ? t('admin.reviewer', { v1: issue.reviewer }) : '',
            })),
          el('p', { class: 'pg-review-sub' }, t('admin.decisionPublishConfirmation')),
          actions));
      listMount.append(el('details', { class: 'pg-review-item' },
        el('summary', { class: 'pg-review-summary' },
          el('div', { class: 'pg-review-status' },
            statusBadge(issue.status),
            badge(ISSUE_RISK_LABELS[issue.risk] || issue.risk, issue.risk === 'high' ? 'red' : issue.risk === 'medium' ? 'amber' : ''),
            issue.revision?.state === 'applied'
              ? badge(t('admin.appliedIn', { v1: issue.revision.applied_in || t('admin.publishedVersion') }), 'teal')
              : issue.revision?.state === 'pending_build'
                ? badge(t('admin.pendingBuild'), 'amber')
                : null,
            issue.revision?.revision_matches === false ? badge(t('admin.revisionMismatch'), 'red') : null,
            issue.isolated ? badge(t('question.quarantined'), 'red') : null,
            badge(issue.field_label || issue.field),
            badge(issue.issue_type_label || issue.issue_type)),
          el('div', { class: 'pg-review-id' },
            el('strong', {}, numberText),
            el('small', {}, [location, issue.qid, `#${issue.id}`, dateText(issue.created_at), issue.bank_version].filter(Boolean).join(' · '))),
          el('p', { class: 'pg-review-evidence' },
            evidenceText ? t('admin.evidence_2', { v1: evidenceText }) : t('admin.evidenceAddTheOriginalPaperLocationOr')),
          el('span', { class: 'pg-review-chevron' }, icon('chevron'))),
        body));
    }
    if (offset > 0 || data.has_more) {
      listMount.append(el('div', { class: 'row end', style: 'margin-top:12px' },
        button(t('common.prevPage'), () => load(offset - pageSize), 'ghost small', null, { disabled: offset === 0 }),
        button(t('common.nextPage'), () => load(offset + pageSize), 'ghost small', null, { disabled: !data.has_more })));
    }
  }

  async function load(nextOffset = offset) {
    const mine = ++loadSeq;
    offset = Math.max(0, nextOffset);
    filters.status = statusSel.value;
    filters.subject = subjectSel.value;
    filters.issue_type = typeSel.value;
    filters.risk = riskSel.value;
    listMount.replaceChildren(el('div', { class: 'loading' }, t('admin.loadingTheIssueQueue')));
    try {
      const data = await api(`/admin/content-issues?${query({ ...filters, offset, limit: pageSize })}`,
        { latest: true, latestKey: 'admin-content-issues' });
      if (mine !== loadSeq) return; // 旧筛选响应丢弃，避免覆盖新结果
      render(data);
    } catch (error) {
      if (mine !== loadSeq || error.aborted) return;
      listMount.replaceChildren(notice(error.message, 'warning'),
        button(t('common.retry'), () => load(offset), 'secondary small', 'repeat'));
    }
  }

  const revisionLine = el('p', { class: 'help', style: 'margin-bottom:12px' },
    t('admin.revisionStateReviewedThisRecordPendingBuild'));
  for (const control of [statusSel, subjectSel, typeSel, riskSel]) {
    control.addEventListener('change', () => load(0));
  }

  const card = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, t('admin.contentIssueReviewCorrection')),
      totalBadge),
    el('p', { class: 'help', style: 'margin-bottom:8px' },
      t('admin.userReportsCarryTheBankVersionQuestion')),
    revisionLine,
    el('div', { class: 'pg-admin-filters' },
      statusSel, subjectSel, typeSel, riskSel),
    el('div', { class: 'pg-actions', style: 'margin-bottom:12px' },
      button(t('admin.exportPublishedRevisions'), () => window.open('/api/admin/content-issues/export?status=published', '_blank'),
        'secondary small', 'download')),
    listMount);
  load(0);
  return card;
}

const HEALTH_LABELS = {
  get app_db() { return t('admin.health.appDb'); },
  get bank() { return t('admin.health.bank'); },
  get storage() { return t('admin.health.storage'); },
  get redis() { return t('admin.health.redis'); },
  get worker() { return t('admin.health.worker'); },
};

function readinessCard(healthRes) {
  if (healthRes.error) {
    return el('section', { class: 'card', style: 'margin-top:22px' },
      el('div', { class: 'card-header' }, el('h2', {}, t('admin.serviceReadiness')), badge(t('admin.unavailable'), 'red')),
      notice(t('admin.failedToReadReadinessChecks', { v1: healthRes.error }), 'warning'));
  }
  const checks = Object.entries(healthRes.checks || {});
  return el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, t('admin.serviceReadiness')),
      badge(healthRes.status === 'ok' ? t('settings.embeddingReady') : t('admin.degraded'), healthRes.status === 'ok' ? 'teal' : 'amber')),
    el('p', { class: 'help', style: 'margin-bottom:12px' },
      t('admin.healthMeta', { time: dateText(healthRes.generated_at), bank: healthRes.checks?.bank?.bank_version || t('admin.unknown') })
      + t('admin.healthDatabase', { v1: healthRes.checks?.app_db?.engine || t('admin.unknown') })),
    healthRes.status === 'ok'
      ? notice(t('admin.allConfiguredDependenciesAreHealthy'))
      : notice(t('admin.degraded', { v1: (healthRes.affected_features || []).join(t('common.listSeparator')) || t('admin.relatedFeatures') })
        + ' ' + t('admin.restoreDependenciesThenRecheck'), 'warning'),
    el('div', { class: 'mini-list' }, ...checks.map(([name, check]) => el('div', { class: 'mini-item' },
      el('div', { class: 'mini-item-content' },
        el('div', { class: 'row' },
          el('strong', {}, HEALTH_LABELS[name] || name),
          badge(check.configured === false ? t('settings.embeddingStatusUnconfigured') : check.ok ? t('admin.ok') : t('admin.error'),
            check.configured === false ? '' : check.ok ? 'teal' : 'red')),
        el('p', {}, check.detail || ''))))));
}

// ---------------------------------------------------------------------------
// 分区卡片：每个卡片自带局部刷新方法，操作后只重读本卡片依赖的接口。
// ---------------------------------------------------------------------------

function knowledgeCard(catalog) {
  let kps = state.knowledgePoints || [];
  let refreshSeq = 0;
  const kpSubjSel = select((catalog.subjects || []).map(s => ({ value: s, label: s })), catalog.subjects?.[0] || '数学');
  const kpTreeMount = el('div', { style: 'max-height:320px;overflow:auto;margin-top:12px' });
  const renderTree = () => kpTreeMount.replaceChildren(renderKpTree(kps, { subject: kpSubjSel.value }));
  kpSubjSel.addEventListener('change', renderTree);
  renderTree();

  const newKpName = input(t('admin.newKnowledgePointNameEGImplicit'));
  const newKpLvl = select([{ value: '1', label: t('admin.level1Chapter') }, { value: '2', label: t('admin.level2Section') }, { value: '3', label: t('admin.level3KnowledgePoint') }], '3');
  const importInput = el('input', { type: 'file', accept: '.json,.csv', style: 'display:none' });

  async function refreshTree() {
    const mine = ++refreshSeq;
    try {
      const items = await ensureKnowledgePoints(true);
      if (mine !== refreshSeq) return;
      kps = items;
      renderTree();
    } catch (error) {
      if (mine === refreshSeq) toast(t('admin.failedToRefreshKnowledgePoints', { v1: error.message }), true);
    }
  }

  importInput.addEventListener('change', async () => {
    const f = importInput.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.set('file', f);
    try {
      const res = await api('/admin/knowledge-points/import', { method: 'POST', body: fd });
      toast(t('admin.importedNewKnowledgePointNodes', { v1: res.imported }));
      await refreshTree();
    } catch (err) { toast(err.message, true); }
    finally { importInput.value = ''; }
  });

  return el('section', { class: 'card' },
    el('div', { class: 'card-header' }, el('h2', {}, t('admin.knowledgePointHierarchyChapterSectionKnowledgePoint')), badge(t('admin.builtInCurriculumTreeForNineSubjects'), 'teal')),
    el('div', { class: 'row between' },
      el('div', { class: 'row' }, kpSubjSel,
        button(t('admin.importCsvJsonOutline'), () => importInput.click(), 'secondary small', 'upload'), importInput),
      button(t('admin.manageOfficialKnowledgeLists'), () => go('notes'), 'ghost small', 'notes')),
    el('div', { class: 'row', style: 'margin-top:12px' },
      el('div', { style: 'flex:1' }, newKpName),
      el('div', { style: 'width:150px' }, newKpLvl),
      button(t('admin.addNode'), e => busy(e.currentTarget, async () => {
        if (!newKpName.value.trim()) return;
        await api('/admin/knowledge-points', {
          method: 'POST', data: { subject: kpSubjSel.value, name: newKpName.value.trim(), level: Number(newKpLvl.value) },
        });
        toast(t('admin.knowledgePointNodeAdded'));
        newKpName.value = '';
        await refreshTree();
      }), 'soft small', 'plus')),
    kpTreeMount);
}

function taggingCard(catalog, onJobComplete) {
  const tagSubj = select((catalog.subjects || []).map(s => ({ value: s, label: s })), '数学');
  const tagLimit = input(t('admin.questionsPerRunEG50'), { type: 'number', value: 50, min: 5, max: 500 });
  const tagPicker = createModelPicker('tagging', { showModeToggle: false });
  const tagEstMount = el('div');
  const tagJobMount = el('div');
  return el('section', { class: 'card' },
    el('div', { class: 'card-header' }, el('h2', {}, t('admin.aiBatchKnowledgePointDifficultyTagging')), badge(t('admin.interruptibleResumable'), 'blue')),
    notice(t('admin.modelsMayOnlyPickKpIdValues')),
    el('div', { class: 'form-grid' }, field(t('admin.targetSubject'), tagSubj), field(t('admin.runLimitQuestions'), tagLimit)),
    el('div', { style: 'margin-top:12px' }, tagPicker.node),
    el('div', { class: 'row end', style: 'margin-top:14px' },
      button(t('admin.estimateVolumeCost'), e => busy(e.currentTarget, async () => {
        const est = await api('/admin/tagging/estimate', {
          method: 'POST', data: { subject: tagSubj.value, limit: Number(tagLimit.value) || 50 },
        });
        tagEstMount.replaceChildren(notice(est.notice));
      }), 'secondary small', 'chart'),
      button(t('admin.localVectorTagging'), e => busy(e.currentTarget, async () => {
        const res = await api('/admin/kp-tag-local', {
          method: 'POST',
          data: { subject: tagSubj.value, limit: Number(tagLimit.value) || 50 },
        });
        toast(res.notice || t('admin.localVectorTaggingDone'));
        onJobComplete?.();
      }), 'secondary small', 'tag'),
      button(t('admin.confirmStartingBatchTagging'), e => busy(e.currentTarget, async () => {
        const est = await api('/admin/tagging/estimate', {
          method: 'POST', data: { subject: tagSubj.value, limit: Number(tagLimit.value) || 50 },
        });
        if (!est.will_process) { toast(t('admin.noUntaggedQuestionsForThisSubject')); return; }
        if (!window.confirm(`${est.notice}\n${t('admin.confirmStartBatchTagging')}`)) return;
        const payload = tagPicker.payload(false);
        const job = await api('/ai/jobs', {
          method: 'POST',
          data: { ...payload, task: 'tagging', subject: tagSubj.value, tagging_limit: Number(tagLimit.value) || 50 },
        });
        mountJob(tagJobMount, job.id, () => { onJobComplete?.(); });
      }), '', 'sparkle')),
    tagEstMount, tagJobMount);
}

function reviewQueueCard(initialItems) {
  let seq = 0;
  const mount = el('div');
  const card = el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, t('admin.aiKnowledgePointTaggingReviewQueue')), badge(t('admin.pending', { v1: initialItems?.length || 0 }), 'amber')));
  const badgeNode = card.querySelector('.card-header .badge');

  function render(items) {
    mount.replaceChildren();
    if (!items.length) {
      mount.append(el('p', { class: 'help' }, t('admin.noAiKnowledgePointTagsAwaitingReview')));
      if (badgeNode) { badgeNode.textContent = t('admin.0Pending'); badgeNode.className = 'badge amber'; }
      return;
    }
    if (badgeNode) { badgeNode.textContent = t('admin.pending', { v1: items.length }); badgeNode.className = 'badge amber'; }
    const list = el('div', { class: 'mini-list' });
    for (const r of items) {
      list.append(el('div', { class: 'mini-item' },
        el('div', { class: 'mini-item-content' },
          el('strong', {}, t('admin.confidence', { v1: r.subject, v2: r.kp_name, v3: Math.round((r.confidence || 0) * 100) })),
          el('p', {}, r.stem_plain)),
        el('div', { class: 'row' },
          button(t('admin.approve'), e => busy(e.currentTarget, async () => {
            await api('/admin/question-kp/review', { method: 'POST', data: { qid: r.qid, kp_id: r.kp_id, action: 'approve' } });
            toast(t('admin.approved'));
            await refresh();
          }), 'soft small', 'check'),
          button(t('admin.reject'), e => busy(e.currentTarget, async () => {
            await api('/admin/question-kp/review', { method: 'POST', data: { qid: r.qid, kp_id: r.kp_id, action: 'reject' } });
            toast(t('admin.aiTagRemoved'));
            await refresh();
          }), 'danger small', 'trash'))));
    }
    mount.append(list);
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await api('/admin/kp-review-queue?limit=30', { latest: true, latestKey: 'admin-kp-review-queue' });
      if (mine !== seq) return;
      render(data.items || []);
    } catch (error) {
      if (mine === seq && !error.aborted) mount.replaceChildren(notice(t('admin.failedToRefreshTheReviewQueue', { v1: error.message }), 'warning'));
    }
  }

  render(initialItems || []);
  card.append(mount);
  return { card, refresh };
}

function hostsCard(initial) {
  let seq = 0;
  const hostInput = input(t('admin.eGApiMyGatewayExampleCom'));
  const customMount = el('div');
  const helpLine = el('p', { class: 'help', style: 'margin-bottom:12px' });
  let presets = initial?.presets || [];

  function render(data) {
    presets = data.presets?.length ? data.presets : presets;
    helpLine.textContent = data.error
      ? t('admin.failedToReadTheAllowlist', { v1: data.error })
      : t('admin.builtInSupportCustomDomainsAddedHere', { v1: presets.join('、') });
    customMount.replaceChildren();
    const custom = data.custom || [];
    if (!custom.length) {
      customMount.append(el('p', { class: 'help', style: 'margin-top:10px' },
        data.error ? t('admin.youCanRetryRefreshingLater') : t('admin.noCustomDomainsAddedYet')));
      return;
    }
    const customList = el('div', { class: 'row', style: 'margin-top:12px' });
    for (const h of custom) {
      customList.append(el('div', { class: 'tpl-chip' },
        el('span', { style: 'padding:4px 8px;font-size:.8125rem' }, h.hostname),
        el('button', {
          type: 'button', class: 'tpl-del-btn', title: t('admin.removeDomain'),
          onClick: async () => {
            await api(`/admin/allowed-hosts/${encodeURIComponent(h.hostname)}`, { method: 'DELETE' });
            toast(t('admin.removedAllowlistDomain', { v1: h.hostname }));
            await refresh();
          },
        }, '×')));
    }
    customMount.append(customList);
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await api('/admin/allowed-hosts', { latest: true, latestKey: 'admin-allowed-hosts' });
      if (mine !== seq) return;
      render(data);
    } catch (error) {
      if (mine === seq && !error.aborted) render({ error: error.message, presets: [], custom: [] });
    }
  }

  render(initial || { presets: [], custom: [] });
  return el('section', { class: 'card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' }, el('h2', {}, t('admin.customModelApiDomainAllowlist')), badge(t('admin.httpsBlocksPrivateIps'), 'teal')),
    helpLine,
    el('div', { class: 'row' },
      el('div', { style: 'flex:1' }, hostInput),
      button(t('admin.addDomain'), e => busy(e.currentTarget, async () => {
        if (!hostInput.value.trim()) return;
        await api('/admin/allowed-hosts', { method: 'POST', data: { hostname: hostInput.value.trim() } });
        toast(t('admin.addedToTheCustomDomainAllowlist'));
        hostInput.value = '';
        await refresh();
      }), 'secondary small', 'plus')),
    customMount);
}

function registrationCard(initial) {
  let seq = 0;
  const card = el('section', { class: 'card', style: 'margin-top:22px' });
  const mount = el('div');

  function render(data) {
    card.replaceChildren(
      el('div', { class: 'card-header' },
        el('h2', {}, t('admin.accountsRegistrationPolicy')),
        badge(data.public_mode ? t('admin.publicMode') : t('admin.localMode'), data.public_mode ? 'amber' : 'teal')),
      mount);
    if (data.error) {
      mount.replaceChildren(notice(t('admin.failedToReadRegistrationPolicy', { v1: data.error }), 'warning'));
      return;
    }
    mount.replaceChildren(
      el('p', { class: 'help', style: 'margin-bottom:12px' },
        t('admin.registrationSummary', {
          state: data.mode === 'open' ? t('admin.open') : t('admin.closed'),
          override: data.env_override ? t('admin.envOverride') : '',
          adminReady: data.admin_ready ? t('admin.readyState') : t('admin.notInitialized'),
          initConfigured: data.admin_init_configured ? t('admin.configured') : t('admin.notConfigured'),
        })),
      el('div', { class: 'row end' },
        button(t('admin.openRegistration'), e => busy(e.currentTarget, async () => {
          await api('/auth/admin/registration', { method: 'POST', data: { mode: 'open' } });
          toast(t('admin.registrationIsOpenNewAccountsDefaultTo'));
          await refresh();
        }), 'soft small', 'check', { disabled: data.mode === 'open' }),
        button(t('admin.closeRegistration'), e => busy(e.currentTarget, async () => {
          await api('/auth/admin/registration', { method: 'POST', data: { mode: 'closed' } });
          toast(t('admin.registrationClosedExistingAccountsAreUnaffected'));
          await refresh();
        }), 'secondary small', 'lock', { disabled: data.mode === 'closed' })),
      !data.admin_ready
        ? notice(t('admin.noAdminAccountAvailableYetForPublic'), 'warning')
        : null);
  }

  async function refresh() {
    const mine = ++seq;
    try {
      const data = await api('/auth/admin/registration', { latest: true, latestKey: 'admin-registration' });
      if (mine !== seq) return;
      render(data);
    } catch (error) {
      if (mine === seq && !error.aborted) render({ error: error.message });
    }
  }

  render(initial || { error: t('admin.registrationPolicyNotLoadedYet') });
  return card;
}

// ---------------------------------------------------------------------------
// AI 用量与配额（WS23）：全站汇总卡片 + 按任务类型表 + 每人每日配额 / 全站并发调整。
// 数据来自 GET /api/admin/ai/usage 与 GET /api/admin/ai/quotas；接口未就绪时只降级为提示，
// 不影响其它管理分区。
// ---------------------------------------------------------------------------

function aiUsageDurationText(ms) {
  const value = Number(ms);
  if (ms === null || ms === undefined || !Number.isFinite(value) || value < 0) return '—';
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.floor(value / 60_000)} min ${Math.round((value % 60_000) / 1000)} s`;
}

function aiUsageRateText(rate) {
  const value = Number(rate);
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—';
}

function aiUsageMoneyText(micro) {
  const value = Number(micro);
  if (micro === null || micro === undefined || !Number.isFinite(value)) return null;
  return (value / 1_000_000).toFixed(4);
}

function aiUsageKindLabel(kind, fallback = '') {
  const key = `tasks.task.${kind}`;
  const translated = t(key);
  return translated !== key ? translated : (fallback || kind || '—');
}

function aiUsageSourceLabel(source) {
  const key = `admin.aiUsage.source.${source}`;
  const translated = t(key);
  return translated !== key ? translated : String(source || '—');
}

function adminAiUsageCard() {
  const content = el('div');
  const state = { usage: null, quotas: null, seq: 0 };

  function render() {
    const usage = state.usage || {};
    const quotas = state.quotas || {};
    if (usage.error && quotas.error) {
      content.replaceChildren(
        notice(t('admin.aiUsage.unavailable', { message: usage.error }), 'warning'),
        el('div', { class: 'row', style: 'margin-top:12px' },
          button(t('admin.reloadSection'), e => busy(e.currentTarget, load), 'secondary small', 'repeat')));
      return;
    }
    const totals = usage.totals || {};
    const costText = aiUsageMoneyText(totals.cost_micro);
    const summary = el('div', { class: 'settings-usage-summary', 'aria-live': 'polite' },
      el('span', {}, t('admin.aiUsage.totalJobs', { count: number(totals.jobs || 0) })),
      el('span', {}, t('admin.aiUsage.failed', { rate: aiUsageRateText(totals.failure_rate) })),
      el('span', {}, t('admin.aiUsage.p50', { duration: aiUsageDurationText(totals.duration_p50_ms) })),
      el('span', {}, t('admin.aiUsage.p95', { duration: aiUsageDurationText(totals.duration_p95_ms) })),
      el('span', {}, t('admin.aiUsage.tokens', { input: number(totals.tokens_in || 0), output: number(totals.tokens_out || 0) })),
      el('span', { class: 'settings-usage-cost' },
        costText === null ? t('admin.aiUsage.costUnknown') : t('admin.aiUsage.cost', { amount: costText })));

    const usageRows = Array.isArray(usage.by_kind) ? usage.by_kind : [];
    const usageTable = el('div', { class: 'table-wrap', style: 'margin-top:12px' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, t('admin.aiUsage.headKind')),
          el('th', {}, t('admin.aiUsage.headJobs')),
          el('th', {}, t('admin.aiUsage.headFailed')),
          el('th', {}, t('admin.aiUsage.headP50')),
          el('th', {}, t('admin.aiUsage.headP95')),
          el('th', {}, t('admin.aiUsage.headTokens')),
          el('th', {}, t('admin.aiUsage.headCost')))),
        el('tbody', {}, ...usageRows.map(row => el('tr', {},
          el('td', {}, aiUsageKindLabel(row.kind)),
          el('td', {}, number(row.jobs || 0)),
          el('td', {}, number(row.failed || 0)),
          el('td', {}, aiUsageDurationText(row.duration_p50_ms)),
          el('td', {}, aiUsageDurationText(row.duration_p95_ms)),
          el('td', {}, t('admin.aiUsage.tokenPair', {
            input: number(row.tokens_in || 0), output: number(row.tokens_out || 0),
          })),
          el('td', { class: 'settings-usage-cost' },
            aiUsageMoneyText(row.cost_micro) === null
              ? t('tasks.priceUnset')
              : `¥ ${aiUsageMoneyText(row.cost_micro)}`))))));

    const quotaRows = Array.isArray(quotas.items) ? quotas.items : [];
    function saveQuota(kind, dailyNode, concurrencyNode) {
      const payload = { kind };
      const dailyText = String(dailyNode.value ?? '').trim();
      const concurrencyText = String(concurrencyNode.value ?? '').trim();
      if (dailyText !== '') payload.daily_limit = Number(dailyText);
      if (concurrencyText !== '') payload.concurrency_limit = Number(concurrencyText);
      if (payload.daily_limit === undefined && payload.concurrency_limit === undefined) {
        throw new Error(t('admin.aiUsage.needValue'));
      }
      return api('/admin/ai/quotas', { method: 'POST', data: payload }).then(async res => {
        toast(t('admin.aiUsage.saved', { kind: aiUsageKindLabel(kind) }));
        await load();
        return res;
      });
    }
    function resetQuota(kind) {
      return api('/admin/ai/quotas', { method: 'POST', data: { kind, reset: true } }).then(async res => {
        toast(t('admin.aiUsage.resetDone', { kind: aiUsageKindLabel(kind) }));
        await load();
        return res;
      });
    }
    const quotaTable = el('div', { class: 'table-wrap', style: 'margin-top:12px' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, t('admin.aiUsage.headQuotaKind')),
          el('th', {}, t('admin.aiUsage.headUsedRunning')),
          el('th', {}, t('admin.aiUsage.dailyLimit')),
          el('th', {}, t('admin.aiUsage.concurrencyLimit')),
          el('th', {}, t('admin.aiUsage.headActions')))),
        el('tbody', {}, ...quotaRows.map(entry => {
          const label = aiUsageKindLabel(entry.kind, entry.label);
          const dailyNode = input('', {
            type: 'number', min: '0', step: '1', value: entry.daily_limit ?? '',
            'data-quota': 'daily', 'aria-label': t('admin.aiUsage.dailyLimitAria', { kind: label }),
          });
          const concurrencyNode = input('', {
            type: 'number', min: '0', step: '1', value: entry.concurrency_limit ?? '',
            'data-quota': 'concurrency', 'aria-label': t('admin.aiUsage.concurrencyAria', { kind: label }),
          });
          return el('tr', { 'data-quota-kind': entry.kind },
            el('td', {},
              el('strong', {}, label),
              el('div', { class: 'settings-usage-connect' }, entry.kind)),
            el('td', {},
              t('admin.aiUsage.usedRunning', {
                used: number(entry.used_today || 0), running: number(entry.running || 0),
              }),
              el('div', { class: 'help' }, entry.remaining_today === null || entry.remaining_today === undefined
                ? t('admin.aiUsage.unlimited')
                : t('admin.aiUsage.remaining', { count: number(entry.remaining_today) }))),
            el('td', {},
              dailyNode,
              el('div', { class: 'help' }, aiUsageSourceLabel(entry.daily_source))),
            el('td', {},
              concurrencyNode,
              el('div', { class: 'help' }, aiUsageSourceLabel(entry.concurrency_source))),
            el('td', {}, el('div', { class: 'row' },
              button(t('admin.aiUsage.save'), e => busy(e.currentTarget,
                () => saveQuota(entry.kind, dailyNode, concurrencyNode)), 'soft small', 'check'),
              button(t('admin.aiUsage.reset'), e => busy(e.currentTarget,
                () => resetQuota(entry.kind)), 'ghost small', 'repeat'))));
        }))));

    content.replaceChildren(
      summary,
      el('h3', { style: 'margin-top:16px' }, t('admin.aiUsage.byKind')),
      usageRows.length ? usageTable : el('p', { class: 'help' }, t('admin.aiUsage.noData')),
      el('h3', { style: 'margin-top:16px' }, t('admin.aiUsage.quotaTitle')),
      el('p', { class: 'help' }, (quotas.notice || '') + ' ' + t('admin.aiUsage.timezoneNote')),
      quotaRows.length ? quotaTable : el('p', { class: 'help' }, t('admin.aiUsage.noData')));
  }

  async function load() {
    const mine = ++state.seq;
    content.replaceChildren(loading(t('admin.aiUsage.loading')));
    const [usageRes, quotaRes] = await Promise.all([
      api('/admin/ai/usage?days=14').catch(error => ({ error: error.message })),
      api('/admin/ai/quotas').catch(error => ({ error: error.message })),
    ]);
    if (mine !== state.seq) return;
    state.usage = usageRes;
    state.quotas = quotaRes;
    render();
  }

  const card = el('section', { class: 'card admin-ai-usage-card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, t('admin.aiUsage.section')),
      el('div', { class: 'row' },
        badge(t('admin.aiUsage.badge', { days: 14 })),
        button(t('admin.aiUsage.refresh'), e => busy(e.currentTarget, load), 'secondary small', 'repeat'))),
    content);
  load();
  return card;
}

// ---------------------------------------------------------------------------
// 运维监控（WS27 / 方案 §7「分发与运维」）：GET /api/admin/monitor 的
// 任务 / AI / 渲染 / 数据 / 广场五组只读检查。单组异常由后端标 warn/error 并在组内提示；
// 接口整体不可用时只降级为提示与重试按钮，不影响其它管理分区；刷新按钮显式绕过服务端短缓存。
// ---------------------------------------------------------------------------

const MONITOR_GROUP_KEYS = ['tasks', 'ai', 'media', 'data', 'plaza'];

const MONITOR_STATUS_META = {
  ok: { label: 'admin.monitor.status.ok', tone: 'teal' },
  warn: { label: 'admin.monitor.status.warn', tone: 'amber' },
  error: { label: 'admin.monitor.status.error', tone: 'red' },
};

function monitorStatusBadge(status) {
  const meta = MONITOR_STATUS_META[status] || MONITOR_STATUS_META.warn;
  return badge(t(meta.label), meta.tone);
}

function monitorGroupTitle(key) {
  return t(`admin.monitor.group.${key}`);
}

function monitorDurationText(seconds) {
  const value = Number(seconds);
  if (seconds === null || seconds === undefined || !Number.isFinite(value) || value < 0) return '—';
  if (value < 60) return t('admin.monitor.duration.seconds', { v1: Math.round(value) });
  if (value < 3600) return t('admin.monitor.duration.minutes', { v1: Math.round(value / 6) / 10 });
  if (value < 86_400) return t('admin.monitor.duration.hours', { v1: Math.round(value / 360) / 10 });
  return t('admin.monitor.duration.days', { v1: Math.round(value / 864) / 10 });
}

function monitorBytesText(bytes) {
  const value = Number(bytes);
  if (bytes === null || bytes === undefined || !Number.isFinite(value) || value < 0) return '—';
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function monitorMetricLine(parts) {
  const items = parts.filter(Boolean);
  if (!items.length) return null;
  return el('div', { class: 'settings-usage-summary', style: 'margin-top:8px' },
    ...items.map(part => el('span', {}, part)));
}

function monitorKindTable(rows) {
  const items = (Array.isArray(rows) ? rows : []).slice(0, 8);
  if (!items.length) return null;
  return el('div', { class: 'table-wrap', style: 'margin-top:10px' },
    el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, t('admin.monitor.tasks.headKind')),
        el('th', {}, t('admin.monitor.tasks.headQueued')),
        el('th', {}, t('admin.monitor.tasks.headRunning')),
        el('th', {}, t('admin.monitor.tasks.headFailed')),
        el('th', {}, t('admin.monitor.tasks.headOldest')))),
      el('tbody', {}, ...items.map(row => el('tr', {},
        el('td', {}, row.label || aiUsageKindLabel(row.kind)),
        el('td', {}, number(row.queued || 0)),
        el('td', {}, number((row.running || 0) + (row.cancel_requested || 0))),
        el('td', {}, number((row.failed || 0) + (row.interrupted || 0))),
        el('td', {}, monitorDurationText(row.oldest_queued_seconds)))))));
}

function monitorTasksExtras(check) {
  const totals = check.totals || {};
  return [
    monitorMetricLine([
      t('admin.monitor.tasks.queue', { count: number(totals.queued || 0) }),
      t('admin.monitor.tasks.running', { count: number((totals.running || 0) + (totals.cancel_requested || 0)) }),
      t('admin.monitor.tasks.failed', { count: number(totals.failed || 0) }),
      t('admin.monitor.tasks.reclaimed', { count: number(check.reclaimed || 0) }),
      t('admin.monitor.tasks.oldestQueue', { value: monitorDurationText(check.oldest_queued_seconds) }),
      t('admin.monitor.tasks.timeout', { count: number(check.timed_out || 0) }),
    ]),
    monitorKindTable(check.by_kind),
  ];
}

function monitorAiExtras(check) {
  const totals = check.totals || {};
  const ocr = check.ocr || {};
  const costText = aiUsageMoneyText(totals.cost_micro);
  return [
    monitorMetricLine([
      t('admin.monitor.ai.window', { days: number(check.window_days || 0) }),
      t('admin.monitor.ai.jobs', { count: number(totals.jobs || 0) }),
      t('admin.monitor.ai.failed', { rate: aiUsageRateText(totals.failure_rate) }),
      t('admin.monitor.ai.tokens', {
        input: number(totals.tokens_in || 0), output: number(totals.tokens_out || 0),
      }),
      costText === null ? t('admin.monitor.ai.costUnknown') : t('admin.monitor.ai.cost', { amount: costText }),
      t('admin.monitor.ai.modelCalls', {
        count: check.model_calls === null || check.model_calls === undefined ? '—' : number(check.model_calls),
      }),
    ]),
    monitorMetricLine([
      t('admin.monitor.ai.ocr', {
        jobs: number(ocr.jobs || 0),
        p50: aiUsageDurationText(ocr.duration_p50_ms),
        p95: aiUsageDurationText(ocr.duration_p95_ms),
        rate: aiUsageRateText(ocr.failure_rate),
      }),
    ]),
  ];
}

function monitorMediaExtras(check) {
  const totals = check.totals || {};
  const caps = check.capabilities || {};
  const assets = check.assets || {};
  const courses = check.courses || {};
  const assetKinds = assets.by_kind || {};
  return [
    monitorMetricLine([
      t('admin.monitor.media.line', {
        queue: number(totals.queued || 0),
        running: number((totals.running || 0) + (totals.cancel_requested || 0)),
        failed: number((totals.failed || 0) + (totals.interrupted || 0)),
        oldest: monitorDurationText(check.oldest_queued_seconds),
      }),
      t('admin.monitor.media.capability', {
        pdf: caps.pdf?.available ? t('admin.monitor.capability.yes') : t('admin.monitor.capability.no'),
        video: caps.video?.available ? t('admin.monitor.capability.yes') : t('admin.monitor.capability.no'),
      }),
      t('admin.monitor.media.assets', {
        video: number(assetKinds.video || 0),
        tts: number(assetKinds.tts || 0),
        recent: number(assets.recent || 0),
      }),
      t('admin.monitor.media.courses', {
        failed: number(courses.failed || 0),
        generating: number((courses.generating || 0) + (courses.outline_ready || 0)),
        ready: number(courses.ready || 0),
      }),
    ]),
    monitorKindTable(check.by_kind),
  ];
}

function monitorDataExtras(check) {
  const info = check.database || {};
  const bank = check.bank || {};
  const backups = check.backups || {};
  const latest = backups.latest;
  return [
    monitorMetricLine([
      t('admin.monitor.data.database', {
        engine: info.engine || '—',
        size: monitorBytesText(info.size_bytes),
        wal: monitorBytesText(info.wal_bytes),
        wait: aiUsageDurationText(info.wait_ms),
      }),
      t('admin.monitor.data.bank', {
        version: bank.version || '—',
        questions: bank.questions === null || bank.questions === undefined ? '—' : number(bank.questions),
      }),
    ]),
    latest
      ? monitorMetricLine([
        t('admin.monitor.data.backup', {
          name: latest.name || '—',
          age: monitorDurationText(latest.age_seconds),
          files: number(latest.file_count || 0),
        }),
      ])
      : notice(t('admin.monitor.data.noBackup'), ''),
  ];
}

function monitorPlazaExtras(check) {
  if (!check.initialized) return [notice(t('admin.monitor.plaza.uninitialized'), '')];
  const feedback = check.feedback || {};
  const withdrawn = check.withdrawn || {};
  return [
    monitorMetricLine([
      t('admin.monitor.plaza.feedback', {
        issues: number(feedback.issues || 0),
        reports: number(feedback.reports || 0),
        appeals: number(feedback.appeals || 0),
        total: number(feedback.total_pending || 0),
      }),
      t('admin.monitor.plaza.withdrawn', {
        checked: number(withdrawn.checked || 0),
        leaked: number(withdrawn.leaked || 0),
        orphan: number(withdrawn.orphan_active || 0),
      }),
    ]),
  ];
}

const MONITOR_EXTRAS = {
  tasks: monitorTasksExtras,
  ai: monitorAiExtras,
  media: monitorMediaExtras,
  data: monitorDataExtras,
  plaza: monitorPlazaExtras,
};

function monitorGroupNode(key, check) {
  const alerts = Array.isArray(check.alerts) ? check.alerts : [];
  const extraFn = MONITOR_EXTRAS[key];
  const extras = extraFn ? extraFn(check) : [];
  return el('div', { class: 'mini-item' },
    el('div', { class: 'mini-item-content' },
      el('div', { class: 'row between' },
        el('strong', {}, monitorGroupTitle(key)),
        monitorStatusBadge(check.status)),
      el('p', { class: 'help' }, check.detail || ''),
      ...extras.filter(Boolean),
      ...alerts.slice(0, 8).map(alert => notice(alert, check.status === 'error' ? 'error' : 'warning'))));
}

function adminMonitorCard() {
  const content = el('div');
  const state = { payload: null, error: null, seq: 0 };

  function renderFailure() {
    content.replaceChildren(
      notice(t('admin.monitor.unavailable', { message: state.error }), 'warning'),
      el('div', { class: 'row', style: 'margin-top:12px' },
        button(t('admin.monitor.reload'), e => busy(e.currentTarget, () => load(true)),
          'secondary small', 'repeat')));
  }

  function render() {
    if (state.error) {
      renderFailure();
      return;
    }
    const payload = state.payload || {};
    const checks = payload.checks || {};
    const degraded = Array.isArray(payload.degraded) ? payload.degraded : [];
    const summary = el('div', { class: 'settings-usage-summary' },
      el('span', {}, t('admin.monitor.generatedAt', { time: dateText(payload.generated_at) })),
      el('span', {}, t('admin.monitor.duration', { value: aiUsageDurationText(payload.duration_ms) })),
      el('span', {}, t('admin.monitor.build', {
        build: payload.build?.build_id || '—',
        bank: payload.build?.bank_version || '—',
      })),
      el('span', {}, payload.cache?.cached
        ? t('admin.monitor.cache.hit', { seconds: Number(payload.cache.age_seconds || 0).toFixed(1) })
        : t('admin.monitor.cache.miss')),
      degraded.length
        ? el('span', { class: 'settings-usage-warn' },
          t('admin.monitor.degradedGroups', {
            names: degraded.map(monitorGroupTitle).join(t('common.listSeparator')),
          }))
        : null);
    const groups = MONITOR_GROUP_KEYS.map(key => monitorGroupNode(
      key, checks[key] || { status: 'warn', detail: '', alerts: [] }));
    content.replaceChildren(summary, el('div', { class: 'mini-list', style: 'margin-top:6px' }, ...groups));
  }

  async function load(refresh = false) {
    const mine = ++state.seq;
    content.replaceChildren(loading(t('admin.monitor.loading')));
    const res = await api(`/admin/monitor${refresh ? '?refresh=1' : ''}`)
      .catch(error => ({ error: error.message }));
    if (mine !== state.seq) return;
    if (res.error) {
      state.error = res.error;
      state.payload = null;
    } else {
      state.error = null;
      state.payload = res;
    }
    render();
  }

  const card = el('section', { class: 'card admin-monitor-card', style: 'margin-top:22px' },
    el('div', { class: 'card-header' },
      el('h2', {}, t('admin.monitor.section')),
      el('div', { class: 'row' },
        badge(t('admin.monitor.badge')),
        button(t('admin.monitor.refresh'), e => busy(e.currentTarget, () => load(true)),
          'secondary small', 'repeat'))),
    content);
  load();
  return card;
}

// ---------------------------------------------------------------------------
// 内容管理主页面：三个分区按 Tab 懒加载，切换后缓存；操作只做卡片级局部刷新。
// ---------------------------------------------------------------------------
export async function renderAdmin(container, version) {
  const catalogPromise = ensureCatalog();
  const panelEls = {
    issues: el('section', { class: 'pg-panel', role: 'tabpanel', id: 'admin-panel-issues', 'aria-labelledby': 'admin-tab-issues' }),
    content: el('section', { class: 'pg-panel', role: 'tabpanel', id: 'admin-panel-content', 'aria-labelledby': 'admin-tab-content' }),
    ops: el('section', { class: 'pg-panel', role: 'tabpanel', id: 'admin-panel-ops', 'aria-labelledby': 'admin-tab-ops' }),
  };
  // 每个分区只加载一次并缓存 DOM；序号用于丢弃切换期间的旧响应。
  const panelState = {
    issues: { loaded: false, seq: 0 },
    content: { loaded: false, seq: 0 },
    ops: { loaded: false, seq: 0 },
  };

  function panelFailure(key, error) {
    const panel = panelEls[key];
    panelState[key].loaded = false;
    panel.replaceChildren(
      notice(t('admin.failedToLoadTheSection', { v1: error.message }), 'warning'),
      el('div', { class: 'row', style: 'margin-top:12px' },
        button(t('admin.reloadSection'), () => loadPanel(key), 'secondary small', 'repeat')));
  }

  async function loadIssues() {
    const panel = panelEls.issues;
    const state = panelState.issues;
    const mine = ++state.seq;
    panel.replaceChildren(loading(t('admin.loadingTheReviewQueue')));
    const catalog = await catalogPromise;
    if (!current(version) || mine !== state.seq) return;
    panel.replaceChildren(contentIssuesCard(catalog));
    state.loaded = true;
  }

  async function loadContent() {
    const panel = panelEls.content;
    const state = panelState.content;
    const mine = ++state.seq;
    panel.replaceChildren(loading(t('admin.loadingTheQuestionBankKnowledgePoints')));
    const [overview, catalog, kps, reviewQueue, templatesRes, communityRes, gridSpec] = await Promise.all([
      api('/admin/overview'),
      catalogPromise,
      ensureKnowledgePoints(true),
      api('/admin/kp-review-queue?limit=30').catch(() => ({ items: [] })),
      api('/admin/prompt-templates').catch(err => ({ error: err.message })),
      api('/admin/community-explanations').catch(err => ({ error: err.message })),
      ensurePromptGrid().catch(() => ({})),
    ]);
    if (!current(version) || mine !== state.seq) return;
    const counts = overview.counts || overview;
    const metrics = el('div', { class: 'stats-grid' },
      statCard(t('admin.sourceBank'), number(catalog.source_total || catalog.total), t('insight.unit.questions'), t('admin.theOriginalBankStaysReadOnly'), 'book'),
      statCard(t('admin.openForPractice'), number(catalog.available_total || catalog.total), t('insight.unit.questions'), t('admin.excludesContentFlaggedQuestions'), 'check'),
      statCard(t('admin.knowledgePointNodes'), number(kps.length), t('insight.unit.items'), t('admin.coversTheCurriculumTreesOfNineSubjects'), 'tag'),
      statCard(t('admin.registeredUsers'), number(counts.users || counts.user_count), t('admission.rank'), t('admin.accountDataIsIsolated'), 'chart'));
    const review = reviewQueueCard(reviewQueue.items || []);
    const tagging = taggingCard(catalog, () => { review.refresh(); });
    const templateCard = sharedReviewCard(t('admin.sharedPromptTemplateReview'), '/admin/prompt-templates', templatesRes, (tpl, refresh) =>
      el('div', { class: 'mini-item' },
        el('div', { class: 'mini-item-content' },
          el('strong', {}, tpl.name),
          el('p', {}, describeTemplateConfig(tpl.config, gridSpec.dimensions || {})),
          tpl.custom_requirements ? el('p', {}, tpl.custom_requirements) : null),
        button(tpl.is_recommended ? t('admin.removeRecommendation') : t('admin.recommend'), e => busy(e.currentTarget, async () => {
          await api(`/prompt-templates/${tpl.id}/recommend`, {
            method: 'POST', data: { recommended: !tpl.is_recommended },
          });
          await refresh(0);
        }), tpl.is_recommended ? 'ghost small' : 'soft small', 'check')),
      t('admin.noPubliclySharedTemplates'));

    const communityCard = (communityRes.enabled || communityRes.error) ? sharedReviewCard(
      t('admin.communityExplanationReview'), '/admin/community-explanations', communityRes, (exp, refresh) =>
        el('div', { class: 'mini-item' },
          el('div', { class: 'mini-item-content' },
            el('details', { class: 'sources' },
              el('summary', {}, `${exp.qid} · ${dateText(exp.created_at)} · ${exp.models.map(m => m.model).join(' + ')}`),
              markdown(exp.content))),
          button(exp.featured ? t('admin.removeFromFeatured') : t('admin.featureOnPlatform'), e => busy(e.currentTarget, async () => {
            await api(`/admin/community-explanations/${exp.id}/feature`, { method: 'PUT', data: { featured: !exp.featured } });
            await refresh(0);
          }), exp.featured ? 'ghost small' : 'soft small', 'check')),
      t('admin.noCommunityExplanations')) : null;

    panel.replaceChildren(
      metrics,
      el('div', { class: 'grid-two' }, knowledgeCard(catalog), tagging),
      review.card,
      templateCard,
      ...(communityCard ? [communityCard] : []));
    state.loaded = true;
  }

  async function loadOps() {
    const panel = panelEls.ops;
    const state = panelState.ops;
    const mine = ++state.seq;
    panel.replaceChildren(loading(t('admin.loadingOperationsAccounts')));
    const [hostsRes, evalRunsRes, registrationRes, healthRes] = await Promise.all([
      api('/admin/allowed-hosts').catch(error => ({ error: error.message, presets: [], custom: [] })),
      api('/admin/eval-runs').catch(() => ({ items: [] })),
      api('/auth/admin/registration').catch(error => ({ error: error.message })),
      api('/health/ready').catch(err => ({ error: err.message })),
    ]);
    if (!current(version) || mine !== state.seq) return;

    const backupOut = el('div');
    const opsCard = el('section', { class: 'card', style: 'margin-top:22px' },
      el('div', { class: 'card-header' },
        el('h2', {}, t('admin.onlineHotBackupEvaluationRecords')),
        button(t('admin.runHotBackupVerifyNow'), e => busy(e.currentTarget, async () => {
          const res = await api('/admin/backup', { method: 'POST' });
          toast(t('admin.onlineHotBackupAndSha256Integrity'));
          backupOut.replaceChildren(notice(t('admin.backupSucceededFilesKbIntegrityCheck', { v1: res.path, v2: res.file_count, v3: Math.round(res.size_bytes / 1024), v4: res.verified?.integrity_check })));
        }), 'secondary small', 'download')),
      el('p', { class: 'help', style: 'margin-bottom:12px' },
        t('admin.theCliAlsoSupportsPythonMApp')),
      backupOut);

    if (evalRunsRes.items?.length) {
      opsCard.append(el('div', { class: 'table-wrap', style: 'margin-top:12px' }, el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, t('admin.evaluation')), el('th', {}, t('admin.primaryVerifierModel')), el('th', {}, t('admin.questions')),
          el('th', {}, t('admin.singleModelAccuracy')), el('th', {}, t('admin.agreementRate')), el('th', {}, t('admin.agreedButWrong')))),
        el('tbody', {}, ...evalRunsRes.items.map(r => el('tr', {},
          el('td', {}, r.name),
          el('td', {}, r.verify_model ? `${r.model} + ${r.verify_model}` : r.model),
          el('td', {}, String(r.total)),
          el('td', {}, `${r.metrics?.single_accuracy ?? '—'}%`),
          el('td', {}, r.metrics?.agreement_rate !== null && r.metrics?.agreement_rate !== undefined ? `${r.metrics.agreement_rate}%` : '—'),
          el('td', {}, r.metrics?.agreed_but_wrong_rate !== null && r.metrics?.agreed_but_wrong_rate !== undefined ? `${r.metrics.agreed_but_wrong_rate}%` : '—')))))));
    }

    panel.replaceChildren(
      readinessCard(healthRes),
      adminMonitorCard(),
      opsCard,
      adminAiUsageCard(),
      registrationCard(registrationRes),
      hostsCard(hostsRes));
    state.loaded = true;
  }

  const loaders = { issues: loadIssues, content: loadContent, ops: loadOps };

  function loadPanel(key) {
    const state = panelState[key];
    if (state.loaded) return;
    state.loaded = true; // 立即置位：切换/重入不重复发起请求
    loaders[key]().catch(error => {
      if (current(version)) panelFailure(key, error);
      else panelState[key].loaded = false;
    });
  }

  const sectionDefs = [
    ['issues', t('admin.review'), 'shield'],
    ['content', t('admin.bankKnowledgePoints'), 'book'],
    ['ops', t('admin.operationsAccounts'), 'sliders'],
  ];
  const tabBar = el('div', { class: 'pg-tabs', role: 'tablist', 'aria-label': t('admin.contentManagementSections') });
  const tabButtons = new Map();
  for (const [key, label, iconName] of sectionDefs) {
    const tab = el('button', {
      type: 'button',
      class: 'pg-tab',
      role: 'tab',
      id: `admin-tab-${key}`,
      'aria-controls': `admin-panel-${key}`,
      'aria-selected': 'false',
      onClick: () => selectTab(key),
    }, icon(iconName), label);
    tabButtons.set(key, tab);
    tabBar.append(tab);
  }
  wireTabKeyboard(tabBar);

  function selectTab(key) {
    adminTab = panelEls[key] ? key : 'issues';
    for (const [name, panel] of Object.entries(panelEls)) panel.hidden = name !== adminTab;
    for (const [name, tab] of tabButtons) tab.setAttribute('aria-selected', String(name === adminTab));
    loadPanel(adminTab);
  }

  container.replaceChildren(
    heading('CONTENT MANAGEMENT', t('admin.careForEveryPieceOfLearningContent'),
      t('admin.theReviewQueueStartsWithQuestionNumbers')),
    tabBar,
    panelEls.issues, panelEls.content, panelEls.ops);
  selectTab(adminTab);
}
