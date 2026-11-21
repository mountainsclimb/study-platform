import { api, ensureCatalog } from '../core/api.js';
import { state } from '../core/state.js';
import { t } from '../core/i18n.js';
import {
  el, button, badge, empty, heading, loading, notice, select, query,
  number, dateText, sourceOf, typeLabel, resultBadge, busy, toast, readableSummary, downloadBlob,
} from '../core/dom.js';
import { go, current } from '../core/router.js';
import { openQuestion } from './question.js';
import { openPresentationMode } from '../components/presentationMode.js';

// 讲错题入口：打开全屏讲解（数据由组件按只读 /api/reveal 拉取；未解锁时组件内提示先作答）。
function openWrongPresentation(qid) {
  openPresentationMode({ qid }).catch(error => toast(error?.message || t('wrongbook.presentationFailed'), true));
}

export async function renderWrongbook(container, version) {
  const catalog = await ensureCatalog();
  if (!current(version)) return;

  const subject = select([{ value: '', label: t('common.allSubjects') }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))], '', { 'aria-label': t('wrongbook.filterSubjectAria') });
  const tagFilter = select(
    [{ value: '', label: t('wrongbook.allErrorTags') }, ...(catalog.error_tag_presets || ['概念不清', '审题失误', '计算错误', '方法不会', '记忆遗漏', '粗心']).map(t => ({ value: t, label: t }))],
    '', { 'aria-label': t('wrongbook.filterErrorTagAria') }
  );
  const dueOnly = el('input', { type: 'checkbox' });
  const includeArchived = el('input', { type: 'checkbox' });
  const summary = el('span', { class: 'pg-filter-summary' });
  const kpAggArea = el('div');
  const sessionArea = el('div');
  const list = el('div');
  const uploadWrongArea = el('div');

  let page = 1;
  const pageSize = 18;
  let activeSession = null;
  // 筛选/翻页请求序号：连续切换筛选时旧响应必须丢弃，避免慢请求覆盖新结果。
  let listSeq = 0;
  let kpAggSeq = 0;
  let uploadSeq = 0;

  // 导出 Anki 牌组：按学科分牌组（正面题干+选项，背面答案+解析+我的错因）。
  async function exportAnki(trigger) {
    await busy(trigger, async () => {
      const params = query({
        format: 'apkg',
        subject: subject.value,
        limit: 200,
        include_archived: includeArchived.checked ? '1' : '',
      });
      const response = await fetch(`/api/wrongbook/export?${params}`, { credentials: 'same-origin' });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || t('wrongbook.exportFailed', { status: response.status }));
      }
      downloadBlob(await response.blob(), '知序错题.apkg');
      toast(t('wrongbook.exportAnkiDone'));
    });
  }

  async function startContinuousReview(trigger) {
    await busy(trigger, async () => {
      const data = await api(`/wrongbook/session?${query({ subject: subject.value, limit: 20 })}`);
      if (!data.items?.length) {
        toast(t('wrongbook.noDueForSession'));
        return;
      }
      activeSession = { items: data.items, index: 0, results: [] };
      renderReviewSession();
    });
  }

  function renderReviewSession() {
    if (!activeSession) {
      sessionArea.replaceChildren();
      return;
    }
    const { items, index, results } = activeSession;
    if (index >= items.length) {
      const ok = results.filter(r => r === 'correct').length;
      sessionArea.replaceChildren(el('section', { class: 'card pg-session-card' },
        el('div', { class: 'card-header' },
          el('h2', {}, t('wrongbook.sessionDoneTitle')),
          button(t('wrongbook.closeSummary'), () => { activeSession = null; sessionArea.replaceChildren(); load(); }, 'ghost small')),
        notice(t('wrongbook.sessionSummary', { total: items.length, ok }))));
      return;
    }

    const cur = items[index];
    const detailMount = el('div');
    // 工具条挂在 openQuestion 触不到的固定容器里：question.js 提交后只重渲染
    // detailMount，不会再把「进入下一道复习题」清掉（P0 主流程修复）。
    const sessionTools = el('div', { class: 'pg-session-tools row end' });
    sessionArea.replaceChildren(el('section', { class: 'card pg-session-card pg-session-card--active' },
      el('div', { class: 'card-header' },
        el('div', { class: 'row' },
          badge(t('wrongbook.sessionProgress', { current: index + 1, total: items.length }), 'teal'),
          el('strong', {}, `${cur.subject} · ${typeLabel(cur, catalog)}`)),
        el('div', { class: 'row' },
          button(t('wrongbook.skipNext'), () => {
            // 按当前题号覆盖结果而不是 push：重复提交/跳过同一题不会把结果数
            // 撑到超过本轮题数（修复「3 题答对 4 道」）。
            activeSession.results[index] = 'skipped';
            activeSession.index++;
            renderReviewSession();
          }, 'secondary small'),
          button(t('wrongbook.exitSession'), () => { activeSession = null; sessionArea.replaceChildren(); load(); }, 'ghost small', 'close'))),
      detailMount,
      sessionTools));

    openQuestion(cur.qid, detailMount, {
      onSubmitted: resp => {
        if (!activeSession) return;
        activeSession.results[index] = resp.result;
        sessionTools.replaceChildren(
          el('span', { class: 'small-text' }, t('wrongbook.answerRecorded')),
          button(index + 1 < items.length ? t('wrongbook.nextQuestion') : t('wrongbook.viewSummary'), () => {
            if (!activeSession) return;
            activeSession.index++;
            renderReviewSession();
          }, '', 'check'));
        sessionTools.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      },
    });
  }

  async function loadKpAggregation() {
    const mine = ++kpAggSeq;
    try {
      const data = await api(`/wrongbook/by-kp?${query({ subject: subject.value })}`,
        { latest: true, latestKey: 'wrongbook-kp-agg' });
      if (mine !== kpAggSeq || !current(version)) return;
      if (!data.items?.length) {
        kpAggArea.replaceChildren();
        return;
      }
      const chips = el('div', { class: 'kp-agg-grid' });
      for (const row of data.items.slice(0, 8)) {
        chips.append(el('div', { class: 'kp-agg-card pg-kp-card' },
          el('div', {},
            el('strong', {}, `${row.subject} · ${row.name}`),
            el('p', { class: 'help' }, t('wrongbook.kpWrongStat', { name: row.name, count: row.wrong_count, due: row.due_count }))),
          button(t('wrongbook.generateErrorList'), () => {
            state.notePreselectKp = { kp_id: row.kp_id, subject: row.subject, name: row.name, source_mode: 'wrongbook' };
            go('notes');
          }, 'soft small', 'sparkle')));
      }
      kpAggArea.replaceChildren(el('section', { class: 'card', style: 'margin-bottom:20px' },
        el('div', { class: 'card-header' }, el('h3', {}, t('wrongbook.kpAggTitle')), badge(t('wrongbook.kpCount', { count: data.items.length }), 'amber')),
        chips));
    } catch {
      kpAggArea.replaceChildren();
    }
  }

  async function load() {
    const mine = ++listSeq;
    list.replaceChildren(loading());
    try {
      const data = await api(`/wrongbook?${query({
        subject: subject.value,
        due_only: dueOnly.checked ? 'true' : 'false',
        error_tag: tagFilter.value,
        include_archived: includeArchived.checked ? 'true' : 'false',
        page,
        page_size: pageSize,
      })}`, { latest: true, latestKey: 'wrongbook-list' });
      if (mine !== listSeq || !current(version)) return;
      const total = data.total ?? data.items?.length ?? 0;
      summary.textContent = t('wrongbook.totalWrong', { count: number(total) });
      list.replaceChildren();

      if (!data.items?.length) {
        list.append(el('div', { class: 'card' }, empty(
          dueOnly.checked ? t('wrongbook.emptyDueTitle') : t('wrongbook.emptyTitle'),
          dueOnly.checked ? t('wrongbook.emptyDueText') : t('wrongbook.emptyText'),
          button(t('common.goPractice'), () => go('practice'), 'soft small'), 'repeat'
        )));
        return;
      }

      const grid = el('div', { class: 'pg-card-grid' });
      for (const entry of data.items) {
        const q = entry.question || entry;
        const result = entry.last_result || entry.result || q.latest_attempt?.result;
        const tags = entry.error_tags || [];
        // 统一题目引用：个人题显示「我的题库」来源；题库题保持原来源展示。
        const isApp = q.source === 'app';
        const sourceLabel = isApp ? (q.source_label || t('wrongbook.sourceMyBank')) : '';
        const source = [
          sourceLabel,
          sourceOf(q),
          q.number_label ? t('question.originalNumber', { label: q.number_label }) : null,
        ].filter(Boolean).join(' · ') || t('wrongbook.sourceSeeDetail');
        const card = el('article', { class: `card pg-question-card${entry.archived ? ' pg-question-card--archived' : ''}` },
          el('div', { class: 'pg-question-head' },
            el('div', { class: 'pg-status-row' },
              isApp ? badge(sourceLabel, 'blue') : null,
              badge(q.subject || t('wrongbook.badgeWrong'), 'teal'),
              badge(typeLabel(q, catalog)),
              entry.archived ? badge(t('common.archived')) : null,
              badge(entry.mastery || q.mastery || t('wrongbook.unmastered'), 'amber')),
            result ? resultBadge(result) : badge(t('question.result.unanswered'))),
          el('p', { class: 'pg-question-source' }, source),
          el('div', { class: 'pg-question-stem' }, readableSummary(q, 170)),
          tags.length ? el('div', { class: 'pg-status-row' }, ...tags.map(t => badge(t, 'amber'))) : null,
          el('div', { class: 'pg-question-review' },
            el('span', {}, t('wrongbook.nextReview', { time: dateText(entry.next_review_at) })),
            el('span', {}, t('wrongbook.intervalDays', { count: Math.round(entry.interval_days || 1) })),
            entry.failures ? el('span', {}, t('wrongbook.failCount', { count: entry.failures })) : null,
            entry.streak ? el('span', {}, t('wrongbook.streakCount', { count: entry.streak })) : null),
          el('div', { class: 'pg-question-actions' },
            entry.archived
              ? button(t('wrongbook.restore'), e => busy(e.currentTarget, async () => {
                  await api('/wrongbook/restore', { method: 'POST', data: { qid: q.qid } });
                  toast(t('wrongbook.restored'));
                  load();
                }), 'ghost small')
              : button(t('common.remove'), e => busy(e.currentTarget, async () => {
                  await api(`/wrongbook/${encodeURIComponent(q.qid)}`, { method: 'DELETE' });
                  toast(t('wrongbook.removed'));
                  load();
                }), 'ghost small'),
            button(t('wrongbook.present'), () => openWrongPresentation(q.qid || entry.qid), 'soft small', 'presentation'),
            button(t('wrongbook.tryAgain'), () => go('practice', q.qid || entry.qid), 'soft small', 'repeat',
              { disabled: entry.unavailable === true, title: entry.unavailable ? t('wrongbook.tryAgainUnavailable') : null })));
        grid.append(card);
      }
      list.append(grid);

      const pages = Math.max(1, Math.ceil(total / pageSize));
      if (pages > 1) {
        list.append(el('div', { class: 'pagination' },
          button(t('common.prevPage'), () => { page = Math.max(1, page - 1); window.scrollTo({ top: 0, behavior: 'smooth' }); load(); }, 'secondary small', null, { disabled: page <= 1 }),
          el('span', {}, t('common.pageOf', { page, pages })),
          button(t('common.nextPage'), () => { page++; window.scrollTo({ top: 0, behavior: 'smooth' }); load(); }, 'secondary small', null, { disabled: page >= pages })));
      }
    } catch (error) {
      if (mine !== listSeq || error.aborted) return;
      list.replaceChildren(el('div', { class: 'error-panel' }, error.message));
    }
  }

  async function loadUploadWrongs() {
    const mine = ++uploadSeq;
    try {
      const data = await api(`/wrongbook/uploads?${query({ subject: subject.value, include_archived: includeArchived.checked ? 'true' : 'false' })}`,
        { latest: true, latestKey: 'wrongbook-uploads' });
      if (mine !== uploadSeq || !current(version)) return;
      if (!data.items?.length) {
        uploadWrongArea.replaceChildren();
        return;
      }
      const grid = el('div', { class: 'pg-card-grid' });
      for (const item of data.items) {
        grid.append(el('article', { class: `card pg-question-card${item.archived ? ' pg-question-card--archived' : ''}` },
          el('div', { class: 'pg-question-head' },
            el('div', { class: 'pg-status-row' },
              badge(item.subject || t('wrongbook.photoBadge'), 'teal'),
              badge(t('wrongbook.uploadedBadge'), 'blue'),
              item.archived ? badge(t('common.archived')) : null),
            el('span', { class: 'pg-question-source' }, t('common.addedAt', { time: dateText(item.added_at) }))),
          item.preview_url ? el('img', { src: item.preview_url, class: 'uploaded-preview', style: 'max-height:140px', alt: item.filename, loading: 'lazy' }) : null,
          el('div', { class: 'pg-question-stem' }, (item.stem_preview || item.filename).slice(0, 160)),
          el('div', { class: 'pg-question-actions' },
            !item.archived
              ? button(t('common.remove'), e => busy(e.currentTarget, async () => {
                  await api(`/wrongbook/uploads/${item.upload_id}`, { method: 'DELETE' });
                  toast(t('wrongbook.uploadWrongRemoved'));
                  loadUploadWrongs();
                }), 'ghost small')
              : null)));
      }
      uploadWrongArea.replaceChildren(el('section', { class: 'pg-kp-section' },
        el('div', { class: 'pg-card-head' }, el('h2', {}, t('wrongbook.uploadWrongTitle')), badge(t('common.countQuestions', { count: data.items.length }), 'blue')),
        grid));
    } catch {
      uploadWrongArea.replaceChildren();
    }
  }

  container.replaceChildren(
    el('div', { class: 'pg-page-head' },
      heading('REVIEW & REFLECT', t('wrongbook.heading'),
        t('wrongbook.subheading'),
        el('div', { class: 'row' },
          button(t('wrongbook.exportAnki'), e => exportAnki(e.currentTarget), 'secondary small', 'download'),
          button(t('common.printPdf'), () => window.print(), 'secondary small', 'download'),
          button(t('wrongbook.startContinuous'), e => startContinuousReview(e.currentTarget), '', 'repeat')))),
    el('section', { class: 'card pg-filter-bar' },
      el('div', { class: 'pg-filter-head' },
        el('h2', {}, t('wrongbook.filterTitle')),
        summary),
      el('div', { class: 'pg-filter-grid' },
        el('div', { class: 'pg-filter-field' }, el('span', { class: 'pg-filter-label' }, t('common.subject')), subject),
        el('div', { class: 'pg-filter-field' }, el('span', { class: 'pg-filter-label' }, t('common.errorTag')), tagFilter)),
      el('div', { class: 'pg-filter-checks' },
        el('label', { class: 'check-label' }, dueOnly, t('wrongbook.dueOnly')),
        el('label', { class: 'check-label' }, includeArchived, t('common.showArchived')))),
    sessionArea,
    kpAggArea,
    list,
    uploadWrongArea
  );

  for (const ctrl of [subject, tagFilter, dueOnly, includeArchived]) {
    ctrl.addEventListener('change', () => { page = 1; load(); loadKpAggregation(); loadUploadWrongs(); });
  }
  await Promise.all([load(), loadKpAggregation(), loadUploadWrongs()]);
}
