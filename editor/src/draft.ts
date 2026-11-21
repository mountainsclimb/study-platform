/**
 * 本地草稿（localStorage）：自动保存未提交的画布内容，防止崩溃/误关丢失。
 * 成功保存后清空；同一课程+场景只保留最新一份。
 */
import type { ZhixuSlideContent } from './types';

const PREFIX = 'zhixu-canvas-draft';

export interface CanvasDraft {
  courseId: number;
  sceneId: number;
  baseVersion: number;
  content: ZhixuSlideContent;
  savedAt: number;
  title: string;
}

export function draftKey(courseId: number, sceneId: number): string {
  return `${PREFIX}:${courseId}:${sceneId}`;
}

export function readDraft(courseId: number, sceneId: number): CanvasDraft | null {
  try {
    const raw = window.localStorage.getItem(draftKey(courseId, sceneId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CanvasDraft;
    if (!parsed || parsed.courseId !== courseId || parsed.sceneId !== sceneId) return null;
    if (!parsed.content || parsed.content.type !== 'slide') return null;
    return parsed;
  } catch {
    return null;
  }
}

export function writeDraft(draft: CanvasDraft): boolean {
  try {
    window.localStorage.setItem(draftKey(draft.courseId, draft.sceneId), JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

export function clearDraft(courseId: number, sceneId: number): void {
  try {
    window.localStorage.removeItem(draftKey(courseId, sceneId));
  } catch {
    /* localStorage 不可用时忽略 */
  }
}

export function formatClock(timestamp: number): string {
  try {
    const date = new Date(timestamp);
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(
      date.getSeconds(),
    ).padStart(2, '0')}`;
  } catch {
    return '';
  }
}
