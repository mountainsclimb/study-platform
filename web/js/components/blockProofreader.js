/* 通用识别块校对组件（拍题 / 知识清单 / 工坊逐页共用）。
 *
 * 数据契约与 `formulaInput.js` 的 `parseBlocksWithFormulas` / `buildConfirmBlocks` 一致：
 * 入参是服务端 blocks（intake-blocks-v1），出参是可直接提交的 blocks 数组。组件只负责
 * 展示与编辑（text / math latex / table 结构预览 / 原图定位），不关心保存到哪张表：
 * 保存由调用方通过 `onSave(blocks)` 注入。
 *
 * 兼容与降级：
 * - 没有 blocks 时展示中文空态，不报错；
 * - 公式组件或 KaTeX 不可用时由 `createFormulaInput` 自己回退纯文本；
 * - 表格预览只接受 `sanitizeTableHtml` 白名单清洗后的 HTML，失败时只显示文字；
 * - 所有用户内容走 textContent / el()，不插入未净化 HTML。
 */
import { el, button, badge, toast } from '../core/dom.js';
import { sanitizeTableHtml } from '../core/markdown.js';
import {
  attachBlockOverlay, buildConfirmBlocks, createFormulaInput, formulaText,
  parseBlocksWithFormulas, renderBlockLocation,
} from './formulaInput.js';
import { openFormulaEditor } from './formulaEditor.js';

const LOW_CONFIDENCE = 0.8;
const KIND_BADGES = { text: '', math: 'blue', table: 'amber', image: 'teal' };

function kindLabel(kind) {
  return formulaText(`study.formula.review.kind.${KIND_BADGES[kind] === undefined ? 'text' : kind}`);
}

function blockPage(block) {
  const page = Number(block?.page);
  return Number.isFinite(page) && page >= 1 ? Math.floor(page) : 1;
}

function blockConfidence(block) {
  const value = Number(block?.confidence);
  return Number.isFinite(value) ? value : null;
}

/**
 * @param {object} options
 * @param {Array<object>} [options.blocks] 服务端识别块
 * @param {boolean} [options.readOnly] 只读模式（工坊查看、课堂回放）
 * @param {number} [options.maxBlocks] 最多渲染/提交的块数
 * @param {Array<{img:HTMLImageElement,shell:HTMLElement,pageIndex:number}>} [options.previews]
 * @param {boolean} [options.showOverlays] 是否在原图上画定位框
 * @param {(blocks:Array<object>)=>void} [options.onChange] 任意编辑后回调
 * @param {(blocks:Array<object>)=>Promise<void>|void} [options.onSave] 保存按钮回调
 * @param {(block:object, mode:'formula'|'stem')=>void} [options.onUseBlock] 拍题页插入/采用
 * @param {string} [options.emptyText] 空态文案
 * @param {string} [options.statusText] 状态栏附加文案（如引擎信息）
 * @returns {{element: HTMLElement, setBlocks: Function, getBlocks: Function, destroy: Function, setActive: Function}}
 */
