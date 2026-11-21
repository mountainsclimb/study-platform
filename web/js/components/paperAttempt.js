/* 试卷工坊 · 整卷练习（个人自测）：开始练习 → 逐题作答/保存草稿 → 提交判分 → 结果与错题提示。
 *
 * - 数据：POST /api/paperlab/papers/{id}/attempts 创建练习；GET /attempts/{id} 读取卷面与已保存作答；
 *   PATCH /attempts/{id}/answers 逐题保存（幂等）；POST /attempts/{id}/submit 判分。
 * - 判分口径由后端保证：单选/多选按快照 label 自动判分、填空归一化比对、主观题标 pending 不硬判；
 *   提交前卷面不返回答案（后端已裁剪），前端不做任何本地判分。
 * - 错题：只读题库来源的错题由后端写入既有错题本；应用题库题记录在练习结果中（页面明确提示）。
 */
import { api } from '../core/api.js';
import { onSessionReset } from '../core/state.js';
import {
  el, button, badge, notice, empty, loading, busy, toast, number, dateText,
  questionText, bankImage,
} from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { current, go } from '../core/router.js';
import { t } from '../core/i18n.js';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const TYPE_LABELS = {
  get SINGLE() { return t('question.type.single_choice'); },
  get MULTIPLE() { return t('question.type.multi_choice'); },
  get BLANK() { return t('question.type.fill_blank'); },
  get SUBJECTIVE() { return t('mybank.subjective'); },
  get MIXED() { return t('mybank.mixed'); },
};
const JUDGEMENT_META = {
  get correct() { return [t('paper.attempt.correct'), 'teal']; },
  get incorrect() { return [t('paper.attempt.wrong'), 'red']; },
  get pending() { return [t('home.importStatus.ready'), 'amber']; },
};

let practiceState = null;

onSessionReset(() => {
  practiceState = null;
});

function typeLabel(type) {
  return TYPE_LABELS[String(type || '').toUpperCase()] || t('physics.question');
}

