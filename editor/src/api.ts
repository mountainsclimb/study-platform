/**
 * 编辑器内的同源 API 调用（cookie 会话；不使用任何外链）。
 *
 * - GET   `/api/classroom/courses/{course}/scenes/{scene}/content` → {scene:{...,version,content},warnings}
 * - PATCH 同上 body `{version, content}`（version 做 CAS；409 时 detail.version 为服务端当前版本）
 *
 * 错误统一抛 `ApiError`（带 HTTP 状态与 detail），由 UI 转成中文提示。
 */
import type { SceneContentResponse } from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(messageOf(detail, status));
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }

  /** 409 冲突时服务端当前版本（用于「覆盖保存」重试）。 */
  get serverVersion(): number | null {
    const detail = this.detail;
    if (detail && typeof detail === 'object' && 'version' in detail) {
      const version = Number((detail as { version?: unknown }).version);
      return Number.isFinite(version) && version > 0 ? version : null;
    }
    return null;
  }
}

function messageOf(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === 'string' && record.message.trim()) return record.message.trim();
    if (Array.isArray(record)) {
      const joined = record
        .map((item) => (item && typeof item === 'object' ? String((item as { msg?: unknown }).msg ?? '') : String(item)))
        .filter(Boolean)
        .join('；');
      if (joined) return joined;
    }
  }
  if (status === 401) return '登录状态已过期，请返回知序页面重新登录。';
  if (status === 403) return '没有权限编辑该课堂。';
  if (status === 404) return '课程或场景不存在（可能已被删除）。';
  if (status === 409) return '内容已被其他窗口修改（版本冲突）。';
  if (status === 422) return '内容未通过服务端校验，请检查后重试。';
  return `请求失败（HTTP ${status}），请稍后重试。`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'X-Requested-With': 'XMLHttpRequest',
    ...(init.headers as Record<string, string> | undefined),
  };
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers,
      cache: 'no-store',
    });
  } catch {
    throw new ApiError(0, '网络连接失败，请确认本机知序服务正常运行。');
  }
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const body = (payload ?? {}) as Record<string, unknown>;
  if (!response.ok) {
    throw new ApiError(response.status, body.detail ?? body.error ?? body);
  }
  return body as T;
}

export const SCENE_CONTENT_BASE = '/api/classroom/courses';

export function sceneContentPath(courseId: number, sceneId: number): string {
  return `${SCENE_CONTENT_BASE}/${courseId}/scenes/${sceneId}/content`;
}

export function fetchSceneContent(courseId: number, sceneId: number): Promise<SceneContentResponse> {
  return request<SceneContentResponse>(sceneContentPath(courseId, sceneId));
}

export interface SaveResult {
  scene: {
    id: number;
    version: number;
    content: unknown;
  };
  warnings?: string[];
}

export function saveSceneContent(
  courseId: number,
  sceneId: number,
  version: number,
  content: unknown,
): Promise<SaveResult> {
  return request<SaveResult>(sceneContentPath(courseId, sceneId), {
    method: 'PATCH',
    body: JSON.stringify({ version, content }),
  });
}
