import { t } from '../core/i18n.js';
/**
 * 每日一签：打开工作台时弹出一句古诗词 / 论语 / 幽梦影，可换一句、可收藏。
 *
 * 数据是构建期从 chinese-poetry（MIT）抽取的 365 条精选短句
 * （`web/vendor/quotes/daily-quotes.json`，见同目录 README 与 LICENSE）。
 *
 * 设计取舍：
 * * 完全离线，取不到数据就静默不弹——绝不联网抓句子；
 * * 同一天永远同一条（按本地日期取模），不依赖 localStorage 也不怕清缓存；
 * * 收藏只存 id 到 localStorage，属于本机偏好，不上传；
 * * 浏览器不允许「关闭页面时弹自定义弹窗」（beforeunload 只能触发原生确认框），
 *   所以这里是「打开时」弹；关闭侧由页面自己的「结束学习」流程负责。
 */
import { el, icon } from '../core/dom.js';

const DATA_URL = '/static/vendor/quotes/daily-quotes.json';
const SEEN_KEY = 'zhixu.quote.lastShown';
const FAV_KEY = 'zhixu.quote.favorites';

let quotesPromise = null;
let offset = 0; // 本次会话内「换一句」的偏移，不改变当天固定那条

function loadQuotes() {
  if (!quotesPromise) {
    quotesPromise = fetch(DATA_URL, { credentials: 'same-origin' })
      .then((response) => (response.ok ? response.json() : []))
      .then((items) => (Array.isArray(items) ? items.filter((item) => item && item.q) : []))
      .catch(() => []);
  }
  return quotesPromise;
}