function judgementBadge(judgement) {
  const meta = JUDGEMENT_META[judgement] || [t('paper.attempt.ungraded'), ''];
  return badge(meta[0], meta[1]);
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

function answerTextOf(value) {
  if (value === null || value === undefined || value === '') return t('question.notAnswered');
  if (Array.isArray(value)) return value.filter(item => String(item).trim()).join(t('common.listSeparator')) || t('question.notAnswered');
  if (typeof value === 'object') {
    const parts = value.parts;
    if (Array.isArray(parts)) {
      const text = parts
        .map(part => (part && typeof part === 'object' ? part.answer : part))
        .filter(item => item !== null && item !== undefined && String(item).trim())
        .join(t('common.semicolonSeparator'));
      return text || t('question.notAnswered');
    }
    return JSON.stringify(value);
  }
  return String(value).trim() || t('question.notAnswered');
}

function isBlankAnswer(value) {
  if (value === null || value === undefined) return true;
  if (Array.isArray(value)) return value.every(item => !String(item || '').trim());
  if (typeof value === 'object') {
    const parts = value.parts;
    if (Array.isArray(parts)) {
      return parts.every(part => {
        const answer = part && typeof part === 'object' ? part.answer : part;
        return answer === null || answer === undefined || !String(answer).trim();
      });
    }
    return true;
  }
  return !String(value).trim();
}

/* ---------------------------------------------------------------------------
 * 题面渲染（卷面无答案；结果页用后端返回的 reference）
 * ------------------------------------------------------------------------- */

function frozenImageNode(paperId, position, index, label = t('question.figure')) {
  return el('img', {
    class: 'pl-snapshot-image',
    src: `/api/paperlab/papers/${paperId}/items/${position}/images/${index}`,
    alt: label,
    loading: 'lazy',
    onError: event => {
      event.currentTarget.replaceWith(el('span', { class: 'image-error' }, t('paper.document.imageTemporarilyUnavailable')));
    },
  });
}

function consumedImageIndexes(item) {
  const content = item.content || {};
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

function imageNodes(item, paperId) {
  const content = item.content || {};
  const images = Array.isArray(content.images) ? content.images : [];
  const consumed = consumedImageIndexes(item);
  const nodes = [];
  images.forEach((image, index) => {
    if (!image || typeof image !== 'object') return;
    if (consumed.has(index)) return;
    if (image.file && paperId) {
      nodes.push(frozenImageNode(paperId, item.item_position, index,
        image.role === 'formula' ? t('question.sourceFormula') : t('question.figure')));
    } else if (image.file) {
      nodes.push(bankImage(item.subject || '', image, {}));
    } else if (image.url) {
      nodes.push(el('img', {
        class: 'pl-snapshot-image', src: image.url, alt: t('question.figure'), loading: 'lazy',
      }));
    }
  });
  return nodes;
}

function frozenBankStem(item, paperId) {
  const content = item.content || {};
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
    if (image && paperId) {
      node.append(frozenImageNode(paperId, item.item_position, images.indexOf(image),
        role === 'formula' ? t('question.sourceFormula') : t('question.figure')));
    } else {
      node.append(el('span', { class: 'image-error' }, t('question.partMissing', { part: part })));
    }
  }
  return node;
}

function stemNode(item, paperId) {
  const content = item.content || {};
  const stem = String(content.stem || '');
  if (!stem) return el('p', { class: 'help' }, t('paper.attempt.thisQuestionHasNoStem'));
  if (item.source === 'bank') {
    if (paperId) return frozenBankStem(item, paperId);
    const images = (content.images || []).filter(image => image && image.file);
    return questionText(stem, images, item.subject || '', '', {});
  }
  return markdown(stem);
}

function materialNode(item) {
  const material = (item.content || {}).material;
  if (!material || typeof material !== 'object') return null;
  const text = String(material.text || '');
  const images = (Array.isArray(material.images) ? material.images : []).filter(image => image && image.file);
  if (!text && !images.length) return null;
  return el('div', { class: 'question-material pl-practice-material' },
    el('strong', {}, t('paper.document.material')),
    text ? questionText(text, images, item.subject || '', '', {}) : null);
}

/* ---------------------------------------------------------------------------
 * 作答控件
 * ------------------------------------------------------------------------- */

function choiceWidget(item, selected, onChange, options = null, multiple = null) {
  const opts = options || (item.content || {}).options || [];
  const isMultiple = multiple === null ? String(item.type).toUpperCase() === 'MULTIPLE' : multiple;
  const box = el('div', { class: 'options pl-practice-options' });
  for (const option of opts) {
    const label = String(option.label || '');
    const input = el('input', {
      type: isMultiple ? 'checkbox' : 'radio',
      name: `pl-practice-${item.item_position}-${label}-${Math.random().toString(36).slice(2, 6)}`,
      checked: Array.isArray(selected) && selected.includes(label),
      value: label,
    });
    input.addEventListener('change', () => {
      if (!isMultiple) {
        onChange(input.checked ? [label] : []);
      } else if (input.checked) {
        const next = Array.isArray(selected) ? [...selected] : [];
        if (!next.includes(label)) next.push(label);
        onChange(next.sort());
      } else {
        onChange((Array.isArray(selected) ? selected : []).filter(value => value !== label));
      }
      box.querySelectorAll('.option').forEach(node => {
        const control = node.querySelector('input');
        node.classList.toggle('is-picked', Boolean(control && control.checked));
      });
    });
    const node = el('label', { class: 'option' },
      input,
      el('span', { class: 'option-label' }, label),
      el('span', { class: 'option-content' }, String(option.text || '')));
    if (input.checked) node.classList.add('is-picked');
    box.append(node);
  }
  return box;
}

function textWidget(item, value, onChange, placeholder) {
  const area = el('textarea', { rows: 4, placeholder: placeholder || t('paper.attempt.writeYourAnswer') });
  area.value = typeof value === 'string' ? value : answerTextOf(value);
  area.addEventListener('input', () => onChange(area.value));
  return area;
}

function answerEditor(item, answer, onChange) {
  const type = String(item.type || '').toUpperCase();
  const box = el('div', { class: 'pl-practice-answer', 'data-no-i18n': '' });
  if (type === 'SINGLE' || type === 'MULTIPLE') {
    box.append(choiceWidget(item, answer, onChange));
    return box;
  }
  if (type === 'BLANK') {
    box.append(
      el('p', { class: 'help' }, t('paper.attempt.separateMultipleBlanksWithEnterTheAnswers')),
      textWidget(item, answer, onChange, t('paper.attempt.eG6X2')));
    return box;
  }
  if (type === 'MIXED') {
    const parts = (item.content || {}).parts || [];
    const current = answer && typeof answer === 'object' && Array.isArray(answer.parts) ? answer.parts : [];
    const byIndex = new Map(current.map(part => [Number(part.index) || 0, part.answer]));
    const nextParts = [...current];
    const sync = (index, partAnswer) => {
      const existing = nextParts.find(part => Number(part.index) === Number(index));
      if (existing) existing.answer = partAnswer;
      else nextParts.push({ index, answer: partAnswer });
      onChange({ parts: nextParts.map(part => ({ index: Number(part.index), answer: part.answer })) });
    };
    for (const part of parts) {
      const index = Number(part.index) || 1;
      const partType = String(part.type || 'SUBJECTIVE').toUpperCase();
      const partAnswer = byIndex.get(index);
      const partBox = el('div', { class: 'pl-practice-part' },
        el('div', { class: 'row' },
          badge(t('paper.question.subQuestion', { v1: index }), 'blue'),
          badge(typeLabel(partType), 'teal'),
          part.judgement ? judgementBadge(part.judgement) : null),
        part.stem ? markdown(String(part.stem)) : null);
      if (partType === 'SINGLE' || partType === 'MULTIPLE') {
        partBox.append(choiceWidget(item, partAnswer, value => sync(index, value),
          part.options || [], partType === 'MULTIPLE'));
      } else {
        partBox.append(textWidget(item, partAnswer, value => sync(index, value),
          t('paper.attempt.subQuestionAnswer', { v1: index })));
      }
      box.append(partBox);
    }
    return box;
  }
  box.append(textWidget(item, answer, onChange, t('paper.attempt.subjectiveWriteYourAnswerItIsMarked')));
  return box;
}

/* ---------------------------------------------------------------------------
 * 历史 / 开始页
 * ------------------------------------------------------------------------- */

function historyRow(entry, { onResume, onViewResult }) {
  const submitted = entry.status === 'submitted';
  return el('div', { class: 'pl-practice-history-item' },
    el('div', { class: 'pl-practice-history-main' },
      el('strong', {}, submitted ? t('paper.document.submittedPts', { v1: number(entry.score), v2: number(entry.max_score) }) : t('paper.document.unsubmittedPractice')),
      el('p', { class: 'help' },
        [dateText(entry.submitted_at || entry.started_at),
          submitted
            ? t('paper.document.correctWrongPendingReview', { v1: entry.correct_count, v2: entry.incorrect_count, v3: entry.pending_count })
            : t('paper.attempt.youCanContinueYourLastAttempt')].filter(Boolean).join(' · '))),
    submitted
      ? button(t('paper.document.viewResults'), () => onViewResult(entry), 'soft small', 'file')
      : button(t('paper.document.continueAnswering'), () => onResume(entry), 'small', 'edit'));
}

async function renderStart(mount, { paperId, version, onExit }) {
  mount.replaceChildren(loading(t('paper.attempt.loadingFullPaperPractice')));
  let paper = null;
  let history = [];
  try {
    const [paperData, historyData] = await Promise.all([
      api(`/paperlab/papers/${paperId}?include_snapshot=false`),
      api(`/paperlab/papers/${paperId}/attempts`),
    ]);
    paper = paperData.paper;
    history = Array.isArray(historyData.items) ? historyData.items : [];
  } catch (error) {
    if (!current(version)) return;
    mount.replaceChildren(
      notice(error.message || t('paper.document.failedToLoadThePaperPleaseTry'), 'error'),
      el('div', { class: 'row' },
        button(t('common.reload'), () => renderStart(mount, { paperId, version, onExit }), '', 'repeat'),
        button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'secondary')));
    return;
  }
  if (!current(version)) return;
  const inProgress = history.find(entry => entry.status !== 'submitted');

  const beginNew = (event) => busy(event.currentTarget, async () => {
    const response = await api(`/paperlab/papers/${paperId}/attempts`, { method: 'POST' });
    practiceState = buildPracticeState(response);
    renderPractice(mount, version, onExit);
  });
  const resume = entry => {
    api(`/paperlab/attempts/${entry.id}`).then(data => {
      if (!current(version)) return;
      practiceState = buildPracticeState(data);
      if ((data.attempt?.status || '') === 'submitted') renderResult(mount, version, onExit);
      else renderPractice(mount, version, onExit);
    }).catch(error => toast(error.message || t('paper.attempt.failedToLoadPracticeRecords'), true));
  };
  const viewResult = entry => {
    api(`/paperlab/attempts/${entry.id}`).then(data => {
      if (!current(version)) return;
      practiceState = buildPracticeState(data);
      renderResult(mount, version, onExit);
    }).catch(error => toast(error.message || t('paper.attempt.failedToLoadPracticeResults'), true));
  };

  const historyBox = el('div', { class: 'pl-practice-history' });
  if (history.length) {
    historyBox.append(el('h3', {}, t('paper.attempt.practiceHistory')));
    for (const entry of history.slice(0, 10)) {
      historyBox.append(historyRow(entry, { onResume: resume, onViewResult: viewResult }));
    }
  } else {
    historyBox.append(el('p', { class: 'help' }, t('paper.attempt.noPracticeRecordsYetSubmitAnAttempt')));
  }

  mount.replaceChildren(el('section', { class: 'card pl-practice-start' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('insight.activityMix.paper_practice')),
        el('p', { class: 'help' }, t('paper.attempt.answerInPaperOrderObjectiveQuestionsAre'))),
      button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'ghost small', 'chevron')),
    el('div', { class: 'pl-practice-start-meta' },
      el('strong', { 'data-no-i18n': '' }, paper.title || t('paper.document.paper', { v1: paperId })),
      paperKindBadges(paper),
      el('p', { class: 'help', 'data-no-i18n': '' },
        [paper.subject, paper.grade, t('home.activity.items', { count: paper.item_count }), t('paper.attempt.pointsInTotal', { v1: number(paper.total_score) }),
          t('paper.attempt.selfTestNoPublishingOrCollectionInvolved')].filter(Boolean).join(' · '))),
    el('div', { class: 'row pl-practice-start-actions' },
      inProgress
        ? button(t('paper.attempt.continueLastAttempt'), () => resume(inProgress), 'secondary', 'edit')
        : null,
      button(inProgress ? t('paper.attempt.startANewPracticeAttempt') : t('paper.attempt.startAnswering'), beginNew, '', 'file')),
    historyBox));
}

