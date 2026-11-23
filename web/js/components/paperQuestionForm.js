/* 试卷工坊 · 题目编辑表单（我的题库 / 识别草稿共用）。
 *
 * 只依赖 core/dom.js：表单字段、选项编辑、答案选择、混合题子题编辑与配图预览；
 * 保存/核验等动作由调用方注入（PATCH /api/paperlab/questions/{id} 或识别草稿 PATCH）。
 * 无框架、无内联脚本；数学公式以文本形式编辑，展示用 markdown()。
 */
import { el, field, input, select, badge, button, toast } from '../core/dom.js';
import { markdown } from '../core/markdown.js';
import { t } from '../core/i18n.js';
import { openFormulaEditor, insertFormulaAtCursor } from './formulaEditor.js';

// 模块级常量不能直接 t()（语言切换后模块不重载），标签一律用 getter 惰性求值。
const TYPE_OPTIONS = [
  { value: 'SINGLE', get label() { return t('question.type.single_choice'); } },
  { value: 'MULTIPLE', get label() { return t('question.type.multi_choice'); } },
  { value: 'BLANK', get label() { return t('question.type.fill_blank'); } },
  { value: 'SUBJECTIVE', get label() { return t('mybank.subjective'); } },
  { value: 'MIXED', get label() { return t('paper.question.mixedWithSubQuestions'); } },
];
const CHOICE_TYPES = new Set(['SINGLE', 'MULTIPLE']);
const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

const DIFFICULTY_OPTIONS = [
  { value: '', get label() { return t('mybank.difficultyUnlabeled'); } },
  { value: '0.2', get label() { return t('practice.difficultyEasy'); } },
  { value: '0.35', get label() { return t('mybank.easier'); } },
  { value: '0.5', get label() { return t('practice.difficultyMedium'); } },
  { value: '0.75', get label() { return t('mybank.harder'); } },
  { value: '0.9', get label() { return t('practice.difficultyHard'); } },
];

function typeLabel(type) {
  const hit = TYPE_OPTIONS.find(item => item.value === type);
  return hit ? hit.label : type || t('mybank.subjective');
}

function splitTags(text) {
  return [...new Set(String(text || '')
    .split(/[,，、;；\n]+/)
    .map(item => item.trim())
    .filter(Boolean))];
}

/** 文本输入框旁的「插入公式」：打开 MathLive，把 `$latex$` 插到光标处。 */
function formulaAction(control) {
  return button(t('study.oss.formula.insert'), async () => {
    try {
      const latex = await openFormulaEditor({ title: t('study.oss.formula.dialogTitle') });
      if (!latex) return;
      if (!insertFormulaAtCursor(control, latex)) return;
      toast(t('study.oss.formula.inserted'));
    } catch {
      toast(t('study.oss.formula.loadFailed'), true);
    }
  }, 'secondary');
}

function fieldWithFormula(label, control) {
  const node = field(label, control);
  node.append(el('div', { class: 'oss-formula-actions' }, formulaAction(control)));
  return node;
}

function clone(value) {
  try {
    return JSON.parse(JSON.stringify(value === undefined ? null : value));
  } catch {
    return null;
  }
}

function optionLetter(index) {
  return LETTERS[index] || String(index + 1);
}

function textOf(value) {
  return value === null || value === undefined ? '' : String(value);
}

/** 生成难度选项：保留题目原有的自定义难度值。 */
function difficultyOptions(current) {
  const options = DIFFICULTY_OPTIONS.slice();
  const value = current === null || current === undefined || current === '' ? '' : String(current);
  if (value && !options.some(item => item.value === value)) {
    options.push({ value, label: t('paper.question.customDifficulty', { v1: Number(current).toFixed(2) }) });
  }
  return options;
}

/**
 * 题目编辑表单。
 * @param {object} args
 * @param {object} args.meta 试卷工坊 /meta（subjects/grades/qtypes）
 * @param {object} args.initial 题目（question 或识别草稿 draft 结构）
 * @returns {{node: HTMLElement, read: () => object, setData: (q: object) => void}}
 */
