import { api, ensureCatalog } from '../core/api.js';
import { state } from '../core/state.js';
import {
  el, button, badge, empty, heading, loading, notice, select, query,
  number, dateText, sourceOf, typeLabel, resultBadge, busy, toast, readableSummary,
} from '../core/dom.js';
import { go, current } from '../core/router.js';
import { openQuestion } from './question.js';

export async function renderWrongbook(container, version) {
  const catalog = await ensureCatalog();
  if (!current(version)) return;

  const subject = select([{ value: '', label: '全部学科' }, ...(catalog.subjects || []).map(s => ({ value: s, label: s }))], '', { 'aria-label': '按学科筛选错题' });
  const tagFilter = select(
    [{ value: '', label: '全部错因' }, ...(catalog.error_tag_presets || ['概念不清', '审题失误', '计算错误', '方法不会', '记忆遗漏', '粗心']).map(t => ({ value: t, label: t }))],
    '', { 'aria-label': '按错因筛选错题' }
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

  async function startContinuousReview(trigger) {
    await busy(trigger, async () => {
      const data = await api(`/wrongbook/session?${query({ subject: subject.value, limit: 20 })}`);
      if (!data.items?.length) {
        toast('当前没有到期的错题需要连续复习。');
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
          el('h2', {}, '🎉 本轮连续复习完成'),
          button('关闭小结', () => { activeSession = null; sessionArea.replaceChildren(); load(); }, 'ghost small')),
        notice(`本轮共复习 ${items.length} 道到期错题，客观/核验答对 ${ok} 道。掌握度与下次复习间隔已自动更新。`)));
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
          badge(`连续复习 ${index + 1} / ${items.length}`, 'teal'),
          el('strong', {}, `${cur.subject} · ${typeLabel(cur, catalog)}`)),
        el('div', { class: 'row' },
          button('跳过下一题', () => {
            // 按当前题号覆盖结果而不是 push：重复提交/跳过同一题不会把结果数
            // 撑到超过本轮题数（修复「3 题答对 4 道」）。
            activeSession.results[index] = 'skipped';
            activeSession.index++;
            renderReviewSession();
          }, 'secondary small'),
          button('退出连续复习', () => { activeSession = null; sessionArea.replaceChildren(); load(); }, 'ghost small', 'close'))),
      detailMount,
      sessionTools));

    openQuestion(cur.qid, detailMount, {
      onSubmitted: resp => {
        if (!activeSession) return;
        activeSession.results[index] = resp.result;
        sessionTools.replaceChildren(
          el('span', { class: 'small-text' }, '本题已提交，记录已计入本轮复习。'),
          button(index + 1 < items.length ? '进入下一道复习题 →' : '查看本轮复习小结', () => {
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
            el('p', { class: 'help' }, `你在「${row.name}」下错了 ${row.wrong_count} 题（到期 ${row.due_count} 题）`)),
          button('生成易错清单', () => {
            state.notePreselectKp = { kp_id: row.kp_id, subject: row.subject, name: row.name, source_mode: 'wrongbook' };
            go('notes');
          }, 'soft small', 'sparkle')));
      }
      kpAggArea.replaceChildren(el('section', { class: 'card', style: 'margin-bottom:20px' },
        el('div', { class: 'card-header' }, el('h3', {}, '按知识点聚合 · 高频易错定位'), badge(`${data.items.length} 个知识点`, 'amber')),
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
      summary.textContent = `共 ${number(total)} 道错题`;
      list.replaceChildren();

      if (!data.items?.length) {
        list.append(el('div', { class: 'card' }, empty(
          dueOnly.checked ? '暂时没有到期错题' : '错题本还是空的',
          dueOnly.checked ? '可以关闭“只看已到复习时间”，查看全部错题。' : '每次答错都会自动留下记录，并按掌握度智能安排复习间隔。',
          button('去题库练习', () => go('practice'), 'soft small'), 'repeat'
        )));
        return;
      }

      const grid = el('div', { class: 'pg-card-grid' });
      for (const entry of data.items) {
        const q = entry.question || entry;
        const result = entry.last_result || entry.result || q.latest_attempt?.result;
        const tags = entry.error_tags || [];
        const source = [
          sourceOf(q),
          q.number_label ? `原卷第${q.number_label}题` : null,
        ].filter(Boolean).join(' · ') || '来源见详情';
        const card = el('article', { class: `card pg-question-card${entry.archived ? ' pg-question-card--archived' : ''}` },
          el('div', { class: 'pg-question-head' },
            el('div', { class: 'pg-status-row' },
              badge(q.subject || '错题', 'teal'),
              badge(typeLabel(q, catalog)),
              entry.archived ? badge('已归档') : null,
              badge(entry.mastery || q.mastery || '未掌握', 'amber')),
            result ? resultBadge(result) : badge('尚未作答')),
          el('p', { class: 'pg-question-source' }, source),
          el('div', { class: 'pg-question-stem' }, readableSummary(q, 170)),
          tags.length ? el('div', { class: 'pg-status-row' }, ...tags.map(t => badge(t, 'amber'))) : null,
          el('div', { class: 'pg-question-review' },
            el('span', {}, `下次复习：${dateText(entry.next_review_at)}`),
            el('span', {}, `间隔 ${Math.round(entry.interval_days || 1)} 天`),
            entry.failures ? el('span', {}, `错 ${entry.failures} 次`) : null,
            entry.streak ? el('span', {}, `连对 ${entry.streak} 次`) : null),
          el('div', { class: 'pg-question-actions' },
            entry.archived
              ? button('恢复复习', e => busy(e.currentTarget, async () => {
                  await api('/wrongbook/restore', { method: 'POST', data: { qid: q.qid } });
                  toast('已恢复到错题本。');
                  load();
                }), 'ghost small')
              : button('移出', e => busy(e.currentTarget, async () => {
                  await api(`/wrongbook/${encodeURIComponent(q.qid)}`, { method: 'DELETE' });
                  toast('已归档移出错题本（保留作答记录）。');
                  load();
                }), 'ghost small'),
            button('再做一次', () => go('practice', q.qid || entry.qid), 'soft small', 'repeat')));
        grid.append(card);
      }
      list.append(grid);

      const pages = Math.max(1, Math.ceil(total / pageSize));
      if (pages > 1) {
        list.append(el('div', { class: 'pagination' },
          button('上一页', () => { page = Math.max(1, page - 1); window.scrollTo({ top: 0, behavior: 'smooth' }); load(); }, 'secondary small', null, { disabled: page <= 1 }),
          el('span', {}, `第 ${page} / ${pages} 页`),
          button('下一页', () => { page++; window.scrollTo({ top: 0, behavior: 'smooth' }); load(); }, 'secondary small', null, { disabled: page >= pages })));
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
              badge(item.subject || '拍题', 'teal'),
              badge('自传拍题', 'blue'),
              item.archived ? badge('已归档') : null),
            el('span', { class: 'pg-question-source' }, `加入于 ${dateText(item.added_at)}`)),
          item.preview_url ? el('img', { src: item.preview_url, class: 'uploaded-preview', style: 'max-height:140px', alt: item.filename, loading: 'lazy' }) : null,
          el('div', { class: 'pg-question-stem' }, (item.stem_preview || item.filename).slice(0, 160)),
          el('div', { class: 'pg-question-actions' },
            !item.archived
              ? button('移出', e => busy(e.currentTarget, async () => {
                  await api(`/wrongbook/uploads/${item.upload_id}`, { method: 'DELETE' });
                  toast('已移出拍题错题本。');
                  loadUploadWrongs();
                }), 'ghost small')
              : null)));
      }
      uploadWrongArea.replaceChildren(el('section', { class: 'pg-kp-section' },
        el('div', { class: 'pg-card-head' }, el('h2', {}, '拍题加入的错题'), badge(`${data.items.length} 道`, 'blue')),
        grid));
    } catch {
      uploadWrongArea.replaceChildren();
    }
  }

  container.replaceChildren(
    el('div', { class: 'pg-page-head' },
      heading('REVIEW & REFLECT', '把错题，变成下一次的把握。',
        '答错自动入本、按掌握度动态拉长复习间隔、连对 3 次自动归档。支持连续复习与知识点聚合归纳。',
        el('div', { class: 'row' },
          button('打印 / 导出 PDF', () => window.print(), 'secondary small', 'download'),
          button('开始连续复习', e => startContinuousReview(e.currentTarget), '', 'repeat')))),
    el('section', { class: 'card pg-filter-bar' },
      el('div', { class: 'pg-filter-head' },
        el('h2', {}, '错题筛选'),
        summary),
      el('div', { class: 'pg-filter-grid' },
        el('div', { class: 'pg-filter-field' }, el('span', { class: 'pg-filter-label' }, '学科'), subject),
        el('div', { class: 'pg-filter-field' }, el('span', { class: 'pg-filter-label' }, '错因'), tagFilter)),
      el('div', { class: 'pg-filter-checks' },
        el('label', { class: 'check-label' }, dueOnly, '只看到期错题'),
        el('label', { class: 'check-label' }, includeArchived, '显示已归档'))),
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