function paperKindBadges(paper) {
  return el('div', { class: 'row' },
    badge(paper.kind === 'manual' ? t('paper.compose.manualComposition') : (paper.kind || t('home.status.papers')), ''),
    badge(paper.status === 'published' ? t('admin.issueStatus.published') : t('paper.attempt.unpublished'), paper.status === 'published' ? 'teal' : 'blue'));
}

/* ---------------------------------------------------------------------------
 * 作答视图
 * ------------------------------------------------------------------------- */

function buildPracticeState(payload) {
  const items = Array.isArray(payload.items) ? payload.items : [];
  const answers = {};
  for (const item of items) {
    answers[item.item_position] = item.answer ?? null;
  }
  return {
    attempt: payload.attempt || {},
    paper: payload.paper || {},
    items,
    answers,
    index: 0,
    mode: (payload.attempt?.status || '') === 'submitted' ? 'result' : 'practice',
    result: payload.result || null,
    // 卷面版本冻结（P0）：true=按开始练习时的逐题快照展示；false=历史作答回退现读，题面不可追溯。
    frozen: payload.frozen !== false,
    paperDeleted: Boolean(payload.paper_deleted),
    notice: typeof payload.notice === 'string' ? payload.notice : '',
  };
}

/** 练习/结果页顶部的版本冻结提示（冻结卷面 / 历史不可追溯 / 试卷已删除）。 */
function freezeNotices(stateData) {
  const nodes = [
    stateData.frozen
      ? notice(t('paper.attempt.frozenSheet'))
      : notice(t('paper.attempt.frozenSheetLegacy'), 'warning'),
  ];
  if (stateData.paperDeleted) {
    nodes.push(notice(t('paper.attempt.paperDeleted'), 'warning'));
  }
  return nodes;
}

