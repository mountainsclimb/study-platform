/* 高考词汇本（方案 §7 批次六）。
 *
 * 后端契约：
 * - GET  /api/vocab/meta            {available,count,seed_sha,source,source_commit,note}
 * - GET  /api/vocab/stats           {available,total,due_today,new,learning,review,relearning}
 * - GET  /api/vocab/items?tag&q&page&limit
 * - POST /api/vocab/cards           {word}
 * - GET  /api/vocab/cards/due?limit&include_future
 * - GET  /api/vocab/cards/{id}
 * - POST /api/vocab/cards/{id}/ratings {rating,card_version,idempotency_key}
 * - GET  /api/vocab/export          .apkg（genanki 可选依赖）
 *
 * 复习卡排程与 /api/review-cards 同一口径（study_review.schedule），页面不估算到期日。
 */
import { api } from '../core/api.js';
import {
  el, heading, button, badge, loading, toast, number, downloadBlob, notice,
} from '../core/dom.js';
import { t } from '../core/i18n.js';
import { state } from '../core/state.js';
import { renderReviewRating, isVersionConflict } from '../components/reviewRating.js';

const TAGS = [
  { value: '', key: 'vocab.tagAll' },
  { value: 'gk', key: 'vocab.tagGk' },
  { value: 'cet4', key: 'vocab.tagCet4' },
  { value: 'cet6', key: 'vocab.tagCet6' },
  { value: 'ky', key: 'vocab.tagKy' },
];

function statCard(label, value, hint) {
  return el('div', { class: 'vb-stat' },
    el('span', { class: 'vb-stat-label' }, label),
    el('strong', { class: 'vb-stat-value' }, value),
    hint ? el('span', { class: 'vb-stat-hint' }, hint) : null);
}

function tagBadges(item) {
  return el('span', { class: 'vb-tags' },
    ...(item.tags || []).slice(0, 4).map(tag => badge(tag, '')));
}

function dueCard(card, onRated) {
  const ratingHolder = el('div', { class: 'vb-rating-holder' });
  const translation = el('p', { class: 'vb-translation', hidden: true }, card.translation || '');
  let rating = null;
  const reveal = button(t('vocab.showMeaning'), () => {
    translation.hidden = false;
    reveal.hidden = true;
    if (!ratingHolder.childNodes.length) {
      rating = renderReviewRating({
        card,
        onRate: async (value, { idempotencyKey, card: current }) => {
          try {
            const result = await api(`/vocab/cards/${card.card_id}/ratings`, {
              method: 'POST',
              data: {
                rating: value,
                card_version: current?.card_version || current?.version || card.card_version,
                idempotency_key: idempotencyKey,
              },
            });
            toast(t('vocab.reviewed', { word: card.word }));
            setTimeout(() => {
              if (rating) { try { rating.destroy(); } catch { /* 忽略 */ } }
              article.remove();
              if (typeof onRated === 'function') onRated();
            }, 800);
            return result;
          } catch (error) {
            if (isVersionConflict(error)) {
              try {
                const latest = await api(`/vocab/cards/${card.card_id}`);
                error.card = latest.card;
              } catch { /* 保留原错误 */ }
            }
            throw error;
          }
        },
      });
      ratingHolder.append(rating.element);
    }
  }, 'soft small');
  const article = el('article', { class: 'vb-card', 'data-word': card.word },
    el('div', { class: 'vb-card-head' },
      el('div', {},
        el('h3', { class: 'vb-word' }, card.word),
        el('p', { class: 'vb-phonetic' }, card.phonetic ? `[${card.phonetic}]` : '')),
      el('div', { class: 'vb-card-meta' },
        badge(card.state === 'new' ? t('vocab.stateNew') : t('vocab.stateReview'), ''),
        el('span', { class: 'help' }, t('vocab.reps', { count: number(card.reps || 0) })))),
    reveal,
    translation,
    ratingHolder);
  return article;
}

function browseRow(item, onAdd) {
  const add = button(t('vocab.add'), async event => {
    const node = event.currentTarget;
    node.disabled = true;
    try {
      await onAdd(item);
      node.textContent = t('vocab.addedShort');
    } catch (error) {
      node.disabled = false;
      toast(error?.message || t('vocab.addFailed', { reason: '' }), true);
    }
  }, 'soft small');
  return el('article', { class: 'vb-row' },
    el('div', { class: 'vb-row-main' },
      el('div', {},
        el('strong', { class: 'vb-word' }, item.word),
        el('span', { class: 'vb-phonetic' }, item.phonetic ? ` [${item.phonetic}]` : '')),
      el('p', { class: 'vb-row-translation' }, item.translation || '')),
    tagBadges(item),
    add);
}

