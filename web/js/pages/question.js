import { api, ensureExplainModes, ensureKnowledgePoints } from '../core/api.js';
import { state, onSessionReset, onBankChange } from '../core/state.js';
import {
  $, el, fill, appendSafe, icon, button, badge, notice, loading, field, select,
  query, dateText, sourceOf, typeLabel, resultLabel, resultBadge, questionText,
  questionBlocks, blockList, showImage, busy, toast,
} from '../core/dom.js';
import { go } from '../core/router.js';
import { mountJob, closeJobStreams } from '../core/jobs.js';
import { createModelPicker } from '../components/modelPicker.js';
import { filePicker } from '../components/filePicker.js';
import { renderAiResult } from '../components/aiResultCard.js';
import { openKpDetailDialog } from '../components/kpDetailModal.js';
import { openPresentationMode } from '../components/presentationMode.js';
import { formulaEditorButton } from '../components/formulaEditor.js';
import { t } from '../core/i18n.js';
import { markdown } from '../core/markdown.js';

/**
 * M3 学习工具文案：新 key 统一 `study.tools.*`，语言包由 Lead 合并；
 * 缺键时回退中文常量（不破坏中英文界面切换，也不用改 locale JSON）。
 */
function toolText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

/**
 * M4 原理演示文案：新 key 统一 `study.m4.*`，语言包由 Lead 合并；
 * 缺键时回退中文常量。用变量 key 调用 t()，避免语言包尚未合并时
 * 静态 locale 扫描（只抓字面量 key）报缺键，也不影响中英文切换。
 */