function currentItem(stateData) {
  return stateData.items[stateData.index] || null;
}

function answeredCount(stateData) {
  return stateData.items.filter(item => !isBlankAnswer(stateData.answers[item.item_position])).length;
}

async function persistCurrent(stateData) {
  const item = currentItem(stateData);
  if (!item) return;
  const attemptId = stateData.attempt?.id;
  if (!attemptId) return;
  await api(`/paperlab/attempts/${attemptId}/answers`, {
    method: 'PATCH',
    data: { item_position: item.item_position, answer: stateData.answers[item.item_position] ?? null },
  });
}

function renderPractice(mount, version, onExit) {
  const stateData = practiceState;
  if (!stateData) return;
  const navigator = el('div', { class: 'pl-practice-nav' });
  const questionHost = el('div', { class: 'pl-practice-question', 'data-no-i18n': '' });
  const statusBox = el('div', { class: 'pl-practice-status' });

  function renderNav() {
    navigator.replaceChildren(el('h3', {}, t('physics.question')));
    const grid = el('div', { class: 'pl-practice-nav-grid' });
    stateData.items.forEach((item, index) => {
      const answered = !isBlankAnswer(stateData.answers[item.item_position]);
      grid.append(el('button', {
        type: 'button',
        class: `pl-practice-nav-item${index === stateData.index ? ' active' : ''}${answered ? ' answered' : ''}`,
        'aria-label': t('paper.attempt.question', {
          v1: index + 1,
          v2: answered ? t('paper.attempt.answeredSuffix') : '',
        }),
        onClick: () => goTo(index),
      }, String(index + 1)));
    });
    navigator.append(grid,
      el('p', { class: 'help' }, t('paper.attempt.answeredQuestions', { v1: answeredCount(stateData), v2: stateData.items.length })),
      el('div', { class: 'pl-practice-nav-actions' },
        button(t('paper.import.saveDraft'), async event => busy(event.currentTarget, async () => {
          await persistCurrent(stateData);
          toast(t('paper.attempt.answersSavedYouCanContinueLater'));
        }), 'secondary small', 'notes'),
        button(t('paper.attempt.submitAndGrade'), () => submitAll(), '', 'check')));
  }

  async function goTo(index) {
    if (index === stateData.index) return;
    try {
      await persistCurrent(stateData);
    } catch (error) {
      toast(error.message || t('paper.attempt.failedToSaveAnswersPleaseTryAgain'), true);
      return;
    }
    stateData.index = Math.max(0, Math.min(stateData.items.length - 1, index));
    renderNav();
    renderQuestion();
  }

  function renderQuestion() {
    const item = currentItem(stateData);
    questionHost.replaceChildren();
    if (!item) {
      questionHost.append(empty(t('paper.attempt.noQuestions'), t('paper.attempt.goBackToThePaperDetailsTo'), null, 'file'));
      return;
    }
    const index = stateData.index;
    const tags = Array.isArray(item.knowledge_tags) ? item.knowledge_tags : [];
    const head = el('div', { class: 'pl-practice-question-head' },
      el('span', { class: 'pl-practice-question-no' }, `${index + 1}`),
      badge(typeLabel(item.type), 'blue'),
      // 来源徽标统一走后端 source_label（高考题库/平台精选/我的题库），缺失时按来源兜底。
      badge(item.source_label
        || (item.source === 'bank' ? t('paper.compose.officialExamBank') : t('question.myBankSource')),
        item.source === 'bank' ? '' : 'teal'),
      badge(difficultyText(item.difficulty), ''),
      badge(t('admission.scorePoints', { v1: number(item.score) }), ''),
      tags.length ? badge(tags.slice(0, 3).join(t('common.listSeparator')), 'teal') : null,
      el('span', { class: 'pl-practice-progress-label', style: 'margin-left:auto' }, t('paper.attempt.question_2', { v1: index + 1, v2: stateData.items.length })));
    const paperId = stateData.attempt?.paper_id || stateData.paper?.id || 0;
    const material = materialNode(item);
    const stem = stemNode(item, paperId);
    const images = imageNodes(item, paperId);
    const answerHost = el('div', { class: 'pl-practice-answer-host' });
    const answer = stateData.answers[item.item_position];
    const editor = answerEditor(item, answer, value => {
      stateData.answers[item.item_position] = value;
      renderNav();
    });
    answerHost.append(editor);
    const footer = el('div', { class: 'row pl-practice-question-actions' },
      button(t('paper.attempt.previous'), () => goTo(index - 1), 'secondary', 'chevron', { disabled: index === 0 }),
      button(t('paper.attempt.next'), () => goTo(index + 1), '', 'chevron', { disabled: index >= stateData.items.length - 1 }));
    const headNodes = [head];
    if (material) headNodes.push(material);
    headNodes.push(stem);
    if (images.length) headNodes.push(el('div', { class: 'pl-snapshot-images pl-practice-images' }, images));
    headNodes.push(answerHost, footer);
    questionHost.append(...headNodes);
    statusBox.replaceChildren(
      el('p', { class: 'help' }, t('paper.attempt.tipUseSaveDraftAtAnyTime')),
      el('p', { class: 'help' }, t('paper.attempt.progressQuestionsAnswered', { v1: answeredCount(stateData), v2: stateData.items.length })));
  }

  async function submitAll() {
    const unanswered = stateData.items.length - answeredCount(stateData);
    if (unanswered > 0) {
      const ok = window.confirm(t('paper.attempt.questionsAreUnansweredSubmitAndGradeUnanswered', { v1: unanswered }));
      if (!ok) return;
    }
    try {
      await persistCurrent(stateData);
      const answers = stateData.items.map(item => ({
        item_position: item.item_position,
        answer: stateData.answers[item.item_position] ?? null,
      }));
      const result = await api(`/paperlab/attempts/${stateData.attempt.id}/submit`, {
        method: 'POST', data: { answers },
      });
      practiceState = buildPracticeState(result);
      renderResult(mount, version, onExit);
    } catch (error) {
      toast(error.message || t('paper.attempt.submissionFailedPleaseTryAgain'), true);
    }
  }

  const shell = el('section', { class: 'card pl-practice' },
    el('div', { class: 'card-header pl-practice-head' },
      el('div', {},
        el('h2', { 'data-no-i18n': '' }, t('paper.attempt.fullPaperPractice', { v1: stateData.paper.title || '' })),
        el('p', { class: 'help' }, t('paper.attempt.selfTestAnswerQuestionByQuestionAnd'))),
      button(t('paper.attempt.backToPaperDetails'), () => {
        if (window.confirm(t('paper.attempt.leavePracticeAnsweredContentIsSavedIn'))) onExit();
      }, 'ghost small', 'chevron')),
    ...freezeNotices(stateData),
    el('div', { class: 'pl-practice-grid' }, navigator, el('div', { class: 'pl-practice-main' }, questionHost, statusBox)));
  mount.replaceChildren(shell);
  renderNav();
  renderQuestion();
}

