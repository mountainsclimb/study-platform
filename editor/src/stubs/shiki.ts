/**
 * `shiki` 离线替代实现（构建优化）。
 *
 * 上游 renderer 的代码元素会 `import('shiki')` 做语法高亮；shiki 全量语言包会把
 * 构建产物推向 ~9.5MB（gzip 1.6MB）。知序课堂幻灯片以文本/图片/公式为主，代码元素
 * 极少且离线部署不需要高亮，因此这里提供一个 resolve 后抛错的轻量实现：
 * renderer 内部对 `codeToHtml` 有 try/catch，会回退成「纯文本代码行」，功能不缺失。
 */
export function createHighlighter(): Promise<Record<string, unknown>> {
  return Promise.resolve({
    getLoadedLanguages(): string[] {
      return [];
    },
    codeToHtml(): string {
      throw new Error('离线构建未包含语法高亮引擎，代码按纯文本显示。');
    },
  });
}
