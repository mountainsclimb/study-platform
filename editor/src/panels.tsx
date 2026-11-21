/**
 * 右侧面板：图层面板 + 属性面板（P6b 增补，补齐上游 editor UI 没有的层面）。
 *
 * - 图层面板：自顶向下列出元素，点选/多选、上移下移置顶置底、复制、删除、锁定；
 * - 属性面板：位置尺寸旋转、按元素类型编辑（形状填充、线条颜色/线宽/线型、图片地址、
 *   LaTeX 源码、默认字体色），以及页面背景色。
 *
 * 面板只调用 App 传入的回调（最终生成标准 EditorTransaction），不直接改文档。
 */
import React, { useEffect, useState } from 'react';
import type { PPTElement, Slide } from '@openmaic/dsl';

export const ELEMENT_TYPE_LABELS: Record<string, string> = {
  text: '文本',
  image: '图片',
  shape: '形状',
  line: '线条',
  latex: '公式',
  table: '表格',
  chart: '图表',
  code: '代码',
  video: '视频',
  audio: '音频',
};

export function elementLabel(element: PPTElement): string {
  const label = ELEMENT_TYPE_LABELS[element.type] || element.type;
  if (element.type === 'text') {
    const plain = String((element as { content?: string }).content || '')
      .replace(/<[^>]+>/g, '')
      .replace(/&nbsp;/g, ' ')
      .trim();
    return plain ? `${label} · ${plain.slice(0, 16)}` : label;
  }
  if (element.type === 'image') {
    const src = String((element as { src?: string }).src || '');
    const name = src.split('/').filter(Boolean).pop() || '';
    return name ? `${label} · ${name.slice(0, 16)}` : label;
  }
  if (element.type === 'latex') return `${label} · ${String((element as { latex?: string }).latex || '').slice(0, 16)}`;
  if (element.type === 'table') {
    const rows = (element as { data?: unknown[][] }).data?.length || 0;
    return `${label} · ${rows} 行`;
  }
  return label;
}

function numberText(value: number): string {
  const rounded = Math.round(Number(value) * 100) / 100;
  return String(rounded);
}

interface NumberFieldProps {
  label: string;
  value: number;
  onCommit: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  title?: string;
}

function NumberField({ label, value, onCommit, min, max, step = 1, title }: NumberFieldProps) {
  const [draft, setDraft] = useState(numberText(value));
  useEffect(() => {
    setDraft(numberText(value));
  }, [value]);
  const commit = () => {
    const parsed = Number(draft);
    if (!Number.isFinite(parsed)) {
      setDraft(numberText(value));
      return;
    }
    let next = parsed;
    if (min !== undefined) next = Math.max(min, next);
    if (max !== undefined) next = Math.min(max, next);
    setDraft(numberText(next));
    if (next !== Number(value)) onCommit(next);
  };
  return (
    <label className="zx-field" title={title}>
      <span>{label}</span>
      <input
        type="number"
        inputMode="decimal"
        step={step}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            commit();
            (event.currentTarget as HTMLInputElement).blur();
          }
        }}
      />
    </label>
  );
}

interface ColorFieldProps {
  label: string;
  value: string;
  onCommit: (value: string) => void;
}

function ColorField({ label, value, onCommit }: ColorFieldProps) {
  const safe = /^#[0-9a-fA-F]{6}$/.test(value) ? value : '#333333';
  return (
    <label className="zx-field zx-field-color">
      <span>{label}</span>
      <input type="color" value={safe} onChange={(event) => onCommit(event.target.value)} />
      <code>{safe}</code>
    </label>
  );
}

export interface LayerPanelProps {
  elements: readonly PPTElement[];
  selectedIds: readonly string[];
  onSelect: (id: string, additive: boolean) => void;
  onReorder: (command: 'front' | 'forward' | 'backward' | 'back') => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onToggleLock: () => void;
  hasSelection: boolean;
  selectedLocked: boolean;
}