/* ---------------------------------------------------------------------------
 * 结果视图
 * ------------------------------------------------------------------------- */

function referenceNode(item) {
  const reference = item.reference || {};
  const box = el('div', { class: 'pl-practice-reference', 'data-no-i18n': '' },
    el('strong', {}, t('question.referenceAnswer')));
  const answers = Array.isArray(reference.answers) ? reference.answers.filter(Boolean) : [];
  if (answers.length) box.append(markdown(answers.join(t('common.semicolonSeparator'))));
  else if (reference.answer_text) box.append(markdown(String(reference.answer_text)));
  if (reference.explanation) {
    box.append(el('div', { class: 'pl-practice-explanation' },
      el('strong', {}, t('question.solution')), markdown(String(reference.explanation))));
  }
  if (reference.rubric) {
    box.append(el('p', { class: 'help' }, t('paper.document.scoringRubric_2', { v1: reference.rubric })));
  }
  return box;
}

function userAnswerNode(item) {
  const answer = item.answer;
  return el('div', { class: 'pl-practice-user-answer' },
    el('strong', {}, t('paper.attempt.myAnswer')),
    el('p', {}, answerTextOf(answer)));
}

function resultItemCard(item, index, paperId) {
  const card = el('article', {
    class: `card pl-practice-result-item pl-practice-result-${item.judgement || 'pending'}`,
  });
  const head = el('div', { class: 'pl-practice-result-head' },
    el('span', { class: 'pl-practice-question-no' }, `${index + 1}`),
    badge(typeLabel(item.type), 'blue'),
    judgementBadge(item.judgement),
    badge(t('paper.attempt.pts', { v1: number(item.score), v2: number(item.max_score ?? item.score) }),
      item.judgement === 'correct' ? 'teal' : ''),
    item.feedback ? el('span', { class: 'help' }, item.feedback) : null);
  card.append(head);
  const material = materialNode(item);
  if (material) card.append(material);
  card.append(stemNode(item, paperId));
  const images = imageNodes(item, paperId);
  if (images.length) card.append(el('div', { class: 'pl-snapshot-images' }, images));
  card.append(userAnswerNode(item), referenceNode(item));
  const parts = item.content?.parts || [];
  if (parts.length) {
    const partBox = el('div', { class: 'pl-practice-result-parts' });
    for (const part of parts) {
      partBox.append(el('div', { class: 'pl-practice-result-part' },
        el('div', { class: 'row' },
          badge(t('paper.question.subQuestion', { v1: part.index }), 'blue'),
          badge(typeLabel(part.type), 'teal'),
          part.judgement ? judgementBadge(part.judgement) : null),
        part.stem ? markdown(String(part.stem)) : null,
        part.reference && (part.reference.answers || []).length
          ? el('div', { class: 'small-text' }, t('physics.referenceAnswer', { v1: (part.reference.answers || []).join(t('common.semicolonSeparator')) }))
          : null));
    }
    card.append(partBox);
  }
  return card;
}