function m4Text(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

/**
 * M5 机械/工厂演示文案：key 统一 `study.m5.*`，语言包由 Lead 合并；
 * 缺键时回退中文常量。与 m4Text 同口径（用变量 key 调 t()，静态 locale 扫描不误报）。
 */
function m5Text(key, fallback, vars) {
  return m4Text(key, fallback, vars);
}

// 掌握度是后端下发的中文枚举值：提交保持原值，界面展示按当前语言映射。
const MASTERY_LEVEL_KEYS = {
  '未掌握': 'practice.masteryNotMastered',
  '初步掌握': 'practice.masteryInitial',
  '比较掌握': 'practice.masteryFair',
  '熟练掌握': 'practice.masteryMastered',
};

function masteryText(level) {
  const key = MASTERY_LEVEL_KEYS[level];
  return key ? t(key) : String(level);
}

// 难度是后端中文枚举：值保持原样提交，展示时按语言映射。
const DIFFICULTY_VALUE_KEYS = {
  '待标注': 'practice.difficultyUnlabeled',
  '简单': 'practice.difficultyEasy',
  '中等': 'practice.difficultyMedium',
  '困难': 'practice.difficultyHard',
};

function difficultyText(value) {
  const key = DIFFICULTY_VALUE_KEYS[value];
  return key ? t(key) : String(value);
}

// 后端 explain_modes.yaml 目前只提供中文名称/简介：按 mode id 在前端映射界面文案，
// 未知 id 回退后端原文（数据不翻译）。
const EXPLAIN_MODE_KEYS = {
  detailed: 'question.mode.detailed',
  standard_steps: 'question.mode.standardSteps',
  pattern_teaching: 'question.mode.patternTeaching',
  extension: 'question.mode.extension',
  hints: 'question.mode.hints',
  variants: 'question.mode.variants',
};

function explainModeName(mode) {
  const key = EXPLAIN_MODE_KEYS[mode?.id];
  return key ? t(key) : String(mode?.name || mode?.id || '');
}

// 后端数据（模型名、难度来源等）徽章：标记 data-no-i18n，验收扫描时豁免。
function dataBadge(text, variant) {
  const node = badge(text, variant);
  node.setAttribute('data-no-i18n', 'true');
  return node;
}

/* ---------------------------------------------------------------------------
 * 平台精选（WS19 `official:<public_id>`）与「收录到我的题库」
 * ------------------------------------------------------------------------- */

// 审核状态：community=社区待核验、verified=已核验；featured 时来源徽标本身就是
// 「平台精选」不再重复，其余（withdrawn/disputed 等）用后端 status_label 兜底。
const OFFICIAL_REVIEW_KEYS = {
  community: 'question.source.reviewCommunity',
  community_review: 'question.source.reviewCommunity',
  verified: 'question.source.reviewVerified',
};

function officialReviewBadge(q) {
  const status = String(q?.review_status || '');
  if (status === 'featured' || status === 'published') return null;
  const key = OFFICIAL_REVIEW_KEYS[status];
  const label = key ? t(key) : String(q?.review_status_label || '');
  if (!label) return null;
  const variant = status === 'verified' ? 'teal' : 'amber';
  return dataBadge(label, variant);
}

function isOfficialQuestion(q) {
  return q?.source === 'official' || String(q?.qid || '').startsWith('official:');
}

function officialPublicIdOf(q) {
  if (q?.public_id) return String(q.public_id);
  const qid = String(q?.qid || '');
  return qid.startsWith('official:') ? qid.slice('official:'.length) : '';
}

/* ---------------------------------------------------------------------------
 * 平台精选公开配图（WS26 契约）：payload.images[] 每项
 * `{index, role, alt, accessible, url, asset, media_type, reason}`。
 * 只有 accessible && url 的条目可渲染；撤回/争议后公开图接口即时 404，
 * 不可公开的图用 figure_notice 提示，绝不拼接私有存储路径。
 * 旧/未来 payload 兼容 figure_urls（数组或对象，元素为 URL 或 {url}）。
 * ------------------------------------------------------------------------- */

// 只允许站内相对路径与 http(s)，避免把任意字符串当地址加载。
function isRenderableImageUrl(url) {
  const value = String(url || '').trim();
  return Boolean(value) && (value.startsWith('/') ? !value.startsWith('//') : /^https?:\/\//i.test(value));
}

function officialFigureUrlOf(entry) {
  if (typeof entry === 'string') return entry.trim();
  if (!entry || typeof entry !== 'object') return '';
  if (entry.accessible === false) return '';
  return String(entry.url || entry.src || entry.href || '').trim();
}

/** 收集可渲染的精选公开配图；role=formula 的图与 [公式N] 占位对应。 */
function officialFigures(q) {
  const figures = [];
  const seen = new Set();
  const push = (entry, fallbackRole) => {
    const url = officialFigureUrlOf(entry);
    if (!isRenderableImageUrl(url) || seen.has(url)) return;
    seen.add(url);
    const object = entry && typeof entry === 'object' ? entry : {};
    const role = String(object.role || fallbackRole || 'figure');
    figures.push({
      url,
      role: role === 'formula' ? 'formula' : 'figure',
      alt: String(object.alt || ''),
    });
  };
  for (const image of Array.isArray(q?.images) ? q.images : []) push(image, 'figure');
  const figureUrls = q?.figure_urls;
  if (!figures.length && Array.isArray(figureUrls)) figureUrls.forEach(entry => push(entry, 'figure'));
  else if (!figures.length && figureUrls && typeof figureUrls === 'object') {
    Object.values(figureUrls).forEach(entry => push(entry, 'figure'));
  }
  return figures;
}

function officialFigureNode(entry, index, q) {
  const alt = entry.alt || t('question.source.figureAlt', { v1: index + 1 });
  const node = el('img', {
    class: 'bank-image official-figure',
    src: entry.url,
    alt,
    loading: 'lazy',
    decoding: 'async',
    'data-official-figure': String(index + 1),
    'data-public-id': officialPublicIdOf(q),
    onClick: () => showImage(entry.url, alt),
  });
  node.addEventListener('error', () => {
    node.replaceWith(el('span', { class: 'image-error' }, t('question.source.figureLoadFailed')));
  });
  return node;
}

/** 题干 [图N]/[公式N] 占位 → 精选公开图节点；找不到对应图时保留可读占位。 */
function officialTextNode(text, figures = [], { tag = 'div', className = 'question-text', q = null } = {}) {
  const node = el(tag, { class: className });
  const pools = {
    figure: figures.filter(item => item.role !== 'formula'),
    formula: figures.filter(item => item.role === 'formula'),
  };
  const used = new Set();
  for (const part of String(text || '').split(/(\[(?:图|公式)\d+\])/g)) {
    const match = part.match(/^\[(图|公式)(\d+)\]$/);
    if (!match) {
      node.append(document.createTextNode(part));
      continue;
    }
    const role = match[1] === '公式' ? 'formula' : 'figure';
    const index = Number(match[2]) - 1;
    const figure = pools[role][index];
    if (figure && !used.has(figure.url)) {
      used.add(figure.url);
      node.append(officialFigureNode(figure, index, q));
    } else {
      node.append(el('span', { class: 'muted' },
        t(role === 'formula' ? 'question.sourceFormula' : 'question.sourceFigure')));
    }
  }
  return { node, used };
}

function officialFigureGallery(q, figures) {
  if (!figures.length) return null;
  return el('div', { class: 'official-figures', 'data-official-figures': String(figures.length) },
    ...figures.map((entry, index) => el('figure', { class: 'official-figure-item' },
      officialFigureNode(entry, index, q),
      entry.alt ? el('figcaption', {}, entry.alt) : null)));
}

/** 精选题干：正文占位内联配图 + 未被占位引用的配图画廊 + figure_notice 降级。 */
function officialStemNode(q, figures) {
  const rendered = officialTextNode(q.stem_plain || q.stem || '', figures, { q });
  const remaining = figures.filter(item => !rendered.used.has(item.url));
  const nodes = [rendered.node, officialFigureGallery(q, remaining)];
  const figureNotice = String(q.figure_notice || '').trim();
  if (figureNotice) nodes.push(notice(figureNotice, 'warning'));
  else if (!figures.length && Array.isArray(q.images) && q.images.length) {
    nodes.push(notice(t('question.source.figureUnavailable'), 'warning'));
  }
  return nodes.filter(Boolean);
}

// 「已收录」的本地会话缓存：收录接口按 source_ref 幂等（重复收录返回已有条目），
// 没有按题库 qid 反查的只读接口；本标签页内记住成功结果，跨会话由点击时的
// already_collected 响应纠正（幂等，不会重复建题）。
const COLLECTED_STORAGE_KEY = 'zhixu.collected.bank.v1';

function collectedRefSet() {
  try {
    const raw = window.sessionStorage.getItem(COLLECTED_STORAGE_KEY);
    const values = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(values) ? values.map(String) : []);
  } catch {
    return new Set();
  }
}

function bankSourceRef(qid) {
  const value = String(qid || '');
  return value.startsWith('bank:') ? value : `bank:${value}`;
}

function isCollectedToBank(qid) {
  return collectedRefSet().has(bankSourceRef(qid));
}

function markCollectedToBank(qid) {
  try {
    const refs = collectedRefSet();
    refs.add(bankSourceRef(qid));
    window.sessionStorage.setItem(COLLECTED_STORAGE_KEY, JSON.stringify([...refs].slice(-200)));
  } catch {
    /* 隐私模式禁用存储时只影响「已收录」提示，不影响收录本身 */
  }
}

// 已收录后把按钮改成不可点的「已收录」，并在同一行补「去我的题库」入口。
function applyCollectedState(node) {
  if (!node) return;
  node.textContent = t('question.collect.collected');
  node.disabled = true;
  node.classList.add('is-collected');
  const row = node.parentElement;
  if (row && !row.querySelector('.go-mybank-link')) {
    row.append(el('button', {
      type: 'button', class: 'button ghost small go-mybank-link',
      onClick: () => go('mybank'),
    }, t('question.collect.goMyBank')));
  }
}

// 收录到我的题库（POST /api/paperlab/questions/collect {qid}）：
// 成功后固定当前题库版本；重复收录由服务端幂等复用已有条目。
function collectToMyBankButton(q) {
  const node = button(t('question.collect.save'), event => {
    const trigger = event.currentTarget;
    busy(trigger, async () => {
      let result;
      try {
        result = await api('/paperlab/questions/collect', { method: 'POST', data: { qid: q.qid } });
      } catch (error) {
        // 收录接口未就绪/来源不可用：给出可读提示（按钮恢复可点，可稍后重试）。
        throw new Error(t('question.collect.failed', { v1: error.message }));
      }
      markCollectedToBank(q.qid);
      toast(result?.already_collected ? t('question.collect.already') : t('question.collect.success'));
      applyCollectedState(trigger);
    }).finally(() => {
      // busy 结束会把 disabled 复位，已收录状态在这里重新固定。
      if (isCollectedToBank(q.qid)) applyCollectedState(trigger);
    });
  }, 'soft small', 'plus', { title: t('question.collect.hint') });
  node.setAttribute('data-collect-qid', String(q.qid || ''));
  return node;
}

// 单次作答幂等键：同一次点击/表单提交复用同一个 submission_id，弱网或代理重试
// 不会在服务端重复入账（服务端按 user+qid+submission_id 唯一约束去重）。
function newSubmissionId() {
  try {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  } catch {
    /* 非安全上下文没有 randomUUID：退回时间戳随机串 */
  }
  return `sub-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

function flagText(flag) {
  if (typeof flag === 'string') return flag;
  return flag?.message || flag?.code || t('question.pendingReview');
}

function qualityNotice(q) {
  const flags = (q.quality_flags || []).map(flagText).filter(Boolean);
  const nodes = [];
  if (flags.length) {
    nodes.push(notice(t('question.originalPaperContentNoticePleaseVerifyAgainst', { v1: flags.join('；') }), 'warning'));
  }
  const needsReview = q.practice_status === 'review' || q.grading_status === 'pending';
  if (needsReview && !flags.some(text => /核验|待复核|判分/.test(text))) {
    nodes.push(notice(t('question.referenceAnswerPendingVerificationYouCanAnswer'), 'warning'));
  }
  return nodes.length ? el('div', { class: 'quality-notices', 'data-no-i18n': 'true' }, nodes) : null;
}

const GROUP_KIND_LABELS = {
  get material() { return t('question.readingMaterial'); },
  get parent() { return t('question.sharedStem'); },
  get material_parent() { return t('question.readingMaterialSharedStem'); },
};

function groupLabel(groupKind) {
  return GROUP_KIND_LABELS[groupKind] || t('question.sharedMaterialStem');
}

// 统一题号显示：父题给原卷题号，小问给问序，父题本身标为「共用题干」。
function memberLabel(member) {
  if (member.group_role === 'parent') return t('question.sharedStem');
  const parts = [];
  const number = String(member.number_label || '').trim();
  if (number) {
    const looksSub = /^[（(]/.test(number) || /^第/.test(number)
      || /^[(（]?[一二三四五六七八九十\d]+[)）]$/.test(number);
    parts.push(looksSub ? (number.startsWith('第') ? number : t('question.part', { v1: number })) : t('question.originalNumber', { label: number }));
  }
  const sub = String(member.sub_question_label || '').trim();
  const subNorm = sub.replace(/^[（(第]+|[）)]+$/g, '').replace(/问$/, '');
  const numberNorm = number.replace(/^[（(]+|[）)]+$/g, '');
  // number_label 已经逐问编号时（如原卷 1、2 两问各自成题），不再重复「第（1）问」。
  if (sub && subNorm && subNorm !== numberNorm) {
    parts.push(sub.startsWith('第') ? sub : t('question.part', { v1: sub }));
  }
  if (!parts.length && member.source_question_id) parts.push(t('question.originalQuestion', { v1: member.source_question_id }));
  return parts.join(' · ') || t('question.questionGroupMember');
}

function memberStatusText(member, isCurrent) {
  if (isCurrent) return t('question.thisQuestion');
  if (member.group_role === 'parent') return t('question.shared');
  if (member.in_bank === false || member.practice_status === 'context' || !member.qid) return t('question.readOnly');
  if (member.practice_status === 'quarantined') return t('question.quarantined');
  if (member.available === false) return t('question.unavailable');
  if (member.practice_status === 'review') return t('home.importStatus.ready');
  return null;
}

function readingPathParts(q) {
  const parts = [];
  const section = q.group?.section_title || q.paper_name || sourceOf(q);
  if (section) parts.push(section);
  const parentNumber = q.group?.parent_number_label;
  if (parentNumber) parts.push(t('question.originalNumber', { label: parentNumber }));
  else if (q.number_label) parts.push(t('question.originalNumber', { label: q.number_label }));
  if (q.group?.group_kind) parts.push(groupLabel(q.group.group_kind));
  return parts.filter(Boolean);
}

function statusChip(q, latest) {
  if (latest) {
    const stateName = latest.result === 'correct' ? 'done'
      : latest.result === 'incorrect' ? 'error' : 'warn';
    return el('span', { class: 'status-chip', 'data-state': stateName }, resultLabel(latest.result));
  }
  if (q.practice_status === 'quarantined') return el('span', { class: 'status-chip', 'data-state': 'error' }, t('question.quarantined'));
  if (q.practice_status === 'context') return el('span', { class: 'status-chip', 'data-state': 'warn' }, t('question.readOnly'));
  return el('span', { class: 'status-chip' }, t('question.notAnswered'));
}

function optionBlockList(q, item) {
  const direct = blockList(item.blocks);
  if (direct.length) return direct;
  const label = String(item.label || '').toUpperCase();
  const entry = (Array.isArray(q.options_blocks) ? q.options_blocks : [])
    .find(option => String(option?.label || '').toUpperCase() === label);
  return blockList(entry?.blocks);
}

function optionContentNode(q, item, keyword, context) {
  const blocks = optionBlockList(q, item);
  if (blocks.length) {
    const node = el('div', { class: 'option-content option-blocks' }, questionBlocks(blocks, q.subject, keyword, context));
    if (node.childNodes.length) return node;
  }
  // 平台精选题：选项文本里的 [图N]/[公式N] 用公开图渲染，不再套题库配图门禁。
  if (isOfficialQuestion(q)) {
    return el('span', { class: 'option-content' },
      officialTextNode(String(item.text || ''), officialFigures(q), {
        tag: 'span', className: 'option-content-text', q,
      }).node);
  }
  return el('span', { class: 'option-content' }, questionText(item.text || '', q.images || [], q.subject, keyword, context));
}

function questionStemNode(q, keyword, context) {
  const blocks = blockList(q.stem_blocks);
  if (blocks.length) {
    const node = el('div', { class: 'question-text question-blocks' }, questionBlocks(blocks, q.subject, keyword, context));
    if (node.childNodes.length) return node;
  }
  return questionText(q.stem_plain || q.stem, q.images || [], q.subject, keyword, context);
}

let activeObjectUrl = null;
function revokePreviewUrl() {
  if (activeObjectUrl) {
    URL.revokeObjectURL(activeObjectUrl);
    activeObjectUrl = null;
  }
}

// 方案 P1「题目报错」：自动带题库版本、题号、字段区域与问题类型。
function openIssueDialog(q) {
  const fieldSel = select([
    { value: 'stem', label: t('question.stem') }, { value: 'options', label: t('question.options') },
    { value: 'formula', label: t('question.formula') }, { value: 'image', label: t('question.image') },
    { value: 'answer', label: t('question.referenceAnswer') }, { value: 'solution', label: t('question.solution') },
    { value: 'other', label: t('insight.other') },
  ], 'stem');
  const typeSel = select([
    { value: 'formula', label: t('question.formulaDisplayOrContentIsWrong') }, { value: 'stem_missing', label: t('question.stemOrConditionMissing') },
    { value: 'option', label: t('question.optionBoundaryOrContentIsWrong') }, { value: 'image', label: t('question.imageMissingOrMisplaced') },
    { value: 'answer', label: t('question.referenceAnswerIsWrong') }, { value: 'other', label: t('question.otherIssue') },
  ], 'other');
  const noteArea = el('textarea', { rows: 3, placeholder: t('question.youCanAddWhatYouSeeOn') });
  const shotInput = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp' });
  const resultBox = el('div', { class: 'issue-result', 'data-no-i18n': 'true' });
  const submit = button(t('question.reportIssue'), event => busy(event.currentTarget, async () => {
    const data = new FormData();
    data.set('field', fieldSel.value);
    data.set('issue_type', typeSel.value);
    data.set('note', noteArea.value.trim());
    const file = shotInput.files?.[0];
    if (file) data.set('screenshot', file);
    const response = await api(`/questions/${encodeURIComponent(q.qid)}/issues`, { method: 'POST', body: data });
    resultBox.replaceChildren(notice(t('question.submittedIssueAnAdministratorWillVerifyIt', { v1: response.id, v2: response.status_label })));
    toast(t('question.issueReportSubmitted'));
    submit.disabled = true;
  }), '', 'check');
  const dialog = el('dialog', { class: 'issue-dialog', 'aria-label': t('question.reportAQuestionIssue') },
    el('div', { class: 'row between' },
      el('h3', {}, icon('edit'), t('question.reportAQuestionIssue_2')),
      el('button', { type: 'button', class: 'close-dialog', 'aria-label': t('common.close'), onClick: () => dialog.close() }, icon('close'))),
    el('div', { class: 'issue-meta' },
      t('question.issueMeta', { bank: q.bank_version || t('question.baselineBank'), number: q.number_label || q.qid })
      + (q.content_revision ? t('question.issueContentRevision', { v1: q.content_revision }) : '')),
    el('div', { class: 'form-grid' }, field(t('question.issueArea'), fieldSel), field(t('question.issueType'), typeSel)),
    field(t('question.additionalDetails'), noteArea),
    field(t('question.optionalScreenshot'), shotInput, t('question.supportsPngJpegWebpScreenshotsAreVisible')),
    el('div', { class: 'row between', style: 'margin-top:12px' },
      el('p', { class: 'help' }, t('question.reportingAnIssueDoesNotAffectYour')), submit),
    resultBox);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
}

/* ---------------------------------------------------------------------------
 * 平台精选纠错（WS28 契约：POST /api/plaza/questions/{public_id}/issues
 * {field, description}，字段白名单 stem/options/answer/explanation/figure/
 * source/other，描述 2–4000 字；只有当前公开可见的精选可提交）。
 * 未登录引导登录；接口未就绪/题目不可见时用 toast 明确提示，不崩页。
 * ------------------------------------------------------------------------- */

const OFFICIAL_ISSUE_FIELDS = ['stem', 'options', 'answer', 'explanation', 'figure', 'source', 'other'];

function openOfficialIssueDialog(q, publicId) {
  if (!publicId) {
    // 旧/不完整 payload（无公开编号）：保留占位提示，不伪造接口调用。
    toast(t('question.source.reportPlaceholder'));
    return;
  }
  if (!state.user) {
    toast(t('question.source.issueLoginRequired'));
    go('home');
    return;
  }
  const fieldSel = select(OFFICIAL_ISSUE_FIELDS.map(value => ({
    value, label: t(`question.source.issue.field.${value}`),
  })), 'answer', { 'aria-label': t('question.source.issueFieldLabel') });
  const description = el('textarea', {
    rows: 4,
    maxlength: '4000',
    placeholder: t('question.source.issueDescriptionPlaceholder'),
    'aria-label': t('question.source.issueDescriptionLabel'),
  });
  const resultBox = el('div', { class: 'issue-result', 'data-no-i18n': 'true' });
  const submit = button(t('question.source.issueSubmit'), event => busy(event.currentTarget, async () => {
    const text = String(description.value || '').trim();
    if (text.length < 2) throw new Error(t('question.source.issueDescriptionRequired'));
    const response = await api(`/plaza/questions/${encodeURIComponent(publicId)}/issues`, {
      method: 'POST',
      data: { field: fieldSel.value, description: text },
    });
    const message = response?.message || t('question.source.issueSubmitted');
    resultBox.replaceChildren(notice(message));
    toast(message);
    // 与广场纠错弹层一致：提交成功后自动关闭，避免重复提交同一问题。
    dialog.close();
  }), '', 'check');
  const dialog = el('dialog', {
    class: 'issue-dialog',
    'data-official-issue-dialog': publicId,
    'aria-label': t('question.source.issueTitle'),
  },
  el('div', { class: 'row between' },
    el('h3', {}, icon('edit'), t('question.source.issueTitle')),
    el('button', {
      type: 'button', class: 'close-dialog', 'aria-label': t('common.close'),
      onClick: () => dialog.close(),
    }, icon('close'))),
  el('p', { class: 'help', 'data-no-i18n': 'true' }, t('question.source.issueHint', { v1: publicId })),
  el('div', { class: 'form-grid' }, field(t('question.source.issueFieldLabel'), fieldSel)),
  field(t('question.source.issueDescriptionLabel'), description,
    t('question.source.issueDescriptionHint')),
  el('div', { class: 'row between', style: 'margin-top:12px' },
    el('p', { class: 'help' }, t('question.source.issuePrivacyHint')), submit),
  resultBox);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
}

/* ---------------------------------------------------------------------------
 * 原理演示入口（方案 §3「科学与机械主流程」界面落点 / §5.4 / §7 M4）
 * ---------------------------------------------------------------------------
 * 只对 物理/数学/科学/化学/生物/机械 六科显示「查看原理」，未知学科不显示。
 * M4 起改为「按学科推荐 + 可切换」：
 * - 按学科选默认预设（SUBJECT_PRESET_HINTS，第一项为默认，其余为同学科备选）；
 * - 弹层内「切换演示」下拉按 kind 分组列出 PRESET_DEMOS 全部预设，切换时销毁
 *   旧播放器再渲染新预设；
 * - 「保存为学习附件」保存当前选中预设的数据（kind/title/data/source_ref），
 *   沿用既有 /artifacts + /links 保存逻辑，失败时中文提示、不伪造成功；
 * - 3D（最小 Three.js 视图）仍作为可选视图；无 WebGL / three 加载失败时统一
 *   播放器自动回退等距示意图，本页不额外处理。
 * 演示数据为本地离线预设（presets.js），播放接口见 components/principleDemo.js。
 * ------------------------------------------------------------------------- */

const PRINCIPLE_SUBJECTS = new Set(['物理', '数学', '科学', '化学', '生物', '机械']);

// 学科 → 推荐预设（第一项为默认；其余为同学科备选，下拉始终可切到全部预设）。
const SUBJECT_PRESET_HINTS = Object.freeze({
  '物理': ['flow-gear-transmission', 'flow-review-loop'],
  '机械': ['flow-gear-transmission', 'iso-anatomy-gearbox', 'flow-review-loop'],
  '数学': ['flow-function-timeline', 'iso-gearbox'],
  '生物': ['iso-biology-stomata', 'flow-photosynthesis-states'],
  '化学': ['flow-photosynthesis-states', 'iso-biology-stomata'],
  '科学': ['flow-review-loop', 'flow-function-timeline', 'iso-biology-stomata'],
});

// 演示切换下拉的分组顺序：流程 → 等距 → 3D（与 PRESET_LIST 的固定顺序一致）。
const PRINCIPLE_GROUP_ORDER = Object.freeze([
  { kind: 'flow_animation', labelKey: 'study.demo.modeFlow' },
  { kind: 'isometric', labelKey: 'study.demo.modeIso' },
  { kind: 'demo_3d', labelKey: 'study.demo.modeThree' },
]);

/** 按学科取默认预设 ID；预设表里不存在时由调用方回退到第一个可用预设。 */
function defaultPrinciplePresetId(subject) {
  const hints = SUBJECT_PRESET_HINTS[String(subject || '').trim()] || [];
  return hints.length ? hints[0] : '';
}

// 统一题目引用：bank:<qid> / app:<id> / official:<public_id>；裸 qid 按题库题补 bank:。
export function questionRefOf(q) {
  const raw = String(q?.ref || q?.qid || '').trim();
  if (!raw) return '';
  if (/^(bank|app|official):/.test(raw)) return raw;
  if (q?.source === 'app') return `app:${raw}`;
  if (q?.source === 'official') {
    const publicId = String(q?.public_id || raw.replace(/^official:/, ''));
    return publicId ? `official:${publicId}` : '';
  }
  return `bank:${raw}`;
}

// 已保存学习附件按题目缓存：接口未就绪/无权限时静默降级到预设，不打扰作答。
const principleArtifactCache = new Map();

function savedArtifactsFor(ref) {
  if (!ref) return Promise.resolve([]);
  const cached = principleArtifactCache.get(ref);
  if (cached) return cached.promise;
  const entry = { items: null, promise: null };
  entry.promise = api(`/artifacts/by-target?${query({ target_kind: 'question', target_key: ref })}`)
    .then((response) => {
      const items = Array.isArray(response?.items) ? response.items : [];
      entry.items = items;
      return items;
    })
    .catch(() => {
      // 静默降级：后端附件接口未上线时仍可直接看本地预设。
      entry.items = [];
      return [];
    });
  principleArtifactCache.set(ref, entry);
  return entry.promise;
}

/** 把 by-target 返回项转成可交给播放器的 artifact；结构不合法时返回 null。 */
function savedArtifactFrom(item, preset) {
  if (!item || typeof item !== 'object') return null;
  const artifact = item.artifact && typeof item.artifact === 'object' ? item.artifact : {};
  let data = item.data;
  if (typeof data === 'string') {
    try {
      data = JSON.parse(data);
    } catch {
      return null;
    }
  }
  if (!data || typeof data !== 'object') return null;
  const kind = String(artifact.kind || preset?.kind || '');
  if (!kind) return null;
  // by-target 的 revision 是版本号数字；兼容早期/对象形态，统一取整数版本号。
  const revision = typeof item.revision === 'object' && item.revision !== null
    ? item.revision.revision
    : item.revision;
  return {
    id: artifact.artifact_id,
    kind,
    title: String(artifact.title || preset?.title || t('study.demo.dialogTitle')),
    data,
    source: String(artifact.source || preset?.source || ''),
    // 附件接口只存 kind + data；假设文案沿用同主题预设（保存时的来源就是该预设）。
    assumptions: Array.isArray(artifact.assumptions) && artifact.assumptions.length
      ? artifact.assumptions
      : (preset?.assumptions || []),
    // source_ref 原样保留：M4 用预设 ID 区分同 kind 的多个预设，空值表示旧附件。
    sourceRef: String(artifact.source_ref || ''),
    revision: Number.isFinite(Number(revision)) ? Number(revision) : null,
    saved: true,
  };
}

/** 题目详情「查看原理」按钮；学科不匹配时返回 null。 */
function principleEntryButton(q) {
  const subject = String(q?.subject || '').trim();
  if (!PRINCIPLE_SUBJECTS.has(subject)) return null;
  const ref = questionRefOf(q);
  // 页面加载时预取已保存附件（失败静默）；点击打开时优先渲染已保存版本。
  if (ref) savedArtifactsFor(ref);
  return button(t('study.demo.open'), event => {
    openPrincipleDialog(q, event.currentTarget).catch((error) => {
      toast(error?.message || t('study.demo.renderFailed', { v1: '' }), true);
    });
  }, 'soft small', 'layers', {
    title: t('study.demo.openHint'),
    'data-principle-open': subject,
  });
}

/**
 * 打开「查看原理」弹层：按学科推荐默认预设 + 「切换演示」下拉 + 已保存附件优先 +
 * 保存为学习附件。不改变题目作答/揭晓/图片/公式等既有行为，入口失败只 toast。
 */
async function openPrincipleDialog(q, trigger) {
  const subject = String(q?.subject || '').trim();
  const ref = questionRefOf(q);
  // 先动态 import：原理播放器与预设不在题目首屏加载链路上，减少详情页初始体积。
  let principleModule;
  try {
    principleModule = await import('../components/principleDemo.js');
  } catch (error) {
    toast(t('study.demo.renderFailed', { v1: error?.message || '' }), true);
    return;
  }
  const {
    renderPrincipleDemo,
    PRESET_DEMOS: presetsById,
    PRESET_LIST: presetOrder,
  } = principleModule;
  const presetMap = presetsById && typeof presetsById === 'object' ? presetsById : {};
  const orderedPresets = (Array.isArray(presetOrder) ? presetOrder : Object.values(presetMap))
    .filter((preset) => preset && preset.id && presetMap[preset.id]);

  const presetOf = (id) => presetMap[String(id || '')] || null;
  // 按 artifact.kind 找本地预设（旧附件没有 source_ref 时兜底出处/假设/已保存状态）。
  const presetForKind = (kind) => orderedPresets.find((preset) => preset.kind === String(kind || '')) || null;

  // 按学科推荐：第一项为默认；推荐 ID 在预设表中缺失时回退到第一个可用预设。
  const recommendedPresetIds = SUBJECT_PRESET_HINTS[subject] || [];
  let initialPresetId = defaultPrinciplePresetId(subject);
  if (!presetOf(initialPresetId)) initialPresetId = '';
  if (!initialPresetId && orderedPresets.length) initialPresetId = orderedPresets[0].id;
  if (!initialPresetId) {
    toast(t('study.demo.renderFailed', { v1: t('study.demo.modeUnknown') }), true);
    return;
  }

  let controller = null;
  const viewState = {
    currentPresetId: initialPresetId,
    artifact: null,
    savedArtifactsByPreset: new Map(), // 预设 ID -> 已保存 artifact（用于优先渲染）
    savedArtifactsByKind: new Map(), // 旧附件（无 source_ref）按 kind 兜底
    interacted: false,
    upgraded: false,
  };

  const titleNode = el('h3', { class: 'pd-dialog-title', 'data-no-i18n': 'true' }, t('study.demo.dialogTitle'));
  const statusNode = el('p', { class: 'pd-dialog-status' }, el('span', { class: 'spinner' }), t('study.demo.loading'));
  const savedNotice = el('p', { class: 'pd-saved-notice', hidden: true }, t('study.demo.loadedSaved'));

  // 「切换演示」下拉：按 kind 分组列出 PRESET_DEMOS 全部预设；推荐预设带「推荐」后缀。
  const recommendedIds = new Set(recommendedPresetIds);
  const recommendedLabelTemplate = m4Text('study.m4.recommendedLabel', '{name}（推荐）');
  function presetOptionLabel(preset) {
    const recommended = recommendedIds.has(preset.id)
      || (Array.isArray(preset.subjectHints) && preset.subjectHints.includes(subject));
    return recommended
      ? recommendedLabelTemplate.replace('{name}', String(preset.title || ''))
      : String(preset.title || '');
  }
  function presetGroups() {
    const groups = [];
    const knownKinds = new Set();
    for (const group of PRINCIPLE_GROUP_ORDER) {
      knownKinds.add(group.kind);
      const items = orderedPresets.filter((preset) => preset.kind === group.kind);
      if (!items.length) continue;
      groups.push({
        optgroup: t(group.labelKey),
        options: items.map((preset) => ({ value: preset.id, label: presetOptionLabel(preset) })),
      });
    }
    // 防御：未知 kind 的预设归入「其它演示」，避免下拉漏项。
    const others = orderedPresets.filter((preset) => !knownKinds.has(preset.kind));
    if (others.length) {
      groups.push({
        optgroup: m4Text('study.m4.groupOther', '其它演示'),
        options: others.map((preset) => ({ value: preset.id, label: presetOptionLabel(preset) })),
      });
    }
    return groups;
  }
  const presetSelect = select(presetGroups(), initialPresetId, {
    id: 'pd-principle-preset',
    class: 'pd-preset-select',
    'aria-label': m4Text('study.m4.switchDemoAria', '选择要演示的原理预设'),
  });
  const presetHint = el('p', { class: 'pd-preset-hint' });
  const switchBar = el('div', { class: 'pd-preset-switch' },
    el('label', { class: 'pd-preset-label', for: 'pd-principle-preset' },
      m4Text('study.m4.switchDemo', '切换演示')),
    presetSelect,
    presetHint);
  presetSelect.addEventListener('change', () => switchPrinciplePreset(presetSelect.value));
  const demoHost = el('div', { class: 'pd-dialog-demo' });
  const metaHost = el('div', { class: 'pd-dialog-meta' });
  const saveButton = button(t('study.demo.saveArtifact'), event => {
    const triggerButton = event.currentTarget;
    busy(triggerButton, async () => {
      try {
        const currentPreset = presetOf(viewState.currentPresetId);
        const saved = await savePrincipleArtifact(currentPreset);
        const savedArtifact = {
          ...viewState.artifact,
          id: saved.artifactId,
          revision: saved.revision,
          // 记录来源预设 ID：M4 同一 kind 可能有多个预设，保存状态按预设区分。
          sourceRef: currentPreset?.id || viewState.artifact?.sourceRef || '',
          saved: true,
        };
        if (currentPreset) viewState.savedArtifactsByPreset.set(currentPreset.id, savedArtifact);
        // 同步本次保存到按题缓存：关闭再打开弹层时也能优先展示刚保存的版本。
        const cacheEntry = principleArtifactCache.get(ref);
        if (cacheEntry?.items) {
          cacheEntry.items = cacheEntry.items.filter((item) => {
            if (currentPreset) return String(item?.artifact?.source_ref || '') !== currentPreset.id;
            return item?.artifact?.kind !== savedArtifact.kind;
          });
          cacheEntry.items.push({
            artifact: {
              artifact_id: saved.artifactId,
              kind: savedArtifact.kind,
              title: savedArtifact.title,
              source_ref: savedArtifact.sourceRef,
              current_revision: saved.revision,
            },
            // 与服务端 by-target 返回结构保持一致：revision 是数字版本号。
            revision: saved.revision,
            data: savedArtifact.data,
          });
        }
        toast(t('study.demo.artifactSaved'));
      } catch (error) {
        // 不伪造成功：失败时按服务端/网络返回的中文原因提示。
        throw new Error(t('study.demo.saveFailed', { v1: error?.message || '' }));
      }
    }).finally(() => {
      if (dialog.open) updateSaveButton();
    });
  }, 'secondary small', 'download', { 'data-principle-save': 'true' });

  const dialog = el('dialog', {
    class: 'pd-dialog',
    'aria-label': t('study.demo.dialogTitle'),
    'data-principle-dialog': '',
  },
  el('div', { class: 'pd-dialog-head' },
    el('div', { class: 'pd-dialog-heading' },
      el('span', { class: 'eyebrow' }, t('study.demo.title')),
      titleNode),
    el('button', {
      type: 'button', class: 'close-dialog', 'aria-label': t('common.close'), onClick: () => dialog.close(),
    }, icon('close'))),
  el('div', { class: 'pd-dialog-body' },
    statusNode,
    switchBar,
    demoHost,
    metaHost,
    el('div', { class: 'pd-dialog-actions' }, saveButton)));

  // 用户一旦操作演示（点击/键盘），就不再自动替换成已保存版本，避免打断观看。
  const markInteracted = () => { viewState.interacted = true; };
  demoHost.addEventListener('pointerdown', markInteracted, { capture: true });
  demoHost.addEventListener('keydown', markInteracted, { capture: true });

  dialog.addEventListener('close', () => {
    if (controller) {
      try { controller.destroy(); } catch { /* 释放失败不影响关闭 */ }
      controller = null;
    }
    dialog.remove();
    trigger?.focus?.();
  });
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  document.body.append(dialog);
  dialog.showModal();

  // 已保存附件按来源预设区分：优先 source_ref 精确匹配；旧附件（无 source_ref）回退到 kind。
  function savedEntryForPreset(preset) {
    if (!preset) return { artifact: null };
    const byPreset = viewState.savedArtifactsByPreset.get(preset.id);
    if (byPreset) return { artifact: byPreset };
    return { artifact: viewState.savedArtifactsByKind.get(preset.kind) || null };
  }

  function artifactForPreset(preset) {
    const saved = savedEntryForPreset(preset).artifact;
    return { artifact: saved || preset, preset, saved: Boolean(saved) };
  }

  function updateSaveButton() {
    const preset = presetOf(viewState.currentPresetId);
    const saved = Boolean(preset && savedEntryForPreset(preset).artifact);
    saveButton.disabled = saved || !ref || !preset;
    saveButton.textContent = saved ? t('study.demo.saved') : t('study.demo.saveArtifact');
    saveButton.classList.toggle('is-saved', saved);
  }

  function updatePresetUi(preset, saved) {
    if (preset) presetSelect.value = preset.id;
    savedNotice.hidden = !saved;
    // 推荐提示始终展示「当前学科的默认推荐」，用户切到其它预设后也不会误导。
    const recommendedPreset = presetOf(recommendedPresetIds[0]) || null;
    const hint = subject && recommendedPreset
      ? m4Text('study.m4.presetHint', '按学科（{v1}）推荐：{v2}；也可以切换到其它演示。', {
        v1: subject,
        v2: recommendedPreset.title || '',
      })
      : '';
    presetHint.textContent = hint;
    presetHint.hidden = !hint;
  }

  function renderMeta(artifact, preset, saved) {
    const source = artifact?.source || preset?.source || '';
    const assumptions = (Array.isArray(artifact?.assumptions) && artifact.assumptions.length
      ? artifact.assumptions
      : (preset?.assumptions || []));
    savedNotice.hidden = !saved;
    metaHost.replaceChildren(
      el('p', { class: 'pd-artifact-notice' }, t('study.demo.artifactNotice')),
      source
        ? el('p', { class: 'pd-source' }, `${t('study.demo.sourceLabel')}：`,
          el('span', { 'data-no-i18n': 'true' }, source))
        : null,
      assumptions.length
        ? el('details', { class: 'pd-assumptions' },
          el('summary', {}, t('study.demo.assumptionsLabel')),
          el('ul', {}, ...assumptions.map((item) => el('li', { 'data-no-i18n': 'true' }, item))))
        : null,
      savedNotice,
    );
  }

  function renderCurrentView() {
    const preset = presetOf(viewState.currentPresetId);
    if (!preset) {
      statusNode.hidden = false;
      statusNode.textContent = t('study.demo.renderFailed', { v1: t('study.demo.modeUnknown') });
      return;
    }
    const { artifact, saved } = artifactForPreset(preset);
    viewState.artifact = artifact;
    titleNode.textContent = artifact.title || t('study.demo.dialogTitle');
    updatePresetUi(preset, saved);
    renderMeta(artifact, preset, saved);
    updateSaveButton();
    statusNode.hidden = true;
    if (controller) {
      // 切换预设时先销毁旧播放器（定时器/补间/事件）再挂载新预设，避免叠加。
      try { controller.destroy(); } catch { /* 旧播放器可能已随弹层移除 */ }
      controller = null;
    }
    demoHost.replaceChildren();
    controller = renderPrincipleDemo(demoHost, artifact, {
      onFrameReady: () => { statusNode.hidden = true; },
    });
  }

  function switchPrinciplePreset(presetId) {
    const preset = presetOf(presetId);
    if (!preset || preset.id === viewState.currentPresetId) return;
    viewState.currentPresetId = preset.id;
    viewState.interacted = true;
    renderCurrentView();
  }

  async function savePrincipleArtifact(currentPreset) {
    const artifact = viewState.artifact;
    if (!artifact) throw new Error(t('study.demo.missingArtifact'));
    if (!ref) throw new Error(t('study.demo.missingQuestionRef'));
    const preset = currentPreset || presetOf(viewState.currentPresetId);
    const payload = {
      kind: artifact.kind,
      title: artifact.title,
      data: artifact.data,
      // 保存当前选中预设的数据与来源：M4 同名 kind 有多个预设，用预设 ID 区分。
      source_ref: artifact.sourceRef || preset?.id || '',
      source_kind: 'principle_preset',
    };
    const created = await api('/artifacts', { method: 'POST', data: payload });
    const artifactId = created?.artifact?.artifact_id;
    if (!artifactId) throw new Error(t('study.demo.missingArtifactId'));
    const revision = created?.revision?.revision;
    const linkBody = { target_kind: 'question', target_key: ref };
    if (revision !== undefined && revision !== null) linkBody.revision = revision;
    await api(`/artifacts/${encodeURIComponent(String(artifactId))}/links`, {
      method: 'POST',
      data: linkBody,
    });
    return { artifactId, revision };
  }

  // 已保存附件：先读缓存快照（页面加载时已预取），命中则直接渲染已保存版本；
  // 未命中时先渲染本地预设，等接口返回且用户尚未操作时再自动升级，失败静默。
  const cachedArtifacts = principleArtifactCache.get(ref);
  function applySavedItems(items) {
    for (const item of Array.isArray(items) ? items : []) {
      const rawRef = String(item?.artifact?.source_ref || '');
      const matchedById = rawRef ? presetOf(rawRef) : null;
      const matchedPreset = matchedById || presetForKind(item?.artifact?.kind);
      const saved = savedArtifactFrom(item, matchedPreset);
      if (!saved) continue;
      if (matchedById) viewState.savedArtifactsByPreset.set(matchedById.id, saved);
      else if (saved.kind) viewState.savedArtifactsByKind.set(saved.kind, saved);
    }
  }
  if (cachedArtifacts?.items) applySavedItems(cachedArtifacts.items);
  renderCurrentView();
  if (!cachedArtifacts?.items) {
    const pending = cachedArtifacts ? cachedArtifacts.promise : savedArtifactsFor(ref);
    pending.then((items) => {
      if (!dialog.open) return;
      applySavedItems(items);
      const preset = presetOf(viewState.currentPresetId);
      if (!preset || !savedEntryForPreset(preset).artifact) {
        updateSaveButton();
        return;
      }
      if (viewState.upgraded) return;
      // 已交互或已切换预设时不打断当前播放，只更新「已保存」状态。
      if (viewState.interacted || viewState.currentPresetId !== initialPresetId) {
        updateSaveButton();
        return;
      }
      viewState.upgraded = true;
      renderCurrentView();
    }).catch(() => {
      /* 静默降级到本地预设 */
    });
  }
}

/* ---------------------------------------------------------------------------
 * M5 机械 3D / 工厂仿真入口（方案 §5.4「机械与工厂」/ §8 M5）
 * ---------------------------------------------------------------------------
 * - 与「查看原理」并列：物理/机械/技术题显示「机械演示」入口；
 * - 点击懒加载 components/mechDemo.js，弹出统一弹层（优先 GET /api/demos/templates，
 *   接口不可用回退本地 MECH_PRESETS；模板参数只读展示，前端不做数值模拟）；
 * - 保存为学习附件时带 links=[{target_kind:'question', target_key:<统一题目引用>}]；
 * - 无 WebGL / three 加载失败由组件内部降级为等距示意图并中文提示，本页不重复处理；
 * - 失败只中文 toast，不影响作答主流程。
 * ------------------------------------------------------------------------- */

const MECH_DEMO_SUBJECTS = new Set(['物理', '机械', '技术']);

// 学科 → 默认模板 ID；服务端模板库没有该 ID 时弹层自动回退到列表第一项。
const MECH_TEMPLATE_HINTS = Object.freeze({
  '物理': 'pump-3d',
  '机械': 'gearbox-3d',
  '技术': 'factory-line',
});

/** 打开机械/工厂演示弹层（懒加载组件；入口失败只 toast）。 */
async function launchMechDemo(q, trigger) {
  const module = await import('../components/mechDemo.js');
  const subject = String(q?.subject || '').trim();
  const ref = questionRefOf(q);
  module.openMechDemo({
    trigger,
    template_id: MECH_TEMPLATE_HINTS[subject] || '',
    links: ref ? [{ target_kind: 'question', target_key: ref }] : [],
    onSaved: () => {
      toast(m5Text('study.m5.saved', '已保存为学习附件'));
    },
  });
}

/** 题目详情「机械演示」按钮；学科不匹配时返回 null（与「查看原理」条件并列）。 */
function mechDemoEntryButton(q) {
  const subject = String(q?.subject || '').trim();
  if (!MECH_DEMO_SUBJECTS.has(subject)) return null;
  return button(m5Text('study.m5.open', '机械演示'), (event) => {
    launchMechDemo(q, event.currentTarget).catch((error) => {
      toast(m5Text('study.m5.openFailed', '打开机械演示失败：{v1}', {
        v1: error?.message || '',
      }), true);
    });
  }, 'soft small', 'sliders', {
    title: m5Text('study.m5.openHint', '打开机械 3D 拆解/剖切演示或工厂产线仿真回放'),
    'data-mech-demo-open': subject,
  });
}

/* ---------------------------------------------------------------------------
 * 批次五（§6.3）：物理题「自由沙盒」入口；Matter.js 首次打开时懒加载。
 * ------------------------------------------------------------------------- */

const SANDBOX_SUBJECTS = new Set(['物理']);

async function launchPhysicsSandbox(q, trigger) {
  const module = await import('../components/physicsSandbox.js');
  const ref = questionRefOf(q);
  module.openPhysicsSandbox({
    trigger,
    titleHint: String((q?.knowledge_tags || [])[0] || ''),
    links: ref ? [{ target_kind: 'question', target_key: ref }] : [],
    ref,
    onSaved: () => toast(t('study.oss.sandbox.savedToast')),
  });
}

function sandboxEntryButton(q) {
  const subject = String(q?.subject || '').trim();
  if (!SANDBOX_SUBJECTS.has(subject)) return null;
  return button(t('study.oss.sandbox.open'), (event) => {
    launchPhysicsSandbox(q, event?.currentTarget || null).catch(error => {
      toast(t('study.oss.sandbox.openFailed', { reason: error?.message || '' }), true);
    });
  }, 'soft small', 'atom', {
    title: t('study.oss.sandbox.openHint'),
    'data-physics-sandbox-open': subject,
  });
}

/* ---------------------------------------------------------------------------
 * 批次五（§6.2）：化学题「分子模型」入口；3Dmol 首次打开时懒加载。
 * ------------------------------------------------------------------------- */

const MOLECULE_SUBJECTS = new Set(['化学']);

async function launchMoleculeViewer(q, trigger) {
  const module = await import('../components/moleculeViewer.js');
  const ref = questionRefOf(q);
  module.openMoleculeViewer({
    trigger,
    titleHint: String((q?.knowledge_tags || [])[0] || t('study.oss.molecule.title')),
    links: ref ? [{ target_kind: 'question', target_key: ref }] : [],
    ref,
    onSaved: () => toast(t('study.oss.molecule.savedToast')),
  });
}

/** 题目详情「分子模型」按钮；学科不匹配时返回 null。 */
function moleculeEntryButton(q) {
  const subject = String(q?.subject || '').trim();
  if (!MOLECULE_SUBJECTS.has(subject)) return null;
  return button(t('study.oss.molecule.open'), (event) => {
    launchMoleculeViewer(q, event?.currentTarget || null).catch(error => {
      toast(t('study.oss.molecule.openFailed', { reason: error?.message || '' }), true);
    });
  }, 'soft small', 'atom', {
    title: t('study.oss.molecule.openHint'),
    'data-molecule-viewer-open': subject,
  });
}

/* ---------------------------------------------------------------------------
 * 批次五（§6.1）：数学题「几何演示」入口；JSXGraph 首次打开时懒加载。
 * ------------------------------------------------------------------------- */

const GEOMETRY_SUBJECTS = new Set(['数学']);

async function launchGeometryDemo(q, trigger) {
  const module = await import('../components/geometryDemo.js');
  const ref = questionRefOf(q);
  module.openGeometryDemo({
    trigger,
    titleHint: String((q?.knowledge_tags || [])[0] || ''),
    links: ref ? [{ target_kind: 'question', target_key: ref }] : [],
    ref,
    onSaved: () => toast(t('study.oss.geometry.savedToast')),
  });
}

/** 题目详情「几何演示」按钮；学科不匹配时返回 null。 */
function geometryEntryButton(q) {
  const subject = String(q?.subject || '').trim();
  if (!GEOMETRY_SUBJECTS.has(subject)) return null;
  return button(t('study.oss.geometry.open'), (event) => {
    launchGeometryDemo(q, event?.currentTarget || null).catch(error => {
      toast(t('study.oss.geometry.openFailed', { reason: error?.message || '' }), true);
    });
  }, 'soft small', 'compass', {
    title: t('study.oss.geometry.openHint'),
    'data-geometry-demo-open': subject,
  });
}

/* ---------------------------------------------------------------------------
 * 批次五（§6.5）：历史/政治题「时间轴」入口；vis-timeline 首次打开时懒加载。
 * ------------------------------------------------------------------------- */

const TIMELINE_SUBJECTS = new Set(['历史', '政治']);

async function launchTimelineDemo(q, trigger) {
  const module = await import('../components/timelineDemo.js');
  const ref = questionRefOf(q);
  module.openTimelineDemo({
    trigger,
    titleHint: String((q?.knowledge_tags || [])[0] || ''),
    links: ref ? [{ target_kind: 'question', target_key: ref }] : [],
    ref,
    onSaved: () => toast(t('study.oss.timeline.savedToast')),
  });
}

/** 题目详情「时间轴」按钮；学科不匹配时返回 null。 */
function timelineEntryButton(q) {
  const subject = String(q?.subject || '').trim();
  if (!TIMELINE_SUBJECTS.has(subject)) return null;
  return button(t('study.oss.timeline.open'), (event) => {
    launchTimelineDemo(q, event?.currentTarget || null).catch(error => {
      toast(t('study.oss.timeline.openFailed', { reason: error?.message || '' }), true);
    });
  }, 'soft small', 'history', {
    title: t('study.oss.timeline.openHint'),
    'data-timeline-demo-open': subject,
  });
}

/* ---------------------------------------------------------------------------
 * M3 数学互动入口（方案 §3「数学主流程」第 2 步 / §5.4「Mafs 与 Excalidraw」「逐步辅导」）
 * ---------------------------------------------------------------------------
 * - 「函数图像」：数学题或题干含公式时显示；懒加载 components/functionGraph.js
 *   （Mafs 兼容定义 + 原生 SVG），先按 by-target 取已保存的 function_graph 附件；
 * - 「画解题思路」：所有题显示；懒加载 components/sketchBoard.js
 *   （Excalidraw 兼容场景 + 原生 canvas），先按 by-target 取已保存的 whiteboard 附件；
 * - 「按步骤提示」：所有题显示；直接调用 /api/guided-hints，未解锁步骤不缓存/不推测正文。
 * 弹层保存成功后不刷新整页；组件不可用/接口未就绪时只中文提示，不影响作答主流程。
 * ------------------------------------------------------------------------- */

/** 函数图入口条件：数学题、后端标记 has_formula、或题干内容块含公式（latex）。 */
function isFunctionGraphQuestion(q) {
  if (!q || typeof q !== 'object') return false;
  if (String(q.subject || '').trim() === '数学') return true;
  if (q.has_formula === true) return true;
  return blockList(q.stem_blocks).some((block) => {
    if (!block || typeof block !== 'object') return false;
    const kind = String(block.kind || block.type || '').toLowerCase();
    return kind === 'math' || (typeof block.latex === 'string' && block.latex.trim() !== '');
  });
}

/** 打开函数图工具（按需加载组件；先探测题目已保存附件，接口不可用时打开默认图像）。 */
async function launchFunctionGraphTool(q, trigger) {
  const ref = questionRefOf(q);
  await busy(trigger, async () => {
    try {
      const module = await import('../components/functionGraph.js');
      await module.openFunctionGraphForTarget({
        ref,
        trigger,
        titleHint: String(q.number_label || q.qid || '').trim(),
        fallbackItems: q.study && Array.isArray(q.study.artifacts) ? q.study.artifacts : [],
      });
    } catch (error) {
      toast(toolText('study.tools.open.failed', '打开函数图像失败：{v1}', {
        v1: error?.message || '',
      }), true);
    }
  });
}

/** 打开解题白板工具（按需加载组件；先探测题目已保存白板附件）。 */
async function launchSketchBoardTool(q, trigger) {
  const ref = questionRefOf(q);
  await busy(trigger, async () => {
    try {
      const module = await import('../components/sketchBoard.js');
      await module.openSketchBoardForTarget({
        ref,
        trigger,
        titleHint: String(q.number_label || q.qid || '').trim(),
        fallbackItems: q.study && Array.isArray(q.study.artifacts) ? q.study.artifacts : [],
      });
    } catch (error) {
      toast(toolText('study.tools.open.failed', '打开解题白板失败：{v1}', {
        v1: error?.message || '',
      }), true);
    }
  });
}

function functionGraphEntryButton(q) {
  if (!isFunctionGraphQuestion(q)) return null;
  return button(toolText('study.tools.open.graph', '函数图像'), event => {
    launchFunctionGraphTool(q, event.currentTarget);
  }, 'soft small', 'chart', {
    title: toolText('study.tools.open.graphHint', '打开函数图像，拖动参数观察图像变化'),
    'data-study-tool-open': 'function-graph',
  });
}

function sketchBoardEntryButton(q) {
  return button(toolText('study.tools.open.board', '画解题思路'), event => {
    launchSketchBoardTool(q, event.currentTarget);
  }, 'soft small', 'edit', {
    title: toolText('study.tools.open.boardHint', '打开白板，画下你的解题思路'),
    'data-study-tool-open': 'whiteboard',
  });
}

function guidedHintsEntryButton(q) {
  return button(toolText('study.tools.open.hints', '按步骤提示'), event => {
    try {
      openGuidedHintsPanel({
        ref: questionRefOf(q),
        trigger: event.currentTarget,
        label: [q.subject, q.number_label || q.qid].filter(Boolean).join(' · '),
      });
    } catch (error) {
      toast(toolText('study.tools.open.failed', '打开逐步提示失败：{v1}', {
        v1: error?.message || '',
      }), true);
    }
  }, 'soft small', 'message', {
    title: toolText('study.tools.open.hintsHint', '按步骤提交尝试，逐级解锁完整解析'),
    'data-study-tool-open': 'guided-hints',
  });
}

/* ------------------------------------------------------------------------- *
 * 逐步提示弹层（方案 §3「数学主流程」/ §5.3「逐步辅导」）
 * - 先 GET /api/guided-hints/by-ref；不存在（404）再 POST /api/guided-hints 创建；
 * - 完整解析是最后一步，必须在前面步骤提交尝试后逐级解锁；未解锁内容由服务端保留，
 *   前端只渲染服务端下发的步骤（locked 步骤只有标题，不缓存/不推测正文）；
 * - 每次提交尝试 POST /advance {attempt}，返回 requires_attempt 时按服务端 message 提示；
 * - 全部解锁后展示完成状态与 source_note（默认「规则分步提示，完整解析仅在提交尝试后解锁」）。
 * ------------------------------------------------------------------------- */

const HINT_SOURCE_NOTE_FALLBACK = '规则分步提示，完整解析仅在提交尝试后解锁';

/**
 * 打开某道题的「按步骤提示」弹层（原生 dialog，Esc 关闭，焦点归还触发按钮）。
 * @param {{ref: string, trigger?: HTMLElement, label?: string}} options
 * @returns {{element: HTMLDialogElement, dialog: HTMLDialogElement, reload: Function,
 *            close: Function, destroy: Function} | null}
 */
export function openGuidedHintsPanel(options = {}) {
  const ref = String(options.ref || '').trim();
  if (!ref) {
    toast(toolText('study.tools.hints.noRef', '当前题目缺少统一引用，暂时无法使用逐步提示。'), true);
    return null;
  }
  const labelText = String(options.label || '').trim();
  const state = {
    hint: null,
    steps: [],
    sourceNote: '',
    loading: true,
    error: '',
    notice: '',
  };
  let requestSeq = 0;
  let destroyed = false;

  const dialog = el('dialog', {
    class: 'st-dialog st-hints-dialog',
    'data-study-tool': 'guided-hints-dialog',
    'aria-label': toolText('study.tools.title.hints', '按步骤提示'),
  });
  const progressNode = el('p', { class: 'st-hints-progress', role: 'status', 'aria-live': 'polite' });
  const statusHost = el('div', { class: 'st-hints-status' });
  const noticeHost = el('div', { class: 'st-hints-notice', 'aria-live': 'polite' });
  const stepsHost = el('div', { class: 'st-hints-steps' });
  const completeHost = el('div', { class: 'st-hints-complete-host' });
  const attemptInput = el('textarea', {
    class: 'st-hints-input',
    rows: '3',
    maxlength: '2000',
    placeholder: toolText('study.tools.hints.inputPlaceholder', '写下你这一步的尝试'),
    'aria-label': toolText('study.tools.hints.inputPlaceholder', '写下你这一步的尝试'),
  });
  const submitButton = button(
    toolText('study.tools.hints.submit', '提交并解锁下一步'),
    () => submitAttempt(),
    '',
    'check',
    { 'data-hints-submit': 'true' },
  );
  const form = el('form', { class: 'st-hints-form' },
    el('label', { class: 'st-hints-input-label' },
      el('span', {}, toolText('study.tools.hints.attemptLabel', '这一步的尝试')),
      attemptInput),
    submitButton);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submitAttempt();
  });
  const formHost = el('div', { class: 'st-hints-form-host' }, form);

  dialog.append(
    el('div', { class: 'st-dialog-head' },
      el('div', {},
        el('div', { class: 'st-dialog-eyebrow' }, toolText('study.tools.hints.eyebrow', '逐步辅导')),
        el('h3', { class: 'st-dialog-title' }, toolText('study.tools.title.hints', '按步骤提示')),
        labelText ? el('p', { class: 'st-dialog-subtitle', 'data-no-i18n': 'true' }, labelText) : null),
      el('button', {
        type: 'button',
        class: 'st-dialog-close',
        'aria-label': toolText('study.tools.close', '关闭'),
        title: toolText('study.tools.close', '关闭'),
        onClick: () => dialog.close(),
      }, icon('close'))),
    el('div', { class: 'st-dialog-body' },
      progressNode,
      statusHost,
      noticeHost,
      stepsHost,
      completeHost,
      formHost),
    el('div', { class: 'st-dialog-actions' },
      button(toolText('study.tools.close', '关闭'), () => dialog.close(), 'secondary')),
  );

  function progressOf() {
    const hint = state.hint && typeof state.hint === 'object' ? state.hint : {};
    const progress = hint.progress && typeof hint.progress === 'object' ? hint.progress : {};
    const unlockedCount = state.steps.filter(step => step && !step.locked).length;
    const totalRaw = Number(progress.total);
    const total = Number.isFinite(totalRaw) && totalRaw > 0 ? totalRaw : state.steps.length;
    const unlockedRaw = Number(progress.unlocked);
    const unlocked = Number.isFinite(unlockedRaw) && unlockedRaw >= 0 ? unlockedRaw : unlockedCount;
    const done = progress.done === true || hint.status === 'completed'
      || (state.steps.length > 0 && state.steps.every(step => step && !step.locked));
    return { total, unlocked: Math.min(unlocked, Math.max(total, unlocked)), done };
  }

  function applyPayload(payload) {
    const hint = payload && payload.hint && typeof payload.hint === 'object' ? payload.hint : {};
    state.hint = hint;
    state.steps = Array.isArray(payload?.steps)
      ? payload.steps.filter(step => step && typeof step === 'object')
      : [];
    state.sourceNote = String(payload?.source_note || hint.source_note || '').trim();
  }

  function render() {
    if (destroyed) return;
    const progress = progressOf();
    dialog.setAttribute('data-hints-state', state.loading ? 'loading' : state.error ? 'error'
      : progress.done ? 'completed' : 'active');

    progressNode.hidden = state.loading || Boolean(state.error);
    if (!progressNode.hidden) {
      progressNode.textContent = toolText('study.tools.hints.progress', '已解锁 {v1}/{v2}', {
        v1: progress.unlocked,
        v2: progress.total,
      });
    }

    if (state.loading) {
      statusHost.replaceChildren(el('p', { class: 'st-hints-loading' },
        el('span', { class: 'spinner' }),
        toolText('study.tools.hints.loading', '正在读取逐步提示…')));
      stepsHost.replaceChildren();
      completeHost.replaceChildren();
      formHost.hidden = true;
    } else if (state.error) {
      statusHost.replaceChildren(el('div', { class: 'st-hints-alert' },
        toolText('study.tools.hints.unavailable', '逐步提示暂不可用：{v1}', { v1: state.error }),
        button(toolText('study.tools.hints.retry', '重试'), () => load(), 'secondary small', 'repeat')));
      stepsHost.replaceChildren();
      completeHost.replaceChildren();
      formHost.hidden = true;
    } else {
      statusHost.replaceChildren();
      renderSteps();
      renderComplete(progress);
      formHost.hidden = progress.done;
    }

    if (state.notice) {
      noticeHost.replaceChildren(el('div', { class: 'st-hints-alert' }, state.notice));
    } else {
      noticeHost.replaceChildren();
    }
  }

  function renderSteps() {
    const rows = state.steps.map((step, index) => {
      const locked = Boolean(step.locked);
      const position = Number(step.position);
      const stepNumber = Number.isFinite(position) ? position + 1 : index + 1;
      const row = el('section', {
        class: `st-hint-step ${locked ? 'is-locked' : 'is-unlocked'}`,
        'data-hint-position': String(Number.isFinite(position) ? position : index),
      });
      const head = el('div', { class: 'st-hint-step-head' },
        el('span', { class: 'st-hint-step-no' }, toolText('study.tools.hints.step', '第 {v1} 步', { v1: stepNumber })),
        el('strong', { class: 'st-hint-step-title', 'data-no-i18n': 'true' },
          String(step.title || '') || toolText('study.tools.hints.untitled', '未命名步骤')));
      if (!locked && step.kind === 'solution') {
        head.append(el('span', { class: 'st-hint-badge' }, toolText('study.tools.hints.solution', '完整解析')));
      }
      if (!locked && step.attempted) {
        head.append(el('span', { class: 'st-hint-badge is-ok' }, toolText('study.tools.hints.attempted', '已提交尝试')));
      }
      row.append(head);
      if (locked) {
        row.append(el('p', { class: 'st-hint-locked' },
          icon('lock'), toolText('study.tools.hints.lockedHint', '提交你的尝试后可解锁')));
      } else {
        const bodyText = String(step.body || '').trim();
        if (bodyText) {
          // 已解锁步骤正文由服务端下发（Markdown）：走统一 markdown() 净化入口，不做 HTML 拼接。
          row.append(el('div', { class: 'st-hint-body' }, markdown(bodyText)));
        }
      }
      return row;
    });
    if (!rows.length) {
      rows.push(el('p', { class: 'st-hints-empty' },
        toolText('study.tools.hints.empty', '这道题暂时没有分步提示。')));
    }
    stepsHost.replaceChildren(...rows);
  }

  function renderComplete(progress) {
    if (!progress.done) {
      completeHost.replaceChildren();
      return;
    }
    const note = state.sourceNote
      || toolText('study.tools.hints.sourceNoteDefault', HINT_SOURCE_NOTE_FALLBACK);
    completeHost.replaceChildren(el('div', { class: 'st-hints-complete', role: 'status' },
      el('strong', { class: 'st-hints-complete-title' },
        icon('check'), toolText('study.tools.hints.completed', '全部步骤已解锁，完整解析已显示。')),
      el('p', { class: 'st-hints-source', 'data-no-i18n': 'true' }, note)));
  }

  function applyAdvance(response) {
    const hint = response?.hint && typeof response.hint === 'object' ? response.hint : state.hint;
    const unlocked = response?.unlocked && typeof response.unlocked === 'object' ? response.unlocked : null;
    if (Array.isArray(response?.steps)) {
      // 服务端在 advance 响应里给出完整步骤视图（已解锁含正文、未解锁只有标题）：
      // 直接采用，避免本地合并产生状态漂移；未解锁正文仍只来自服务端。
      state.steps = response.steps.filter(step => step && typeof step === 'object');
    } else if (unlocked) {
      const position = Number(unlocked.position);
      const index = state.steps.findIndex(step => Number(step?.position) === position);
      if (index >= 0) state.steps[index] = unlocked;
      else state.steps.push(unlocked);
    }
    if (hint) state.hint = hint;
    if (response?.source_note) state.sourceNote = String(response.source_note).trim();
    if (unlocked) {
      toast(toolText('study.tools.hints.unlockedNext', '已解锁下一步：{v1}', {
        v1: String(unlocked.title || '').trim() || toolText('study.tools.hints.nextStep', '下一步'),
      }));
    } else if (response?.requires_attempt) {
      state.notice = String(response.message || '').trim()
        || toolText('study.tools.hints.requiresAttempt', '请先提交你这一步的尝试，再解锁下一步。');
    } else if (response?.message) {
      state.notice = String(response.message).trim();
    } else {
      state.notice = '';
    }
    render();
  }

  async function submitAttempt() {
    if (state.loading || state.error || destroyed) return;
    const hintId = String(state.hint?.hint_id || '').trim();
    if (!hintId) {
      state.notice = toolText('study.tools.hints.missingHint', '服务端未返回提示会话 ID，请关闭后重试。');
      render();
      return;
    }
    const text = String(attemptInput.value || '').trim();
    if (text.length < 1) {
      state.notice = toolText('study.tools.hints.emptyAttempt', '请先写下你这一步的尝试（至少 1 个字）。');
      render();
      attemptInput.focus();
      return;
    }
    await busy(submitButton, async () => {
      try {
        const response = await api(`/guided-hints/${encodeURIComponent(hintId)}/advance`, {
          method: 'POST',
          data: { attempt: text },
        });
        if (destroyed) return;
        attemptInput.value = '';
        applyAdvance(response);
      } catch (error) {
        if (destroyed) return;
        state.notice = toolText('study.tools.hints.advanceFailed', '提交失败：{v1}', {
          v1: error?.message || '',
        });
        render();
      }
    });
  }

  async function load() {
    const token = ++requestSeq;
    state.loading = true;
    state.error = '';
    state.notice = '';
    attemptInput.value = '';
    render();
    try {
      let payload = null;
      try {
        // 优先读取已有会话；404（尚未创建）或接口暂不可用时回退 POST 创建（已存在会重放）。
        payload = await api(`/guided-hints/by-ref?${query({ ref })}`);
      } catch {
        payload = await api('/guided-hints', { method: 'POST', data: { ref } });
      }
      if (destroyed || token !== requestSeq) return;
      applyPayload(payload);
      state.loading = false;
      render();
    } catch (error) {
      if (destroyed || token !== requestSeq) return;
      state.loading = false;
      state.error = error?.message || '';
      render();
    }
  }

  dialog.addEventListener('close', () => {
    destroyed = true;
    requestSeq += 1;
    dialog.remove();
    try {
      options.trigger?.focus?.();
    } catch {
      /* 焦点归还失败忽略 */
    }
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  document.body.append(dialog);
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
  load();

  return {
    element: dialog,
    dialog,
    reload: load,
    close: () => dialog.close(),
    destroy: () => dialog.close(),
  };
}

export async function openQuestion(qid, container = $('#question-detail'), { scrollOnMobile = false, onSubmitted = null } = {}) {
  if (!container) return;
  revokePreviewUrl();
  const changed = state.selectedQid !== qid;
  state.selectedQid = qid;
  if (changed) {
    state.questionJobId = null;
    // 换题时关闭上一题遗留的 SSE（讲解/核验流），任务结果仍可从历史记录重新挂载。
    closeJobStreams();
    // 通知题库页同步结果卡高亮（题组小问切换时阅读区与选中态保持一致）。
    window.dispatchEvent(new CustomEvent('zhixu:question-opened', { detail: { qid } }));
  }
  container.replaceChildren(loading(t('question.openingTheFullQuestion')));
  if (scrollOnMobile && window.innerWidth <= 900) {
    container.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  try {
    const q = await api(`/questions/${encodeURIComponent(qid)}`);
    if (!container.isConnected || state.selectedQid !== qid) return;
    let savedExps = { items: [], community_items: [], community_enabled: false };
    let explanationsError = '';
    // 平台精选题是 official 只读引用：讲解库按题库 qid 存，后端未接 official 时
    // 不发请求，避免每道精选题都多一条「讲解加载失败」提示。
    if (!String(qid).startsWith('official:')) {
      try {
        savedExps = await api(`/explanations?${query({ qid })}`);
      } catch (error) {
        explanationsError = error.message;
      }
    }
    await Promise.allSettled([ensureExplainModes(), ensureKnowledgePoints()]);
    if (!container.isConnected || state.selectedQid !== qid) return;
    state.currentQuestion = q;
    renderQuestion(q, container, savedExps.items || [], {
      onSubmitted,
      communityItems: savedExps.community_items || [],
      communityEnabled: savedExps.community_enabled,
      savedExplanationsError: explanationsError,
    });
  } catch (error) {
    container.replaceChildren(el('div', { class: 'error-panel' }, error.message,
      button(t('question.retryOpeningTheQuestion'), () => openQuestion(qid, container, { scrollOnMobile, onSubmitted }), 'secondary small', 'repeat')));
  }
}

function actionsHostFor(container) {
  const host = document.getElementById('reading-actions');
  if (host && host.isConnected && container.closest('.reading-pane')) return host;
  return null;
}

export function renderQuestion(
  q, container, savedExplanations = [],
  { onSubmitted = null, communityItems = [], communityEnabled = false, savedExplanationsError = '' } = {},
) {
  const latest = q.latest_attempt;
  const correct = latest?.result === 'correct';
  // 统一题目引用：bank:<qid> / app:<id> / official:<public_id>。
  // 个人题与平台精选题都按来源隐藏题库专属入口（AI 讲解、管理员标注等），
  // 避免请求只读题库或讲解库不支持的接口。
  const isApp = q.source === 'app';
  const isOfficial = isOfficialQuestion(q);
  const officialPublicId = officialPublicIdOf(q);
  const keyword = state.filters?.q || '';
  const assetContext = { qid: q.qid, bankVersion: q.bank_version || '' };
  const actionsHost = actionsHostFor(container);

  // ---- 阅读路径与标题（共用材料、当前问、原卷题号统一在此确认）
  const pathParts = readingPathParts(q);
  const path = el('div', { class: 'reading-path' },
    el('span', { class: 'path-section' }, pathParts.slice(0, 1).join(' · ')),
    pathParts.slice(1).map(part => el('span', {}, part)));
  const title = el('div', { class: 'reading-title-row' },
    el('h2', {}, memberLabel({
      number_label: q.number_label,
      sub_question_label: q.sub_question_label,
      group_role: q.group_role,
      source_question_id: q.source_question_id,
    })),
    statusChip(q, latest));
  container.replaceChildren(path, title);

  const collectButton = !isApp && !isOfficial ? collectToMyBankButton(q) : null;
  const metaRow = el('div', { class: 'row', style: 'margin:0 0 8px' },
    isApp ? badge(q.source_label || t('question.myBankSource'), 'blue') : null,
    isOfficial ? badge(t('question.source.official'), 'blue') : null,
    isOfficial ? officialReviewBadge(q) : null,
    !isApp && !isOfficial ? badge(t('question.source.bank'), '') : null,
    badge(q.subject, 'teal'), badge(typeLabel(q, state.catalog)), difficultyBadge(q),
    q.subject === '物理'
      ? button(t('nav.physics'), () => go('physics'), 'ghost small', 'atom')
      : null,
    // 方案界面落点：题目详情提供「查看原理」；仅六科显示（见 principleEntryButton）。
    principleEntryButton(q),
    // M5 机械与工厂：物理/机械/技术题提供「机械演示」（懒加载 mechDemo.js，失败仅 toast）。
    mechDemoEntryButton(q),
    // 批次五（§6.3）：物理题「自由沙盒」（懒加载 Matter.js，探索模式）。
    sandboxEntryButton(q),
    // 批次五（§6.2）：化学题「分子模型」（懒加载 3Dmol，失败仅 toast）。
    moleculeEntryButton(q),
    // 批次五（§6.1）：数学题「几何演示」（懒加载 JSXGraph，失败仅 toast）。
    geometryEntryButton(q),
    // 批次五（§6.5）：历史/政治题「时间轴」（懒加载 vis-timeline，失败仅 toast）。
    timelineEntryButton(q),
    // M3 数学互动：函数图像（数学/公式题）、画解题思路（所有题）、按步骤提示（所有题）。
    functionGraphEntryButton(q),
    sketchBoardEntryButton(q),
    guidedHintsEntryButton(q),
    button(t('question.presentationMode'), () => {
      openPresentationMode({ qid: q.qid, question: q })
        .catch(error => toast(error?.message || t('wrongbook.presentationFailed'), true));
    }, 'soft small', 'presentation', {
      disabled: !q.unlocked,
      title: q.unlocked
        ? t('question.presentationModeRevealTheAnswerAndSolution')
        : t('question.submitYourOwnAnswerFirstPresentationMode'),
    }),
    // 报错入口：平台精选题接入广场纠错接口（POST /plaza/questions/{public_id}/issues），
    // 高考题库题沿用既有题库报错弹窗；个人题无此入口。
    isApp ? null
      : button(t('question.reportAnIssue'), () => {
        if (isOfficial) {
          openOfficialIssueDialog(q, officialPublicId);
          return;
        }
        openIssueDialog(q);
      }, 'ghost small', 'edit', isOfficial ? {
        'data-official-issue': 'true',
        'data-public-id': officialPublicId,
      } : {}),
    collectButton,
    isOfficial
      ? button(t('question.collect.unavailable'), () => toast(t('question.source.collectUnavailable')), 'ghost small', 'lock')
      : null);
  container.append(metaRow);
  // 已收录状态要在按钮进入 DOM 后才能补「去我的题库」入口。
  if (collectButton && isCollectedToBank(q.qid)) applyCollectedState(collectButton);
  if (isApp) container.append(notice(t('question.myBankNotice')));
  if (isOfficial) {
    // 平台精选题：来源编号 + 精选版本（WS19 official 读取层字段），缺失时降级不显示。
    const publicLine = [
      officialPublicId ? t('question.source.publicId', { v1: officialPublicId }) : '',
      q.published_version ? t('question.source.featuredVersion', { v1: q.published_version }) : '',
    ].filter(Boolean).join(' · ');
    if (publicLine) container.append(el('p', { class: 'help', 'data-no-i18n': 'true' }, publicLine));
    // 精选编号直达广场公开详情（#/plaza?id=public_id）；缺失公开编号时降级不渲染链接。
    if (officialPublicId) {
      container.append(el('div', { class: 'row official-actions', style: 'margin:0 0 8px' },
        button(t('question.source.openInPlaza'), () => go('plaza', null, { id: officialPublicId }),
          'soft small', 'layers', { 'data-official-plaza-link': officialPublicId })));
    }
    container.append(notice(t('question.source.officialNotice')));
    // 后端可见性提示（例如历史引用的题已撤回/暂停展示）原样展示，不伪造结论。
    const backendNotice = String(q.notice || '').trim();
    if (backendNotice) container.append(el('div', { class: 'notice warning', 'data-no-i18n': 'true' }, backendNotice));
  }
  const quality = qualityNotice(q);
  // 平台精选题的核验状态以后端 official 标签/notice 为准，不再叠加题库侧的
  // 「原卷参考答案待核验」提示（避免把已发布的精选题说成待核验）。
  if (quality && !isOfficial) container.append(quality);

  // ---- 知识点与解法标签
  const kpRow = el('div', { class: 'row', style: 'margin:0 0 10px' });
  for (const kp of q.knowledge_points || []) {
    const tagLabel = kp.source === 'ai' || kp.source === 'ai_pending_review'
      ? t('question.aiTagged', { v1: kp.name })
      : kp.source === 'ai_reviewed' ? t('question.reviewed', { v1: kp.name }) : kp.name;
    kpRow.append(el('button', {
      type: 'button', class: 'kp-chip', title: t('question.viewTheOfficialKnowledgeListAndSimilar'),
      onClick: () => openKpDetailDialog(kp.kp_id, kp.name),
    }, `# ${tagLabel}`));
  }
  for (const m of q.methods || []) {
    kpRow.append(el('button', {
      type: 'button', class: 'method-chip', title: t('question.filterSimilarQuestionsByThisQuestionType'),
      onClick: () => {
        state.filters = { subject: m.subject, method_id: String(m.method_id) };
        go('practice', null, { subject: m.subject, method_id: m.method_id });
      },
    }, `⚙ ${m.name}`));
  }
  if (kpRow.childNodes.length) container.append(kpRow);

  // ---- 题组成员导航（实时状态：隔离/上下文成员不可点）
  const group = q.group || {};
  const memberIndex = Array.isArray(q.member_index) && q.member_index.length
    ? q.member_index
    : (Array.isArray(group.member_index) ? group.member_index : []);
  if (memberIndex.length > 1) {
    const navigable = memberIndex.filter(member => member && member.group_role !== 'parent');
    const available = navigable.filter(member => member.available !== false && member.in_bank !== false
      && member.practice_status !== 'quarantined');
    const readonly = navigable.filter(member => member.in_bank === false || member.practice_status === 'context');
    const tabs = el('nav', { class: 'reading-tabs', 'aria-label': t('question.subQuestionsInThisGroup') });
    for (const member of memberIndex) {
      if (!member || typeof member !== 'object') continue;
      const isCurrent = Boolean(member.qid && member.qid === q.qid);
      const actionable = Boolean(member.qid) && member.in_bank !== false
        && member.available !== false && member.practice_status !== 'quarantined';
      const statusText = memberStatusText(member, isCurrent);
      tabs.append(el('button', {
        type: 'button',
        class: 'reading-tab',
        'aria-current': isCurrent ? 'true' : null,
        disabled: !actionable || isCurrent,
        title: actionable ? t('question.openThisSubQuestion') : (statusText || t('question.readOnly')),
        onClick: () => actionable && openQuestion(member.qid, container, {
          onSubmitted, scrollOnMobile: window.innerWidth <= 900,
        }),
      }, memberLabel(member), statusText ? el('small', {}, statusText) : null));
    }
    container.append(tabs);
    if (available.length !== navigable.length) {
      container.append(el('p', { class: 'help', style: 'margin:-10px 0 12px' },
        t('question.thisGroupHasQuestionsCanBeAnswered', { v1: navigable.length, v2: available.length })
        + (readonly.length ? t('question.readOnly_2', { v1: readonly.length }) : '')
        + (navigable.length - available.length - readonly.length > 0
          ? t('question.pendingReview_2', { v1: navigable.length - available.length - readonly.length }) : '') + '。'));
    }
  }

  // ---- 共用材料/题干：整组只显示一次，长材料可折叠
  const sharedBlocks = blockList(q.shared_blocks);
  if (!sharedBlocks.length) sharedBlocks.push(...blockList(group.shared_blocks));
  if (sharedBlocks.length) {
    const body = el('div', { class: 'shared-material-body question-text question-blocks' },
      questionBlocks(sharedBlocks, q.subject, keyword, assetContext));
    const material = el('section', { class: 'shared-material' },
      el('h3', {}, groupLabel(group.group_kind || q.group_kind)),
      body);
    const textLength = sharedBlocks.reduce((sum, block) => sum + String(block.text || '').length, 0);
    if (textLength > 520) {
      const collapsedHeight = '12em';
      const toggle = el('button', {
        type: 'button', class: 'shared-material-toggle', 'aria-expanded': 'false',
        onClick: () => {
          const expanded = toggle.getAttribute('aria-expanded') === 'true';
          toggle.setAttribute('aria-expanded', String(!expanded));
          body.style.maxHeight = expanded ? collapsedHeight : 'none';
          toggle.textContent = expanded ? t('question.expandAllMaterial') : t('question.collapseMaterial');
        },
      }, t('question.expandAllMaterial'));
      body.style.maxHeight = collapsedHeight;
      body.style.overflow = 'hidden';
      material.append(toggle);
    }
    container.append(material);
  } else if (q.group && (q.group.material_text || q.group.parent_stem)) {
    const material = el('section', { class: 'shared-material' },
      el('h3', {}, t('question.readingMaterial')),
      el('div', { class: 'question-text' },
        q.group.material_text
          ? questionText(q.group.material_text, q.group.material_images || [], q.subject, keyword, assetContext)
          : null,
        q.group.parent_stem && q.group.parent_stem !== q.stem_plain && q.group.parent_stem !== q.group.material_text
          ? questionText(q.group.parent_stem, q.group.material_images || [], q.subject, keyword, assetContext)
          : null));
    container.append(material);
  }

  // ---- 只读成员题干
  const readOnlyMembers = memberIndex.filter(member =>
    member && member.in_bank === false && blockList(member.blocks).length);
  for (const member of readOnlyMembers) {
    container.append(el('details', { class: 'shared-material' },
      el('summary', {}, t('question.readOnly_3', { v1: memberLabel(member) })),
      el('div', { class: 'question-text question-blocks', style: 'margin-top:8px' },
        questionBlocks(member.blocks, q.subject, keyword, assetContext))));
  }

  // ---- 题干与选项（平台精选题渲染广场公开配图；题库/个人题沿用题库配图门禁）
  if (isOfficial) {
    container.append(...officialStemNode(q, officialFigures(q)));
  } else {
    container.append(questionStemNode(q, keyword, assetContext));
  }

  const options = q.options || [];
  const optionInputs = [];
  const multi = q.type === 'multi_choice' || String(q.type || '').includes('多选') || String(q.type || '').includes('不定项');
  const latestChoiceSet = new Set(String(latest?.answer_text || '').toUpperCase().replace(/[^A-H]/g, '').split(''));
  const refAnsStr = q.unlocked ? String(q.answer?.text || q.answer_text || '').trim().toUpperCase() : '';
  const refChoiceSet = /^[A-H]+$/.test(refAnsStr) ? new Set(refAnsStr.split('')) : new Set();
  const blocked = q.practice_status === 'quarantined' || q.practice_allowed === false;

  const form = el('form', { id: 'answer-form', class: 'answer-form' });
  const choicesMount = el('div', { class: 'choice-list' });
  if (options.length) {
    for (const item of options) {
      const lbl = String(item.label || '').toUpperCase();
      const wasSelected = latestChoiceSet.has(lbl);
      const isRightOption = q.unlocked && refChoiceSet.has(lbl);
      const isWrongPick = q.unlocked && wasSelected && refChoiceSet.size > 0 && !isRightOption;
      let result = null;
      if (isRightOption) result = 'correct';
      else if (isWrongPick) result = 'wrong';
      const optionInput = el('input', {
        type: multi ? 'checkbox' : 'radio',
        name: 'choice', value: item.label, checked: wasSelected,
        disabled: blocked,
        'aria-label': t('question.option', { v1: item.label }),
      });
      optionInputs.push(optionInput);
      choicesMount.append(el('label', { class: 'choice', 'data-result': result },
        optionInput,
        el('span', { class: 'choice-letter', 'aria-hidden': 'true' }, item.label),
        optionContentNode(q, item, keyword, assetContext),
        wasSelected ? el('span', { class: 'choice-result' }, isWrongPick ? t('question.yourChoice') : t('question.yourChoice_2')) : null,
        !wasSelected && isRightOption ? el('span', { class: 'choice-result' }, t('question.correctAnswer')) : null));
    }
    form.append(el('fieldset', { style: 'border:0;margin:0;padding:0' }, choicesMount));
    if (multi) form.append(el('p', { class: 'help' }, t('question.multipleChoiceCheckAllOptionsThatFit')));
  }
  if (blocked) {
    form.append(el('div', { class: 'reading-note', 'data-state': 'error' },
      t('question.thisQuestionIsUnderReviewAndCannot')));
  }

  // 作答图片（主观题必需）
  const chosen = el('div', { class: 'file-chosen hide' });
  const preview = el('div');
  let selectedFile = null;
  if (!blocked) {
    const upload = filePicker(
      options.length ? t('question.youCanAlsoUploadAPhotoOf') : t('question.uploadAPhotoOfYourCompletedAnswer'),
      t('question.supportsPngJpegWebpBlankImagesAre'),
      'image/png,image/jpeg,image/webp',
      file => {
        selectedFile = file;
        chosen.textContent = file.name;
        chosen.classList.remove('hide');
        preview.replaceChildren();
        revokePreviewUrl();
        activeObjectUrl = URL.createObjectURL(file);
        preview.append(el('img', { class: 'uploaded-preview', src: activeObjectUrl, alt: t('question.previewOfThisAnswerPhoto') }));
      },
      { capture: 'environment' },
    );
    form.append(upload, chosen, preview);
  }

  // 主观题作答（个人题）：允许直接输入文字，与上传照片二选一或并用。
  const typedAnswer = isApp && !options.length
    ? el('textarea', {
        rows: 4,
        placeholder: t('question.writeYourAnswer'),
        'aria-label': t('question.writeYourAnswer'),
      })
    : null;
  if (typedAnswer) {
    form.append(field(t('question.writeYourAnswer'), typedAnswer));
  }

  const submitButton = el('button', {
    type: 'submit', form: 'answer-form', class: 'button',
    disabled: blocked,
  }, icon('check'), latest ? t('question.submitThisAnswer') : t('question.submitAnswerToUnlock'));
  form.addEventListener('submit', event => {
    event.preventDefault();
    busy(submitButton, async () => {
      const selected = optionInputs.filter(i => i.checked).map(i => i.value);
      const typed = typedAnswer?.value.trim() || '';
      if (!selected.length && !selectedFile && !typed) {
        throw new Error(options.length ? t('question.chooseAnAnswerOrUploadAPhoto') : t('question.pleaseUploadAPhotoOfYourOwn'));
      }
      const data = new FormData();
      data.set('qid', q.qid);
      data.set('submission_id', newSubmissionId());
      if (selected.length) data.set('answer', JSON.stringify(selected));
      else if (typed) data.set('answer', typed);
      if (selectedFile) data.set('file', selectedFile);
      const response = await api('/attempts', { method: 'POST', body: data });
      revokePreviewUrl();
      toast(response.result === 'correct'
        ? t('question.correctYouCanNowRecordYourMastery')
        : response.result === 'incorrect'
          ? t('question.submittedTheWrongAnswerHasBeenAdded')
          : t('question.submittedYouCanNowViewTheReference'));
      if (onSubmitted) onSubmitted(response);
      await openQuestion(q.qid, container, { onSubmitted });
    });
  });
  container.append(form);

  // ---- 底部操作条：阅读区独立于滚动内容，手机上始终可见
  // P0：has_answer=false 不渲染「查看答案」、has_solution=false 不渲染「查看解析」；
  // 两者都没有时操作组只保留提交按钮，不留空按钮或占位。
  const revealButtons = [];
  if (q.has_answer) {
    revealButtons.push(button(t('question.viewAnswer'), event => busy(event.currentTarget, async () => {
      // event.currentTarget 在 await 之后会变成 null：先取到按钮再改状态。
      const trigger = event.currentTarget;
      const opened = await revealPanel('answer', container, q, trigger);
      trigger.setAttribute('aria-expanded', String(opened));
      trigger.textContent = opened ? t('question.hideAnswer') : t('question.viewAnswer');
      trigger.prepend(icon('eye'));
    }), 'secondary', 'eye', { disabled: !q.unlocked, 'aria-expanded': 'false' }));
  }
  if (q.has_solution) {
    revealButtons.push(button(t('question.viewSolution'), event => busy(event.currentTarget, async () => {
      const trigger = event.currentTarget;
      const opened = await revealPanel('solution', container, q, trigger);
      trigger.setAttribute('aria-expanded', String(opened));
      trigger.textContent = opened ? t('question.hideSolution') : t('question.viewSolution');
      trigger.prepend(icon('notes'));
    }), 'secondary', 'notes', { disabled: !q.unlocked, 'aria-expanded': 'false' }));
  }
  const actionGroupChildren = [...revealButtons, submitButton];
  const actionBar = el('div', { class: 'reading-actions-inner' },
    el('p', { class: 'reading-actions-meta' },
      blocked ? t('question.thisQuestionIsPaused')
        : latest ? t('question.latestSubmission', { v1: dateText(latest.created_at), v2: resultLabel(latest.result) })
          : t('question.answerFirstThenCompareWithTheAnswer')),
    actionGroupChildren.length
      ? el('div', { class: 'reading-actions-group' }, ...actionGroupChildren)
      : null);
  if (actionsHost) {
    actionsHost.replaceChildren(actionBar);
    actionsHost.hidden = false;
  } else {
    container.append(el('div', { class: 'reading-actions', style: 'position:sticky;bottom:0' }, actionBar));
  }

  // ---- 答案与解析（独立展开，互不覆盖）
  const revealMount = el('section', { class: 'answer-section' });
  container.append(revealMount);

  // ---- 错因标签
  if (latest?.result === 'incorrect' || q.wrong_info?.in_wrongbook) {
    const presets = state.catalog?.error_tag_presets || ['概念不清', '审题失误', '计算错误', '方法不会', '记忆遗漏', '粗心'];
    const currentTags = new Set(q.wrong_info?.error_tags || []);
    const tagWrap = el('div', { class: 'error-tags-panel' },
      el('div', { class: 'row between', style: 'margin-bottom:8px' },
        el('strong', { class: 'small-text' }, icon('tag'), t('question.errorCauseCategoriesUsedForWrongAnswer_2')),
        q.wrong_info?.failures ? badge(t('question.wrongTimes', { v1: q.wrong_info.failures }), 'red') : null));
    const chips = el('div', { class: 'row' });
    const saveTags = async () => {
      await api('/wrongbook/tags', { method: 'POST', data: { qid: q.qid, error_tags: [...currentTags] } });
      toast(t('question.errorCauseTagsSaved'));
    };
    for (const preset of presets) {
      const active = currentTags.has(preset);
      const btn = el('button', {
        type: 'button', class: `tag-pill${active ? ' active' : ''}`, 'aria-pressed': String(active),
        onClick: async () => {
          if (currentTags.has(preset)) currentTags.delete(preset);
          else currentTags.add(preset);
          btn.classList.toggle('active', currentTags.has(preset));
          btn.setAttribute('aria-pressed', String(currentTags.has(preset)));
          try { await saveTags(); } catch (e) { toast(e.message, true); }
        },
      }, preset);
      chips.append(btn);
    }
    const customTagInput = el('input', { class: 'input small-input', placeholder: t('question.customErrorCausePressEnterToAdd'), style: 'width:160px;min-height:34px;padding:4px 10px' });
    customTagInput.addEventListener('keydown', async e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const v = customTagInput.value.trim();
      if (!v) return;
      currentTags.add(v);
      customTagInput.value = '';
      try { await saveTags(); await openQuestion(q.qid, container, { onSubmitted }); } catch (err) { toast(err.message, true); }
    });
    chips.append(customTagInput);
    tagWrap.append(chips);
    container.append(tagWrap);
  }

  // ---- 历次作答
  const history = q.attempts_history || (latest ? [latest] : []);
  if (history.length) {
    const histDet = el('details', { class: 'sources', style: 'margin:16px 0' },
      el('summary', {}, t('question.attemptsTimesLatest', { v1: history.length, v2: dateText(history[0].created_at), v3: resultLabel(history[0].result) })));
    for (const att of history) {
      const item = el('div', { class: 'source-block' },
        el('div', { class: 'row between' }, el('strong', {}, dateText(att.created_at)), resultBadge(att.result)));
      if (att.answer_text) item.append(el('div', { class: 'small-text', style: 'margin-top:6px' }, t('question.submittedAnswer', { v1: att.answer_text })));
      if (att.image_url) {
        item.append(el('img', {
          src: att.image_url, class: 'uploaded-preview', style: 'max-height:160px;margin:8px 0',
          alt: t('question.answerImage'), loading: 'lazy', onClick: () => showImage(att.image_url, t('question.historicalAnswerImage')),
        }));
      }
      if (att.grade_source === 'ai_cross_verified') {
        item.append(el('p', { class: 'help' }, t('question.theResultComesFromIndependentAiCross')));
      }
      histDet.append(item);
    }
    container.append(histDet);
  }

  // ---- 掌握度
  const masteryNode = el('div', {},
    el('div', { class: 'row between' },
      el('strong', { class: 'small-text' }, t('question.haveYouMasteredThisQuestion')),
      button(q.wrong_info?.in_wrongbook ? t('question.inWrongBook') : t('question.addToWrongBook'), event => busy(event.currentTarget, async () => {
        await api('/wrongbook', { method: 'POST', data: { qid: q.qid } });
        toast(t('question.addedToWrongBook'));
        await openQuestion(q.qid, container, { onSubmitted });
      }), 'ghost small', 'plus')));
  const masteryGroup = el('div', { class: 'mastery-group', role: 'group', 'aria-label': t('question.masteryLevel') });
  const levelHints = {
    '未掌握': t('question.alwaysAvailableReviewInterval1Day'),
    '初步掌握': t('question.availableAfterAnsweringReviewInterval2Days'),
    '比较掌握': t('question.availableAfterTheLatestCorrectAnswerInterval'),
    '熟练掌握': t('question.availableAfterTheLatestCorrectAnswerInterval_2'),
  };
  for (const level of ['未掌握', '初步掌握', '比较掌握', '熟练掌握']) {
    const isDisabled = (level === '初步掌握' && !latest) || (['比较掌握', '熟练掌握'].includes(level) && !correct);
    const isSelected = q.mastery === level;
    masteryGroup.append(el('button', {
      type: 'button', class: `mastery-option${isSelected ? ' selected' : ''}`,
      'aria-pressed': String(isSelected), title: levelHints[level], disabled: isDisabled,
      onClick: event => busy(event.currentTarget, async () => {
        await api('/mastery', { method: 'POST', data: { qid: q.qid, level } });
        toast(t('question.masteryUpdatedTo', { v1: masteryText(level) }));
        await openQuestion(q.qid, container, { onSubmitted });
      }),
    }, masteryText(level)));
  }
  masteryNode.append(masteryGroup);
  container.append(masteryNode);

  // ---- AI 学习助手
  const aiSection = el('section', { class: 'ai-panel' },
    el('h3', {}, icon('sparkle'), t('question.aiStudyAssistant6ExplanationModesCross')));
  const aiControls = createModelPicker('explain', { showModeToggle: true, showJudge: true });
  aiSection.append(aiControls.node);
  const custom = el('textarea', { placeholder: t('question.optionalAddYourExplanationRequirementsEG'), rows: 2 });
  const customFormula = formulaEditorButton(custom, { variant: 'ghost small' });
  aiSection.append(el('div', { style: 'margin-top:12px' },
    field(t('question.additionalRequirements'),
      el('div', { class: 'formula-field-with-action' }, custom, customFormula))));
  const jobMount = el('div');
  const modes = state.explainModes || [
    { id: 'detailed', name: t('upload.mode.detailed.name'), short: t('upload.mode.detailed.short') },
    { id: 'standard_steps', name: t('upload.mode.standard_steps.name'), short: t('upload.mode.standard_steps.short') },
    { id: 'pattern_teaching', name: t('upload.mode.pattern_teaching.name'), short: t('upload.mode.pattern_teaching.short') },
    { id: 'extension', name: t('upload.mode.extension.name'), short: t('upload.mode.extension.short') },
    { id: 'hints', name: t('upload.mode.hints.name'), short: t('question.layeredHintsWithoutTheAnswer') },
    { id: 'variants', name: t('upload.mode.variants.name'), short: t('question.2VariationQuestionsOnTheSameKnowledge') },
  ];
  async function launchExplain(modeId, trigger) {
    await busy(trigger, async () => {
      if (!q.unlocked) throw new Error(t('question.submitYourOwnAnswerBeforeUsingAi'));
      const payload = aiControls.payload(false);
      const job = await api('/ai/jobs', {
        method: 'POST',
        data: { ...payload, task: 'explain', qid: q.qid, explain_mode: modeId, requirements: custom.value.trim() },
      });
      state.questionJobId = job.id;
      mountJob(jobMount, job.id, () => {
        // 任务已到终态：清空 job 指针，之后重新打开题目不会再挂载已完成的旧任务。
        state.questionJobId = null;
        openQuestion(q.qid, container, { onSubmitted });
      }, { titleHint: `${q.subject} · ${q.number_label || q.qid}` });
    });
  }
  async function launchGrade(trigger) {
    await busy(trigger, async () => {
      if (!q.unlocked || !latest?.id) throw new Error(t('question.submitYourAnswerFirst'));
      const payload = aiControls.payload(true);
      const job = await api('/ai/jobs', {
        method: 'POST',
        data: { ...payload, task: 'grade', qid: q.qid, attempt_id: latest.id, requirements: custom.value.trim() },
      });
      state.questionJobId = job.id;
      mountJob(jobMount, job.id, () => {
        state.questionJobId = null;
        openQuestion(q.qid, container, { onSubmitted });
      });
    });
  }
  const modeActions = el('div', { class: 'ai-actions' });
  for (const m of modes) {
    modeActions.append(button(explainModeName(m), e => launchExplain(m.id, e.currentTarget), 'secondary small', 'sparkle', {
      disabled: !q.unlocked, title: m.short,
    }));
  }
  modeActions.append(button(t('question.crossVerifyMyAnswer'), e => launchGrade(e.currentTarget), 'soft small', 'shield', {
    disabled: !q.unlocked || !latest?.id,
  }));
  aiSection.append(modeActions,
    el('p', { class: 'help', style: 'margin-top:10px' },
      t('question.generatedExplanationsAreSavedPerQuestionSo')),
    jobMount);

  function renderSavedExplanations(items, community) {
    const savedSection = el('div', { class: 'saved-explanations' },
      el('div', { class: 'row between', style: 'margin:16px 0 10px' },
        el('strong', { class: 'small-text' }, icon('history'),
          t('question.savedExplainHeader', { v1: community ? t('question.communityExplain') : t('question.savedAiExplain'), v2: items.length })),
        el('span', { class: 'help' }, t('question.freeToReuseNoExtraCharge'))));
    items.forEach((exp, idx) => {
      const modeName = explainModeName(modes.find(m => m.id === exp.mode)) || exp.mode;
      const det = el('details', { class: 'saved-exp-item', open: !community && idx === 0 && !state.questionJobId },
        el('summary', {},
          dataBadge(modeName, 'teal'),
          exp.featured ? badge(t('question.platformFeatured'), 'blue') : exp.shared && !community ? badge(t('question.shared_2'), 'teal') : null,
          el('span', {}, ` ${dateText(exp.created_at)}`),
          exp.models?.length ? el('small', { class: 'muted' }, ` · ${exp.models.map(m => m.model).join(' + ')}`) : null),
        renderAiResult(exp, { task: 'explain', qid: q.qid, titleHint: `${q.subject} · ${modeName}` }));
      if (!community && exp.can_share) {
        const share = el('input', { type: 'checkbox', checked: exp.shared });
        share.addEventListener('change', e => busy(e.currentTarget, async () => {
          const shared = share.checked;
          share.checked = exp.shared;
          await api(`/explanations/${exp.id}/share`, { method: 'PUT', data: { shared } });
          toast(shared ? t('question.addedToTheCommunityExplanationPool') : t('question.removedFromTheCommunityExplanationPool'));
          if (state.selectedQid === q.qid) await openQuestion(q.qid, container, { onSubmitted });
        }));
        det.append(el('label', { class: 'check-label', style: 'margin-top:12px' },
          share, t('question.shareThisExplanationWithUsersWhoHave')));
      }
      savedSection.append(det);
    });
    aiSection.append(savedSection);
  }

  if (savedExplanations.length) renderSavedExplanations(savedExplanations, false);
  if (communityItems.length) renderSavedExplanations(communityItems, true);
  if (savedExplanationsError) {
    aiSection.append(el('div', { class: 'notice warning' },
      t('question.failedToLoadSavedExplanations', { v1: savedExplanationsError }),
      button(t('common.reload'), () => openQuestion(q.qid, container, { onSubmitted }), 'secondary small', 'repeat')));
  }
  // 个人题与平台精选题暂不接题库专属的 AI 讲解/双模型核验（任务链路按只读题库 qid 取题）。
  if (!isApp && !isOfficial) container.append(aiSection);
  if (state.questionJobId) {
    mountJob(jobMount, state.questionJobId, () => {
      state.questionJobId = null;
      openQuestion(q.qid, container, { onSubmitted });
    }, { titleHint: `${q.subject} · ${q.number_label || q.qid}` });
  }

  // ---- 管理员标注（只针对只读题库题；个人题走工坊自己的核验流程，平台精选题只读）
  if (state.user.role === 'admin' && !isApp && !isOfficial) {
    const difficulty = select(
      ['待标注', '简单', '中等', '困难'].map(value => ({ value, label: difficultyText(value) })),
      q.difficulty_source === '管理员标注' ? q.difficulty : '待标注',
    );
    const subjectKps = (state.knowledgePoints || []).filter(k => k.subject === q.subject && k.level >= 2);
    const kpSelect = select(
      [{ value: '', label: t('question.selectAKnowledgePointToAdd') }, ...subjectKps.map(k => ({ value: k.id, label: `${k.level === 2 ? '▸ ' : '　· '}${k.name}` }))],
      '',
    );
    const curKpIds = new Set((q.knowledge_points || []).map(k => k.kp_id));
    const methodInput = el('input', { class: 'input', placeholder: t('question.eGDerivativesDiscussionWithParameters') });
    container.append(el('div', { class: 'form-section admin-only' },
      el('div', { class: 'row between' },
        field(t('question.adminDifficultyLabel'), difficulty),
        button(t('question.saveDifficulty'), event => busy(event.currentTarget, async () => {
          await api('/question-metadata', { method: 'POST', data: { qid: q.qid, difficulty: difficulty.value } });
          toast(t('question.difficultyLabelSaved'));
          await openQuestion(q.qid, container, { onSubmitted });
        }), 'secondary small')),
      subjectKps.length ? el('div', { class: 'row between', style: 'margin-top:12px' },
        field(t('question.linkedKnowledgePoint'), kpSelect),
        button(t('question.addKnowledgePoint'), event => busy(event.currentTarget, async () => {
          if (!kpSelect.value) return;
          curKpIds.add(Number(kpSelect.value));
          await api('/admin/question-kp', { method: 'POST', data: { qid: q.qid, kp_ids: [...curKpIds] } });
          toast(t('question.knowledgePointLabelsUpdated'));
          await openQuestion(q.qid, container, { onSubmitted });
        }), 'secondary small', 'plus')) : null,
      el('div', { class: 'row between', style: 'margin-top:12px' },
        field(t('question.questionTypeMethodTags'), methodInput),
        button(t('question.addTag'), event => busy(event.currentTarget, async () => {
          if (!methodInput.value.trim()) return;
          await api('/admin/question-method', { method: 'POST', data: { qid: q.qid, method_name: methodInput.value.trim() } });
          toast(t('question.questionTypeMethodTagAdded'));
          await openQuestion(q.qid, container, { onSubmitted });
        }), 'secondary small', 'plus'))));
  }
}