export function questionForm({ meta = {}, initial = {} }) {
  const base = initial || {};
  const original = clone(base.content) || {};
  const originalParts = Array.isArray(original.parts) ? clone(original.parts) : [];
  const subjects = (meta.subjects || []).length
    ? meta.subjects
    : ['语文', '数学', '英语', '物理', '化学', '生物', '政治', '历史', '地理'];
  const grades = (meta.grades || []).length
    ? meta.grades
    : ['小学一年级', '小学二年级', '小学三年级', '小学四年级', '小学五年级', '小学六年级',
      '初中一年级', '初中二年级', '初中三年级', '高中一年级', '高中二年级', '高中三年级'];
  let type = TYPE_OPTIONS.some(item => item.value === base.type) ? base.type : 'SUBJECTIVE';
  let options = Array.isArray(original.options)
    ? original.options.map((item, index) => ({
      label: item && item.label ? String(item.label) : optionLetter(index),
      text: textOf(item && item.text),
    }))
    : [];
  let answers = Array.isArray(original.answers) ? original.answers.map(textOf).filter(Boolean) : [];
  let partStates = originalParts.map((part, index) => ({
    index: Number(part && part.index) || index + 1,
    type: CHOICE_TYPES.has(part && part.type) ? part.type : 'SUBJECTIVE',
    stem: textOf(part && part.stem),
    options: Array.isArray(part && part.options)
      ? part.options.map((item, optIndex) => ({
        label: item && item.label ? String(item.label) : optionLetter(optIndex),
        text: textOf(item && item.text),
      }))
      : [],
    answers: Array.isArray(part && part.answers) ? part.answers.map(textOf).filter(Boolean) : [],
    explanation: textOf(part && part.explanation),
    rubric: textOf(part && part.rubric),
    knowledgeTags: (Array.isArray(part && part.knowledge_tags) ? part.knowledge_tags : []).map(textOf).filter(Boolean),
  }));

  const typeSelect = select(TYPE_OPTIONS, type);
  const subjectSelect = select(
    [{ value: '', label: t('paper.question.subjectRequired') }, ...subjects.map(item => ({ value: item, label: item }))],
    textOf(base.subject),
  );
  const gradeSelect = select(
    [{ value: '', label: t('paper.import.gradeOptional') }, ...grades.map(item => ({ value: item, label: item }))],
    textOf(base.grade),
  );
  const difficultySelect = select(difficultyOptions(base.difficulty), base.difficulty ?? '');
  const tagsInput = input(t('paper.question.separateKnowledgePointsWithCommasEG'), {
    value: (Array.isArray(base.knowledge_tags) ? base.knowledge_tags : []).join('，'),
    maxlength: '400',
  });
  const stemInput = el('textarea', { rows: 5, placeholder: t('paper.question.stemUseForFormulasEGF') });
  stemInput.value = textOf(original.stem);
  const explanationInput = el('textarea', { rows: 4, placeholder: t('paper.question.solutionOptionalAiCanFillItIn') });
  explanationInput.value = textOf(original.explanation);
  const rubricInput = el('textarea', { rows: 3, placeholder: t('paper.question.scoringRubricRequiredForSubjectiveQuestionsAi') });
  rubricInput.value = textOf(original.rubric);

  const optionsBox = el('div', { class: 'mb-options-editor' });
  const answersBox = el('div', { class: 'mb-answers-editor' });
  const partsBox = el('div', { class: 'mb-parts-editor' });
  const imagesBox = el('div', { class: 'mb-images-preview' });

  function markOptionsDirty() {
    // 选项变化后按 label 同步已选答案；删除选项时移除对应答案。
    const labels = new Set(options.map(item => item.label));
    answers = answers.filter(answer => !CHOICE_TYPES.has(type) || labels.has(answer));
  }

  function renderOptions() {
    optionsBox.replaceChildren();
    if (!CHOICE_TYPES.has(type)) return;
    options.forEach((option, index) => {
      option.label = optionLetter(index);
      const textInput = input(t('paper.question.optionContent', { v1: option.label }), { value: option.text, maxlength: '4000' });
      textInput.addEventListener('input', () => { options[index].text = textInput.value; });
      optionsBox.append(el('div', { class: 'mb-option-row' },
        el('span', { class: 'mb-option-label', 'aria-hidden': 'true' }, option.label),
        textInput,
        button(t('upload.remove'), () => {
          options.splice(index, 1);
          markOptionsDirty();
          renderOptions();
          renderAnswers();
        }, 'ghost small', 'trash')));
    });
    if (options.length < 12) {
      optionsBox.append(el('div', { class: 'row' },
        button(t('paper.question.addOption'), () => {
          options.push({ label: optionLetter(options.length), text: '' });
          markOptionsDirty();
          renderOptions();
          renderAnswers();
        }, 'soft small', 'plus')));
    }
  }

  function renderAnswers() {
    answersBox.replaceChildren();
    if (CHOICE_TYPES.has(type)) {
      if (!options.length) {
        answersBox.append(el('p', { class: 'help' }, t('paper.question.addOptionsBeforeChoosingTheCorrectAnswer')));
        return;
      }
      const checks = [];
      options.forEach(option => {
        const check = el('input', {
          type: type === 'SINGLE' ? 'radio' : 'checkbox',
          name: `mb-answer-${Math.random().toString(36).slice(2, 8)}`,
          checked: answers.includes(option.label),
          value: option.label,
        });
        check.addEventListener('change', () => {
          if (type === 'SINGLE') {
            answers = check.checked ? [option.label] : [];
            checks.forEach(node => { if (node !== check) node.checked = false; });
          } else if (check.checked) {
            answers = [...new Set([...answers, option.label])].sort();
          } else {
            answers = answers.filter(item => item !== option.label);
          }
        });
        checks.push(check);
        answersBox.append(el('label', { class: 'mb-choice' },
          check, badge(option.label, 'blue'), el('span', { class: 'mb-choice-text' }, option.text || t('paper.question.optionContentNotFilledIn'))));
      });
      answersBox.append(el('p', { class: 'help' }, type === 'SINGLE' ? t('paper.question.singleChoiceQuestionsAcceptOnlyOneAnswer') : t('paper.question.multipleChoiceQuestionsAcceptSeveralAnswers')));
      return;
    }
    const area = el('textarea', {
      rows: 3,
      placeholder: type === 'BLANK'
        ? t('paper.question.fillInTheBlankOneAnswerPer')
        : t('paper.question.subjectiveStandardReferenceAnswersOnePerLine'),
    });
    area.value = answers.join('\n');
    area.addEventListener('input', () => {
      answers = area.value.split('\n').map(item => item.trim()).filter(Boolean);
    });
    answersBox.append(area);
  }

  function renderParts() {
    partsBox.replaceChildren();
    if (type !== 'MIXED') return;
    if (!partStates.length) {
      partsBox.append(el('p', { class: 'help' }, t('paper.question.mixedQuestionsMustContainSubQuestionsThis')));
      return;
    }
    partStates.forEach((part, index) => {
      const answerArea = el('div', { class: 'mb-part-answers' });
      if (CHOICE_TYPES.has(part.type)) {
        const nodes = [];
        part.options.forEach(option => {
          const check = el('input', {
            type: part.type === 'SINGLE' ? 'radio' : 'checkbox',
            name: `mb-part-${index}-${Math.random().toString(36).slice(2, 8)}`,
            checked: part.answers.includes(option.label),
            value: option.label,
          });
          check.addEventListener('change', () => {
            if (part.type === 'SINGLE') {
              part.answers = check.checked ? [option.label] : [];
              nodes.forEach(node => { if (node !== check) node.checked = false; });
            } else if (check.checked) {
              part.answers = [...new Set([...part.answers, option.label])].sort();
            } else {
              part.answers = part.answers.filter(item => item !== option.label);
            }
          });
          nodes.push(check);
          answerArea.append(el('label', { class: 'mb-choice' }, check, badge(option.label, 'blue'),
            el('span', { class: 'mb-choice-text' }, option.text || t('paper.question.optionContentNotFilledIn'))));
        });
      } else {
        const area = el('textarea', { rows: 2, placeholder: t('paper.question.subQuestionAnswersOnePerLine') });
        area.value = part.answers.join('\n');
        area.addEventListener('input', () => {
          part.answers = area.value.split('\n').map(item => item.trim()).filter(Boolean);
        });
        answerArea.append(area);
      }
      const partExplanation = el('textarea', { rows: 2, placeholder: t('paper.question.subQuestionSolutionOptional') });
      partExplanation.value = part.explanation;
      partExplanation.addEventListener('input', () => { part.explanation = partExplanation.value; });
      const partRubric = el('textarea', { rows: 2, placeholder: t('paper.question.subQuestionScoringRubricRequiredForSubjective') });
      partRubric.value = part.rubric;
      partRubric.addEventListener('input', () => { part.rubric = partRubric.value; });
      const partTags = input(t('paper.question.subQuestionKnowledgePointsCommaSeparatedOptional'), { value: part.knowledgeTags.join('，') });
      partTags.addEventListener('input', () => { part.knowledgeTags = splitTags(partTags.value); });
      partsBox.append(el('details', { class: 'mb-part-card', open: index === 0 },
        el('summary', {},
          badge(t('paper.question.subQuestion', { v1: part.index }), 'blue'),
          badge(typeLabel(part.type), 'teal'),
          el('span', { class: 'mb-part-stem' }, part.stem || t('paper.question.noSubQuestionStem'))),
        el('div', { class: 'mb-part-body' },
          el('p', { class: 'help' }, t('paper.question.subQuestionTypesAndOptionsComeFrom')),
          answerArea,
          fieldWithFormula(t('paper.question.subQuestionSolution'), partExplanation),
          fieldWithFormula(t('paper.question.subQuestionScoringRubric'), partRubric),
          field(t('paper.question.subQuestionKnowledgePoints'), partTags))));
    });
  }

  function renderImages() {
    imagesBox.replaceChildren();
    const images = Array.isArray(original.images) ? original.images : [];
    if (!images.length) return;
    imagesBox.append(el('p', { class: 'mb-images-label' }, t('paper.question.questionImagesCroppedFromTheOriginalPaper')));
    const row = el('div', { class: 'mb-image-row' });
    images.forEach((image, index) => {
      const url = image && image.url ? String(image.url) : '';
      if (!url) return;
      row.append(el('img', {
        class: 'mb-image-thumb',
        src: url,
        alt: t('paper.question.questionImage', { v1: index + 1 }),
        loading: 'lazy',
        onClick: () => window.open(url, '_blank', 'noopener'),
      }));
    });
    imagesBox.append(row);
  }

  function syncVisibility() {
    const choice = CHOICE_TYPES.has(type);
    optionsBox.classList.toggle('hide', !choice);
    partsBox.classList.toggle('hide', type !== 'MIXED');
    renderOptions();
    renderAnswers();
    renderParts();
  }
  typeSelect.addEventListener('change', () => {
    type = typeSelect.value;
    markOptionsDirty();
    syncVisibility();
  });

  renderImages();

  const node = el('div', { class: 'mb-question-form' },
    el('div', { class: 'mb-form-grid' },
      field(t('upload.fieldType'), typeSelect),
      field(t('common.subject'), subjectSelect),
      field(t('mybank.grade'), gradeSelect),
      field(t('practice.difficulty'), difficultySelect),
      field(t('practice.knowledgePoint'), tagsInput, t('paper.question.separateWithCommasAiFillInMerges'))),
    fieldWithFormula(t('question.stem'), stemInput),
    optionsBox,
    answersBox,
    fieldWithFormula(t('question.solution'), explanationInput),
    fieldWithFormula(t('paper.question.scoringRubric'), rubricInput),
    partsBox,
    imagesBox);

  function read() {
    if (!subjectSelect.value) throw new Error(t('paper.question.chooseASubject'));
    if (!stemInput.value.trim()) throw new Error(t('paper.question.theStemCannotBeEmpty'));
    if (CHOICE_TYPES.has(type) && !options.some(item => item.text.trim())) {
      throw new Error(t('paper.question.choiceQuestionsNeedAtLeastOneOption'));
    }
    if (type === 'MIXED' && !partStates.length) {
      throw new Error(t('paper.question.mixedQuestionsMustContainSubQuestions'));
    }
    const content = clone(original) || {};
    content.stem = stemInput.value.trim();
    content.options = CHOICE_TYPES.has(type)
      ? options.map(item => ({ label: item.label, text: item.text.trim() })).filter(item => item.text)
      : [];
    content.answers = CHOICE_TYPES.has(type) ? answers.slice() : answers.slice();
    content.explanation = explanationInput.value.trim();
    content.rubric = rubricInput.value.trim();
    content.parts = type === 'MIXED'
      ? partStates.map((part, index) => ({
        ...(originalParts[index] || {}),
        index: part.index,
        answers: part.answers.slice(),
        explanation: part.explanation.trim(),
        rubric: part.rubric.trim(),
        knowledge_tags: part.knowledgeTags.slice(),
      }))
      : [];
    return {
      type,
      subject: subjectSelect.value,
      grade: gradeSelect.value,
      difficulty: difficultySelect.value === '' ? null : Number(difficultySelect.value),
      knowledge_tags: splitTags(tagsInput.value),
      content,
    };
  }

  function setData(question) {
    const data = question || {};
    type = TYPE_OPTIONS.some(item => item.value === data.type) ? data.type : 'SUBJECTIVE';
    typeSelect.value = type;
    subjectSelect.value = textOf(data.subject);
    gradeSelect.value = textOf(data.grade);
    const difficultyValue = data.difficulty ?? '';
    difficultySelect.replaceChildren(...difficultyOptions(data.difficulty).map(item =>
      el('option', { value: item.value }, item.label)));
    difficultySelect.value = difficultyValue === null ? '' : String(difficultyValue);
    tagsInput.value = (Array.isArray(data.knowledge_tags) ? data.knowledge_tags : []).join('，');
    const content = clone(data.content) || {};
    Object.assign(original, content);
    originalParts.length = 0;
    (Array.isArray(content.parts) ? content.parts : []).forEach(part => originalParts.push(part));
    options = (Array.isArray(content.options) ? content.options : []).map((item, index) => ({
      label: item && item.label ? String(item.label) : optionLetter(index),
      text: textOf(item && item.text),
    }));
    answers = Array.isArray(content.answers) ? content.answers.map(textOf).filter(Boolean) : [];
    stemInput.value = textOf(content.stem);
    explanationInput.value = textOf(content.explanation);
    rubricInput.value = textOf(content.rubric);
    partStates = originalParts.map((part, index) => ({
      index: Number(part && part.index) || index + 1,
      type: CHOICE_TYPES.has(part && part.type) ? part.type : 'SUBJECTIVE',
      stem: textOf(part && part.stem),
      options: (Array.isArray(part && part.options) ? part.options : []).map((item, optIndex) => ({
        label: item && item.label ? String(item.label) : optionLetter(optIndex),
        text: textOf(item && item.text),
      })),
      answers: Array.isArray(part && part.answers) ? part.answers.map(textOf).filter(Boolean) : [],
      explanation: textOf(part && part.explanation),
      rubric: textOf(part && part.rubric),
      knowledgeTags: (Array.isArray(part && part.knowledge_tags) ? part.knowledge_tags : []).map(textOf).filter(Boolean),
    }));
    renderImages();
    syncVisibility();
  }

  syncVisibility();
  return { node, read, setData };
}

