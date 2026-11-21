/* 试卷工坊 · 我的试卷：列表 / 详情（快照题面）/ 题序分值编辑 / 发布 / 导出与打印 / 答题卡扫描入口。
 *
 * 只使用既有设施：
 * - `GET/PATCH/DELETE /api/paperlab/papers*`、`POST /papers/{id}/publish`；
 * - `GET /papers/{id}/export?format=docx|html|pdf&answers=0|1`（Word/PDF 下载；打印 iframe 同源加载后
 *   window.print()；PDF 由后端浏览器渲染，引擎不可用时 503 提示改用打印）；
 * - 题面渲染复用 core/dom.js 的 `questionText/bankImage/markdown`（应用库快照用 markdown，高考库快照按
 *   `[图N]/[公式N]` 占位还原图片）；
 * - 答题卡：`paperCard.js`（模板/SVG/打印/PDF）；扫描识别（OMR）：`paperScan.js`
 *   （上传多页照片 → 逐页叠加预览 → 人工纠错 → 确认判分）。
 */
import { api, errorText } from '../core/api.js';
import { onSessionReset } from '../core/state.js';
import {
  el, button, badge, notice, empty, loading, busy, toast, dateText, number, field, input,
  questionText, bankImage,
} from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { current, go } from '../core/router.js';
import { renderPaperPractice } from './paperAttempt.js';
import { renderCardSection, renderCardStudio } from './paperCard.js';
import { renderScanSection, renderScanStudio } from './paperScan.js';
import { t } from '../core/i18n.js';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
export const PAPER_KIND_LABELS = {
  get manual() { return t('paper.compose.manualComposition'); },
  get exam() { return t('paper.document.testPaper'); },
  get smart() { return t('paper.document.aiSmartComposition'); },
  get knowledge() { return t('home.paperKind.knowledge'); },
  get wrong() { return t('home.paperKind.wrong'); },
  get layered() { return t('home.paperKind.layered'); },
  get replay() { return t('paper.compose.wrongAnswerReplayPaper'); },
  get consolidate() { return t('home.paperKind.consolidate'); },
};
const PAPER_STATUS_META = {
  get draft() { return [t('mybank.draft'), 'blue']; },
  get ready() { return [t('admin.issueStatus.toPublish'), 'teal']; },
  get published() { return [t('admin.issueStatus.published'), 'teal']; },
};
const TYPE_LABELS = {
  get SINGLE() { return t('question.type.single_choice'); },
  get MULTIPLE() { return t('question.type.multi_choice'); },
  get BLANK() { return t('question.type.fill_blank'); },
  get SUBJECTIVE() { return t('mybank.subjective'); },
  get MIXED() { return t('mybank.mixed'); },
};
export const DEFAULT_SCORES = {
  SINGLE: 2, MULTIPLE: 4, BLANK: 2, SUBJECTIVE: 10, MIXED: 10,
};
const PAPER_STATUSES = [
  { value: '', get label() { return t('practice.allStatuses'); } },
  { value: 'draft', get label() { return t('mybank.draft'); } },
  { value: 'ready', get label() { return t('admin.issueStatus.toPublish'); } },
  { value: 'published', get label() { return t('admin.issueStatus.published'); } },
];
const PAPER_KIND_FILTERS = [
  { value: '', get label() { return t('admin.allTypes'); } },
  ...Object.keys(PAPER_KIND_LABELS).map(value => ({ value, get label() { return PAPER_KIND_LABELS[value]; } })),
];
const PAPER_PAGE_SIZE = 10;
const PRINT_FRAME_REMOVE_MS = 60000;

let listPage = 1;
let listFilters = { subject: '', kind: '', status: '' };

onSessionReset(() => {
  listPage = 1;
  listFilters = { subject: '', kind: '', status: '' };
});

export function paperKindLabel(kind) {
  return PAPER_KIND_LABELS[kind] || kind || t('home.status.papers');
}

export function paperKindBadge(kind) {
  return badge(paperKindLabel(kind), kind && kind !== 'manual' ? 'amber' : '');
}

export function paperStatusBadge(status) {
  const meta = PAPER_STATUS_META[status] || [status || t('tasks.unknownStatus'), ''];
  return badge(meta[0], meta[1]);
}

export function typeLabelOf(type) {
  return TYPE_LABELS[type] || type || t('physics.question');
}

function difficultyText(value) {
  if (value === null || value === undefined || value === '') return t('mybank.difficultyUnlabeled');
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  if (num <= 0.3) return t('practice.difficultyEasy');
  if (num <= 0.45) return t('mybank.easier');
  if (num <= 0.65) return t('practice.difficultyMedium');
  if (num <= 0.85) return t('mybank.harder');
  return t('practice.difficultyHard');
}

function scoreText(value) {
  const num = Number(value);
  return Number.isFinite(num) ? t('admission.scorePoints', { v1: number(num) }) : t('paper.document.pts');
}

/** 参考分：按题型默认分（与后端 DEFAULT_SCORES 对齐）。 */
export function defaultScoreFor(type) {
  return DEFAULT_SCORES[String(type || '').toUpperCase()] || 5;
}

/* ---------------------------------------------------------------------------
 * 共享：从 API 拉取试卷 / 下载 / 打印
 * ------------------------------------------------------------------------- */

export async function fetchPaper(paperId) {
  return api(`/paperlab/papers/${encodeURIComponent(paperId)}?include_snapshot=true`);
}

