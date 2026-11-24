/* 化学方程式配平弹层（app/chem_balance.py 的前端入口）。
 *
 * - 只提交化学式与一个箭头；系数由服务端线性规划求解，前端不猜系数。
 * - 可逆反应勾选后把 `->` / `=` / `→` 归一为 `⇌`，输出保留可逆箭头。
 * - 常见物质按钮只做文本插入，不发起额外请求；所有内容 textContent 渲染。
 */
import { api } from '../core/api.js';
import { el, button, field, input, notice, toast, busy } from '../core/dom.js';
import { t } from '../core/i18n.js';

const COMMON_SUBSTANCES = [
  'H2', 'O2', 'N2', 'Cl2', 'C', 'S', 'P', 'Fe', 'Cu', 'Zn', 'Al', 'Mg',
  'H2O', 'CO2', 'CO', 'CH4', 'C2H5OH', 'NH3', 'SO2', 'SO3', 'NO', 'NO2',
  'HCl', 'H2SO4', 'HNO3', 'NaOH', 'KOH', 'Ca(OH)2', 'Na2CO3', 'NaHCO3',
  'CaCO3', 'Fe2O3', 'CuO', 'MnO2', 'KMnO4', 'NaCl', 'BaCl2', 'BaSO4',
  'AgNO3', 'CuSO4', 'KNO3', 'CaCl2',
];

function chemText(key, fallback, vars) {
  const value = t(key, vars);
  if (value !== key) return value;
  return String(fallback).replace(/\{(\w+)\}/g, (match, name) => (
    vars && vars[name] !== undefined && vars[name] !== null ? String(vars[name]) : match
  ));
}

function normalizeArrow(equation, reversible) {
  const value = String(equation || '').trim();
  if (!reversible) return value;
  return value.replace(/<=>|<->|⇋|↔|->|→|⟶|=/g, '⇌');
}

function insertAtCursor(target, snippet) {
  const source = String(target.value || '');
  const start = Number.isInteger(target.selectionStart) ? target.selectionStart : source.length;
  const end = Number.isInteger(target.selectionEnd) ? target.selectionEnd : start;
  target.value = `${source.slice(0, start)}${snippet}${source.slice(end)}`;
  const caret = start + snippet.length;
  try { target.setSelectionRange(caret, caret); } catch { /* 忽略 */ }
  target.focus();
}

/**
 * 打开配平弹层。
 * @param {{initialEquation?:string}} [options]
 * @returns {HTMLDialogElement}
 */
export function openChemBalancer({ initialEquation = '' } = {}) {
  const dialog = el('dialog', {
    class: 'modal-dialog chem-balance-dialog',
    'aria-label': chemText('study.chem.balance.title', '化学方程式配平'),
  });
  const equationInput = input(chemText('study.chem.balance.placeholder', '例如 H2 + O2 -> H2O'), {
    value: initialEquation,
    maxlength: '200',
    class: 'chem-balance-input',
  });
  const reversible = el('input', { type: 'checkbox', class: 'chem-balance-reversible' });
  const resultHost = el('div', { class: 'chem-balance-result', role: 'status', 'aria-live': 'polite' });
  const substanceRow = el('div', { class: 'chem-balance-substances' },
    ...COMMON_SUBSTANCES.map(formula => button(formula, () => {
      insertAtCursor(equationInput, formula);
    }, 'ghost small chem-balance-chip')));

  const submit = button(chemText('study.chem.balance.submit', '配平'), event => busy(event.currentTarget, async () => {
    const equation = normalizeArrow(equationInput.value, Boolean(reversible.checked));
    if (!equation) {
      toast(chemText('study.chem.balance.empty', '请输入化学方程式'), true);
      return;
    }
    resultHost.replaceChildren(el('p', { class: 'help' }, chemText('study.chem.balance.loading', '正在配平…')));
    try {
      const result = await api('/chem/balance', {
        method: 'POST', data: { equation },
      });
      if (!result || result.ok !== true) {
        resultHost.replaceChildren(notice(
          String(result?.reason || chemText('study.chem.balance.failed', '无法配平该方程式')), 'warning',
        ));
        return;
      }
      resultHost.replaceChildren(
        el('p', { class: 'chem-balance-equation' }, String(result.balanced || '')),
        el('p', { class: 'help' },
          chemText('study.chem.balance.coefficients', '系数：{values}', {
            values: (result.coefficients || []).join('、'),
          })),
        el('p', { class: 'help' },
          result.reversible
            ? chemText('study.chem.balance.reversibleResult', '已按可逆反应输出 ⇌')
            : chemText('study.chem.balance.forwardResult', '已按正向反应输出 ->')),
      );
    } catch (error) {
      resultHost.replaceChildren(notice(
        chemText('study.chem.balance.requestFailed', '配平请求失败：{reason}', {
          reason: error?.message || '',
        }), 'error',
      ));
    }
  }), '', 'check');

  dialog.append(
    el('div', { class: 'row between' },
      el('h2', {}, chemText('study.chem.balance.title', '化学方程式配平')),
      el('button', {
        type: 'button', class: 'close-dialog',
        'aria-label': t('common.close'), onClick: () => dialog.close(),
      }, '×')),
    el('p', { class: 'help' },
      chemText('study.chem.balance.hint', '只输入化学式与一个箭头（-> 或 ⇌），不要带配平系数。')),
    field(chemText('study.chem.balance.equation', '方程式'), equationInput),
    el('label', { class: 'row chem-balance-reversible-row' },
      reversible,
      el('span', {}, chemText('study.chem.balance.reversible', '可逆反应（输出 ⇌）'))),
    el('p', { class: 'help' }, chemText('study.chem.balance.common', '常见物质（点击插入）')),
    substanceRow,
    el('div', { class: 'row end', style: 'margin-top:12px' }, submit),
    resultHost,
  );
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
  equationInput.focus();
  return dialog;
}