/**
 * 打开题目编辑弹层（dialog）。保存失败时保留表单内容并在弹层内提示；
 * 版本冲突（409）时额外提供「重新加载最新版本」。
 * @param {object} args
 * @param {string} args.title 弹层标题
 * @param {object} args.meta /meta
 * @param {object} args.question 当前题目
 * @param {(values: object) => Promise<void>} args.onSave 保存动作（可抛错）
 * @param {() => Promise<object>} [args.reload] 冲突/失败后重新加载最新题目
 */
export function openQuestionEditor({ title, meta, question, onSave, reload = null, saveLabel = t('settings.wizard.saveChanges') }) {
  const dialog = el('dialog', { class: 'modal-dialog mb-editor-dialog', 'aria-label': title });
  const form = questionForm({ meta, initial: question });
  const errorBox = el('div', { class: 'notice error mb-editor-error', hidden: true });
  const conflictBox = el('div', { class: 'row mb-editor-conflict-actions', hidden: true });
  const saveBtn = button(saveLabel, null, '', 'check');
  const cancelBtn = button(t('common.cancel'), () => dialog.close(), 'secondary');
  const head = el('div', { class: 'card-header mb-editor-head' },
    el('div', {},
      el('h3', {}, title),
      el('p', { class: 'help' }, t('paper.question.savesYourEditsVersionNumbersPreventOverwriting'))),
    button(t('admin.closed'), () => dialog.close(), 'ghost small', 'close'));

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }
  function hideError() {
    errorBox.hidden = true;
    conflictBox.hidden = true;
    conflictBox.replaceChildren();
  }

  saveBtn.addEventListener('click', async () => {
    if (saveBtn.disabled) return;
    hideError();
    let values;
    try {
      values = form.read();
    } catch (error) {
      showError(error.message);
      return;
    }
    saveBtn.disabled = true;
    saveBtn.setAttribute('aria-busy', 'true');
    try {
      await onSave(values);
      toast(t('paper.question.questionSaved'));
      dialog.close();
    } catch (error) {
      const message = error?.message || t('paper.question.saveFailedPleaseTryAgain');
      showError(message);
      if (/版本冲突|已被修改/.test(message) && reload) {
        conflictBox.hidden = false;
        conflictBox.replaceChildren(
          el('span', { class: 'help' }, t('paper.question.yourChangesHaveNotBeenSaved')),
          button(t('paper.question.reloadTheLatestVersion'), async () => {
            try {
              const fresh = await reload();
              form.setData(fresh);
              reloadedQuestion = fresh;
              hideError();
              toast(t('paper.question.theLatestVersionIsLoadedMakeYour'));
            } catch (reloadError) {
              showError(reloadError.message || t('paper.question.failedToLoadTheLatestVersion'));
            }
          }, 'soft small', 'repeat'));
      }
    } finally {
      if (saveBtn.isConnected) {
        saveBtn.disabled = false;
        saveBtn.removeAttribute('aria-busy');
      }
    }
  });

  let reloadedQuestion = question;
  dialog.append(head, form.node, errorBox, conflictBox,
    el('div', { class: 'row end mb-editor-footer' }, cancelBtn, saveBtn));
  document.body.append(dialog);
  const closeOnRouteChange = () => { try { dialog.close(); } catch { /* ignore */ } };
  window.addEventListener('hashchange', closeOnRouteChange);
  dialog.addEventListener('close', () => {
    window.removeEventListener('hashchange', closeOnRouteChange);
    dialog.remove();
  });
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });
  dialog.showModal();
  stemFocus(form.node);
  return {
    dialog,
    form,
    close: () => dialog.close(),
    currentQuestion: () => reloadedQuestion,
  };

  function stemFocus(root) {
    const stem = root.querySelector('textarea');
    if (stem && typeof stem.focus === 'function') {
      try { stem.focus({ preventScroll: true }); } catch { /* ignore */ }
    }
  }
}

export { typeLabel, splitTags };