function difficultyBadge(q) {
  const diff = q.difficulty || '待标注';
  const src = q.difficulty_source ? ` · ${q.difficulty_source}` : '';
  return dataBadge(`${difficultyText(diff)}${src}`, diff !== '待标注' ? 'blue' : '');
}

let revealedCache = null;
// 登出 / 401 过期 / 题库热切换时清空已展开的答案与解析缓存（含授权数据）。
onSessionReset(() => { revealedCache = null; revokePreviewUrl(); });
onBankChange(() => { revealedCache = null; });
/**
 * 展开/收起答案或解析面板；返回本次操作后面板是否展开。
 * `trigger` 只用于把焦点移回触发按钮，不在这里读写事件对象。
 */
async function revealPanel(kind, container, q, trigger = null) {
  const box = container.querySelector('.answer-section');
  if (!box) return false;
  const isAnswer = kind === 'answer';
  const existing = box.querySelector(`[data-panel="${kind}"]`);
  if (existing) {
    existing.remove();
    trigger?.focus();
    return false;
  }
  revealedCache = revealedCache && revealedCache.qid === q.qid
    ? revealedCache : { qid: q.qid, data: null };
  if (!revealedCache.data) {
    try {
      revealedCache.data = await api(`/reveal?${query({ qid: q.qid })}`);
    } catch (error) {
      revealedCache = null;
      // 平台精选题：答案门控由 official 详情接口负责，/reveal 未接 official 时
      // 在答案区给出一次降级提示，不把整页打成错误态、也不重复堆提示。
      if (isOfficialQuestion(q)) {
        if (box && !box.querySelector('.official-reveal-notice')) {
          box.append(el('div', { class: 'notice warning official-reveal-notice' },
            t('question.source.revealUnavailable', { v1: error.message })));
        }
        return false;
      }
      throw error;
    }
  }
  const data = revealedCache.data.question || revealedCache.data;
  const blocks = blockList(isAnswer ? data.answer_blocks : data.solution_blocks);
  const text = isAnswer
    ? (data.answer?.text || data.answer_text || (typeof data.answer === 'string' ? data.answer : ''))
    : (data.solution || data.answer?.solution || '');
  const assetContext = { qid: q.qid, bankVersion: q.bank_version || '' };
  const closeButton = button(t('question.collapse'), () => {
    panel.remove();
    trigger?.setAttribute('aria-expanded', 'false');
    if (trigger) {
      trigger.textContent = isAnswer ? t('question.viewAnswer') : t('question.viewSolution');
      trigger.prepend(icon(isAnswer ? 'eye' : 'notes'));
      trigger.focus();
    }
  }, 'ghost small');
  const panel = el('div', { class: 'answer-box' + (isAnswer ? '' : ' solution-box'), 'data-panel': kind },
    el('h3', {}, isAnswer ? t('question.originalReferenceAnswer') : t('question.originalSolution')),
    blocks.length
      ? el('div', { class: 'question-text question-blocks' }, questionBlocks(blocks, q.subject, '', assetContext))
      : questionText(text || (isAnswer ? t('question.theOriginalPaperProvidesNoDisplayableAnswer') : t('question.theOriginalPaperHasNoSolution')), q.images || [], q.subject, '', assetContext),
    closeButton);
  box.append(panel);
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  return true;
}
