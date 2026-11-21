/**
 * 知序 slide content ↔ OpenMAIC SlideContent 的适配层。
 *
 * 知序后端（`app/classroom/schema.py::normalize_slide_content`）持久化的规范结构是：
 *   `{ type:'slide', schemaVersion:1, canvas:{ width:1000, height:562.5, theme, elements } }`
 * 其中元素是 PPTist 风格字段。`@openmaic/dsl` 的 `SlideContent.canvas: Slide` 要求
 * `{ id, viewportSize, viewportRatio, theme, elements }`，两者字段几乎一致，本文件负责：
 *   1. 读取时补齐 DSL 必填字段（slide.id / viewportSize / viewportRatio、元素 rotate、
 *      table.cellMinHeight + 单元格 id、code.lines 等），无法转换时跳过并给出 warning；
 *   2. 保存时回到后端规范结构（width/height/theme/elements + 可选 background），
 *      后端归一化会重新校验/钳制/净化，因此这里只做结构性转换。
 */
import katex from 'katex';
import { safeKatexOptions } from '@openmaic/dsl';
import type { PPTElement, Slide, SlideContent, SlideTheme } from '@openmaic/dsl';

export const CANVAS_WIDTH = 1000;
export const CANVAS_HEIGHT = 562.5;
/** 后端 `schema.MAX_ELEMENTS`：单页最多 24 个元素，超出会被服务端丢弃。 */
export const MAX_ELEMENTS = 24;

export interface ZhixuTheme {
  backgroundColor: string;
  themeColors: string[];
  fontColor: string;
  fontName: string;
}

export interface ZhixuSlideContent {
  type: 'slide';
  schemaVersion?: number;
  canvas: {
    width: number;
    height: number;
    theme: ZhixuTheme;
    background?: Record<string, unknown>;
    elements: Record<string, unknown>[];
  };
}

export interface SceneContentResponse {
  scene: {
    id: number;
    course_id: number;
    title: string;
    scene_type: string;
    status: string;
    version: number;
    content: unknown;
    updated_at: string;
  };
  warnings?: string[];
}

const DEFAULT_THEME: ZhixuTheme = {
  backgroundColor: '#ffffff',
  themeColors: ['#1f6feb', '#e8590c', '#2f9e44', '#7048e8', '#c2255c'],
  fontColor: '#222222',
  fontName: 'Microsoft YaHei',
};

const COLOR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

