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

function flagText(flag) {
  if (typeof flag === 'string') return flag;
  return flag?.message || flag?.code || '待复核';
}

function qualityNotice(q) {
  const flags = (q.quality_flags || []).map(flagText).filter(Boolean);
  const nodes = [];
  if (flags.length) {
    nodes.push(notice(`原题内容提醒：${flags.join('；')}。请结合原卷核对。`, 'warning'));
  }
  const needsReview = q.practice_status === 'review' || q.grading_status === 'pending';
  if (needsReview && !flags.some(text => /核验|待复核|判分/.test(text))) {
    nodes.push(notice('参考答案待核验：本题可正常作答，作答记录会先保存，暂不自动判分。请以原卷为准。', 'warning'));
  }
  return nodes.length ? el('div', { class: 'quality-notices' }, nodes) : null;
}

const GROUP_KIND_LABELS = {
  material: '阅读材料',
  parent: '共用题干',
  material_parent: '阅读材料与共用题干',
};

function groupLabel(groupKind) {
  return GROUP_KIND_LABELS[groupKind] || '共用材料 / 题干';
}

// 统一题号显示：父题给原卷题号，小问给问序，父题本身标为「共用题干」。
function memberLabel(member) {
  if (member.group_role === 'parent') return '共用题干';
  const parts = [];
  const number = String(member.number_label || '').trim();
  if (number) {
    const looksSub = /^[（(]/.test(number) || /^第/.test(number)
      || /^[(（]?[一二三四五六七八九十\d]+[)）]$/.test(number);
    parts.push(looksSub ? (number.startsWith('第') ? number : `第${number}问`) : `原卷第${number}题`);
  }
  const sub = String(member.sub_question_label || '').trim();
  const subNorm = sub.replace(/^[（(第]+|[）)]+$/g, '').replace(/问$/, '');
  const numberNorm = number.replace(/^[（(]+|[）)]+$/g, '');
  // number_label 已经逐问编号时（如原卷 1、2 两问各自成题），不再重复「第（1）问」。
  if (sub && subNorm && subNorm !== numberNorm) {
    parts.push(sub.startsWith('第') ? sub : `第${sub}问`);
  }
  if (!parts.length && member.source_question_id) parts.push(`原题 ${member.source_question_id}`);
  return parts.join(' · ') || '题组成员';
}

function memberStatusText(member, isCurrent) {
  if (isCurrent) return '本题';
  if (member.group_role === 'parent') return '共用';
  if (member.in_bank === false || member.practice_status === 'context' || !member.qid) return '仅供阅读';
  if (member.practice_status === 'quarantined') return '已隔离';
  if (member.available === false) return '暂不可答';
  if (member.practice_status === 'review') return '待核验';
  return null;
}

function readingPathParts(q) {
  const parts = [];
  const section = q.group?.section_title || q.paper_name || sourceOf(q);
  if (section) parts.push(section);
  const parentNumber = q.group?.parent_number_label;
  if (parentNumber) parts.push(`原卷第${parentNumber}题`);
  else if (q.number_label) parts.push(`原卷第${q.number_label}题`);
  if (q.group?.group_kind) parts.push(groupLabel(q.group.group_kind));
  return parts.filter(Boolean);
}

function statusChip(q, latest) {
  if (latest) {
    const stateName = latest.result === 'correct' ? 'done'
      : latest.result === 'incorrect' ? 'error' : 'warn';
    return el('span', { class: 'status-chip', 'data-state': stateName }, resultLabel(latest.result));
  }
  if (q.practice_status === 'quarantined') return el('span', { class: 'status-chip', 'data-state': 'error' }, '已隔离');
  if (q.practice_status === 'context') return el('span', { class: 'status-chip', 'data-state': 'warn' }, '仅供阅读');
  return el('span', { class: 'status-chip' }, '未作答');
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
    { value: 'stem', label: '题干' }, { value: 'options', label: '选项' },
    { value: 'formula', label: '公式' }, { value: 'image', label: '图片' },
    { value: 'answer', label: '参考答案' }, { value: 'solution', label: '解析' },
    { value: 'other', label: '其他' },
  ], 'stem');
  const typeSel = select([
    { value: 'formula', label: '公式显示或内容有误' }, { value: 'stem_missing', label: '题干或条件缺失' },
    { value: 'option', label: '选项边界或内容有误' }, { value: 'image', label: '图片缺失或错位' },
    { value: 'answer', label: '参考答案有误' }, { value: 'other', label: '其他问题' },
  ], 'other');
  const noteArea = el('textarea', { rows: 3, placeholder: '可补充你在原卷上看到的情况（例如：第（2）问的公式原卷是分式）。' });
  const shotInput = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp' });
  const resultBox = el('div', { class: 'issue-result' });
  const submit = button('提交报错', event => busy(event.currentTarget, async () => {
    const data = new FormData();
    data.set('field', fieldSel.value);
    data.set('issue_type', typeSel.value);
    data.set('note', noteArea.value.trim());
    const file = shotInput.files?.[0];
    if (file) data.set('screenshot', file);
    const response = await api(`/questions/${encodeURIComponent(q.qid)}/issues`, { method: 'POST', body: data });
    resultBox.replaceChildren(notice(`已提交，编号 #${response.id}（${response.status_label}）。管理员会按原卷核对。`));
    toast('题目报错已提交。');
    submit.disabled = true;
  }), '', 'check');
  const dialog = el('dialog', { class: 'issue-dialog', 'aria-label': '报告题目问题' },
    el('div', { class: 'row between' },
      el('h3', {}, icon('edit'), ' 报告题目问题'),
      el('button', { type: 'button', class: 'close-dialog', 'aria-label': '关闭', onClick: () => dialog.close() }, icon('close'))),
    el('div', { class: 'issue-meta' },
      `题库版本：${q.bank_version || '基线库'} · 题号：${q.number_label || q.qid}`
      + (q.content_revision ? ` · 内容版本：${q.content_revision}` : '')),
    el('div', { class: 'form-grid' }, field('问题所在区域', fieldSel), field('问题类型', typeSel)),
    field('补充说明', noteArea),
    field('可选截图', shotInput, '支持 PNG、JPEG、WebP，截图仅你和管理员可见。'),
    el('div', { class: 'row between', style: 'margin-top:12px' },
      el('p', { class: 'help' }, '报错不会影响你的历史作答记录。'), submit),
    resultBox);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  dialog.showModal();
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
  container.replaceChildren(loading('正在打开完整题目…'));
  if (scrollOnMobile && window.innerWidth <= 900) {
    container.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  try {
    const q = await api(`/questions/${encodeURIComponent(qid)}`);
    if (!container.isConnected || state.selectedQid !== qid) return;
    let savedExps = { items: [], community_items: [], community_enabled: false };
    let explanationsError = '';
    try {
      savedExps = await api(`/explanations?${query({ qid })}`);
    } catch (error) {
      explanationsError = error.message;
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
      button('重试打开题目', () => openQuestion(qid, container, { scrollOnMobile, onSubmitted }), 'secondary small', 'repeat')));
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

  const metaRow = el('div', { class: 'row', style: 'margin:0 0 8px' },
    badge(q.subject, 'teal'), badge(typeLabel(q, state.catalog)), difficultyBadge(q),
    button('报告问题', () => openIssueDialog(q), 'ghost small', 'edit'));
  container.append(metaRow);
  const quality = qualityNotice(q);
  if (quality) container.append(quality);

  // ---- 知识点与解法标签
  const kpRow = el('div', { class: 'row', style: 'margin:0 0 10px' });
  for (const kp of q.knowledge_points || []) {
    const tagLabel = kp.source === 'ai' || kp.source === 'ai_pending_review'
      ? `${kp.name} · AI标注`
      : kp.source === 'ai_reviewed' ? `${kp.name} · 已审核` : kp.name;
    kpRow.append(el('button', {
      type: 'button', class: 'kp-chip', title: '查看该知识点官方清单与同类题',
      onClick: () => openKpDetailDialog(kp.kp_id, kp.name),
    }, `# ${tagLabel}`));
  }
  for (const m of q.methods || []) {
    kpRow.append(el('button', {
      type: 'button', class: 'method-chip', title: '按该题型解法筛选同类题',
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
    const tabs = el('nav', { class: 'reading-tabs', 'aria-label': '同题组小问' });
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
        title: actionable ? '打开该小问' : (statusText || '仅供阅读'),
        onClick: () => actionable && openQuestion(member.qid, container, {
          onSubmitted, scrollOnMobile: window.innerWidth <= 900,
        }),
      }, memberLabel(member), statusText ? el('small', {}, statusText) : null));
    }
    container.append(tabs);
    if (available.length !== navigable.length) {
      container.append(el('p', { class: 'help', style: 'margin:-10px 0 12px' },
        `本组共 ${navigable.length} 问，可作答 ${available.length} 问`
        + (readonly.length ? `，仅供阅读 ${readonly.length} 问` : '')
        + (navigable.length - available.length - readonly.length > 0
          ? `，待复核 ${navigable.length - available.length - readonly.length} 问` : '') + '。'));
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
          toggle.textContent = expanded ? '展开全部材料' : '收起材料';
        },
      }, '展开全部材料');
      body.style.maxHeight = collapsedHeight;
      body.style.overflow = 'hidden';
      material.append(toggle);
    }
    container.append(material);
  } else if (q.group && (q.group.material_text || q.group.parent_stem)) {
    const material = el('section', { class: 'shared-material' },
      el('h3', {}, '阅读材料'),
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
      el('summary', {}, `${memberLabel(member)} · 仅供阅读`),
      el('div', { class: 'question-text question-blocks', style: 'margin-top:8px' },
        questionBlocks(member.blocks, q.subject, keyword, assetContext))));
  }

  // ---- 题干与选项
  container.append(questionStemNode(q, keyword, assetContext));

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
        'aria-label': `选项 ${item.label}`,
      });
      optionInputs.push(optionInput);
      choicesMount.append(el('label', { class: 'choice', 'data-result': result },
        optionInput,
        el('span', { class: 'choice-letter', 'aria-hidden': 'true' }, item.label),
        optionContentNode(q, item, keyword, assetContext),
        wasSelected ? el('span', { class: 'choice-result' }, isWrongPick ? '✗ 你的选择' : '✓ 你的选择') : null,
        !wasSelected && isRightOption ? el('span', { class: 'choice-result' }, '✓ 正确答案') : null));
    }
    form.append(el('fieldset', { style: 'border:0;margin:0;padding:0' }, choicesMount));
    if (multi) form.append(el('p', { class: 'help' }, '多选题，请勾选所有符合题意的选项。'));
  }
  if (blocked) {
    form.append(el('div', { class: 'reading-note', 'data-state': 'error' },
      '本题已进入复核，暂不可作答；你已有的作答记录仍可查看。'));
  }

  // 作答图片（主观题必需）
  const chosen = el('div', { class: 'file-chosen hide' });
  const preview = el('div');
  let selectedFile = null;
  if (!blocked) {
    const upload = filePicker(
      options.length ? '也可以上传你的作答图片' : '上传你完成的作答图片',
      '支持 PNG、JPEG、WebP；空白图片会被拦截，提交后可查看答案与解析。',
      'image/png,image/jpeg,image/webp',
      file => {
        selectedFile = file;
        chosen.textContent = file.name;
        chosen.classList.remove('hide');
        preview.replaceChildren();
        revokePreviewUrl();
        activeObjectUrl = URL.createObjectURL(file);
        preview.append(el('img', { class: 'uploaded-preview', src: activeObjectUrl, alt: '本次作答图片预览' }));
      },
      { capture: 'environment' },
    );
    form.append(upload, chosen, preview);
  }

  const submitButton = el('button', {
    type: 'submit', form: 'answer-form', class: 'button',
    disabled: blocked,
  }, icon('check'), latest ? '提交这次作答' : '提交作答，解锁答案');
  form.addEventListener('submit', event => {
    event.preventDefault();
    busy(submitButton, async () => {
      const selected = optionInputs.filter(i => i.checked).map(i => i.value);
      if (!selected.length && !selectedFile) {
        throw new Error(options.length ? '请选择答案或上传作答图片。' : '请先上传自己的作答图片。');
      }
      const data = new FormData();
      data.set('qid', q.qid);
      if (selected.length) data.set('answer', JSON.stringify(selected));
      if (selectedFile) data.set('file', selectedFile);
      const response = await api('/attempts', { method: 'POST', body: data });
      revokePreviewUrl();
      toast(response.result === 'correct'
        ? '回答正确，可以标注掌握程度。'
        : response.result === 'incorrect'
          ? '已提交。错题已加入复习计划。'
          : '已提交，可查看参考答案。');
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
    revealButtons.push(button('查看答案', event => busy(event.currentTarget, async () => {
      // event.currentTarget 在 await 之后会变成 null：先取到按钮再改状态。
      const trigger = event.currentTarget;
      const opened = await revealPanel('answer', container, q, trigger);
      trigger.setAttribute('aria-expanded', String(opened));
      trigger.textContent = opened ? '收起答案' : '查看答案';
      trigger.prepend(icon('eye'));
    }), 'secondary', 'eye', { disabled: !q.unlocked, 'aria-expanded': 'false' }));
  }
  if (q.has_solution) {
    revealButtons.push(button('查看解析', event => busy(event.currentTarget, async () => {
      const trigger = event.currentTarget;
      const opened = await revealPanel('solution', container, q, trigger);
      trigger.setAttribute('aria-expanded', String(opened));
      trigger.textContent = opened ? '收起解析' : '查看解析';
      trigger.prepend(icon('notes'));
    }), 'secondary', 'notes', { disabled: !q.unlocked, 'aria-expanded': 'false' }));
  }
  const actionGroupChildren = [...revealButtons, submitButton];
  const actionBar = el('div', { class: 'reading-actions-inner' },
    el('p', { class: 'reading-actions-meta' },
      blocked ? '本题已暂停作答'
        : latest ? `最近提交：${dateText(latest.created_at)} · ${resultLabel(latest.result)}`
          : '先作答，再对照答案与解析。'),
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
        el('strong', { class: 'small-text' }, icon('tag'), ' 错因分类（用于错题归纳与 AI 易错清单）'),
        q.wrong_info?.failures ? badge(`已错 ${q.wrong_info.failures} 次`, 'red') : null));
    const chips = el('div', { class: 'row' });
    const saveTags = async () => {
      await api('/wrongbook/tags', { method: 'POST', data: { qid: q.qid, error_tags: [...currentTags] } });
      toast('错因标签已保存。');
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
    const customTagInput = el('input', { class: 'input small-input', placeholder: '自定义错因，回车添加', style: 'width:160px;min-height:34px;padding:4px 10px' });
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
      el('summary', {}, `历次作答（${history.length} 次 · 最近 ${dateText(history[0].created_at)} · ${resultLabel(history[0].result)}）`));
    for (const att of history) {
      const item = el('div', { class: 'source-block' },
        el('div', { class: 'row between' }, el('strong', {}, dateText(att.created_at)), resultBadge(att.result)));
      if (att.answer_text) item.append(el('div', { class: 'small-text', style: 'margin-top:6px' }, `提交答案：${att.answer_text}`));
      if (att.image_url) {
        item.append(el('img', {
          src: att.image_url, class: 'uploaded-preview', style: 'max-height:160px;margin:8px 0',
          alt: '作答图片', loading: 'lazy', onClick: () => showImage(att.image_url, '历史作答图片'),
        }));
      }
      if (att.grade_source === 'ai_cross_verified') {
        item.append(el('p', { class: 'help' }, '结果来自 AI 独立交叉核验，非官方判定。'));
      }
      histDet.append(item);
    }
    container.append(histDet);
  }

  // ---- 掌握度
  const masteryNode = el('div', {},
    el('div', { class: 'row between' },
      el('strong', { class: 'small-text' }, '这道题，你掌握了吗？'),
      button(q.wrong_info?.in_wrongbook ? '已在错题本' : '加入错题本', event => busy(event.currentTarget, async () => {
        await api('/wrongbook', { method: 'POST', data: { qid: q.qid } });
        toast('已加入错题本。');
        await openQuestion(q.qid, container, { onSubmitted });
      }), 'ghost small', 'plus')));
  const masteryGroup = el('div', { class: 'mastery-group', role: 'group', 'aria-label': '掌握程度' });
  const levelHints = {
    '未掌握': '随时可选（复习间隔 1 天）',
    '初步掌握': '作答后可选（复习间隔 2 天）',
    '比较掌握': '最新判对可选（间隔 ×2，≥4 天）',
    '熟练掌握': '最新判对可选（间隔 ×2.5，≥7 天）',
  };
  for (const level of ['未掌握', '初步掌握', '比较掌握', '熟练掌握']) {
    const isDisabled = (level === '初步掌握' && !latest) || (['比较掌握', '熟练掌握'].includes(level) && !correct);
    const isSelected = q.mastery === level;
    masteryGroup.append(el('button', {
      type: 'button', class: `mastery-option${isSelected ? ' selected' : ''}`,
      'aria-pressed': String(isSelected), title: levelHints[level], disabled: isDisabled,
      onClick: event => busy(event.currentTarget, async () => {
        await api('/mastery', { method: 'POST', data: { qid: q.qid, level } });
        toast(`掌握程度已更新为「${level}」。`);
        await openQuestion(q.qid, container, { onSubmitted });
      }),
    }, level));
  }
  masteryNode.append(masteryGroup);
  container.append(masteryNode);

  // ---- AI 学习助手
  const aiSection = el('section', { class: 'ai-panel' },
    el('h3', {}, icon('sparkle'), 'AI 学习助手（6 种讲解模式 · 交叉验证）'));
  const aiControls = createModelPicker('explain', { showModeToggle: true, showJudge: true });
  aiSection.append(aiControls.node);
  const custom = el('textarea', { placeholder: '可选：补充你的讲解要求（例如：从高一基础视角讲起，不要跳步）', rows: 2 });
  aiSection.append(el('div', { style: 'margin-top:12px' }, field('补充要求', custom)));
  const jobMount = el('div');
  const modes = state.explainModes || [
    { id: 'detailed', name: '详细解析', short: '题意→思路→步骤→易错点' },
    { id: 'standard_steps', name: '标准步骤（大题）', short: '得分点与规范书写' },
    { id: 'pattern_teaching', name: '题型教学', short: '识别特征→通法框架' },
    { id: 'extension', name: '知识拓展', short: '拓展结论与跨章联系' },
    { id: 'hints', name: '启发引导', short: '分层提示不给答案' },
    { id: 'variants', name: '变式练习', short: '2道同考点变式题' },
  ];
  async function launchExplain(modeId, trigger) {
    await busy(trigger, async () => {
      if (!q.unlocked) throw new Error('请先提交自己的作答，再使用 AI 查看讲解。');
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
      if (!q.unlocked || !latest?.id) throw new Error('请先提交作答。');
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
    modeActions.append(button(m.name, e => launchExplain(m.id, e.currentTarget), 'secondary small', 'sparkle', {
      disabled: !q.unlocked, title: m.short,
    }));
  }
  modeActions.append(button('交叉核验我的作答', e => launchGrade(e.currentTarget), 'soft small', 'shield', {
    disabled: !q.unlocked || !latest?.id,
  }));
  aiSection.append(modeActions,
    el('p', { class: 'help', style: 'margin-top:10px' },
      '生成的讲解会按题保存，切题回来不重复计费；所有 AI 结果均标注来源，不代表官方答案。'),
    jobMount);

  function renderSavedExplanations(items, community) {
    const savedSection = el('div', { class: 'saved-explanations' },
      el('div', { class: 'row between', style: 'margin:16px 0 10px' },
        el('strong', { class: 'small-text' }, icon('history'),
          ` ${community ? '社区讲解' : '已保存的 AI 讲解'}（${items.length}）`),
        el('span', { class: 'help' }, '免费复用，不再扣费')));
    items.forEach((exp, idx) => {
      const modeName = modes.find(m => m.id === exp.mode)?.name || exp.mode;
      const det = el('details', { class: 'saved-exp-item', open: !community && idx === 0 && !state.questionJobId },
        el('summary', {},
          badge(modeName, 'teal'),
          exp.featured ? badge('平台精选', 'blue') : exp.shared && !community ? badge('已共享', 'teal') : null,
          el('span', {}, ` ${dateText(exp.created_at)}`),
          exp.models?.length ? el('small', { class: 'muted' }, ` · ${exp.models.map(m => m.model).join(' + ')}`) : null),
        renderAiResult(exp, { task: 'explain', qid: q.qid, titleHint: `${q.subject} · ${modeName}` }));
      if (!community && exp.can_share) {
        const share = el('input', { type: 'checkbox', checked: exp.shared });
        share.addEventListener('change', e => busy(e.currentTarget, async () => {
          const shared = share.checked;
          share.checked = exp.shared;
          await api(`/explanations/${exp.id}/share`, { method: 'PUT', data: { shared } });
          toast(shared ? '已加入社区讲解池。' : '已从社区讲解池撤回。');
          if (state.selectedQid === q.qid) await openQuestion(q.qid, container, { onSubmitted });
        }));
        det.append(el('label', { class: 'check-label', style: 'margin-top:12px' },
          share, '共享本条讲解给已作答的用户'));
      }
      savedSection.append(det);
    });
    aiSection.append(savedSection);
  }

  if (savedExplanations.length) renderSavedExplanations(savedExplanations, false);
  if (communityItems.length) renderSavedExplanations(communityItems, true);
  if (savedExplanationsError) {
    aiSection.append(el('div', { class: 'notice warning' },
      `历史讲解暂时加载失败：${savedExplanationsError}`,
      button('重新加载', () => openQuestion(q.qid, container, { onSubmitted }), 'secondary small', 'repeat')));
  }
  container.append(aiSection);
  if (state.questionJobId) {
    mountJob(jobMount, state.questionJobId, () => {
      state.questionJobId = null;
      openQuestion(q.qid, container, { onSubmitted });
    }, { titleHint: `${q.subject} · ${q.number_label || q.qid}` });
  }

  // ---- 管理员标注
  if (state.user.role === 'admin') {
    const difficulty = select(['待标注', '简单', '中等', '困难'], q.difficulty_source === '管理员标注' ? q.difficulty : '待标注');
    const subjectKps = (state.knowledgePoints || []).filter(k => k.subject === q.subject && k.level >= 2);
    const kpSelect = select(
      [{ value: '', label: '选择要添加的知识点…' }, ...subjectKps.map(k => ({ value: k.id, label: `${k.level === 2 ? '▸ ' : '　· '}${k.name}` }))],
      '',
    );
    const curKpIds = new Set((q.knowledge_points || []).map(k => k.kp_id));
    const methodInput = el('input', { class: 'input', placeholder: '例如：导数-含参讨论' });
    container.append(el('div', { class: 'form-section admin-only' },
      el('div', { class: 'row between' },
        field('管理员难度标注', difficulty),
        button('保存难度', event => busy(event.currentTarget, async () => {
          await api('/question-metadata', { method: 'POST', data: { qid: q.qid, difficulty: difficulty.value } });
          toast('难度标注已保存。');
          await openQuestion(q.qid, container, { onSubmitted });
        }), 'secondary small')),
      subjectKps.length ? el('div', { class: 'row between', style: 'margin-top:12px' },
        field('绑定知识点', kpSelect),
        button('添加知识点', event => busy(event.currentTarget, async () => {
          if (!kpSelect.value) return;
          curKpIds.add(Number(kpSelect.value));
          await api('/admin/question-kp', { method: 'POST', data: { qid: q.qid, kp_ids: [...curKpIds] } });
          toast('知识点标注已更新。');
          await openQuestion(q.qid, container, { onSubmitted });
        }), 'secondary small', 'plus')) : null,
      el('div', { class: 'row between', style: 'margin-top:12px' },
        field('题型/解法标签', methodInput),
        button('添加标签', event => busy(event.currentTarget, async () => {
          if (!methodInput.value.trim()) return;
          await api('/admin/question-method', { method: 'POST', data: { qid: q.qid, method_name: methodInput.value.trim() } });
          toast('题型/解法标签已添加。');
          await openQuestion(q.qid, container, { onSubmitted });
        }), 'secondary small', 'plus'))));
  }
}

function difficultyBadge(q) {
  const diff = q.difficulty || '待标注';
  const src = q.difficulty_source ? ` · ${q.difficulty_source}` : '';
  return badge(`${diff}${src}`, diff !== '待标注' ? 'blue' : '');
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
      throw error;
    }
  }
  const data = revealedCache.data.question || revealedCache.data;
  const blocks = blockList(isAnswer ? data.answer_blocks : data.solution_blocks);
  const text = isAnswer
    ? (data.answer?.text || data.answer_text || (typeof data.answer === 'string' ? data.answer : ''))
    : (data.solution || data.answer?.solution || '');
  const assetContext = { qid: q.qid, bankVersion: q.bank_version || '' };
  const closeButton = button('收起', () => {
    panel.remove();
    trigger?.setAttribute('aria-expanded', 'false');
    if (trigger) {
      trigger.textContent = isAnswer ? '查看答案' : '查看解析';
      trigger.prepend(icon(isAnswer ? 'eye' : 'notes'));
      trigger.focus();
    }
  }, 'ghost small');
  const panel = el('div', { class: 'answer-box' + (isAnswer ? '' : ' solution-box'), 'data-panel': kind },
    el('h3', {}, isAnswer ? '原卷参考答案' : '原卷解析'),
    blocks.length
      ? el('div', { class: 'question-text question-blocks' }, questionBlocks(blocks, q.subject, '', assetContext))
      : questionText(text || (isAnswer ? '原卷未提供可显示的答案。' : '原卷没有解析。'), q.images || [], q.subject, '', assetContext),
    closeButton);
  box.append(panel);
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  return true;
}