export function LayerPanel({
  elements,
  selectedIds,
  onSelect,
  onReorder,
  onDuplicate,
  onDelete,
  onToggleLock,
  hasSelection,
  selectedLocked,
}: LayerPanelProps) {
  const ordered = [...elements].reverse();
  return (
    <div className="zx-panel">
      <div className="zx-panel-toolbar">
        <button type="button" className="zx-btn zx-btn-sm" disabled={!hasSelection} onClick={() => onReorder('front')} title="置于最顶层">
          置顶
        </button>
        <button type="button" className="zx-btn zx-btn-sm" disabled={!hasSelection} onClick={() => onReorder('forward')} title="上移一层">
          上移
        </button>
        <button type="button" className="zx-btn zx-btn-sm" disabled={!hasSelection} onClick={() => onReorder('backward')} title="下移一层">
          下移
        </button>
        <button type="button" className="zx-btn zx-btn-sm" disabled={!hasSelection} onClick={() => onReorder('back')} title="置于最底层">
          置底
        </button>
      </div>
      <div className="zx-panel-toolbar">
        <button type="button" className="zx-btn zx-btn-sm" disabled={!hasSelection} onClick={onDuplicate}>
          复制
        </button>
        <button type="button" className="zx-btn zx-btn-sm" disabled={!hasSelection} onClick={onToggleLock}>
          {selectedLocked ? '解锁' : '锁定'}
        </button>
        <button type="button" className="zx-btn zx-btn-sm zx-btn-danger" disabled={!hasSelection} onClick={onDelete}>
          删除
        </button>
      </div>
      <ul className="zx-layer-list" role="listbox" aria-label="图层列表" aria-multiselectable="true">
        {ordered.map((element) => {
          const selected = selectedIds.includes(element.id);
          return (
            <li key={element.id}>
              <button
                type="button"
                role="option"
                aria-selected={selected}
                className={`zx-layer-row${selected ? ' is-selected' : ''}${element.lock ? ' is-locked' : ''}`}
                onClick={(event) => onSelect(element.id, event.shiftKey || event.ctrlKey || event.metaKey)}
                title={`${elementLabel(element)}（${element.id}）`}
              >
                <span className={`zx-layer-type zx-type-${element.type}`}>
                  {ELEMENT_TYPE_LABELS[element.type] || element.type}
                </span>
                <span className="zx-layer-label">{elementLabel(element)}</span>
                {element.lock ? <span className="zx-layer-lock" aria-label="已锁定">🔒</span> : null}
              </button>
            </li>
          );
        })}
      </ul>
      {ordered.length === 0 ? <p className="zx-panel-empty">本页还没有元素，可用顶部工具栏插入文本/图片/形状等。</p> : null}
    </div>
  );
}

export interface PropertyPanelProps {
  slide: Slide;
  element: PPTElement | null;
  onPatchElement: (patch: Record<string, unknown>) => void;
  onPatchTheme: (color: string) => void;
  onDelete: () => void;
  onDuplicate: () => void;
  onImageSrc: (src: string) => void;
}