async function fetchExportBlob(paperId, { format, answers }) {
  const params = new URLSearchParams({ format, answers: answers ? '1' : '0' });
  const response = await fetch(
    `/api/paperlab/papers/${encodeURIComponent(paperId)}/export?${params}`,
    { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } },
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(errorText(payload.detail || payload.error || t('wrongbook.exportFailed', { status: response.status })));
  }
  return { blob: await response.blob(), headers: response.headers };
}

function parseDownloadName(headers, fallback) {
  const disposition = headers?.get?.('Content-Disposition') || '';
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) {
    try { return decodeURIComponent(star[1]); } catch { /* ignore */ }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

async function downloadDocx(paper, answers) {
  const { blob, headers } = await fetchExportBlob(paper.id, { format: 'docx', answers });
  const filename = parseDownloadName(headers, t('paper.document.docx', {
      v1: paper.title || t('home.status.papers'),
      v2: answers ? t('paper.document.withAnswersSuffix') : t('paper.document.studentVersionSuffix'),
    }));
  const url = URL.createObjectURL(blob);
  try {
    const link = el('a', { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

/** 服务端 PDF 导出：与浏览器打印同源渲染；引擎不可用时后端 503，前端提示改用打印页。 */
async function downloadPdf(paper, answers) {
  const { blob, headers } = await fetchExportBlob(paper.id, { format: 'pdf', answers });
  const filename = parseDownloadName(headers,
    t('paper.document.pdf', {
      v1: paper.title || t('home.status.papers'),
      v2: answers ? t('paper.document.withAnswersSuffix') : t('paper.document.studentVersionSuffix'),
    }));
  const url = URL.createObjectURL(blob);
  try {
    const link = el('a', { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

/**
 * 打印/另存 PDF：同源隐藏 iframe 加载后端自包含打印页（后端带 SAMEORIGIN frame 策略），
 * 加载完成后调用 contentWindow.print()；PDF 由浏览器打印对话框「另存为 PDF」产生。
 */
function printPaper(paper, answers) {
  const params = new URLSearchParams({ format: 'html', answers: answers ? '1' : '0' });
  const frame = el('iframe', {
    class: 'pl-print-frame',
    title: t('paper.document.paperPrintPreview'),
    src: `/api/paperlab/papers/${encodeURIComponent(paper.id)}/export?${params}`,
    'aria-hidden': 'true',
  });
  frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
  const cleanup = () => { try { frame.remove(); } catch { /* ignore */ } };
  frame.addEventListener('load', () => {
    try {
      const win = frame.contentWindow;
      win.focus();
      win.print();
      toast(t('paper.document.theBrowserPrintDialogIsOpenChoose'));
    } catch (error) {
      toast(t('paper.document.cannotOpenThePrintDialogAutomaticallyClick', {
        v1: error.message || t('paper.document.browserRestriction'),
      }), true);
    }
    setTimeout(cleanup, PRINT_FRAME_REMOVE_MS);
  });
  frame.addEventListener('error', () => {
    toast(t('paper.document.failedToLoadThePrintPagePlease'), true);
    cleanup();
  });
  document.body.append(frame);
}

function openPrintPreview(paper, answers) {
  const params = new URLSearchParams({ format: 'html', answers: answers ? '1' : '0' });
  const url = `/api/paperlab/papers/${encodeURIComponent(paper.id)}/export?${params}`;
  window.open(url, '_blank', 'noopener');
}

/* ---------------------------------------------------------------------------
 * 题面渲染（快照）
 * ------------------------------------------------------------------------- */

function appImageNodes(snapshot) {
  const content = snapshot.content || {};
  const images = Array.isArray(content.images) ? content.images : [];
  const nodes = [];
  for (const image of images) {
    if (!image || typeof image !== 'object') continue;
    const url = image.url || '';
    if (!url) continue;
    nodes.push(el('img', {
      class: 'pl-snapshot-image',
      src: url,
      alt: image.role === 'formula' ? t('question.sourceFormula') : t('question.figure'),
      loading: 'lazy',
    }));
  }
  return nodes;
}

function frozenPaperImage(paperId, position, index, role) {
  return el('img', {
    class: 'pl-snapshot-image',
    src: `/api/paperlab/papers/${paperId}/items/${position}/images/${index}`,
    alt: role === 'formula' ? t('question.sourceFormula') : t('question.figure'),
    loading: 'lazy',
    onError: event => {
      event.currentTarget.replaceWith(el('span', { class: 'image-error' }, t('paper.document.imageTemporarilyUnavailable')));
    },
  });
}

function consumedSnapshotImageIndexes(snapshot) {
  const content = snapshot.content || {};
  const images = Array.isArray(content.images) ? content.images : [];
  const used = new Set();
  const text = String(content.stem || '');
  for (const match of text.matchAll(/\[(图|公式)(\d+)\]/g)) {
    const role = match[1] === '公式' ? 'formula' : 'figure';
    const ranked = images.filter(image => image && image.file && (image.role || 'figure') === role);
    const image = ranked[Number(match[2]) - 1];
    if (image) used.add(images.indexOf(image));
  }
  return used;
}

function bankImageNodes(snapshot, paperId = null, position = null) {
  const content = snapshot.content || {};
  const images = Array.isArray(content.images) ? content.images : [];
  if (!images.length) return [];
  const consumed = consumedSnapshotImageIndexes(snapshot);
  const nodes = [];
  images.forEach((image, index) => {
    if (!image || !image.file) return;
    if (consumed.has(index)) return;
    if (paperId && position) {
      nodes.push(frozenPaperImage(paperId, position, index, image.role));
    } else {
      nodes.push(bankImage(snapshot.subject || '', image, {
        qid: snapshot.source === 'bank' ? snapshot.source_id : undefined,
        bankVersion: snapshot.bank_version,
      }));
    }
  });
  return nodes;
}

function frozenBankStem(snapshot, paperId, position) {
  const content = snapshot.content || {};
  const text = String(content.stem || '');
  const images = Array.isArray(content.images) ? content.images : [];
  const node = el('div', { class: 'question-text' });
  for (const part of text.split(/(\[(?:图|公式)\d+\])/g)) {
    const match = part.match(/^\[(图|公式)(\d+)\]$/);
    if (!match) {
      node.append(document.createTextNode(part));
      continue;
    }
    const role = match[1] === '公式' ? 'formula' : 'figure';
    const ranked = images.filter(image => image && image.file && (image.role || 'figure') === role);
    const image = ranked[Number(match[2]) - 1];
    if (image) {
      node.append(frozenPaperImage(paperId, position, images.indexOf(image), role));
    } else {
      node.append(el('span', { class: 'image-error' }, t('question.partMissing', { part: part })));
    }
  }
  return node;
}

function stemNode(snapshot, paperId = null, position = null) {
  const content = snapshot.content || {};
  const stem = String(content.stem || '');
  if (!stem) return el('p', { class: 'help' }, t('paper.document.thisQuestionHasNoStemSnapshot'));
  if (snapshot.source === 'bank') {
    if (paperId && position) return frozenBankStem(snapshot, paperId, position);
    return questionText(stem, (content.images || []).filter(image => image && image.file),
      snapshot.subject || '', '', {});
  }
  return markdown(stem);
}

function materialNode(snapshot) {
  const content = snapshot.content || {};
  const material = content.material;
  if (!material || typeof material !== 'object') return null;
  const text = String(material.text || '');
  const images = (Array.isArray(material.images) ? material.images : []).filter(image => image && image.file);
  if (!text && !images.length) return null;
  return el('div', { class: 'question-material pl-snapshot-material' },
    el('strong', {}, t('paper.document.material')),
    text ? questionText(text, images, snapshot.subject || '', '', {}) : null);
}

function optionsNode(snapshot) {
  const content = snapshot.content || {};
  const options = Array.isArray(content.options) ? content.options : [];
  if (!options.length) return null;
  return el('div', { class: 'options pl-snapshot-options' }, options.map(option =>
    el('div', { class: 'option' },
      el('span', { class: 'option-label' }, option.label || ''),
      el('span', { class: 'option-content' }, String(option.text || '')))));
}

function answersNode(snapshot) {
  const content = snapshot.content || {};
  const answers = Array.isArray(content.answers) ? content.answers.filter(Boolean).map(String) : [];
  const answerText = String(content.answer_text || '').trim();
  const explanation = String(content.explanation || '').trim();
  const rubric = String(content.rubric || '').trim();
  const parts = Array.isArray(content.parts) ? content.parts : [];
  if (!answers.length && !answerText && !explanation && !rubric && !parts.length) {
    return el('p', { class: 'help' }, t('paper.document.thisSnapshotHasNoReferenceAnswerChoose'));
  }
  const box = el('div', { class: 'answer-box pl-snapshot-answer' });
  if (answers.length) {
    box.append(el('div', {}, el('strong', {}, t('paper.document.referenceAnswer')), markdown(answers.join(t('common.semicolonSeparator')))));
  } else if (answerText) {
    box.append(el('div', {}, el('strong', {}, t('paper.document.referenceAnswer')), markdown(answerText)));
  }
  if (explanation) {
    box.append(el('div', { class: 'pl-snapshot-explanation' }, el('strong', {}, t('paper.document.solution')), markdown(explanation)));
  }
  if (rubric) box.append(el('div', {}, el('strong', {}, t('paper.document.scoringRubric')), rubric));
  if (parts.length) {
    const partBox = el('div', { class: 'pl-snapshot-parts' }, el('strong', {}, t('paper.document.subQuestions', { v1: parts.length })));
    parts.forEach((part, index) => {
      const partAnswers = Array.isArray(part.answers) ? part.answers.filter(Boolean).map(String) : [];
      partBox.append(el('div', { class: 'pl-snapshot-part' },
        el('span', { class: 'pl-snapshot-part-index' }, t('paper.question.subQuestion', { v1: part.index || index + 1 })),
        badge(typeLabelOf(part.type), 'blue'),
        part.stem ? markdown(String(part.stem)) : null,
        partAnswers.length ? el('div', { class: 'small-text' }, t('mybank.answer', { v1: partAnswers.join(t('common.semicolonSeparator')) })) : null,
        part.explanation ? el('div', { class: 'small-text' }, t('paper.document.solution_2', { v1: part.explanation })) : null,
        part.rubric ? el('div', { class: 'small-text' }, t('paper.document.scoringRubric_2', { v1: part.rubric })) : null));
    });
    box.append(partBox);
  }
  return box;
}

function snapshotTitle(item) {
  const snapshot = item.snapshot || {};
  const origin = snapshot.origin || {};
  return [origin.year, origin.province, origin.paper_name, origin.number_label]
    .filter(Boolean).join(' · ');
}

function renderQuestionBody(item, { showAnswers = true, paperId = null, position = null } = {}) {
  const snapshot = item.snapshot || {};
  const tags = Array.isArray(snapshot.knowledge_tags) ? snapshot.knowledge_tags : [];
  const box = el('div', { class: 'pl-paper-question-body', 'data-no-i18n': '' });
  const material = materialNode(snapshot);
  if (material) box.append(material);
  box.append(stemNode(snapshot, paperId, position));
  const options = optionsNode(snapshot);
  if (options) box.append(options);
  const appImages = snapshot.source === 'bank' ? [] : appImageNodes(snapshot);
  const bankImages = snapshot.source === 'bank'
    ? bankImageNodes(snapshot, paperId, position) : [];
  if (appImages.length) box.append(el('div', { class: 'pl-snapshot-images' }, appImages));
  if (bankImages.length) box.append(el('div', { class: 'pl-snapshot-images' }, bankImages));
  if (showAnswers) box.append(answersNode(snapshot));
  return { node: box, tags, origin: snapshotTitle(item) };
}

/* ---------------------------------------------------------------------------
 * 试卷列表
 * ------------------------------------------------------------------------- */

export async function renderPaperList({ mount, meta, version, onOpen }) {
  mount.replaceChildren(loading(t('paper.document.loadingMyPapers')));
  const subjects = meta?.subjects || [];
  const filterCard = el('section', { class: 'card pl-paper-filter' });
  const listHost = el('div', { class: 'pl-paper-list' });
  const pagination = el('div', { class: 'pagination pl-paper-pagination', hidden: true });

  const subjectSelect = el('select', { class: 'input' });
  subjectSelect.append(el('option', { value: '' }, t('insight.subjectAll')));
  for (const subject of subjects) subjectSelect.append(el('option', { value: subject }, subject));
  subjectSelect.value = listFilters.subject;
  const kindSelect = el('select', { class: 'input' });
  for (const option of PAPER_KIND_FILTERS) kindSelect.append(el('option', { value: option.value }, option.label));
  kindSelect.value = listFilters.kind;
  const statusSelect = el('select', { class: 'input' });
  for (const option of PAPER_STATUSES) statusSelect.append(el('option', { value: option.value }, option.label));
  statusSelect.value = listFilters.status;

  const refreshBtn = button(t('tasks.refresh'), event => busy(event.currentTarget, () => load()), 'secondary', 'repeat');
  const createBtn = button(t('paper.document.newPaperGoToCompose'), () => go('paperlab', null, { tab: 'compose' }), '', 'plus');

  filterCard.append(
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('paperlab.myPapers')),
        el('p', { class: 'help' }, t('paper.document.papersStoreASnapshotOfTheQuestions'))),
      el('div', { class: 'row' }, refreshBtn, createBtn)),
    el('div', { class: 'pl-filter-grid' },
      field(t('common.subject'), subjectSelect),
      field(t('paper.document.type'), kindSelect),
      field(t('mybank.status'), statusSelect)),
  );
  mount.replaceChildren(filterCard, listHost, pagination);
  let total = 0;

  async function load() {
    listHost.replaceChildren(loading(t('paper.document.loadingPaperList')));
    pagination.hidden = true;
    try {
      const params = new URLSearchParams({
        page: String(listPage), page_size: String(PAPER_PAGE_SIZE),
      });
      listFilters = { subject: subjectSelect.value, kind: kindSelect.value, status: statusSelect.value };
      for (const [key, value] of Object.entries(listFilters)) {
        if (value) params.set(key, value);
      }
      const data = await api(`/paperlab/papers?${params}`);
      if (!current(version)) return;
      total = Number(data.total) || 0;
      renderList(Array.isArray(data.items) ? data.items : [], data);
    } catch (error) {
      if (!current(version)) return;
      listHost.replaceChildren(
        notice(error.message || t('paper.document.failedToLoadThePaperListPlease'), 'error'),
        el('div', { class: 'row' }, button(t('common.retry'), event => busy(event.currentTarget, load), '', 'repeat')));
    }
  }

  function renderList(items, data) {
    listHost.replaceChildren();
    if (!items.length) {
      const filtered = Object.values(listFilters).some(Boolean);
      listHost.append(empty(
        filtered ? t('paper.document.noMatchingPapers') : t('paper.document.noPapersCreatedYet'),
        filtered ? t('paper.document.tryClearingTheFiltersOrCreateA') : t('paper.document.goToComposeAndPickQuestionsManually'),
        button(filtered ? t('practice.clearFilters') : t('paper.document.goToCompose'), () => {
          if (filtered) {
            subjectSelect.value = '';
            kindSelect.value = '';
            statusSelect.value = '';
            listPage = 1;
            load();
          } else {
            go('paperlab', null, { tab: 'compose' });
          }
        }, 'soft', filtered ? 'repeat' : 'plus'),
        'printer'));
      pagination.hidden = true;
      return;
    }
    const cards = el('div', { class: 'pl-paper-cards' });
    for (const paper of items) {
      cards.append(paperCard(paper));
    }
    listHost.append(el('div', { class: 'pl-paper-count' }, t('paper.document.papersInTotal', { v1: total })), cards);
    const totalPages = Math.max(1, Math.ceil(total / PAPER_PAGE_SIZE));
    pagination.hidden = false;
    pagination.replaceChildren(
      el('span', {}, t('common.pageOf', { page: listPage, pages: totalPages })),
      el('div', { class: 'row' },
        button(t('common.prevPage'), () => { if (listPage > 1) { listPage -= 1; load(); } },
          'secondary small', 'chevron', { disabled: listPage <= 1 }),
        button(t('common.nextPage'), () => { if (listPage < totalPages) { listPage += 1; load(); } },
          'secondary small', 'chevron', { disabled: listPage >= totalPages })));
  }

  function paperCard(paper) {
    return el('article', { class: 'pl-paper-card', 'data-paper-id': String(paper.id) },
      el('div', { class: 'pl-paper-card-head' },
        el('strong', { class: 'pl-paper-card-title' }, paper.title || t('paper.document.paper', { v1: paper.id })),
        paperKindBadge(paper.kind),
        paperStatusBadge(paper.status)),
      el('p', { class: 'pl-paper-card-meta' },
        [paper.subject, paper.grade, t('home.activity.items', { count: paper.item_count }), t('paper.document.pointsInTotal', { v1: number(paper.total_score) }), t('common.updatedAt', { time: dateText(paper.updated_at) })]
          .filter(Boolean).join(' · ')),
      el('div', { class: 'row pl-paper-card-actions' },
        button(t('paper.document.openPaper'), () => onOpen(paper.id), 'soft small', 'file'),
        button(t('common.delete'), async event => {
          if (!window.confirm(t('paper.document.deletePaperThisCannotBeUndone', { v1: paper.title || paper.id }))) return;
          await busy(event.currentTarget, async () => {
            try {
              await api(`/paperlab/papers/${paper.id}`, { method: 'DELETE' });
              toast(t('paper.document.paperDeleted'));
              load();
            } catch (error) {
              toast(error.message || t('mybank.deleteFailedPleaseTryAgain'), true);
            }
          });
        }, 'ghost small', 'trash')));
  }

  await load();
}

/* ---------------------------------------------------------------------------
 * 试卷详情（含编辑/发布/导出打印）
 * ------------------------------------------------------------------------- */

export async function renderPaperDetail({ mount, paperId, meta, version, onBack }) {
  mount.replaceChildren(loading(t('paper.document.loadingPaper')));
  let detail;
  try {
    detail = await fetchPaper(paperId);
  } catch (error) {
    if (!current(version)) return;
    mount.replaceChildren(
      notice(error.message || t('paper.document.failedToLoadThePaperPleaseTry'), 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderPaperDetail({ mount, paperId, meta, version, onBack }), '', 'repeat'),
        button(t('paper.document.backToPaperList'), () => onBack(), 'secondary')));
    return;
  }
  if (!current(version)) return;

  const ui = {
    detail,
    items: (detail.items || []).map(item => ({
      id: item.id,
      ref: item.question_ref,
      score: Number(item.score) || 0,
      section: item.section || '',
      snapshot: item.snapshot || {},
      type: item.type,
      knowledge_tags: item.knowledge_tags || [],
      selection_reason: item.selection_reason || '',
    })),
    dirty: false,
    editing: false,
    title: detail.paper?.title || '',
    grade: detail.paper?.grade || '',
    instructions: detail.paper?.instructions || '',
  };
  const editable = () => ui.detail.paper?.status !== 'published';

  const noticeBox = el('div', { class: 'pl-paper-notice' });
  const headBox = el('div', { class: 'pl-paper-detail-head' });
  const actionsBox = el('div', { class: 'pl-paper-actions' });
  const metaBox = el('div', { class: 'pl-paper-meta-form' });
  const itemsBox = el('div', { class: 'pl-paper-items' });
  const exportBox = el('section', { class: 'card pl-paper-export' });

  function renderHead() {
    const paper = ui.detail.paper || {};
    headBox.replaceChildren(
      el('div', { class: 'row' },
        button(t('paper.document.backToPaperList'), () => onBack(), 'ghost small', 'chevron'),
        el('div', {},
          el('h2', { class: 'pl-paper-detail-title', 'data-no-i18n': '' }, paper.title || t('paper.document.paper', { v1: paperId })),
          el('p', { class: 'help', 'data-no-i18n': '' }, [
            paper.subject, paper.grade, t('home.activity.items', { count: ui.items.length }),
            t('paper.document.pointsInTotal', { v1: number(ui.items.reduce((sum, item) => sum + (Number(item.score) || 0), 0)) }),
            t('paper.document.status', { v1: (PAPER_STATUS_META[paper.status] || [paper.status])[0] }),
            t('mybank.versionV', { v1: paper.version }),
          ].filter(Boolean).join(' · ')))),
      actionsBox);
  }

  function renderActions() {
    const paper = ui.detail.paper || {};
    const published = paper.status === 'published';
    // 注意：DOM replaceChildren/append 会把 null 渲染成字面量 "null"，这里统一过滤
    const actionNodes = [
      paperKindBadge(paper.kind),
      paperStatusBadge(paper.status),
      published
        ? button(t('paper.document.unpublishMakeEditableAgain'), event => busy(event.currentTarget, () => setPublished(false)), 'secondary small', 'repeat')
        : button(t('paper.document.publishPaper'), event => busy(event.currentTarget, () => setPublished(true)), 'small', 'check'),
      button(t('insight.activityMix.paper_practice'), () => openPractice(), 'small', 'edit'),
      button(t('paper.document.answerSheet'), () => {
        if (cardBox && cardBox.scrollIntoView) cardBox.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }, 'secondary small', 'printer'),
      button(t('paper.document.scanAnswerSheet'), () => openScanStudio(), 'secondary small', 'scan'),
      editable()
        ? button(ui.editing ? t('paper.document.exitEditing') : t('paper.document.editQuestionOrderPoints'), () => {
          ui.editing = !ui.editing;
          renderActions();
          renderItems();
          renderMetaForm();
        }, 'secondary small', 'edit')
        : null,
      button(t('paper.document.deletePaper'), async event => {
        if (!window.confirm(t('paper.document.deleteThisPaperThisCannotBeUndone'))) return;
        await busy(event.currentTarget, async () => {
          try {
            await api(`/paperlab/papers/${paperId}`, { method: 'DELETE' });
            toast(t('paper.document.paperDeleted'));
            onBack();
          } catch (error) {
            toast(error.message || t('mybank.deleteFailedPleaseTryAgain'), true);
          }
        });
      }, 'ghost small', 'trash'),
    ].filter(Boolean);
    actionsBox.replaceChildren(...actionNodes);
  }

  function renderNotice() {
    const paper = ui.detail.paper || {};
    noticeBox.replaceChildren();
    if (paper.status === 'published') {
      noticeBox.append(notice(
        t('paper.document.paperPublishedOrderPointsAndContentAre')
        + t('paper.document.theWordExportAndPrintPageCome'), ''));
    } else if (paper.status === 'draft') {
      noticeBox.append(notice(t('paper.document.draftPapersStayEditableCheckQuestionsAnd')));
    }
    noticeBox.append(notice(
      t('paper.document.answerSheetsAreGeneratedFromThePaper')
      + t('paper.document.scanAnswerSheetAcceptsMultiPagePhotos')));
  }

  function renderMetaForm() {
    metaBox.replaceChildren();
    const paper = ui.detail.paper || {};
    if (!ui.editing || !editable()) {
      if (paper.instructions) {
        metaBox.append(el('p', { class: 'help pl-paper-instructions', 'data-no-i18n': '' }, t('paper.document.notes', { v1: paper.instructions })));
      }
      return;
    }
    const titleInput = input(t('paper.compose.paperTitle'), { value: ui.title, maxlength: '120' });
    titleInput.addEventListener('input', () => { ui.title = titleInput.value; ui.dirty = true; });
    const gradeSelect = el('select', { class: 'input' });
    gradeSelect.append(el('option', { value: '' }, t('paper.import.gradeOptional')));
    for (const grade of (meta?.grades || [])) gradeSelect.append(el('option', { value: grade }, grade));
    gradeSelect.value = ui.grade || '';
    gradeSelect.addEventListener('change', () => { ui.grade = gradeSelect.value; ui.dirty = true; });
    const instructionsInput = el('textarea', { rows: 2, placeholder: t('paper.compose.examInstructionsOptional') });
    instructionsInput.value = ui.instructions;
    instructionsInput.addEventListener('input', () => { ui.instructions = instructionsInput.value; ui.dirty = true; });
    metaBox.append(
      el('div', { class: 'pl-paper-meta-grid' },
        field(t('paper.compose.paperTitle'), titleInput),
        field(t('mybank.grade'), gradeSelect),
        field(t('paper.compose.examInstructions'), instructionsInput)),
      el('p', { class: 'help' }, t('paper.document.tipSavingReSavesTheWholePaper')));
  }

  function moveItem(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= ui.items.length) return;
    const [item] = ui.items.splice(index, 1);
    ui.items.splice(target, 0, item);
    ui.dirty = true;
    renderItems();
  }

  function renderItems() {
    itemsBox.replaceChildren();
    if (!ui.items.length) {
      itemsBox.append(empty(t('paper.document.thePaperHasNoQuestionsYet'), t('paper.document.goBackToComposeToPickQuestions'),
        button(t('paper.document.goToCompose'), () => go('paperlab', null, { tab: 'compose' }), 'soft', 'plus'), 'file'));
      return;
    }
    if (!ui.editing || !editable()) {
      // 阅读模式：按 section 分组
      const groups = [];
      for (const item of ui.items) {
        const section = item.section || t('physics.question');
        let group = groups.find(entry => entry.section === section);
        if (!group) { group = { section, items: [] }; groups.push(group); }
        group.items.push(item);
      }
      groups.forEach((group, groupIndex) => {
        itemsBox.append(el('h3', { class: 'pl-paper-section-title', 'data-no-i18n': '' }, group.section));
        group.items.forEach((item, index) => {
          const rendered = renderQuestionBody(item, {
            paperId, position: ui.items.indexOf(item) + 1,
          });
          const card = el('article', { class: 'card pl-paper-question', 'data-item-id': String(item.id) },
            el('div', { class: 'pl-paper-question-head' },
              el('span', { class: 'pl-paper-question-no' }, `${ui.items.indexOf(item) + 1}`),
              badge(typeLabelOf(item.type), 'blue'),
              badge(scoreText(item.score), ''),
              item.knowledge_tags.length
                ? el('span', { 'data-no-i18n': '' },
                  badge(item.knowledge_tags.slice(0, 3).join(t('common.listSeparator')), 'teal'))
                : null,
              rendered.origin
                ? el('span', { class: 'help pl-paper-origin', 'data-no-i18n': '' }, rendered.origin)
                : null),
            rendered.node,
            item.selection_reason
              ? el('p', { class: 'help pl-paper-reason', 'data-no-i18n': '' }, t('paper.document.selectionRationale', { v1: item.selection_reason }))
              : null);
          itemsBox.append(card);
        });
      });
      return;
    }
    // 编辑模式：扁平列表 + 顺序/分值/分节
    itemsBox.append(el('div', { class: 'pl-paper-edit-bar' },
      el('span', { class: 'help' }, t('paper.document.afterAdjustingOrderSectionsOrPointsClick'))));
    const list = el('div', { class: 'pl-paper-edit-list' });
    ui.items.forEach((item, index) => {
      const scoreInput = el('input', {
        type: 'number', class: 'input pl-score-input', min: '0', max: '100', step: '0.5',
        value: String(item.score), 'aria-label': t('paper.compose.pointsForQuestion', { v1: index + 1 }),
      });
      scoreInput.addEventListener('input', () => {
        ui.items[index].score = Number(scoreInput.value) || 0;
        ui.dirty = true;
      });
      const sectionInput = input(t('paper.compose.sectionEGMultipleChoice'), { value: item.section, maxlength: '40' });
      sectionInput.addEventListener('input', () => {
        ui.items[index].section = sectionInput.value.trim();
        ui.dirty = true;
      });
      const rendered = renderQuestionBody(item, {
        showAnswers: false, paperId, position: index + 1,
      });
      list.append(el('article', { class: 'pl-paper-edit-item', 'data-item-id': String(item.id) },
        el('div', { class: 'pl-paper-edit-head' },
          el('span', { class: 'pl-paper-question-no' }, `${index + 1}`),
          badge(typeLabelOf(item.type), 'blue'),
          badge(item.snapshot?.source === 'bank' ? t('paper.compose.officialExamBank') : t('home.status.bankTotal'), item.snapshot?.source === 'bank' ? '' : 'teal'),
          el('div', { class: 'row pl-paper-edit-order' },
            button(t('paper.compose.moveUp'), () => moveItem(index, -1), 'ghost small', 'chevron', { disabled: index === 0 }),
            button(t('paper.compose.moveDown'), () => moveItem(index, 1), 'ghost small', 'chevron',
              { disabled: index === ui.items.length - 1 }),
            button(t('upload.remove'), () => {
              if (!window.confirm(t('paper.document.removeQuestionFromThePaperItTakes', { v1: index + 1 }))) return;
              ui.items.splice(index, 1);
              ui.dirty = true;
              renderItems();
            }, 'ghost small', 'trash'))),
        el('div', { class: 'pl-paper-edit-fields' },
          field(t('paper.compose.points'), scoreInput),
          field(t('paper.compose.section'), sectionInput)),
        el('details', { class: 'pl-paper-edit-preview' },
          el('summary', {}, t('paper.document.viewQuestionSnapshot')),
          rendered.node)));
    });
    itemsBox.append(list, el('div', { class: 'row end pl-paper-edit-actions' },
      button(t('settings.wizard.saveChanges'), event => busy(event.currentTarget, saveItems), '', 'check')));
  }

  async function saveItems() {
    const paper = ui.detail.paper || {};
    if (!editable()) throw new Error(t('paper.document.thePaperIsPublishedAndCannotBe'));
    const payload = {
      version: paper.version,
      title: (ui.title || '').trim() || paper.title,
      grade: ui.grade,
      instructions: ui.instructions,
      items: ui.items.map(item => ({
        ref: item.ref,
        score: Number(item.score) || 0,
        section: item.section || '',
      })),
    };
    try {
      const result = await api(`/paperlab/papers/${paperId}`, { method: 'PATCH', data: payload });
      applyDetail(result);
      ui.dirty = false;
      toast(t('paper.document.paperStructureSavedAndQuestionSnapshotsRe'));
    } catch (error) {
      const message = error.message || t('paper.question.saveFailedPleaseTryAgain');
      noticeBox.replaceChildren(notice(message, 'error'),
        /版本冲突|已被修改/.test(message)
          ? el('div', { class: 'row' },
            button(t('paper.question.reloadTheLatestVersion'), event => busy(event.currentTarget, () => renderPaperDetail({
              mount, paperId, meta, version, onBack,
            })), 'soft small', 'repeat'))
          : null);
      throw error;
    }
  }

  function applyDetail(result) {
    detail = result;
    ui.detail = result;
    ui.items = (result.items || []).map(item => ({
      id: item.id,
      ref: item.question_ref,
      score: Number(item.score) || 0,
      section: item.section || '',
      snapshot: item.snapshot || {},
      type: item.type,
      knowledge_tags: item.knowledge_tags || [],
      selection_reason: item.selection_reason || '',
    }));
    ui.editing = false;
    renderHead();
    renderActions();
    renderNotice();
    renderMetaForm();
    renderItems();
    renderExport();
  }

  async function setPublished(published) {
    try {
      const result = await api(`/paperlab/papers/${paperId}/publish`, {
        method: 'POST', data: { published },
      });
      applyDetail(result);
      toast(published ? t('paper.document.paperPublishedOrderAndSnapshotsAreLocked') : t('paper.document.unpublishedYouCanEditAgain'));
    } catch (error) {
      noticeBox.replaceChildren(notice(error.message || t('paper.document.thePublishActionFailedPleaseTryAgain'), 'error'));
      throw error;
    }
  }

  function renderExport() {
    const paper = ui.detail.paper || {};
    exportBox.replaceChildren(
      el('div', { class: 'card-header' },
        el('div', {},
          el('h3', {}, t('paper.document.exportAndPrint')),
          el('p', { class: 'help' }, t('paper.document.contentComesFromTheQuestionSnapshotAt')))),
      el('div', { class: 'pl-export-actions' },
        button(t('paper.document.downloadWordWithAnswersAndSolutions'), async event => busy(event.currentTarget, async () => {
          try {
            await downloadDocx(paper, true);
            toast(t('paper.document.wordWithAnswersDownloadStarted'));
          } catch (error) {
            toast(error.message || t('paper.document.wordExportFailedPleaseTryAgain'), true);
            throw error;
          }
        }), '', 'download'),
        button(t('paper.document.downloadWordStudentVersionNoAnswers'), async event => busy(event.currentTarget, async () => {
          try {
            await downloadDocx(paper, false);
            toast(t('paper.document.wordStudentVersionDownloadStarted'));
          } catch (error) {
            toast(error.message || t('paper.document.wordExportFailedPleaseTryAgain'), true);
            throw error;
          }
        }), 'secondary', 'download'),
        button(t('paper.document.downloadPdfWithAnswers'), async event => busy(event.currentTarget, async () => {
          try {
            await downloadPdf(paper, true);
            toast(t('paper.document.pdfWithAnswersDownloadStarted'));
          } catch (error) {
            toast(t('paper.document.youCanUsePrintSaveAsPdf', {
              v1: error.message || t('paper.document.pdfExportFailed'),
            }), true);
            throw error;
          }
        }), '', 'download'),
        button(t('paper.document.downloadPdfStudentVersion'), async event => busy(event.currentTarget, async () => {
          try {
            await downloadPdf(paper, false);
            toast(t('paper.document.pdfStudentVersionDownloadStarted'));
          } catch (error) {
            toast(t('paper.document.youCanUsePrintStudentVersionAnd', {
              v1: error.message || t('paper.document.pdfExportFailed'),
            }), true);
            throw error;
          }
        }), 'secondary', 'download'),
        button(t('paper.document.printSaveAsPdfWithAnswers'), () => printPaper(paper, true), 'secondary', 'printer'),
        button(t('paper.document.printStudentVersion'), () => printPaper(paper, false), 'secondary', 'printer'),
        button(t('paper.document.previewPrintPage'), () => openPrintPreview(paper, true), 'ghost', 'eye')));
  }

  const practiceBox = el('section', { class: 'card pl-paper-practice' });
  const cardBox = el('section', { class: 'card pl-paper-cards' });
  const scanBox = el('section', { class: 'card pl-paper-scans' });

  function openCardStudio(cardId = null) {
    renderCardStudio({
      mount,
      paperId,
      version,
      cardId,
      onExit: () => renderPaperDetail({ mount, paperId, meta, version, onBack }),
    });
  }

  function openScanStudio(scanId = null) {
    renderScanStudio({
      mount,
      paperId,
      version,
      scanId,
      onExit: () => renderPaperDetail({ mount, paperId, meta, version, onBack }),
    });
  }

  function renderScanSectionFor() {
    renderScanSection({
      mount: scanBox,
      paperId,
      version,
      onOpenScan: scanId => openScanStudio(scanId),
    });
  }

  function renderCardSectionFor() {
    renderCardSection({ mount: cardBox, paperId, version, onOpenCard: cardId => openCardStudio(cardId) });
  }

  function openPractice(attemptId = null) {
    renderPaperPractice({
      mount,
      paperId,
      version,
      attemptId,
      onExit: () => renderPaperDetail({ mount, paperId, meta, version, onBack }),
    });
  }

  function practiceHistoryRow(entry) {
    const submitted = entry.status === 'submitted';
    return el('div', { class: 'pl-practice-history-item' },
      el('div', { class: 'pl-practice-history-main' },
        el('strong', {}, submitted
          ? t('paper.document.submittedPts', { v1: number(entry.score), v2: number(entry.max_score) })
          : t('paper.document.unsubmittedPractice')),
        el('p', { class: 'help' }, [
          dateText(entry.submitted_at || entry.started_at),
          submitted
            ? t('paper.document.correctWrongPendingReview', { v1: entry.correct_count, v2: entry.incorrect_count, v3: entry.pending_count })
            : t('paper.document.youCanContinueYourLastAttempt'),
        ].filter(Boolean).join(' · '))),
      submitted
        ? button(t('paper.document.viewResults'), () => openPractice(entry.id), 'soft small', 'file')
        : button(t('paper.document.continueAnswering'), () => openPractice(entry.id), 'small', 'edit'));
  }

  function renderPracticeCard() {
    practiceBox.replaceChildren(el('div', { class: 'card-header' },
      el('div', {},
        el('h3', {}, t('paper.document.fullPaperPracticeSelfTest')),
        el('p', { class: 'help' }, t('paper.document.answerInPaperOrderObjectiveQuestionsAre'))),
      button(t('paper.document.startFullPaperPractice'), () => openPractice(), '', 'edit')));
    const listHost = el('div', { class: 'pl-practice-history' }, el('p', { class: 'help' }, t('paper.document.loadingPracticeHistory')));
    practiceBox.append(listHost);
    api(`/paperlab/papers/${paperId}/attempts`).then(data => {
      if (!current(version)) return;
      const items = Array.isArray(data.items) ? data.items : [];
      listHost.replaceChildren();
      if (!items.length) {
        listHost.append(el('p', { class: 'help' }, t('paper.document.noPracticeRecordsYetClickStartFull')));
        return;
      }
      listHost.append(el('h4', { class: 'pl-practice-history-title' }, t('paper.document.practiceHistoryAttempts', { v1: items.length })));
      for (const entry of items.slice(0, 5)) {
        listHost.append(practiceHistoryRow(entry));
      }
    }).catch(() => {
      if (!current(version)) return;
      listHost.replaceChildren(el('p', { class: 'help' }, t('paper.document.failedToLoadPracticeHistoryRefreshLater')));
    });
  }

  mount.replaceChildren(headBox, noticeBox, metaBox, itemsBox, exportBox, practiceBox, cardBox, scanBox);
  renderHead();
  renderActions();
  renderNotice();
  renderMetaForm();
  renderItems();
  renderExport();
  renderPracticeCard();
  renderCardSectionFor();
  renderScanSectionFor();
}
