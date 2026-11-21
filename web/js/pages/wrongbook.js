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
// 四档复习（fsrs-reference v5.0-local）：错题页只读复习卡接口，行内挂评分区；不改旧错题表语义。
import {
  algorithmLabel, dueCards, ensureReviewCards, formatDue, isVersionConflict, renderReviewRating,
} from '../components/reviewRating.js';

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
  // 四档复习：顶部统计条 + 到期卡索引 + 评分后行内日期覆盖（只更新本行，不整页刷新）。
  const reviewBar = el('div', { class: 'review-due-bar', role: 'status', 'aria-live': 'polite', hidden: true });
  const reviewCardIndex = new Map();
  const reviewDueOverrides = new Map();
  let reviewDueToday = 0;
  let reviewStats = null;
  let reviewReady = null;

  let page = 1;
  const pageSize = 18;
  let activeSession = null;
  // M2：/api/wrongbook 顶层 due_note（服务端统一到期口径说明）；接口未下发时用固定文案键兜底。
  let reviewDueNote = '';
  // 筛选/翻页请求序号：连续切换筛选时旧响应必须丢弃，避免慢请求覆盖新结果。
  let listSeq = 0;
  let kpAggSeq = 0;
  let uploadSeq = 0;

  /* ---------------------------------------------------------------------------
   * 四档复习接线（fsrs-reference v5.0-local）
   *
   * 口径：复习卡由 POST /review-cards/sync 从错题幂等迁移；GET /review-cards/due 返回到期卡，
   * 卡上有 question_ref（bank:/app:/official: 规范引用）与 qid（写库键：题库裸 qid、
   * app:/official: 带前缀）。错题列表项的 ref/qid 与之同口径：先按 ref 匹配，qid 兜底。
   * 匹配到的到期卡在行内渲染四档评分区；评分成功后只用服务端返回的 due_at 更新本行
   * 「下次复习」并提示，不整页刷新；降级/409/弱网重试由组件与页面协作兜底。
   * 接口不可用时只降级提示，错题本原有筛选/讲题/导出/标签/归档逻辑完全不变。
   * ------------------------------------------------------------------------- */

  // 统一引用 → 可比较别名集合：原值 + 规范引用（裸 qid 视作 bank:）+ 题库写库键。
  function reviewAliases(values) {
    const aliases = new Set();
    for (const value of values) {
      const text = String(value ?? '').trim();
      if (!text) continue;
      aliases.add(text);
      const matched = text.match(/^(bank|app|official):(.*)$/i);
      if (matched) {
        const source = matched[1].toLowerCase();
        const key = matched[2].trim();
        if (!key) continue;
        aliases.add(`${source}:${key}`);
        // 只有题库的写库键是裸 qid；app:/official: 的写库键本身带前缀，不能去前缀
        // 否则个人题 id 可能与题库 qid 撞车。
        if (source === 'bank') aliases.add(key);
      } else {
        aliases.add(`bank:${text}`);
      }
    }
    return [...aliases];
  }

  function reviewCardAliases(card) {
    return reviewAliases([
      card?.question_ref,
      card?.qid,
      String(card?.card_key || '').replace(/^q:/, ''),
    ]);
  }

  function reviewEntryAliases(entry, question) {
    return reviewAliases([entry?.ref, entry?.qid, question?.ref, question?.qid]);
  }

  function buildReviewCardIndex(items) {
    const index = new Map();
    for (const item of items || []) {
      if (!item || item.card_id === undefined || item.card_id === null) continue;
      for (const alias of reviewCardAliases(item)) {
        if (!index.has(alias)) index.set(alias, item);
      }
    }
    return index;
  }

  function lookupReviewCard(entry, question) {
    for (const alias of reviewEntryAliases(entry, question)) {
      const found = reviewCardIndex.get(alias);
      if (found) return found;
    }
    return null;
  }

  // 评分成功后该卡已不在到期队列：从索引移除，后续筛选重渲染不会重复挂评分区。
  function removeReviewCardFromIndex(card) {
    const cardId = Number(card?.card_id);
    for (const alias of reviewCardAliases(card)) {
      const currentCard = reviewCardIndex.get(alias);
      if (!currentCard || Number(currentCard.card_id) === cardId) reviewCardIndex.delete(alias);
    }
  }

  function setReviewDueOverride(entry, question, dueAt) {
    if (!dueAt) return;
    for (const alias of reviewEntryAliases(entry, question)) reviewDueOverrides.set(alias, dueAt);
  }

  // 行内「下次复习」：评分后用服务端 due_at 覆盖旧错题表的 next_review_at，重渲染也保持一致。
  function reviewNextDueValue(entry, question) {
    for (const alias of reviewEntryAliases(entry, question)) {
      if (reviewDueOverrides.has(alias)) return reviewDueOverrides.get(alias);
    }
    return entry?.next_review_at;
  }

  function reviewAlgorithmText(source) {
    return algorithmLabel({
      algorithm: source?.algorithm,
      algorithm_version: source?.algorithm_version,
    });
  }

  /* M2「统一到期值」展示接线（只做展示，不改排程/评分逻辑）：
   * - 统计条下方追加一行小字口径说明：优先用 /api/wrongbook 顶层 due_note（服务端原文），
   *   接口未下发时用固定文案键 study.review.unifiedNote 兜底；
   * - 列表项若 due_source === "review-cards"，在「下次复习」旁挂轻量徽标（服务端排程），
   *   review_card_rated 为 true 时再标「已评分卡」；legacy 不显示徽标（不误标旧数据）；
   * - 日期格式保持原样（dateText），不在这里改写 next_review_at。 */
  function reviewUnifiedNoteText() {
    return String(reviewDueNote || '').trim() || t('study.review.unifiedNote');
  }

  function reviewUnifiedNoteNode() {
    return el('span', {
      class: 'review-due-bar-help review-due-bar-unified-note',
      style: 'flex-basis:100%',
      'data-review-unified-note': 'true',
    }, reviewUnifiedNoteText());
  }

  function reviewDueBadgeNodes(entry) {
    if (entry?.due_source !== 'review-cards') return null;
    const sourceBadge = badge(t('study.review.serverSchedule'), 'teal');
    sourceBadge.setAttribute('title', reviewUnifiedNoteText());
    const nodes = [sourceBadge];
    if (entry.review_card_rated) nodes.push(badge(t('study.review.ratedCard'), 'blue'));
    return nodes;
  }

  function renderReviewBar() {
    const algorithm = reviewAlgorithmText(reviewStats || [...reviewCardIndex.values()][0] || null);
    reviewBar.hidden = false;
    reviewBar.classList.remove('is-unavailable');
    reviewBar.replaceChildren(
      el('span', { class: 'review-due-bar-main' },
        badge(t('study.review.badge'), 'teal'),
        el('strong', { class: 'review-due-bar-count' },
          t('study.review.stats', { count: number(reviewDueToday), algorithm }))),
      el('span', { class: 'review-due-bar-help' }, t('study.review.scaleNote')),
      // M2：服务端统一到期口径说明（与首页/学情/练习同一天）。
      reviewUnifiedNoteNode());
  }

  function renderReviewUnavailable() {
    reviewBar.hidden = false;
    reviewBar.classList.add('is-unavailable');
    reviewBar.replaceChildren(
      el('span', { class: 'review-due-bar-main' }, badge(t('study.review.badge'), 'amber')),
      el('span', { class: 'review-due-bar-help' }, t('study.review.unavailable')));
  }

  // 409 后重新拉取 due 同步索引（其它行状态保持一致），再按单卡接口取最新版本供重新评分。
  async function refreshReviewCard(card) {
    const due = await dueCards(200);
    if (!due.unavailable) {
      reviewCardIndex.clear();
      for (const [alias, item] of buildReviewCardIndex(due.items)) reviewCardIndex.set(alias, item);
    }
    try {
      const data = await api(`/review-cards/${encodeURIComponent(card.card_id)}`);
      return data?.card || null;
    } catch {
      return null;
    }
  }

  function applyRatingSuccess(result, { entry, question, nextReviewNode }) {
    if (!result || result.degraded) {
      // 降级：到期时间保持原值，不更新行内日期；组件内已展示中文提示与「重试」入口。
      if (result?.degraded) toast(t('study.review.degraded'), true);
      return;
    }
    if (result.card) removeReviewCardFromIndex(result.card);
    if (!result.due_at) return;
    setReviewDueOverride(entry, question, result.due_at);
    if (nextReviewNode?.isConnected) {
      nextReviewNode.textContent = t('wrongbook.nextReview', { time: dateText(result.due_at) });
    }
    reviewDueToday = Math.max(0, Number(reviewDueToday || 0) - 1);
    renderReviewBar();
    toast(t('study.review.scheduledToast', {
      date: formatDue(result.due_at),
      algorithm: algorithmLabel(result),
    }));
  }

  // 评分请求：必传 card_version 与服务端幂等键；409 重新拉取后把最新卡交回组件更换版本。
  async function submitRating({ card, rating, idempotencyKey, entry, question, nextReviewNode }) {
    const cardVersion = Number(card?.version ?? card?.card_version);
    try {
      const result = await api(`/review-cards/${encodeURIComponent(card.card_id)}/ratings`, {
        method: 'POST',
        data: {
          rating,
          card_version: Number.isInteger(cardVersion) ? cardVersion : undefined,
          idempotency_key: idempotencyKey,
        },
      });
      if (current(version)) applyRatingSuccess(result, { entry, question, nextReviewNode });
      return result;
    } catch (error) {
      if (!isVersionConflict(error)) throw error;
      toast(t('study.review.conflict'), true);
      const latest = await refreshReviewCard(card);
      if (latest?.due_at) setReviewDueOverride(entry, question, latest.due_at);
      const conflict = new Error(t('study.review.conflict'));
      conflict.conflict = true;
      conflict.card = latest;
      throw conflict;
    }
  }

  // 页面加载时先触发一次幂等迁移，再并行取「到期卡 + 统计」；接口失败只降级提示。
  async function loadReviewData() {
    try {
      await ensureReviewCards();
      const [due, stats] = await Promise.all([
        dueCards(200),
        api('/review-cards/stats').catch(() => null),
      ]);
      if (!current(version)) return;
      if (due.unavailable) {
        renderReviewUnavailable();
        return;
      }
      reviewCardIndex.clear();
      for (const [alias, item] of buildReviewCardIndex(due.items)) reviewCardIndex.set(alias, item);
      reviewStats = stats;
      reviewDueToday = Number(stats?.due_today ?? due.total ?? due.items.length) || 0;
      renderReviewBar();
    } catch {
      if (current(version)) renderReviewUnavailable();
    }
  }

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
      // 先等四档复习索引就绪：首次渲染即可挂行内评分区；接口不可用则已降级为空索引。
      if (reviewReady) await reviewReady;
      if (mine !== listSeq || !current(version)) return;
      const data = await api(`/wrongbook?${query({
        subject: subject.value,
        due_only: dueOnly.checked ? 'true' : 'false',
        error_tag: tagFilter.value,
        include_archived: includeArchived.checked ? 'true' : 'false',
        page,
        page_size: pageSize,
      })}`, { latest: true, latestKey: 'wrongbook-list' });
      if (mine !== listSeq || !current(version)) return;
      // M2：服务端 /api/wrongbook 顶层 due_note 是统一到期口径说明（首页/错题本/学情同一天）；
      // 与当前说明不同时只更新统计条下方那行小字，不整页刷新、不影响筛选与行内评分状态。
      const serverDueNote = typeof data.due_note === 'string' ? data.due_note.trim() : '';
      if (serverDueNote && serverDueNote !== reviewDueNote) {
        reviewDueNote = serverDueNote;
        if (!reviewBar.hidden && !reviewBar.classList.contains('is-unavailable')) renderReviewBar();
      }
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
        // 四档复习：到期卡与错题按统一引用匹配（ref 优先、qid 兜底）后，在行内挂评分区。
        // 归档行即使残留卡片也不提供评分（到期队列只含未归档卡）。
        const dueCard = entry.archived ? null : lookupReviewCard(entry, q);
        const nextReviewNode = el('span', {},
          t('wrongbook.nextReview', { time: dateText(reviewNextDueValue(entry, q)) }));
        // M2：服务端统一到期徽标（review-cards 才显示；已真实评分再标「已评分卡」）。
        const dueBadges = reviewDueBadgeNodes(entry);
        const ratingSlot = dueCard
          ? renderReviewRating({
              card: dueCard,
              onRate: (rating, context) => submitRating({
                card: context?.card || dueCard,
                rating,
                idempotencyKey: context?.idempotencyKey,
                entry,
                question: q,
                nextReviewNode,
              }),
            }).element
          : null;
        const children = [
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
            nextReviewNode,
            dueBadges,
            el('span', {}, t('wrongbook.intervalDays', { count: Math.round(entry.interval_days || 1) })),
            entry.failures ? el('span', {}, t('wrongbook.failCount', { count: entry.failures })) : null,
            entry.streak ? el('span', {}, t('wrongbook.streakCount', { count: entry.streak })) : null),
        ];
        if (ratingSlot) children.push(ratingSlot);
        children.push(el('div', { class: 'pg-question-actions' },
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
        grid.append(el('article', {
          class: `card pg-question-card${entry.archived ? ' pg-question-card--archived' : ''}`,
          // M2 数据来源标记：review-cards（服务端四档卡）| legacy（旧到期兜底），便于核对。
          'data-due-source': entry.due_source || '',
        }, ...children));
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
    reviewBar,
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

  // 四档复习在目录就绪后与列表并行加载；列表渲染内部会等 reviewReady 就绪再挂评分区。
  reviewReady = loadReviewData();
  for (const ctrl of [subject, tagFilter, dueOnly, includeArchived]) {
    ctrl.addEventListener('change', () => { page = 1; load(); loadKpAggregation(); loadUploadWrongs(); });
  }
  await Promise.all([load(), loadKpAggregation(), loadUploadWrongs(), reviewReady]);
}
