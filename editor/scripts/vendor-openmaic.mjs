#!/usr/bin/env node
/**
 * 把 OpenMAIC 的只读源码 vendored 到本仓库（P6b 自由画布编辑器）。
 *
 * 来源：https://github.com/THU-MAIC/OpenMAIC
 * commit：18b68aea3a87d7626e805b136d9b908eb277b00a
 * 许可：MIT（Copyright (c) 2026 THU-MAIC），见各包 LICENSE。
 *
 * 只做两件事：
 *  1) 复制 packages/@openmaic/{dsl,geometry,renderer,editor}/ 的 src、LICENSE、README、package.json；
 *  2) 把源码里 ESM 风格的相对导入扩展名 `.js` 去掉（例如 `./slides.js` -> `./slides`），
 *     让 Vite 按 `.ts/.tsx` 解析。不修改任何运行时语义。
 *
 * 用法：node scripts/vendor-openmaic.mjs [OPENMAIC_ROOT]
 * 默认 OPENMAIC_ROOT = <repo>/../OpenMAIC（相对 editor/scripts 解析）。
 */
import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const EDITOR_ROOT = resolve(HERE, '..');
const DEFAULT_SOURCE = resolve(EDITOR_ROOT, '..', '..', 'OpenMAIC');
const SOURCE = resolve(process.argv[2] || process.env.OPENMAIC_ROOT || DEFAULT_SOURCE);
const DEST = join(EDITOR_ROOT, 'vendor', 'openmaic');
const PACKAGES = ['dsl', 'geometry', 'renderer', 'editor'];

function assertSource() {
  const missing = PACKAGES.filter((name) => !existsSync(join(SOURCE, 'packages', '@openmaic', name, 'src')));
  if (missing.length) {
    throw new Error(
      `OpenMAIC 源码目录不可用：${SOURCE}\n缺少包：${missing.join(', ')}\n` +
        '可传入参数：node scripts/vendor-openmaic.mjs E:/path/to/OpenMAIC',
    );
  }
}

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

/** 去掉相对导入的 `.js` 扩展名（保留 @openmaic/* 与第三方包名不动）。 */
function rewriteRelativeJsImports(file) {
  const before = readFileSync(file, 'utf8');
  const after = before.replace(
    /((?:from|import)\s*\(?\s*['"])(\.\.?\/[^'"]*?)\.js(['"][^;]*?;?)/g,
    '$1$2$3',
  );
  if (after !== before) writeFileSync(file, after, 'utf8');
  return before !== after;
}

function copyPackage(name) {
  const from = join(SOURCE, 'packages', '@openmaic', name);
  const to = join(DEST, name);
  rmSync(to, { recursive: true, force: true });
  mkdirSync(to, { recursive: true });
  cpSync(join(from, 'src'), join(to, 'src'), { recursive: true });
  for (const extra of ['LICENSE', 'README.md', 'package.json']) {
    if (existsSync(join(from, extra))) cpSync(join(from, extra), join(to, extra));
  }
  let rewritten = 0;
  let files = 0;
  for (const file of walk(join(to, 'src'))) {
    if (!/\.(ts|tsx)$/.test(file)) continue;
    files += 1;
    if (rewriteRelativeJsImports(file)) rewritten += 1;
  }
  return { files, rewritten };
}

function gitCommit() {
  try {
    return execFileSync('git', ['-C', SOURCE, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  } catch {
    return 'unknown';
  }
}

function dirSize(dir) {
  let total = 0;
  for (const file of walk(dir)) total += statSync(file).size;
  return total;
}

assertSource();
rmSync(DEST, { recursive: true, force: true });
mkdirSync(DEST, { recursive: true });
const summary = {};
for (const name of PACKAGES) {
  summary[name] = copyPackage(name);
  console.log(`vendored ${name}: ${summary[name].files} files, ${summary[name].rewritten} import paths rewritten`);
}
const manifest = {
  source: 'https://github.com/THU-MAIC/OpenMAIC',
  commit: gitCommit(),
  license: 'MIT',
  copyright: 'Copyright (c) 2026 THU-MAIC',
  packages: summary,
  bytes: dirSize(DEST),
  note: '只允许改 import 路径 / 构建配置；运行语义与上游一致。',
};
writeFileSync(join(DEST, 'VENDORED.json'), JSON.stringify(manifest, null, 2) + '\n', 'utf8');
console.log(`done -> ${relative(EDITOR_ROOT, DEST)} (${(manifest.bytes / 1024).toFixed(1)} KiB, commit ${manifest.commit})`);
