import { api } from '../core/api.js';
import { state, readLocal, saveLocal } from '../core/state.js';
import { el, field, select, badge } from '../core/dom.js';
import { go } from '../core/router.js';

function capIcons(caps = {}) {
  const parts = [];
  if (caps.vision) parts.push('👁视觉');
  if (caps.reasoning) parts.push('🧠推理');
  if (caps.json) parts.push('{}JSON');
  return parts.length ? ` [${parts.join(' ')}]` : '';
}

export function groupedModelOptions({ requireVision = false } = {}) {
  const groups = [];
  const flatValues = new Set();
  for (const provider of state.providers || []) {
    const details = provider.model_details?.length
      ? provider.model_details
      : (provider.models || []).map(m => ({ model_id: m, display_name: m, caps: {}, enabled: true }));
    const opts = [];
    for (const d of details) {
      if (d.enabled === false) continue;
      if (requireVision && d.caps && d.caps.vision === false) continue;
      const val = JSON.stringify([provider.id, d.model_id]);
      flatValues.add(val);
      opts.push({
        value: val,
        label: `${d.display_name || d.model_id}${capIcons(d.caps)}`,
      });
    }
    if (opts.length) {
      groups.push({ optgroup: provider.name, options: opts });
    }
  }
  // If requireVision filtered out everything, fall back to showing all models with a hint
  // (fallback 必须可见地告诉用户：否则一进拍题/OCR 就被静默换成非视觉模型)。
  if (requireVision && !groups.length) {
    const fallback = groupedModelOptions({ requireVision: false });
    return { ...fallback, visionFallback: true };
  }
  return { groups, flatValues, visionFallback: false };
}