export function createBlockProofreader(options = {}) {
  const {
    blocks = [],
    readOnly = false,
    maxBlocks = 200,
    previews = [],
    showOverlays = false,
    onChange = null,
    onSave = null,
    onUseBlock = null,
    emptyText = '',
    statusText = '',
  } = options;

  let state = parseBlocksWithFormulas(blocks).slice(0, Math.max(1, maxBlocks));
  let activeId = null;
  let saving = false;
  let destroyed = false;
  const editors = new Map();
  const overlays = [];

  const statusBox = el('div', { class: 'upload-block-status' });
  const summary = el('p', { class: 'help upload-block-summary' });
  const list = el('div', { class: 'upload-block-list' });
  const footer = el('div', {
    class: 'row block-proofreader-footer',
    style: 'margin-top:10px;flex-wrap:wrap;gap:8px',
  });
  const root = el('section', { class: 'block-proofreader' }, statusBox, summary, list, footer);

  function currentBlocks() {
    for (const [block, entry] of editors) {
      if (entry.editor && typeof entry.editor.getValue === 'function') {
        block.latexInput = entry.editor.getValue();
      }
    }
    return buildConfirmBlocks(state);
  }

  function emitChange() {
    if (typeof onChange === 'function') onChange(currentBlocks());
  }

  function updateSummary() {
    if (!summary.isConnected) return;
    const reviewCount = state.filter(block => block.needsReview).length;
    const lowCount = state.filter(block => {
      const confidence = blockConfidence(block);
      return confidence !== null && confidence < LOW_CONFIDENCE;
    }).length;
    let text = formulaText('study.formula.review.blockCount', {
      count: state.length, review: reviewCount,
    });
    if (lowCount) text += ` · ${formulaText('study.formula.review.lowConfidenceCount', { count: lowCount })}`;
    summary.textContent = text;
  }

  function setActive(id) {
    activeId = id;
    for (const overlay of overlays) {
      try { overlay.setActive(id); } catch { /* 单个叠加层异常不影响列表 */ }
    }
    for (const item of list.querySelectorAll('.upload-block-item')) {
      item.classList.toggle('is-active', item.dataset.blockId === String(id));
    }
  }

  function destroyOverlays() {
    for (const overlay of overlays) {
      try { overlay.destroy(); } catch { /* 忽略 */ }
    }
    overlays.length = 0;
  }

  function attachOverlays() {
    destroyOverlays();
    if (!showOverlays || !previews.length || !state.length) return;
    for (const preview of previews) {
      if (!preview || !preview.img || !preview.shell) continue;
      const pageBlocks = state.filter(block => blockPage(block) === preview.pageIndex + 1);
      if (!pageBlocks.length) continue;
      overlays.push(attachBlockOverlay(
        preview.img, preview.shell, pageBlocks, id => setActive(id),
      ));
    }
    if (activeId !== null) {
      for (const overlay of overlays) overlay.setActive(activeId);
    }
  }

  function destroyEditors() {
    for (const entry of editors.values()) {
      try { entry.editor?.destroy?.(); } catch { /* 忽略 */ }
    }
    editors.clear();
  }

  function syncItem(item, block) {
    if (!item) return;
    item.classList.toggle('needs-review', Boolean(block.needsReview));
    item.classList.toggle('is-edited', Boolean(block.changed));
    const stateBadge = item.querySelector('.upload-block-review-state');
    if (stateBadge) {
      stateBadge.className = `badge upload-block-review-state${block.needsReview ? ' amber' : ' teal'}`;
      stateBadge.textContent = block.needsReview
        ? formulaText('study.formula.review.needsReview')
        : formulaText('study.formula.review.reviewed');
    }
    const reviewBtn = item.querySelector('.upload-block-review-btn');
    if (reviewBtn) reviewBtn.disabled = !block.needsReview || readOnly;
  }

  function markReviewed(block, item) {
    block.changed = true;
    block.needsReview = false;
    syncItem(item, block);
    updateSummary();
    emitChange();
  }

  function renderActions(block, item) {
    const actions = el('div', { class: 'upload-block-actions' });
    if (!readOnly && typeof onUseBlock === 'function') {
      if (block.kind === 'math') {
        actions.append(button(
          formulaText('study.formula.review.insertFormula'),
          () => onUseBlock({ ...block }, 'formula'),
          'ghost small', 'plus',
        ));
      }
      actions.append(button(
        formulaText('study.formula.review.useAsStem'),
        () => onUseBlock({ ...block }, 'stem'),
        'ghost small', 'check',
      ));
    }
    if (!readOnly) {
      actions.append(el('button', {
        type: 'button',
        class: 'button ghost small upload-block-review-btn',
        disabled: !block.needsReview,
        onClick: () => markReviewed(block, item),
      }, badge(block.needsReview
        ? formulaText('study.formula.review.markReviewed')
        : formulaText('study.formula.review.reviewed'))));
    }
    return actions.childNodes.length ? actions : null;
  }

  function renderTablePreview(block, item) {
    const tableHtml = String(block.original?.table_html || block.table_html || '').trim();
    if (!tableHtml) return;
    try {
      const template = document.createElement('template');
      template.innerHTML = sanitizeTableHtml(tableHtml);
      const table = template.content.querySelector('table');
      if (table) {
        item.append(el('span', { class: 'badge teal' },
          formulaText('study.oss.table.structure')));
        item.append(el('div', { class: 'table-wrap upload-block-table-preview' }, table));
      }
    } catch {
      /* 净化器不可用时只显示文字，不插入未净化 HTML。 */
    }
  }

  function renderItem(block) {
    const item = el('div', {
      class: 'upload-block-item',
      'data-block-id': String(block.id),
    });
    item.dataset.blockId = String(block.id);
    if (block.needsReview) item.classList.add('needs-review');
    if (block.changed) item.classList.add('is-edited');
    if (activeId !== null && String(activeId) === String(block.id)) item.classList.add('is-active');

    const head = el('div', { class: 'upload-block-head' },
      badge(kindLabel(block.kind), KIND_BADGES[block.kind] || ''),
      el('button', {
        type: 'button',
        class: 'upload-block-location',
        title: formulaText('study.formula.review.overlayHint'),
        'aria-label': `${kindLabel(block.kind)} · ${renderBlockLocation(block)}`,
        onClick: () => setActive(block.id),
      }, renderBlockLocation(block)));
    const confidence = blockConfidence(block);
    if (confidence !== null) {
      const node = el('span', { class: 'upload-block-confidence' },
        formulaText('study.formula.review.confidence', { percent: Math.round(confidence * 100) }));
      if (confidence < LOW_CONFIDENCE) {
        node.append(badge(formulaText('study.formula.review.lowConfidence'), 'amber'));
      }
      head.append(node);
    }
    head.append(el('span', {
      class: `badge upload-block-review-state${block.needsReview ? ' amber' : ' teal'}`,
    }, block.needsReview
      ? formulaText('study.formula.review.needsReview')
      : formulaText('study.formula.review.reviewed')));
    item.append(head);

    if (block.kind === 'math') {
      if (readOnly) {
        item.append(el('pre', { class: 'upload-block-readonly-text' },
          block.latexInput || block.text || ''));
      } else {
        const editor = createFormulaInput({
          value: block.latexInput,
          placeholder: formulaText('study.formula.review.formulaPlaceholder'),
          ariaLabel: formulaText('study.formula.review.mathEditorAria', { id: block.id }),
          rows: 2,
          allowPlainFallback: true,
          onChange: value => {
            block.latexInput = value;
            block.changed = true;
            block.needsReview = false;
            syncItem(item, block);
            updateSummary();
            emitChange();
          },
        });
        editors.set(block, { editor });
        const visualEdit = button(formulaText('study.oss.formula.visualEdit'), async () => {
          try {
            const value = await openFormulaEditor({
              initialLatex: block.latexInput,
              title: formulaText('study.oss.formula.dialogTitle'),
            });
            if (value === null) return;
            block.latexInput = value;
            block.changed = true;
            block.needsReview = false;
            editor.setValue?.(value);
            syncItem(item, block);
            updateSummary();
            emitChange();
          } catch {
            /* 编辑器打开失败时保留文本编辑面与当前值。 */
          }
        }, 'secondary');
        item.append(editor.element, el('div', { class: 'oss-formula-actions' }, visualEdit));
      }
    } else if (block.kind === 'text') {
      if (readOnly) {
        item.append(el('pre', { class: 'upload-block-readonly-text' }, block.text || ''));
      } else {
        const editor = el('textarea', {
          class: 'upload-block-text',
          rows: '2',
          maxlength: '4000',
          placeholder: formulaText('study.formula.review.textPlaceholder'),
          'aria-label': formulaText('study.formula.review.textEditorAria', { id: block.id }),
          value: block.text,
        });
        editor.addEventListener('input', () => {
          block.text = editor.value;
          block.changed = true;
          block.needsReview = false;
          syncItem(item, block);
          updateSummary();
          emitChange();
        });
        item.append(editor);
      }
    } else if (block.kind === 'table') {
      renderTablePreview(block, item);
      if (block.text) item.append(el('pre', { class: 'upload-block-readonly-text' }, block.text));
      item.append(el('p', { class: 'upload-block-readonly' },
        formulaText('study.formula.review.keepOriginal')));
    } else {
      if (block.text) item.append(el('pre', { class: 'upload-block-readonly-text' }, block.text));
      item.append(el('p', { class: 'upload-block-readonly' },
        formulaText('study.formula.review.keepOriginal')));
    }

    const actions = renderActions(block, item);
    if (actions) item.append(actions);
    return item;
  }

  function renderStatus() {
    statusBox.replaceChildren();
    const row = el('div', { class: 'row', style: 'flex-wrap:wrap;gap:8px' });
    if (readOnly) row.append(badge(formulaText('study.formula.review.readOnly'), 'blue'));
    if (statusText) row.append(badge(statusText, 'teal'));
    statusBox.append(row);
  }

  function renderFooter() {
    footer.replaceChildren();
    if (readOnly || typeof onSave !== 'function') return;
    const saveBtn = button(
      formulaText('study.formula.review.save'),
      async () => {
        if (saving) return;
        saving = true;
        saveBtn.disabled = true;
        saveBtn.classList.add('is-busy');
        try {
          await onSave(currentBlocks());
        } catch (error) {
          const message = error && error.message ? error.message : '';
          const prefix = formulaText('study.formula.review.saveFailed');
          toast(message ? `${prefix}：${message}` : prefix, true);
        } finally {
          saving = false;
          saveBtn.disabled = false;
          saveBtn.classList.remove('is-busy');
        }
      },
      'primary', 'check',
    );
    footer.append(saveBtn, el('span', { class: 'help' },
      formulaText('study.formula.review.saveHint')));
  }

  function render() {
    if (destroyed) return;
    destroyEditors();
    destroyOverlays();
    renderStatus();
    list.replaceChildren();
    if (!state.length) {
      list.append(el('div', { class: 'upload-block-degraded' },
        emptyText || formulaText('study.formula.review.empty')));
      updateSummary();
      return;
    }
    for (const block of state) list.append(renderItem(block));
    attachOverlays();
    updateSummary();
  }

  function setBlocks(next, { force = true } = {}) {
    if (!Array.isArray(next)) return;
    if (!force && state.length) return;
    state = parseBlocksWithFormulas(next).slice(0, Math.max(1, maxBlocks));
    activeId = null;
    render();
  }

  function destroy() {
    destroyed = true;
    destroyEditors();
    destroyOverlays();
    root.remove();
  }

  renderFooter();
  render();
  return {
    element: root,
    setBlocks,
    getBlocks: currentBlocks,
    setActive,
    destroy,
  };
}