function renderResult(mount, version, onExit) {
  const stateData = practiceState;
  if (!stateData) return;
  const attempt = stateData.attempt || {};
  const result = stateData.result || {};
  const summary = result.summary || {
    score: attempt.score, max_score: attempt.max_score,
    correct_count: attempt.correct_count, incorrect_count: attempt.incorrect_count,
    pending_count: attempt.pending_count,
  };
  const autoTotal = (summary.correct_count || 0) + (summary.incorrect_count || 0);
  const accuracy = autoTotal ? Math.round((summary.correct_count / autoTotal) * 100) : null;
  const wrongCount = (result.wrong_added || []).length;
  const unlinked = result.app_bank_unlinked || [];

  const retry = event => busy(event.currentTarget, async () => {
    const response = await api(`/paperlab/papers/${attempt.paper_id}/attempts`, { method: 'POST' });
    practiceState = buildPracticeState(response);
    renderPractice(mount, version, onExit);
  });

  mount.replaceChildren(el('section', { class: 'card pl-practice-result' },
    el('div', { class: 'card-header' },
      el('div', {},
        el('h2', {}, t('paper.attempt.practiceResults')),
        el('p', { class: 'help', 'data-no-i18n': '' }, t('paper.attempt.submitted', { v1: stateData.paper.title || '', v2: dateText(attempt.submitted_at) }))),
      button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'ghost small', 'chevron')),
    ...freezeNotices(stateData),
    el('div', { class: 'pl-practice-score-row' },
      el('div', { class: 'pl-practice-score-main' },
        el('strong', { class: 'pl-practice-score' }, `${number(summary.score)} / ${number(summary.max_score)}`),
        el('span', { class: 'help' }, t('paper.attempt.objectiveScoreFullPaperTotal'))),
      el('div', { class: 'pl-practice-score-stats' },
        badge(t('paper.attempt.correct_2', { v1: number(summary.correct_count) }), 'teal'),
        badge(t('paper.attempt.wrong_2', { v1: number(summary.incorrect_count) }), 'red'),
        badge(t('paper.attempt.pendingReview', { v1: number(summary.pending_count) }), 'amber'),
        accuracy === null ? null : badge(t('paper.attempt.objectiveAccuracy', { v1: accuracy }), 'blue'))),
    notice(result.notice || t('paper.attempt.submissionCompleteSubjectiveQuestionsAreNotAuto')),
    wrongCount
      ? notice(t('paper.attempt.wrongAnswersWereAddedToTheWrong', { v1: wrongCount }), 'warning')
      : null,
    unlinked.length
      ? notice(t('paper.attempt.theBankSourcedQuestionsAreRecordedIn', { v1: unlinked.length }), '')
      : null,
    el('div', { class: 'row pl-practice-result-actions' },
      button(t('paper.attempt.viewWrongAnswerBook'), () => go('wrongbook'), 'secondary', 'repeat'),
      button(t('paper.attempt.tryAgain'), retry, '', 'rotate')),
    el('h3', { class: 'pl-practice-review-title' }, t('paper.attempt.reviewQuestionByQuestion')),
    el('div', { class: 'pl-practice-result-list' },
      stateData.items.map((item, index) => resultItemCard(item, index, attempt.paper_id)))));
}

