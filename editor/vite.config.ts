import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const vendor = (pkg: string, entry: string) => resolve(here, 'vendor', 'openmaic', pkg, entry);

/**
 * 构建配置（P6b 自由画布编辑器）。
 *
 * - `base: './'`：产物可以挂在任意静态前缀下（知序用 `/static/editor/index.html`）。
 * - `build.outDir: ../web/editor`：构建结果直接入库，无 Node 环境的部署也能用。
 * - `@openmaic/*` alias 指向 vendored 源码（MIT，来源 commit 记录在 editor/vendor/openmaic/VENDORED.json），
 *   不依赖上游 dist（上游包 `main` 指向未提交的 dist）。
 */
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      // 离线优化：shiki 全量语言包 ~9.5MB，课堂代码元素极少，按纯文本回退。
      { find: 'shiki', replacement: resolve(here, 'src/stubs/shiki.ts') },
      // 更具体的子路径必须排在包根之前（Vite 的字符串 alias 是前缀匹配）。
      { find: '@openmaic/renderer/elements', replacement: vendor('renderer', 'src/elements/index.ts') },
      { find: '@openmaic/renderer/geometry', replacement: vendor('renderer', 'src/utils/element.ts') },
      { find: '@openmaic/renderer', replacement: vendor('renderer', 'src/index.ts') },
      { find: '@openmaic/dsl', replacement: vendor('dsl', 'src/index.ts') },
      { find: '@openmaic/geometry', replacement: vendor('geometry', 'src/index.ts') },
      { find: '@openmaic/editor/core', replacement: vendor('editor', 'src/core/index.ts') },
      { find: '@openmaic/editor/react', replacement: vendor('editor', 'src/react/index.ts') },
      { find: '@openmaic/editor/ui', replacement: vendor('editor', 'src/ui/index.ts') },
      { find: '@openmaic/editor', replacement: vendor('editor', 'src/core/index.ts') },
    ],
    extensions: ['.ts', '.tsx', '.js', '.jsx', '.json'],
  },
  build: {
    outDir: resolve(here, '..', 'web', 'editor'),
    emptyOutDir: true,
    assetsDir: 'assets',
    target: 'es2020',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts') || id.includes('zrender')) return 'charts';
            if (id.includes('shiki')) return 'code-highlight';
            if (id.includes('katex')) return 'katex';
            if (id.includes('prosemirror')) return 'prosemirror';
            if (id.includes('@openmaic')) return 'openmaic';
            return 'vendor';
          }
          if (id.includes('vendor/openmaic')) return 'openmaic';
          return undefined;
        },
      },
    },
  },
  server: {
    fs: { allow: ['..'] },
  },
});
