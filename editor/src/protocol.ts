/**
 * 父页 ↔ iframe 编辑器的 postMessage 协议常量。
 *
 * 协议（同源，`event.origin === location.origin`；父页会校验 `event.source === iframe.contentWindow`）：
 *   iframe → 父页
 *     `{ type: 'zhixu-slide-ready' }`                    编辑器挂载完成（父页可隐藏加载态）
 *     `{ type: 'zhixu-slide-saved', version, close }`    PATCH 保存成功；close=true 表示「保存并关闭」
 *     `{ type: 'zhixu-slide-dirty', dirty }`             未保存变更状态（可选，父页提示可关闭拦截）
 *     `{ type: 'zhixu-slide-close' }`                    用户关闭编辑器（未保存时已在内部确认）
 *
 * 与 `web/js/components/classroomCanvas.js` 中的常量必须保持一致（tests/test_classroom_canvas.py 校验）。
 */
export const CANVAS_MESSAGE = {
  ready: 'zhixu-slide-ready',
  saved: 'zhixu-slide-saved',
  dirty: 'zhixu-slide-dirty',
  close: 'zhixu-slide-close',
} as const;

/**
 * 父页的 origin。
 *
 * 编辑器有两种打开方式：
 * 1. 普通页面（`/static/editor/index.html?course=&scene=`）：`location.origin` 就是站内 origin；
 * 2. 父页 iframe `srcdoc`（知序平台的 CSP 是 `frame-ancestors 'none'`，网络 URL 无法被 iframe 嵌入，
 *    因此父页把 index.html 读入后用 srcdoc 挂载）：srcdoc 文档的 `location.origin` 是字符串 `"null"`，
 *    但 `document.baseURI` 继承父页 URL，可据此还原真实同源 origin。
 */
export function parentOrigin(): string {
  try {
    const base = new URL(document.baseURI);
    if (base.protocol === 'http:' || base.protocol === 'https:') return base.origin;
  } catch {
    /* fallthrough */
  }
  return window.location.origin;
}

/** 向父页发送消息；独立打开（无父页）时静默忽略。 */
export function postToParent(payload: Record<string, unknown>): void {
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(payload, parentOrigin());
    }
  } catch {
    /* 父页不可用时忽略（独立调试场景） */
  }
}