export async function renderVocab(container, version, params = null) {
  container.replaceChildren(loading(t('vocab.loading')));
  let meta;
  try {
    meta = await api('/vocab/meta');
  } catch (error) {
    container.replaceChildren(el('p', { class: 'help' }, error?.message || t('vocab.loadFailed')));
    return;
  }
  if (!meta.available) {
    container.replaceChildren(notice(meta.note || t('vocab.unavailable'), 'warning'));
    return;
  }

  let stats = { total: 0, due_today: 0 };
  try { stats = await api('/vocab/stats'); } catch { /* 统计失败不阻塞页面 */ }

  const dueMount = el('div', { class: 'vb-list' });
  const statsGrid = el('div', { class: 'vb-stats' });
  const browseList = el('div', { class: 'vb-list vb-browse' });
  const moreWrap = el('div', { class: 'vb-more' });
  let page = 1;
  let total = 0;
  let activeTag = '';
  let keyword = '';
  let loadingItems = false;

  function renderStats() {
    statsGrid.replaceChildren(
      statCard(t('vocab.statTotal'), number(stats.total || 0), t('vocab.statTotalHint')),
      statCard(t('vocab.statDue'), number(stats.due_today || 0), t('vocab.statDueHint')),
      statCard(t('vocab.statWords'), number(meta.count || 0), t('vocab.statWordsHint')));
  }

  async function refreshStats() {
    try { stats = await api('/vocab/stats'); renderStats(); } catch { /* 保留旧值 */ }
  }

  async function loadDue() {
    const data = await api('/vocab/cards/due?limit=20');
    if (!data.cards?.length) {
      dueMount.replaceChildren(el('p', { class: 'help vb-empty' }, t('vocab.dueEmpty')));
      return;
    }
    dueMount.replaceChildren(...data.cards.map(card => dueCard(card, refreshStats)));
  }

  async function loadItems(reset = false) {
    if (loadingItems) return;
    loadingItems = true;
    if (reset) { page = 1; browseList.replaceChildren(); }
    const query = new URLSearchParams({ page: String(page), limit: '20' });
    if (activeTag) query.set('tag', activeTag);
    if (keyword) query.set('q', keyword);
    try {
      const data = await api(`/vocab/items?${query.toString()}`);
      total = data.total || 0;
      if (!data.items?.length) {
        if (reset) browseList.replaceChildren(el('p', { class: 'help vb-empty' }, t('vocab.browseEmpty')));
      } else {
        browseList.append(...data.items.map(item => browseRow(item, async () => {
          await api('/vocab/cards', { method: 'POST', data: { word: item.word } });
          toast(t('vocab.added', { word: item.word }));
          refreshStats();
          loadDue().catch(() => {});
        })));
      }
      moreWrap.replaceChildren();
      if (browseList.childNodes.length && page * 20 < total) {
        moreWrap.append(button(t('vocab.loadMore'), () => { page += 1; loadItems(false); }, 'ghost small'));
      }
    } finally {
      loadingItems = false;
    }
  }

  const searchInput = el('input', {
    class: 'input', type: 'search', placeholder: t('vocab.searchPlaceholder'),
    'aria-label': t('vocab.searchPlaceholder'), value: keyword,
  });
  const tagSelect = el('select', { class: 'input', 'aria-label': t('vocab.tagLabel') },
    ...TAGS.map(tag => el('option', { value: tag.value }, t(tag.key))));
  const searchButton = button(t('vocab.search'), () => {
    keyword = searchInput.value.trim();
    activeTag = tagSelect.value;
    loadItems(true).catch(error => toast(error?.message || t('vocab.loadFailed'), true));
  }, 'secondary small');

  const exportButton = button(t('vocab.exportAnki'), async event => {
    const node = event.currentTarget;
    node.disabled = true;
    try {
      const response = await fetch('/api/vocab/export', { credentials: 'same-origin' });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || t('vocab.exportFailed', { reason: response.status }));
      }
      downloadBlob(await response.blob(), '知序词汇.apkg');
    } catch (error) {
      toast(error?.message || t('vocab.exportFailed', { reason: '' }), true);
    } finally {
      node.disabled = false;
    }
  }, 'ghost small', 'download');

  const dictationMount = el('div', { class: 'vb-dictation-mount' });
  const startDictation = button(t('vocab.dictation.start'), async event => {
    const node = event.currentTarget;
    node.disabled = true;
    try {
      const module = await import('../components/dictationPractice.js');
      module.createDictationPractice({ container: dictationMount });
      node.hidden = true;
    } catch (error) {
      node.disabled = false;
      toast(error?.message || t('vocab.dictation.failed'), true);
    }
  }, 'soft small', 'message');

  const pageNode = el('section', { class: 'vb-page' },
    heading('VOCABULARY', t('vocab.title'), t('vocab.sub'),
      state.user ? exportButton : null),
    statsGrid,
    el('div', { class: 'vb-source' }, t('vocab.sourceNote', {
      count: number(meta.count || 0), commit: String(meta.source_commit || '').slice(0, 8),
    })),
    el('section', { class: 'vb-section vb-dictation-entry' },
      el('div', { class: 'vb-section-head' },
        el('h2', {}, t('vocab.dictation.entryTitle')),
        el('p', { class: 'help' }, t('vocab.dictation.entrySub'))),
      startDictation,
      dictationMount),
    el('section', { class: 'vb-section' },
      el('div', { class: 'vb-section-head' },
        el('h2', {}, t('vocab.sectionDue')),
        el('p', { class: 'help' }, t('vocab.sectionDueSub'))),
      dueMount),
    el('section', { class: 'vb-section' },
      el('div', { class: 'vb-section-head' },
        el('h2', {}, t('vocab.sectionBrowse')),
        el('p', { class: 'help' }, t('vocab.sectionBrowseSub'))),
      el('div', { class: 'vb-controls' }, searchInput, tagSelect, searchButton),
      browseList,
      moreWrap));
  container.replaceChildren(pageNode);
  renderStats();
  await Promise.all([loadDue(), loadItems(true)]);
}