/* ---------------------------------------------------------------------------
 * 入口
 * ------------------------------------------------------------------------- */

/**
 * 渲染整卷练习（开始页 → 作答 → 结果）。
 * @param {object} args
 * @param {HTMLElement} args.mount 挂载容器（试卷详情所在的页面 body）
 * @param {number} args.paperId 试卷 id
 * @param {number} args.version 路由渲染版本
 * @param {() => void} args.onExit 返回试卷详情
 * @param {number} [args.attemptId] 直接打开某次练习（查看结果/继续作答）
 */
export async function renderPaperPractice({ mount, paperId, version, onExit, attemptId = null }) {
  if (!current(version)) return;
  if (attemptId) {
    mount.replaceChildren(loading(t('paper.attempt.loadingPracticeRecords')));
    try {
      const payload = await api(`/paperlab/attempts/${attemptId}`);
      if (!current(version)) return;
      practiceState = buildPracticeState(payload);
      if ((payload.attempt?.status || '') === 'submitted') renderResult(mount, version, onExit);
      else renderPractice(mount, version, onExit);
      return;
    } catch (error) {
      if (!current(version)) return;
      mount.replaceChildren(
        notice(error.message || t('paper.attempt.failedToLoadPracticeRecordsPleaseTry'), 'error'),
        el('div', { class: 'row' }, button(t('paper.attempt.backToPaperDetails'), () => onExit(), 'secondary')));
      return;
    }
  }
  await renderStart(mount, { paperId, version, onExit });
}
