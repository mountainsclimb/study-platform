/**
 * 知序 P6b 自由画布编辑器（React + Vite，vendored @openmaic/editor）。
 *
 * 入口参数（iframe src 由 web/js/components/classroomCanvas.js 生成）：
 *   /static/editor/index.html?course=<courseId>&scene=<sceneId>
 *
 * 数据来源：知序同源 API（cookie 会话）
 *   GET   /api/classroom/courses/{course}/scenes/{scene}/content
 *   PATCH 同上 body `{version, content}`（version CAS；409 冲突可重载或覆盖保存）
 *
 * 父页通信：`window.parent.postMessage`，协议见 ./protocol.ts。
 */
import React, {
  Component,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from 'react';
import { SlideRendererProvider } from '@openmaic/renderer';
import type { PPTElement, Slide, SlideContent } from '@openmaic/dsl';
import {
  applyEditorTransaction,
  createEditorTransaction,
  type EditorOperation,
  type EditorTransaction,
} from '@openmaic/editor/core';
import { EMPTY_SELECTION, type Selection } from '@openmaic/editor/react';
import {
  EditableSlideCanvasWithUI,
  type EditorHostCapabilities,
} from '@openmaic/editor/ui';
import { ApiError, fetchSceneContent, saveSceneContent } from './api';
import {
  MAX_ELEMENTS,
  normalizeImageSrc,
  toBackendContent,
  toEditorContent,
  type ZhixuSlideContent,
} from './types';
import { CANVAS_MESSAGE, postToParent } from './protocol';
import { clearDraft, formatClock, readDraft, writeDraft, type CanvasDraft } from './draft';
import { AssetUrlPicker } from './AssetPicker';
import { LayerPanel, PropertyPanel } from './panels';
import './app.css';

const HISTORY_LIMIT = 120;

interface History {
  past: SlideContent[];
  present: SlideContent;
  future: SlideContent[];
}

interface Notice {
  text: string;
  kind: 'info' | 'success' | 'warning' | 'error';
  sticky?: boolean;
}

interface BootData {
  courseId: number;
  sceneId: number;
  sceneTitle: string;
  version: number;
  content: SlideContent;
  warnings: string[];
  draft: CanvasDraft | null;
}

const INSERT_ITEMS = ['text', 'image', 'table', 'chart', 'line', 'background', 'latex'] as const;

const SHAPE_PRESETS = [
  { id: 'rect', label: '矩形', path: 'M 0 0 L 1 0 L 1 1 L 0 1 Z' },
  {
    id: 'roundRect',
    label: '圆角矩形',
    path: 'M 0.12 0 L 0.88 0 Q 1 0 1 0.12 L 1 0.88 Q 1 1 0.88 1 L 0.12 1 Q 0 1 0 0.88 L 0 0.12 Q 0 0 0.12 0 Z',
  },
  { id: 'ellipse', label: '圆形', path: 'M 0.5 0 A 0.5 0.5 0 1 1 0.5 1 A 0.5 0.5 0 1 1 0.5 0 Z' },
  { id: 'triangle', label: '三角形', path: 'M 0.5 0 L 1 1 L 0 1 Z' },
] as const;

const ZOOM_OPTIONS = [
  { value: 'fit', label: '适应窗口' },
  { value: '1', label: '100%' },
  { value: '0.9', label: '90%' },
  { value: '0.75', label: '75%' },
  { value: '0.5', label: '50%' },
] as const;

let elementIdSequence = 0;

function elementIds(slide: SlideContent): Set<string> {
  return new Set(slide.canvas.elements.map((element) => element.id));
}

function createElementId(type: string, taken: Set<string>): string {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    elementIdSequence += 1;
    const id = `${type}-${Date.now().toString(36)}-${elementIdSequence.toString(36)}`;
    if (!taken.has(id)) return id;
  }
  return `${type}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 按 transaction 的 history 语义推进本地历史（上限 HISTORY_LIMIT，满足 ≥100 步撤销）。 */
function applyHistory(current: History, transaction: EditorTransaction): History {
  const nextContent = applyEditorTransaction(current.present, transaction);
  if (nextContent === current.present) return current;
  if (transaction.history === 'neutral') return { ...current, present: nextContent };
  const past = [...current.past, current.present];
  return {
    past: past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past,
    present: nextContent,
    future: [],
  };
}

function parseParams(): { courseId: number; sceneId: number } {
  const params = new URLSearchParams(window.location.search);
  let courseId = Number(params.get('course')) || 0;
  let sceneId = Number(params.get('scene')) || 0;
  // srcdoc 挂载时没有查询串：父页把参数 JSON 写进 iframe 的 window.name
  // （形如 `zhixu-canvas:{"course":1,"scene":2}`）。
  if ((!courseId || !sceneId) && typeof window.name === 'string' && window.name.startsWith('zhixu-canvas:')) {
    try {
      const data = JSON.parse(window.name.slice('zhixu-canvas:'.length)) as {
        course?: unknown;
        scene?: unknown;
      };
      courseId = Number(data.course) || courseId;
      sceneId = Number(data.scene) || sceneId;
    } catch {
      /* 参数不可解析时保持 0，由上层给出中文提示 */
    }
  }
  return { courseId, sceneId };
}

/** 渲染兜底：上游 renderer 遇到异常元素时给出可读错误，而不是白屏。 */
class CanvasErrorBoundary extends Component<{ children: ReactNode; onError?: (message: string) => void }, { message: string | null }> {
  state: { message: string | null } = { message: null };

  static getDerivedStateFromError(error: unknown): { message: string } {
    return { message: error instanceof Error ? error.message : '画布渲染失败' };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error('[canvas-editor] render error', error, info);
    this.props.onError?.(error instanceof Error ? error.message : '画布渲染失败');
  }

  render(): ReactNode {
    if (this.state.message) {
      return (
        <div className="zx-render-error">
          <h3>画布渲染失败</h3>
          <p>{this.state.message}</p>
          <p className="zx-help">可关闭编辑器返回课堂；如反复出现请把该场景反馈给管理员。</p>
        </div>
      );
    }
    return this.props.children;
  }
}

interface AppState {
  phase: 'loading' | 'error' | 'ready';
  message?: string;
  authExpired?: boolean;
  data?: BootData;
}

function App() {
  const params = useMemo(parseParams, []);
  const [state, setState] = useState<AppState>({ phase: 'loading' });

  const load = useCallback(async () => {
    if (!params.courseId || !params.sceneId) {
      setState({ phase: 'error', message: '缺少 course/scene 参数：请从课堂播放器点击「自由画布编辑」进入。' });
      return;
    }
    setState({ phase: 'loading' });
    try {
      const response = await fetchSceneContent(params.courseId, params.sceneId);
      const scene = response.scene;
      if (!scene) throw new Error('服务端未返回场景内容。');
      const sceneType = String(scene.scene_type || '');
      if (sceneType !== 'slide') {
        throw new Error(`该场景类型为「${sceneType || '未知'}」，自由画布编辑器只支持幻灯片（slide）场景。`);
      }
      const converted = toEditorContent(scene.content, params.sceneId);
      const serverPayload = JSON.stringify(toBackendContent(converted.content));
      const storedDraft = readDraft(params.courseId, params.sceneId);
      const usableDraft =
        storedDraft && storedDraft.baseVersion === Number(scene.version) && JSON.stringify(storedDraft.content) !== serverPayload
          ? storedDraft
          : null;
      setState({
        phase: 'ready',
        data: {
          courseId: params.courseId,
          sceneId: params.sceneId,
          sceneTitle: String(scene.title || '') || '未命名幻灯片',
          version: Number(scene.version) || 1,
          content: converted.content,
          warnings: [...(Array.isArray(response.warnings) ? response.warnings : []), ...converted.warnings],
          draft: usableDraft,
        },
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setState({ phase: 'error', message: error.message, authExpired: true });
        return;
      }
      setState({
        phase: 'error',
        message: error instanceof Error ? error.message : '载入幻灯片失败，请重试。',
      });
    }
  }, [params.courseId, params.sceneId]);

  useEffect(() => {
    void load();
  }, [load]);

  const closeEditor = useCallback(() => {
    postToParent({ type: CANVAS_MESSAGE.close });
  }, []);

  if (state.phase === 'loading') {
    return (
      <div className="zx-boot">
        <div className="zx-spinner" aria-hidden="true" />
        <p>正在载入幻灯片…</p>
      </div>
    );
  }
  if (state.phase === 'error') {
    return (
      <div className="zx-boot zx-boot-error" role="alert">
        <h2>无法打开自由画布编辑器</h2>
        <p>{state.message}</p>
        <div className="zx-row">
          {state.authExpired ? null : (
            <button type="button" className="zx-btn zx-btn-primary" onClick={() => void load()}>
              重试
            </button>
          )}
          <button type="button" className="zx-btn" onClick={closeEditor}>
            返回课堂
          </button>
        </div>
      </div>
    );
  }
  return <Workspace key={`${state.data!.courseId}:${state.data!.sceneId}`} data={state.data!} reload={load} />;
}

function Workspace({ data, reload }: { data: BootData; reload: () => Promise<void> }) {
  const [history, setHistory] = useState<History>(() => ({ past: [], present: data.content, future: [] }));
  const historyRef = useRef(history);
  const [version, setVersionState] = useState(data.version);
  const versionRef = useRef(version);
  const [savedJson, setSavedJsonState] = useState(() => JSON.stringify(toBackendContent(data.content)));
  const savedJsonRef = useRef(savedJson);
  const [selection, setSelection] = useState<Selection>(EMPTY_SELECTION);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const noticeTimerRef = useRef<number | null>(null);
  const [conflict, setConflict] = useState<number | null>(null);
  const [reloading, setReloading] = useState(false);
  const [authExpired, setAuthExpired] = useState(false);
  const [draftPrompt, setDraftPrompt] = useState<CanvasDraft | null>(data.draft);
  const [draftSavedAt, setDraftSavedAt] = useState<number | null>(null);
  const [zoom, setZoom] = useState<string>('fit');
  const [actualScale, setActualScale] = useState(1);
  const [sidebarVisible, setSidebarVisible] = useState(() => (typeof window !== 'undefined' ? window.innerWidth >= 1180 : true));
  const [sidebarTab, setSidebarTab] = useState<'layers' | 'props'>('layers');
  const [renderError, setRenderError] = useState<string | null>(null);

  const commitHistory = useCallback((next: History) => {
    historyRef.current = next;
    setHistory(next);
  }, []);

  const setVersion = useCallback((next: number) => {
    versionRef.current = next;
    setVersionState(next);
  }, []);

  const setSaved = useCallback((next: string) => {
    savedJsonRef.current = next;
    setSavedJsonState(next);
  }, []);

  const pushNotice = useCallback((text: string, kind: Notice['kind'] = 'info', sticky = false) => {
    setNotice({ text, kind, sticky });
  }, []);

  useEffect(() => {
    if (!notice || notice.sticky) return;
    if (noticeTimerRef.current) window.clearTimeout(noticeTimerRef.current);
    noticeTimerRef.current = window.setTimeout(() => setNotice(null), notice.kind === 'success' ? 3200 : 6000);
    return () => {
      if (noticeTimerRef.current) window.clearTimeout(noticeTimerRef.current);
    };
  }, [notice]);

  // 挂载完成通知父页（父页可据此隐藏加载态）。
  useEffect(() => {
    postToParent({ type: CANVAS_MESSAGE.ready });
    if (data.warnings.length) {
      pushNotice(`载入提示：${data.warnings.join('；')}`, 'warning', true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const payload = useMemo(() => toBackendContent(history.present), [history.present]);
  const payloadJson = useMemo(() => JSON.stringify(payload), [payload]);
  const dirty = payloadJson !== savedJson;

  useEffect(() => {
    postToParent({ type: CANVAS_MESSAGE.dirty, dirty });
  }, [dirty]);

  // 自动保存本地草稿（防崩溃/误关丢失）；保存成功后清空。
  useEffect(() => {
    if (!dirty) return undefined;
    const timer = window.setTimeout(() => {
      const ok = writeDraft({
        courseId: data.courseId,
        sceneId: data.sceneId,
        baseVersion: versionRef.current,
        content: payload,
        savedAt: Date.now(),
        title: data.sceneTitle,
      });
      if (ok) setDraftSavedAt(Date.now());
    }, 800);
    return () => window.clearTimeout(timer);
  }, [dirty, payloadJson, data.courseId, data.sceneId, data.sceneTitle]);

  const elementIdSet = useMemo(() => elementIds(history.present), [history.present]);
  const elements = history.present.canvas.elements;

  // 删除/撤销后清理已不存在的选中项。
  useEffect(() => {
    setSelection((previous) => {
      const kept = previous.elementIds.filter((id) => elementIdSet.has(id));
      if (kept.length === previous.elementIds.length) return previous;
      return {
        elementIds: kept,
        primaryId: kept[0],
        editingId: previous.editingId && kept.includes(previous.editingId) ? previous.editingId : undefined,
      };
    });
  }, [elementIdSet]);

  const handleTransaction = useCallback(
    (transaction: EditorTransaction) => {
      const current = historyRef.current;
      const next = applyHistory(current, transaction);
      if (next === current) return;
      if (next.present.canvas.elements.length > MAX_ELEMENTS) {
        pushNotice(`单页最多 ${MAX_ELEMENTS} 个元素，已忽略本次添加。`, 'warning');
        return;
      }
      commitHistory(next);
    },
    [commitHistory, pushNotice],
  );

  const commitOperations = useCallback(
    (operations: EditorOperation[], options?: { history?: 'record' | 'neutral' }) => {
      if (!operations.length) return;
      try {
        const transaction = createEditorTransaction({
          origin: 'toolbar',
          history: options?.history ?? 'record',
          operations,
        });
        handleTransaction(transaction);
      } catch (error) {
        pushNotice(`操作失败：${error instanceof Error ? error.message : '未知错误'}`, 'error');
      }
    },
    [handleTransaction, pushNotice],
  );

  const undo = useCallback(() => {
    const current = historyRef.current;
    if (!current.past.length) return;
    const previous = current.past[current.past.length - 1];
    commitHistory({
      past: current.past.slice(0, -1),
      present: previous,
      future: [current.present, ...current.future],
    });
  }, [commitHistory]);

  const redo = useCallback(() => {
    const current = historyRef.current;
    if (!current.future.length) return;
    const nextContent = current.future[0];
    commitHistory({
      past: [...current.past, current.present].slice(-HISTORY_LIMIT),
      present: nextContent,
      future: current.future.slice(1),
    });
  }, [commitHistory]);

  const selectedElements = useMemo(
    () => elements.filter((element) => selection.elementIds.includes(element.id)),
    [elements, selection.elementIds],
  );
  const primaryElement = useMemo(
    () => elements.find((element) => element.id === selection.primaryId) || selectedElements[0] || null,
    [elements, selection.primaryId, selectedElements],
  );
  const primaryLocked = Boolean((primaryElement as { lock?: boolean } | null)?.lock);

  const handleSelectElement = useCallback((id: string, additive: boolean) => {
    setSelection((previous) => {
      if (!additive) return { elementIds: [id], primaryId: id };
      const exists = previous.elementIds.includes(id);
      const ids = exists ? previous.elementIds.filter((item) => item !== id) : [...previous.elementIds, id];
      return { elementIds: ids, primaryId: ids[ids.length - 1] };
    });
  }, []);

  /** 生成删除/复制/层级等操作时统一从最新 history 读取元素。 */
  const currentElements = useCallback(() => historyRef.current.present.canvas.elements, []);

  const deleteSelection = useCallback(() => {
    const ids = selection.elementIds;
    if (!ids.length) return;
    if (currentElements().length - ids.length < 1) {
      pushNotice('幻灯片至少需要保留 1 个元素。', 'warning');
      return;
    }
    commitOperations([{ type: 'element.deleteMany', elementIds: [...ids] }]);
  }, [commitOperations, currentElements, pushNotice, selection.elementIds]);

  const duplicateSelection = useCallback(() => {
    const ids = selection.elementIds;
    if (!ids.length) return;
    if (currentElements().length + ids.length > MAX_ELEMENTS) {
      pushNotice(`单页最多 ${MAX_ELEMENTS} 个元素。`, 'warning');
      return;
    }
    const taken = elementIds(historyRef.current.present);
    const idMap: Record<string, string> = {};
    for (const id of ids) {
      const source = currentElements().find((element) => element.id === id);
      if (!source) continue;
      const nextId = createElementId(source.type, taken);
      taken.add(nextId);
      idMap[id] = nextId;
    }
    const operations: EditorOperation[] = [{ type: 'element.duplicate', elementIds: [...ids], idMap }];
    commitOperations(operations);
    setSelection({ elementIds: Object.values(idMap), primaryId: Object.values(idMap)[0] });
  }, [commitOperations, currentElements, pushNotice, selection.elementIds]);

  const toggleLock = useCallback(() => {
    if (!primaryElement) return;
    commitOperations([
      { type: 'element.update', elementId: primaryElement.id, patch: { lock: !primaryLocked } as Record<string, unknown> },
    ]);
  }, [commitOperations, primaryElement, primaryLocked]);

  const reorderSelection = useCallback(
    (command: 'front' | 'forward' | 'backward' | 'back') => {
      const list = currentElements();
      const selected = new Set(selection.elementIds);
      const indexes = list.map((element, index) => (selected.has(element.id) ? index : -1)).filter((index) => index >= 0);
      if (!indexes.length) return;
      const operations: EditorOperation[] = [];
      if (command === 'front') {
        for (const index of indexes) operations.push({ type: 'element.reorder', elementId: list[index].id, index: list.length - 1 });
      } else if (command === 'back') {
        for (const index of [...indexes].reverse()) operations.push({ type: 'element.reorder', elementId: list[index].id, index: 0 });
      } else if (command === 'forward') {
        for (const index of [...indexes].reverse()) {
          operations.push({ type: 'element.reorder', elementId: list[index].id, index: Math.min(list.length - 1, index + 1) });
        }
      } else {
        for (const index of indexes) {
          operations.push({ type: 'element.reorder', elementId: list[index].id, index: Math.max(0, index - 1) });
        }
      }
      commitOperations(operations);
    },
    [commitOperations, currentElements, selection.elementIds],
  );

  const patchElement = useCallback(
    (patch: Record<string, unknown>) => {
      if (!primaryElement) return;
      commitOperations([{ type: 'element.update', elementId: primaryElement.id, patch: patch as never }]);
    },
    [commitOperations, primaryElement],
  );

  const patchTheme = useCallback(
    (backgroundColor: string) => {
      const slide = historyRef.current.present.canvas;
      commitOperations([
        {
          type: 'slide.update',
          patch: {
            theme: { ...slide.theme, backgroundColor },
            background: { type: 'solid', color: backgroundColor },
          },
        },
      ]);
    },
    [commitOperations],
  );

  const applyImageSrc = useCallback(
    (raw: string) => {
      const src = normalizeImageSrc(raw);
      if (!src) {
        pushNotice('图片只支持站内相对地址（以 / 开头，如 /api/classroom/assets/12）。', 'warning');
        return;
      }
      patchElement({ src });
    },
    [patchElement, pushNotice],
  );

  const insertShape = useCallback(
    (preset: (typeof SHAPE_PRESETS)[number]) => {
      const slide = historyRef.current.present.canvas;
      if (slide.elements.length >= MAX_ELEMENTS) {
        pushNotice(`单页最多 ${MAX_ELEMENTS} 个元素。`, 'warning');
        return;
      }
      const taken = elementIds(historyRef.current.present);
      const id = createElementId('shape', taken);
      const offset = (slide.elements.length % 6) * 22;
      const element = {
        id,
        type: 'shape',
        left: 140 + offset,
        top: 120 + offset,
        width: 260,
        height: 160,
        rotate: 0,
        path: preset.path,
        viewBox: [1, 1],
        fill: slide.theme.themeColors[0] || '#1f6feb',
        fixedRatio: false,
      } as unknown as PPTElement;
      commitOperations([{ type: 'element.add', element, index: slide.elements.length }]);
      setSelection({ elementIds: [id], primaryId: id });
    },
    [commitOperations, pushNotice],
  );

  const doSave = useCallback(
    async (options: { close?: boolean; version?: number } = {}): Promise<boolean> => {
      if (savingRef.current) return false;
      const current = historyRef.current.present;
      if (!current.canvas.elements.length) {
        pushNotice('幻灯片至少需要保留 1 个元素才能保存。', 'error');
        return false;
      }
      const useVersion = options.version ?? versionRef.current;
      const content = toBackendContent(current);
      savingRef.current = true;
      setSaving(true);
      try {
        const result = await saveSceneContent(data.courseId, data.sceneId, useVersion, content);
        const nextVersion = Number(result.scene?.version) || useVersion + 1;
        setVersion(nextVersion);
        setConflict(null);
        // 用服务端归一化后的内容替换本地（保证本地与持久化一致；组合/锁定等
        // 后端 schema 白名单之外的扩展属性会在这一步被收回，属已知限制）。
        let savedPayload = JSON.stringify(content);
        try {
          const normalized = toEditorContent(result.scene?.content, data.sceneId).content;
          const normalizedPayload = toBackendContent(normalized);
          commitHistory({ ...historyRef.current, present: normalized });
          savedPayload = JSON.stringify(normalizedPayload);
        } catch {
          /* 归一化内容不可用时保留本地内容，按已发送内容作为基线 */
        }
        setSaved(savedPayload);
        clearDraft(data.courseId, data.sceneId);
        setDraftSavedAt(null);
        setDraftPrompt(null);
        const warnings = Array.isArray(result.warnings) ? result.warnings : [];
        if (warnings.length) {
          pushNotice(`已保存（版本 ${nextVersion}）。服务端校验调整：${warnings.join('；')}`, 'warning', true);
        } else {
          pushNotice(`已保存（版本 ${nextVersion}）`, 'success');
        }
        postToParent({ type: CANVAS_MESSAGE.saved, version: nextVersion, close: Boolean(options.close) });
        if (options.close) postToParent({ type: CANVAS_MESSAGE.close });
        return true;
      } catch (error) {
        if (error instanceof ApiError) {
          if (error.status === 401) {
            setAuthExpired(true);
            pushNotice(error.message, 'error', true);
          } else if (error.status === 409) {
            setConflict(error.serverVersion);
            pushNotice(`保存冲突：${error.message}`, 'warning', true);
          } else {
            pushNotice(`保存失败：${error.message}`, 'error', true);
          }
        } else {
          pushNotice(`保存失败：${error instanceof Error ? error.message : '未知错误'}`, 'error', true);
        }
        return false;
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    },
    [commitHistory, data.courseId, data.sceneId, pushNotice, setSaved, setVersion],
  );

  const reloadFromServer = useCallback(async () => {
    setReloading(true);
    try {
      const response = await fetchSceneContent(data.courseId, data.sceneId);
      const converted = toEditorContent(response.scene.content, data.sceneId);
      const normalizedPayload = toBackendContent(converted.content);
      commitHistory({ past: [], present: converted.content, future: [] });
      setVersion(Number(response.scene.version) || versionRef.current);
      setSaved(JSON.stringify(normalizedPayload));
      setConflict(null);
      clearDraft(data.courseId, data.sceneId);
      setDraftPrompt(null);
      pushNotice(`已重新载入服务端版本（版本 ${Number(response.scene.version) || versionRef.current}）`, 'success');
    } catch (error) {
      pushNotice(`重新载入失败：${error instanceof Error ? error.message : '未知错误'}`, 'error', true);
    } finally {
      setReloading(false);
    }
  }, [commitHistory, data.courseId, data.sceneId, pushNotice, setSaved, setVersion]);

  const requestClose = useCallback(() => {
    const currentPayload = JSON.stringify(toBackendContent(historyRef.current.present));
    if (currentPayload !== savedJsonRef.current) {
      const confirmed = window.confirm('当前修改尚未保存，确定放弃并关闭编辑器吗？');
      if (!confirmed) return;
    }
    postToParent({ type: CANVAS_MESSAGE.close });
  }, []);

  const restoreDraft = useCallback(() => {
    if (!draftPrompt) return;
    try {
      const converted = toEditorContent(draftPrompt.content, data.sceneId).content;
      commitHistory({
        past: [...historyRef.current.past, historyRef.current.present].slice(-HISTORY_LIMIT),
        present: converted,
        future: [],
      });
      pushNotice(`已恢复 ${formatClock(draftPrompt.savedAt)} 的本地草稿，请确认后保存。`, 'info', true);
    } catch (error) {
      pushNotice(`草稿无法恢复：${error instanceof Error ? error.message : '内容不可用'}`, 'warning');
    }
    setDraftPrompt(null);
  }, [commitHistory, data.sceneId, draftPrompt, pushNotice]);

  const discardDraft = useCallback(() => {
    clearDraft(data.courseId, data.sceneId);
    setDraftPrompt(null);
  }, [data.courseId, data.sceneId]);

  // 全局快捷键：撤销/重做/保存（文本编辑中不拦截，交给 ProseMirror）。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const editable = Boolean(
        target &&
          typeof target.closest === 'function' &&
          target.closest('input, textarea, select, [contenteditable="true"], .ProseMirror'),
      );
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();
      if (mod && !event.altKey && key === 'z') {
        if (editable) return;
        event.preventDefault();
        if (event.shiftKey) redo();
        else undo();
      } else if (mod && !event.altKey && key === 'y') {
        if (editable) return;
        event.preventDefault();
        redo();
      } else if (mod && !event.altKey && key === 's') {
        event.preventDefault();
        void doSave();
      } else if (event.key === 'Escape' && notice) {
        setNotice(null);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [doSave, notice, redo, undo]);

  const host = useMemo<EditorHostCapabilities>(
    () => ({
      locale: 'zh-CN',
      createElementId: (type) => createElementId(type, elementIds(historyRef.current.present)),
      renderAssetPicker: (request) => <AssetUrlPicker request={request} />,
      onError: (error) => pushNotice(`资源不可用：${error.message}`, 'warning'),
      shortcutsEnabled: true,
    }),
    [pushNotice],
  );

  const canvas = history.present.canvas;
  const zoomScale = zoom === 'fit' ? undefined : Number(zoom);
  const zoomLabel = zoom === 'fit' ? `${Math.round(actualScale * 100)}%` : `${Math.round(Number(zoom) * 100)}%`;

  return (
    <div className="zx-app">
      <header className="zx-header">
        <div className="zx-header-left">
          <strong className="zx-title">自由画布编辑</strong>
          <span className="zx-scene-title" title={data.sceneTitle}>
            {data.sceneTitle}
          </span>
          <span className="zx-badge" title="当前场景版本（保存后递增）">
            v{version}
          </span>
          <span className={`zx-status ${dirty ? 'is-dirty' : 'is-saved'}`}>
            {saving ? '保存中…' : dirty ? '未保存' : '已保存'}
          </span>
        </div>
        <div className="zx-header-actions">
          <div className="zx-group" role="group" aria-label="撤销重做">
            <button type="button" className="zx-btn" disabled={!history.past.length} onClick={undo} title="撤销（Ctrl+Z）">
              ↶ 撤销
            </button>
            <button type="button" className="zx-btn" disabled={!history.future.length} onClick={redo} title="重做（Ctrl+Shift+Z / Ctrl+Y）">
              ↷ 重做
            </button>
          </div>
          <label className="zx-inline-field" title="画布缩放">
            <span>缩放</span>
            <select value={zoom} onChange={(event) => setZoom(event.target.value)}>
              {ZOOM_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.value === 'fit' ? `适应窗口（${zoomLabel}）` : option.label}
                </option>
              ))}
            </select>
          </label>
          <div className="zx-group" role="group" aria-label="插入形状">
            <span className="zx-group-label">插入形状</span>
            {SHAPE_PRESETS.map((preset) => (
              <button key={preset.id} type="button" className="zx-btn zx-btn-sm" onClick={() => insertShape(preset)} title={`插入${preset.label}`}>
                {preset.label}
              </button>
            ))}
          </div>
          <button type="button" className="zx-btn" onClick={() => setSidebarVisible((visible) => !visible)}>
            {sidebarVisible ? '收起图层/属性' : '图层/属性'}
          </button>
          <div className="zx-group zx-save-group">
            <button type="button" className="zx-btn zx-btn-primary" disabled={saving} onClick={() => void doSave()}>
              保存
            </button>
            <button type="button" className="zx-btn zx-btn-primary-outline" disabled={saving} onClick={() => void doSave({ close: true })}>
              保存并关闭
            </button>
            <button type="button" className="zx-btn" onClick={requestClose}>
              关闭
            </button>
          </div>
        </div>
      </header>

      {conflict !== null ? (
        <div className="zx-banner is-warning" role="alert">
          <span>
            内容已在其他窗口/标签页被修改（服务端当前版本 {conflict}，本地基于 v{version}）。请选择重新载入，或使用
            <strong>「覆盖保存」</strong>把当前编辑内容写入最新版本。
          </span>
          <span className="zx-banner-actions">
            <button type="button" className="zx-btn zx-btn-sm" disabled={reloading} onClick={() => void reloadFromServer()}>
              {reloading ? '载入中…' : '重新载入服务端版本'}
            </button>
            <button
              type="button"
              className="zx-btn zx-btn-sm zx-btn-primary"
              disabled={saving}
              onClick={() => void doSave({ version: conflict })}
            >
              覆盖保存（以 v{conflict} 为基线）
            </button>
          </span>
        </div>
      ) : null}

      {draftPrompt ? (
        <div className="zx-banner is-info" role="status">
          <span>
            检测到本地草稿：{formatClock(draftPrompt.savedAt)} 自动保存（基于版本 {draftPrompt.baseVersion}）。
          </span>
          <span className="zx-banner-actions">
            <button type="button" className="zx-btn zx-btn-sm zx-btn-primary" onClick={restoreDraft}>
              恢复草稿
            </button>
            <button type="button" className="zx-btn zx-btn-sm" onClick={discardDraft}>
              丢弃草稿
            </button>
          </span>
        </div>
      ) : null}

      {notice ? (
        <div className={`zx-banner is-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>
          <span>{notice.text}</span>
          <span className="zx-banner-actions">
            <button type="button" className="zx-btn zx-btn-sm" onClick={() => setNotice(null)}>
              知道了
            </button>
          </span>
        </div>
      ) : null}

      <div className="zx-body">
        <main className="zx-canvas-host">
          {renderError ? (
            <div className="zx-render-error">
              <h3>画布渲染失败</h3>
              <p>{renderError}</p>
              <button type="button" className="zx-btn" onClick={() => void reload()}>
                重新载入场景
              </button>
            </div>
          ) : (
            <CanvasErrorBoundary onError={setRenderError}>
              <SlideRendererProvider slide={canvas} classroomSizing>
                <EditableSlideCanvasWithUI
                  slide={canvas}
                  documentSlide={canvas}
                  host={host}
                  selection={selection}
                  onSelectionChange={setSelection}
                  onTransaction={handleTransaction}
                  insertItems={INSERT_ITEMS}
                  scale={zoomScale}
                  onScaleChange={setActualScale}
                  snapping
                  className="zx-canvas"
                />
              </SlideRendererProvider>
            </CanvasErrorBoundary>
          )}
        </main>
        {sidebarVisible ? (
          <aside className="zx-sidebar" aria-label="图层与属性面板">
            <div className="zx-sidebar-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={sidebarTab === 'layers'}
                className={sidebarTab === 'layers' ? 'is-active' : ''}
                onClick={() => setSidebarTab('layers')}
              >
                图层（{elements.length}）
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={sidebarTab === 'props'}
                className={sidebarTab === 'props' ? 'is-active' : ''}
                onClick={() => setSidebarTab('props')}
              >
                属性
              </button>
            </div>
            <div className="zx-sidebar-body">
              {sidebarTab === 'layers' ? (
                <LayerPanel
                  elements={elements}
                  selectedIds={selection.elementIds}
                  onSelect={handleSelectElement}
                  onReorder={reorderSelection}
                  onDuplicate={duplicateSelection}
                  onDelete={deleteSelection}
                  onToggleLock={toggleLock}
                  hasSelection={selection.elementIds.length > 0}
                  selectedLocked={primaryLocked}
                />
              ) : (
                <PropertyPanel
                  slide={canvas}
                  element={primaryElement}
                  onPatchElement={patchElement}
                  onPatchTheme={patchTheme}
                  onDelete={deleteSelection}
                  onDuplicate={duplicateSelection}
                  onImageSrc={applyImageSrc}
                />
              )}
            </div>
          </aside>
        ) : null}
      </div>

      <footer className="zx-footer">
        <span>
          操作：拖拽选中元素后的蓝色边框移动 / 8 向手柄缩放 / 双击编辑文字或形状 / Shift 框选多选 /
          右键菜单（复制、层级、组合、对齐）/ Ctrl+C、V / Ctrl+Z 撤销 / Ctrl+S 保存
        </span>
        <span className="zx-footer-right">
          {draftSavedAt ? `草稿自动保存于 ${formatClock(draftSavedAt)}` : dirty ? '修改未保存' : ''}
        </span>
      </footer>

      {authExpired ? (
        <div className="zx-auth-overlay" role="alert">
          <div className="zx-auth-card">
            <h2>登录状态已过期</h2>
            <p>请返回知序页面重新登录后再编辑。当前未保存的修改已自动保存为本地草稿。</p>
            <button type="button" className="zx-btn zx-btn-primary" onClick={requestClose}>
              返回课堂
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default App;