export function PropertyPanel({
  slide,
  element,
  onPatchElement,
  onPatchTheme,
  onDelete,
  onDuplicate,
  onImageSrc,
}: PropertyPanelProps) {
  const [imageSrc, setImageSrc] = useState('');

  useEffect(() => {
    setImageSrc(element && element.type === 'image' ? String((element as { src?: string }).src || '') : '');
  }, [element?.id, element?.type]);

  const themeColor = slide.theme.backgroundColor;

  if (!element) {
    return (
      <div className="zx-panel">
        <p className="zx-panel-empty">未选中元素。点击画布元素或图层可编辑属性。</p>
        <div className="zx-property-section">
          <h4>页面</h4>
          <ColorField label="背景色" value={themeColor} onCommit={onPatchTheme} />
          <p className="zx-help">画布尺寸 {slide.viewportSize} × {Math.round(slide.viewportSize * slide.viewportRatio)}</p>
        </div>
      </div>
    );
  }

  const locked = Boolean((element as { lock?: boolean }).lock);
  const isLine = element.type === 'line';
  return (
    <div className="zx-panel">
      <div className="zx-property-section">
        <h4>
          {ELEMENT_TYPE_LABELS[element.type] || element.type}
          {locked ? '（已锁定）' : ''}
        </h4>
        <p className="zx-help">ID：{element.id}</p>
        <div className="zx-grid2">
          <NumberField label="X" value={element.left} onCommit={(value) => onPatchElement({ left: value })} />
          <NumberField label="Y" value={element.top} onCommit={(value) => onPatchElement({ top: value })} />
          {isLine ? (
            <NumberField
              label="线宽"
              value={element.width}
              min={1}
              max={8}
              onCommit={(value) => onPatchElement({ width: value })}
            />
          ) : (
            <>
              <NumberField label="宽" value={element.width} min={8} onCommit={(value) => onPatchElement({ width: value })} />
              <NumberField label="高" value={element.height} min={8} onCommit={(value) => onPatchElement({ height: value })} />
              <NumberField
                label="旋转"
                value={(element as { rotate?: number }).rotate ?? 0}
                min={-360}
                max={360}
                onCommit={(value) => onPatchElement({ rotate: value })}
              />
            </>
          )}
        </div>
      </div>

      {element.type === 'text' ? (
        <div className="zx-property-section">
          <h4>文本</h4>
          <ColorField
            label="默认字体色"
            value={String((element as { defaultColor?: string }).defaultColor || '#333333')}
            onCommit={(value) => onPatchElement({ defaultColor: value })}
          />
          <p className="zx-help">双击画布上的文字可进入富文本编辑；字号/加粗/对齐/颜色等用文本工具条调整。</p>
        </div>
      ) : null}

      {element.type === 'shape' ? (
        <div className="zx-property-section">
          <h4>形状</h4>
          <ColorField
            label="填充"
            value={String((element as { fill?: string }).fill || '#5b9bd5')}
            onCommit={(value) => onPatchElement({ fill: value })}
          />
          <p className="zx-help">双击形状可编辑形状内文字。</p>
        </div>
      ) : null}

      {element.type === 'line' ? (
        <div className="zx-property-section">
          <h4>线条</h4>
          <ColorField
            label="颜色"
            value={String((element as { color?: string }).color || '#333333')}
            onCommit={(value) => onPatchElement({ color: value })}
          />
          <label className="zx-field">
            <span>线型</span>
            <select
              value={String((element as { style?: string }).style || 'solid')}
              onChange={(event) => onPatchElement({ style: event.target.value })}
            >
              <option value="solid">实线</option>
              <option value="dashed">虚线</option>
              <option value="dotted">点线</option>
            </select>
          </label>
        </div>
      ) : null}

      {element.type === 'image' ? (
        <div className="zx-property-section">
          <h4>图片</h4>
          <label className="zx-field zx-field-wide">
            <span>站内地址</span>
            <input
              type="text"
              value={imageSrc}
              placeholder="/api/classroom/assets/12"
              onChange={(event) => setImageSrc(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') onImageSrc(imageSrc);
              }}
            />
          </label>
          <button type="button" className="zx-btn zx-btn-sm" onClick={() => onImageSrc(imageSrc)}>
            应用地址
          </button>
          <p className="zx-help">只支持站内相对地址；外链/本地上传会被后端丢弃。</p>
        </div>
      ) : null}

      {element.type === 'latex' ? (
        <div className="zx-property-section">
          <h4>公式</h4>
          <label className="zx-field zx-field-wide">
            <span>LaTeX</span>
            <textarea
              rows={3}
              defaultValue={String((element as { latex?: string }).latex || '')}
              onBlur={(event) => {
                const value = event.target.value.trim();
                if (value) onPatchElement({ latex: value, html: undefined });
              }}
            />
          </label>
          <p className="zx-help">失焦后生效；复杂公式可直接双击公式元素用内置公式编辑器。</p>
        </div>
      ) : null}

      {element.type === 'table' ? (
        <div className="zx-property-section">
          <h4>表格</h4>
          <p className="zx-help">
            {(element as { data?: unknown[][] }).data?.length || 0} 行 ×{' '}
            {(element as { data?: unknown[][] }).data?.[0]?.length || 0} 列（双击单元格编辑文字）
          </p>
        </div>
      ) : null}

      {element.type === 'chart' ? (
        <div className="zx-property-section">
          <h4>图表</h4>
          <p className="zx-help">类型：{String((element as { chartType?: string }).chartType || 'bar')}（数据编辑暂未开放）</p>
        </div>
      ) : null}

      <div className="zx-property-section">
        <h4>操作</h4>
        <div className="zx-panel-toolbar">
          <button type="button" className="zx-btn zx-btn-sm" onClick={onDuplicate}>
            复制
          </button>
          <button type="button" className="zx-btn zx-btn-sm zx-btn-danger" onClick={onDelete}>
            删除
          </button>
        </div>
      </div>

      <div className="zx-property-section">
        <h4>页面</h4>
        <ColorField label="背景色" value={themeColor} onCommit={onPatchTheme} />
      </div>
    </div>
  );
}