function num(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function color(value: unknown, fallback: string): string {
  const candidate = text(value);
  return COLOR_RE.test(candidate) ? candidate : fallback;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function normalizeTheme(raw: unknown): SlideTheme {
  const source = record(raw);
  const themeColors = list(source.themeColors)
    .map((item) => color(item, ''))
    .filter(Boolean);
  return {
    backgroundColor: color(source.backgroundColor, DEFAULT_THEME.backgroundColor),
    themeColors: themeColors.length ? themeColors : [...DEFAULT_THEME.themeColors],
    fontColor: color(source.fontColor, DEFAULT_THEME.fontColor),
    fontName: text(source.fontName, DEFAULT_THEME.fontName) || DEFAULT_THEME.fontName,
  };
}

function baseGeometry(raw: Record<string, unknown>) {
  return {
    left: num(raw.left, 0),
    top: num(raw.top, 0),
    width: Math.max(1, num(raw.width, 320)),
    height: Math.max(1, num(raw.height, 120)),
    rotate: num(raw.rotate, 0),
  };
}

function pointPair(value: unknown): [number, number] {
  const values = list(value);
  return [num(values[0], 0), num(values[1], 0)];
}

function normalizeTableCell(raw: unknown, row: number, column: number, tableId: string) {
  const source = record(raw);
  return {
    id: text(source.id) || `${tableId}-cell-${row}-${column}`,
    colspan: Math.max(1, Math.min(6, num(source.colspan, 1))),
    rowspan: Math.max(1, Math.min(8, num(source.rowspan, 1))),
    text: text(source.text),
    ...(source.style && typeof source.style === 'object' ? { style: source.style } : {}),
  };
}

function codeLines(raw: Record<string, unknown>): { id: string; content: string }[] {
  const lines = list(raw.lines)
    .map((item, index) => {
      const line = record(item);
      const content = text(line.content, typeof item === 'string' ? item : '');
      return { id: text(line.id) || `L${index + 1}`, content };
    })
    .filter((line) => typeof line.content === 'string');
  if (lines.length) return lines;
  const code = text(raw.code);
  if (!code) return [{ id: 'L1', content: '' }];
  return code.split('\n').map((content, index) => ({ id: `L${index + 1}`, content }));
}

/**
 * 知序元素 → DSL 元素。无法转换的元素返回 null 并写入 warnings（不阻塞整页加载）。
 * 注意：DSL 的 `PPTLineElement` 没有 height/rotate；`PPTTableElement` 需要 outline/cellMinHeight。
 */
function toEditorElement(raw: unknown, index: number, warnings: string[]): PPTElement | null {
  const source = record(raw);
  const type = text(source.type).toLowerCase();
  const id = text(source.id) || `${type || 'element'}_${index + 1}`;
  if (!type) {
    warnings.push(`已跳过第 ${index + 1} 个缺少 type 的元素`);
    return null;
  }
  const geometry = baseGeometry(source);
  switch (type) {
    case 'text': {
      const content = text(source.content);
      if (!content) {
        warnings.push(`文本元素 ${id} 没有内容，已跳过`);
        return null;
      }
      return {
        ...geometry,
        id,
        type: 'text',
        content,
        defaultFontName: text(source.defaultFontName),
        defaultColor: color(source.defaultColor ?? source.color, '#333333'),
        ...(source.lineHeight !== undefined ? { lineHeight: num(source.lineHeight, 1.5) } : {}),
        ...(source.vAlign ? { vAlign: source.vAlign as 'top' | 'middle' | 'bottom' } : {}),
      } as PPTElement;
    }
    case 'image': {
      let src = text(source.src || source.url);
      if (/^\d+$/.test(src)) src = `/api/classroom/assets/${src}`;
      if (!src) {
        warnings.push(`图片元素 ${id} 缺少 src，已跳过`);
        return null;
      }
      return {
        ...geometry,
        id,
        type: 'image',
        src,
        fixedRatio: Boolean(source.fixedRatio),
        ...(text(source.alt) ? { alt: text(source.alt) } : {}),
      } as unknown as PPTElement;
    }
    case 'shape': {
      const path = text(source.path) || 'M 0 0 L 1 0 L 1 1 L 0 1 Z';
      const viewBoxRaw = list(source.viewBox);
      const viewBox: [number, number] = [
        num(viewBoxRaw[0], 1) || 1,
        num(viewBoxRaw[1], 1) || 1,
      ];
      return {
        ...geometry,
        id,
        type: 'shape',
        path,
        viewBox,
        fill: color(source.fill, '#5b9bd5'),
        fixedRatio: Boolean(source.fixedRatio),
        ...(source.text && typeof source.text === 'object'
          ? { text: source.text as Record<string, unknown> }
          : {}),
      } as unknown as PPTElement;
    }
    case 'line': {
      const startRaw = list(source.start);
      const endRaw = list(source.end);
      if (startRaw.length !== 2 || endRaw.length !== 2) {
        warnings.push(`线条元素 ${id} 缺少 start/end，已跳过`);
        return null;
      }
      const style = text(source.style);
      return {
        id,
        type: 'line',
        left: num(source.left, 0),
        top: num(source.top, 0),
        width: Math.max(1, num(source.width, 2)),
        start: pointPair(startRaw),
        end: pointPair(endRaw),
        style: style === 'dashed' || style === 'dotted' ? style : 'solid',
        color: color(source.color, '#333333'),
        points: [text(list(source.points)[0]), text(list(source.points)[1])] as [
          '' | 'arrow' | 'dot',
          '' | 'arrow' | 'dot',
        ],
      } as unknown as PPTElement;
    }
    case 'latex': {
      const latex = text(source.latex).replace(/^\$+|\$+$/g, '');
      if (!latex) {
        warnings.push(`公式元素 ${id} 缺少 latex，已跳过`);
        return null;
      }
      // 后端只持久化 latex 源码；renderer 需要 `html`（KaTeX 渲染结果）才会显示公式，
      // 因此载入时按需重算（与上游 latex-editor 的 safeKatexOptions 一致）。
      let html = text(source.html);
      if (!html) {
        try {
          html = katex.renderToString(
            latex,
            safeKatexOptions({ displayMode: true, output: 'html', throwOnError: false }),
          );
        } catch {
          html = '';
        }
      }
      return {
        ...geometry,
        id,
        type: 'latex',
        latex,
        ...(html ? { html } : {}),
        color: color(source.color, '#000000'),
        align: ['left', 'center', 'right'].includes(text(source.align))
          ? (text(source.align) as 'left' | 'center' | 'right')
          : 'center',
      } as unknown as PPTElement;
    }
    case 'table': {
      const rows = list(source.data).map((row, rowIndex) =>
        list(row).map((cell, columnIndex) =>
          normalizeTableCell(cell, rowIndex, columnIndex, id),
        ),
      );
      if (!rows.length || rows.every((row) => row.length === 0)) {
        warnings.push(`表格元素 ${id} 缺少 data，已跳过`);
        return null;
      }
      const colWidthsRaw = list(source.colWidths);
      const columnCount = Math.max(...rows.map((row) => row.length), 1);
      const colWidths = colWidthsRaw.length
        ? colWidthsRaw.map((item) => num(item, 0)).filter((item) => item > 0)
        : Array.from({ length: columnCount }, () => 1 / columnCount);
      const outline = record(source.outline);
      return {
        ...geometry,
        id,
        type: 'table',
        colWidths: colWidths.length ? colWidths : Array.from({ length: columnCount }, () => 1 / columnCount),
        cellMinHeight: Math.max(16, num(source.cellMinHeight, 36)),
        data: rows,
        outline: {
          width: num(outline.width, 1),
          style: ['solid', 'dashed', 'dotted'].includes(text(outline.style))
            ? (text(outline.style) as 'solid' | 'dashed' | 'dotted')
            : 'solid',
          color: color(outline.color, '#cccccc'),
        },
      } as unknown as PPTElement;
    }
    case 'chart': {
      const data = record(source.data);
      const labels = list(data.labels).map((item) => text(item));
      const series = list(data.series).map((row) => list(row).map((item) => num(item, 0)));
      if (!labels.length || !series.length) {
        warnings.push(`图表元素 ${id} 缺少有效 data，已跳过`);
        return null;
      }
      const chartTypes = ['bar', 'column', 'line', 'pie', 'ring', 'area', 'radar', 'scatter'];
      const chartType = text(source.chartType).toLowerCase();
      return {
        ...geometry,
        id,
        type: 'chart',
        chartType: chartTypes.includes(chartType) ? chartType : 'bar',
        data: { labels, legends: list(data.legends).map((item) => text(item)), series },
        themeColors: (list(source.themeColors).map((item) => color(item, '')).filter(Boolean))
          .slice(0, 8),
      } as unknown as PPTElement;
    }
    case 'code': {
      return {
        ...geometry,
        id,
        type: 'code',
        language: text(source.language, 'text') || 'text',
        lines: codeLines(source),
      } as unknown as PPTElement;
    }
    default:
      warnings.push(`已跳过暂不支持的幻灯片元素类型：${type}`);
      return null;
  }
}

/** 未知类型的元素类型集合（用于编辑器保存时提示）。 */
const EDITOR_SUPPORTED_TYPES = new Set([
  'text',
  'image',
  'shape',
  'line',
  'latex',
  'table',
  'chart',
  'code',
  'video',
  'audio',
]);

/**
 * 知序 GET content → DSL SlideContent。
 * 顶层结构不可用（非 slide / 无 elements）时抛错，由调用方显示中文错误。
 */
export function toEditorContent(raw: unknown, sceneId: number): {
  content: SlideContent;
  warnings: string[];
} {
  const payload = record(raw);
  const canvas = record(payload.canvas ?? payload);
  const rawElements = list(canvas.elements);
  if (!rawElements.length) {
    throw new Error('服务端返回的幻灯片内容没有 elements（可能不是幻灯片场景）。');
  }
  const warnings: string[] = [];
  const elements: PPTElement[] = [];
  for (let index = 0; index < rawElements.length; index += 1) {
    if (elements.length >= MAX_ELEMENTS) break;
    const converted = toEditorElement(rawElements[index], index, warnings);
    if (converted) elements.push(converted);
  }
  if (!elements.length) {
    throw new Error('幻灯片内容中的元素均无法识别，无法进入自由画布编辑。');
  }
  if (rawElements.length > MAX_ELEMENTS) {
    warnings.push(`原有 ${rawElements.length} 个元素，超过单页上限 ${MAX_ELEMENTS}，超出部分未载入`);
  }
  const width = num(canvas.width ?? canvas.viewportSize, CANVAS_WIDTH) || CANVAS_WIDTH;
  let height = num(canvas.height, 0);
  if (!height) {
    const ratio = num(canvas.viewportRatio, 0);
    height = ratio > 0 ? ratio * width : CANVAS_HEIGHT;
  }
  const ratio = height > 0 && width > 0 ? height / width : CANVAS_HEIGHT / CANVAS_WIDTH;
  const slide: Slide = {
    id: `slide-scene-${sceneId}`,
    viewportSize: width,
    viewportRatio: ratio,
    theme: normalizeTheme(canvas.theme ?? payload.theme),
    elements,
  };
  return { content: { type: 'slide', schemaVersion: 1, canvas: slide }, warnings };
}

/** DSL SlideContent → 知序 PATCH content（后端会再次归一化/净化）。 */
export function toBackendContent(content: SlideContent): ZhixuSlideContent {
  const slide = content.canvas;
  const width = Math.max(1, Math.round(slide.viewportSize || CANVAS_WIDTH));
  const ratio = slide.viewportRatio > 0 ? slide.viewportRatio : CANVAS_HEIGHT / CANVAS_WIDTH;
  const height = Math.round(width * ratio * 100) / 100;
  const theme = slide.theme;
  const elements = slide.elements
    .filter((element) => EDITOR_SUPPORTED_TYPES.has(String(element.type)))
    .map((element) => {
      const copy = { ...element } as Record<string, unknown>;
      // LaTeX 的 KaTeX HTML 由渲染器按需重算，后端会丢弃它；不发可减小 PATCH 体积。
      if (element.type === 'latex') delete copy.html;
      if (element.type === 'image' && typeof copy.src === 'string') {
        // 后端只接受站内相对地址；外链图片在保存时会被丢弃并告警。
        if (/^https?:\/\//i.test(copy.src) || copy.src.startsWith('data:')) delete copy.src;
      }
      return copy;
    });
  const background = slide.background && typeof slide.background === 'object'
    ? { ...slide.background }
    : undefined;
  const canvas: ZhixuSlideContent['canvas'] = {
    width,
    height,
    theme: {
      backgroundColor: color(theme.backgroundColor, DEFAULT_THEME.backgroundColor),
      themeColors: (theme.themeColors || []).map((item) => color(item, '')).filter(Boolean),
      fontColor: color(theme.fontColor, DEFAULT_THEME.fontColor),
      fontName: text(theme.fontName, DEFAULT_THEME.fontName) || DEFAULT_THEME.fontName,
    },
    elements,
  };
  // 后端只认纯色背景（background.color → theme.backgroundColor），其余类型不会持久化。
  if (background && text(background.color)) canvas.background = background;
  return { type: 'slide', schemaVersion: 1, canvas };
}

/** 把用户输入的同源图片地址规整成可持久化形式（拒绝外链 / data URL）。 */
export function normalizeImageSrc(input: string): string | null {
  const value = input.trim();
  if (!value) return null;
  if (/^(https?:)?\/\//i.test(value) || value.startsWith('data:')) return null;
  if (!value.startsWith('/')) return null;
  if (value.length > 900) return null;
  return value;
}