function ymd(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 同一天固定同一条：按本地日期距 2026-01-01 的天数取模，365 条刚好一年一轮。 */
function dailyIndex(length, date = new Date()) {
  if (!length) return 0;
  const today = Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
  const epoch = Date.UTC(2026, 0, 1);
  const days = Math.round((today - epoch) / 86400000);
  return ((days % length) + length) % length;
}

function attribution(quote) {
  const author = quote.a || '';
  const title = quote.t || '';
  const source = quote.s || '';
  if (source === '诗经') return `《诗经·${title}》`;
  if (source === '论语') return `${author}《论语·${title}》`;
  if (source === '幽梦影') return `${author}《幽梦影》`;
  // 宋词三百首 / 水墨唐诗是选集名，篇名本身已经说明出处，不再重复标注选集。
  return author ? `${author}《${title}》` : `《${title}》`;
}

function readFavorites() {
  try {
    const value = JSON.parse(localStorage.getItem(FAV_KEY) || '[]');
    return Array.isArray(value) ? value.map(Number).filter(Number.isFinite) : [];
  } catch {
    return [];
  }
}

function writeFavorites(ids) {
  try {
    localStorage.setItem(FAV_KEY, JSON.stringify([...new Set(ids)]));
  } catch {
    /* 存储不可用时收藏只在本次会话生效 */
  }
}

function markSeen(today) {
  try {
    localStorage.setItem(SEEN_KEY, today);
  } catch {
    /* ignore */
  }
}

export function hasSeenToday(date = new Date()) {
  try {
    return localStorage.getItem(SEEN_KEY) === ymd(date);
  } catch {
    return false;
  }
}

/**
 * 弹出一张日签卡片。
 * @param {object} [options]
 * @param {boolean} [options.auto] 是否为「打开工作台」自动弹出（自动弹出时关闭会记 seen）
 */
export async function openDailyQuote(options = {}) {
  const quotes = await loadQuotes();
  if (!quotes.length) return false;
  if (document.querySelector('.dq-dialog[open]')) return false;

  const today = ymd();
  const base = dailyIndex(quotes.length);
  const favorites = readFavorites();
  let index = (base + offset) % quotes.length;

  const dialog = el('dialog', { class: 'dq-dialog', 'aria-label': t('dailyQuote.dialogAria') });
  const quoteText = el('blockquote', { class: 'dq-text' });
  const quoteFrom = el('div', { class: 'dq-from' });
  const favBtn = el('button', { type: 'button', class: 'button ghost dq-fav' });
  const footerMeta = el('div', { class: 'dq-meta' });
  const nextBtn = el('button', { type: 'button', class: 'button secondary' }, t('dailyQuote.next'));
  const closeBtn = el('button', { type: 'button', class: 'button dq-close' }, t('dailyQuote.start'));

  function paint() {
    const quote = quotes[index];
    quoteText.textContent = quote.q;
    quoteFrom.textContent = attribution(quote);
    const starred = favorites.includes(Number(quote.id));
    favBtn.replaceChildren(icon(starred ? 'star' : 'star-off'), starred ? t('dailyQuote.favorited') : t('dailyQuote.favorite'));
    favBtn.setAttribute('aria-pressed', String(starred));
    footerMeta.textContent = t('dailyQuote.meta', { index: index + 1, total: quotes.length, count: favorites.length });
  }

  favBtn.addEventListener('click', () => {
    const id = Number(quotes[index].id);
    const at = favorites.indexOf(id);
    if (at >= 0) favorites.splice(at, 1);
    else favorites.push(id);
    writeFavorites(favorites);
    paint();
  });
  nextBtn.addEventListener('click', () => {
    offset = (offset + 1) % quotes.length;
    index = (base + offset) % quotes.length;
    paint();
  });

  let closing = false;
  function close() {
    if (closing) return;
    closing = true;
    dialog.classList.add('is-out');
    const finish = () => {
      dialog.close();
      dialog.remove();
      if (options.auto) markSeen(today);
    };
    dialog.addEventListener('transitionend', finish, { once: true });
    setTimeout(finish, 260); // 兜底：过渡被禁用或未触发
  }
  closeBtn.addEventListener('click', close);
  dialog.addEventListener('cancel', (event) => { // Esc
    event.preventDefault();
    close();
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) close();
  });

  dialog.append(
    el('div', { class: 'dq-inner' },
      el('div', { class: 'dq-head' },
        el('span', { class: 'dq-eyebrow' }, t('dailyQuote.eyebrow')),
        el('button', {
          type: 'button', class: 'icon-button dq-x', 'aria-label': t('dailyQuote.closeAria'),
          onClick: close,
        }, icon('close'))),
      quoteText,
      quoteFrom,
      el('div', { class: 'dq-actions' }, favBtn, nextBtn, closeBtn),
      footerMeta));

  paint();
  document.body.append(dialog);
  dialog.showModal();
  // 下一帧再加 is-in，让入场过渡真正发生。
  requestAnimationFrame(() => dialog.classList.add('is-in'));
  return true;
}

/** 侧栏底部的「今日一句」小卡：显示当天那一句，点击重新打开弹窗。 */
export async function mountDailyQuoteTeaser(node) {
  if (!node) return;
  const quotes = await loadQuotes();
  if (!quotes.length) return;
  const quote = quotes[dailyIndex(quotes.length)];
  node.replaceChildren(
    el('span', { class: 'dq-teaser-text' }, `“${quote.q}”`),
    el('span', { class: 'dq-teaser-from' }, attribution(quote)));
  node.classList.add('dq-teaser');
  node.setAttribute('role', 'button');
  node.setAttribute('tabindex', '0');
  node.setAttribute('title', t('dailyQuote.teaserTitle'));
  const open = () => openDailyQuote();
  node.addEventListener('click', open);
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      open();
    }
  });
}

/** 每天首次打开工作台时自动弹一次（延迟执行，不阻塞首屏）。 */
export async function maybeShowDailyQuote(delay = 900) {
  if (hasSeenToday()) return;
  if (document.querySelector('dialog[open]')) return;
  setTimeout(() => {
    if (!document.querySelector('.main')) return; // 还在登录页
    if (document.querySelector('dialog[open]')) return;
    openDailyQuote({ auto: true });
  }, delay);
}