export function createModelPicker(scope, { forceCross = false, requireVision = false, showModeToggle = true, showJudge = false } = {}) {
  const { groups, flatValues, visionFallback } = groupedModelOptions({ requireVision });
  const localKey = `zhixu.models.${state.user?.id || 'anon'}.${scope}`;
  const saved = readLocal(localKey, {});
  const td = state.taskDefaults?.[scope] || {};

  const defaultPrimary = td.primary_provider_id && td.primary_model
    ? JSON.stringify([td.primary_provider_id, td.primary_model])
    : saved.primary;
  const defaultVerifier = td.verify_provider_id && td.verify_model
    ? JSON.stringify([td.verify_provider_id, td.verify_model])
    : saved.verifier;
  const defaultJudge = td.judge_provider_id && td.judge_model
    ? JSON.stringify([td.judge_provider_id, td.judge_model])
    : saved.judge;

  const firstVal = flatValues.has(defaultPrimary)
    ? defaultPrimary
    : (groups[0]?.options?.[0]?.value || '');
  const verVal = flatValues.has(defaultVerifier) ? defaultVerifier : '';
  const judgeVal = flatValues.has(defaultJudge) ? defaultJudge : '';

  const primary = select(
    [{ value: '', label: groups.length ? '请选择主模型' : '尚未配置模型' }, ...groups],
    firstVal
  );
  const verifier = select([{ value: '', label: '请选择独立核验模型（模型 B）' }, ...groups], verVal);
  const judge = select([{ value: '', label: '自动（无裁判时由模型 B 裁定）' }, ...groups], judgeVal);

  const initialCross = forceCross || Boolean(saved.cross) || td.mode === 'accurate';
  const crossCheck = el('input', { type: 'checkbox', checked: initialCross, disabled: forceCross });
  const peerCheck = el('input', { type: 'checkbox', checked: Boolean(saved.peerReview) });

  const modePills = el('div', { class: 'mode-toggle-row' });
  const fastBtn = el('button', { type: 'button', class: `mode-pill${!initialCross ? ' active' : ''}` }, '⚡ 快速模式（单模型）');
  const accurateBtn = el('button', { type: 'button', class: `mode-pill${initialCross ? ' active' : ''}` }, '🛡 精确模式（双模型交叉验证）');
  if (showModeToggle && !forceCross) {
    fastBtn.addEventListener('click', () => { crossCheck.checked = false; sync(); });
    accurateBtn.addEventListener('click', () => { crossCheck.checked = true; sync(); });
    modePills.append(fastBtn, accurateBtn);
  }

  const verifierField = field('核验模型（模型 B）', verifier);
  const judgeField = field('独立裁判模型（模型 C，可选）', judge, '不选时由核验模型 B 担任裁判，绝不由主模型 A 自判。');
  const peerLabel = el('label', { class: 'check-label', style: 'margin-top:8px' }, peerCheck, '分歧时启用 1 轮互审（A 看 B、B 看 A 各自审视修正）');

  const grid = el('div', { class: 'ai-fields' }, field(requireVision ? '视觉模型（主模型 A）' : '主模型（模型 A）', primary), verifierField);
  const extraCrossBox = el('div', { class: 'cross-extra' }, showJudge ? judgeField : null, showModeToggle && !forceCross ? peerLabel : null);

  const currentLabel = el('p', { class: 'model-picker-current help', 'aria-live': 'polite' });
  const node = el('div', { class: 'model-picker' },
    showModeToggle && !forceCross ? modePills : null,
    grid,
    !showModeToggle || forceCross
      ? el('label', { class: 'check-label', style: 'margin-top:10px' }, crossCheck, '独立交叉验证', forceCross ? badge('作答判分必选', 'amber') : null)
      : null,
    extraCrossBox,
    currentLabel,
    visionFallback
      ? el('p', { class: 'notice warning vision-fallback-hint', role: 'status' },
        '当前连接里还没有标记为「视觉」的模型，已回退为全部模型可选；请确认所选模型支持图片输入，否则拍题 / OCR 会识别失败。可在「模型设置 → 模型能力标签」中勾选视觉。')
      : null
  );

  function optionLabelOf(value) {
    if (!value) return '';
    for (const group of groups) {
      const hit = (group.options || []).find(item => item.value === value);
      if (hit) return hit.label;
    }
    return '';
  }

  function refreshCurrentLabel() {
    const isCross = forceCross || crossCheck.checked;
    const parts = [optionLabelOf(primary.value) || '未选择'];
    if (isCross && verifier.value) parts.push(`核验：${optionLabelOf(verifier.value)}`);
    if (isCross && showJudge && judge.value) parts.push(`裁判：${optionLabelOf(judge.value)}`);
    currentLabel.textContent = `当前模型：${parts.join(' · ')} · ${isCross ? '精确模式（交叉核验）' : '快速模式（单模型）'}`;
  }

  /** 切换模型/模式后立即写回内存默认模型，避免切到下一题又弹回旧选择。 */
  function writeBackState(isCross) {
    const [pid, mid] = primary.value ? JSON.parse(primary.value) : [null, null];
    const [vpid, vmid] = isCross && verifier.value ? JSON.parse(verifier.value) : [null, null];
    const [jpid, jmid] = isCross && judge.value ? JSON.parse(judge.value) : [null, null];
    state.taskDefaults = state.taskDefaults || {};
    state.taskDefaults[scope] = {
      task: scope,
      primary_provider_id: pid,
      primary_model: mid,
      verify_provider_id: vpid,
      verify_model: vmid,
      judge_provider_id: jpid,
      judge_model: jmid,
      mode: isCross ? 'accurate' : 'fast',
    };
    window.dispatchEvent(new CustomEvent('zhixu:task-default-changed', {
      detail: { scope, ...state.taskDefaults[scope] },
    }));
  }

  function sync() {
    const isCross = forceCross || crossCheck.checked;
    verifierField.classList.toggle('hide', !isCross);
    extraCrossBox.classList.toggle('hide', !isCross);
    grid.style.gridTemplateColumns = isCross ? '' : '1fr';
    fastBtn.classList.toggle('active', !isCross);
    accurateBtn.classList.toggle('active', isCross);
    saveLocal(localKey, {
      primary: primary.value,
      verifier: verifier.value,
      judge: judge.value,
      cross: isCross,
      peerReview: peerCheck.checked,
    });
    writeBackState(isCross);
    refreshCurrentLabel();
    // Also persist task default asynchronously if valid
    if (state.user && primary.value && ['explain', 'grade', 'ocr', 'notes', 'tagging', 'upload'].includes(scope)) {
      const [pid, mid] = JSON.parse(primary.value);
      const [vpid, vmid] = isCross && verifier.value ? JSON.parse(verifier.value) : [null, null];
      const [jpid, jmid] = isCross && judge.value ? JSON.parse(judge.value) : [null, null];
      api('/ai/task-defaults', {
        method: 'PUT',
        data: {
          task: scope, primary_provider_id: pid, primary_model: mid,
          verify_provider_id: vpid, verify_model: vmid,
          judge_provider_id: jpid, judge_model: jmid,
          mode: isCross ? 'accurate' : 'fast',
        },
      }).catch(() => {});
    }
  }

  primary.addEventListener('change', sync);
  verifier.addEventListener('change', sync);
  judge.addEventListener('change', sync);
  crossCheck.addEventListener('change', sync);
  peerCheck.addEventListener('change', sync);
  // Initial DOM sync without firing API PUT
  const initCross = forceCross || crossCheck.checked;
  verifierField.classList.toggle('hide', !initCross);
  extraCrossBox.classList.toggle('hide', !initCross);
  grid.style.gridTemplateColumns = initCross ? '' : '1fr';
  refreshCurrentLabel();

  if (!groups.length) {
    node.append(el('p', { class: 'help', style: 'margin-top:10px' },
      'AI 功能使用你自己的 API。',
      el('button', { type: 'button', class: 'text-button', onClick: () => go('settings') }, '前往模型设置')));
  }

  return {
    node,
    isCross: () => forceCross || crossCheck.checked,
    payload(requiredCross = false) {
      if (requiredCross && !crossCheck.checked) {
        crossCheck.checked = true;
        sync();
      }
      if (!primary.value) throw new Error('请先在模型设置中添加 API 和可使用的模型。');
      const [provider_id, model] = JSON.parse(primary.value);
      const cross_verify = requiredCross || forceCross || crossCheck.checked;
      let verify_provider_id, verify_model, judge_provider_id, judge_model;
      if (cross_verify) {
        if (!verifier.value) {
          verifier.focus();
          throw new Error('交叉验证需要选择第二个模型（模型 B）。');
        }
        [verify_provider_id, verify_model] = JSON.parse(verifier.value);
        if (provider_id === verify_provider_id && model === verify_model) {
          throw new Error('交叉验证请选择不同的模型，不能重复使用同一模型。');
        }
        if (judge.value) {
          [judge_provider_id, judge_model] = JSON.parse(judge.value);
        }
      }
      return {
        provider_id,
        model,
        cross_verify,
        verify_provider_id,
        verify_model,
        judge_provider_id,
        judge_model,
        structured_verify: cross_verify && showModeToggle,
        peer_review: cross_verify && peerCheck.checked,
      };
    },
  };
}
