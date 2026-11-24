#!/usr/bin/env node
/* 构建 markmap 交互脑图的单文件 vendor 产物（方案 §6.4）。
 *
 * markmap 的 npm 包没有自包含浏览器产物（入口依赖 d3 与 markmap-common），
 * 因此在开发机上做一次性打包，产物随仓库分发：
 *   node scripts/build_markmap_bundle.mjs           构建并写出 web/vendor/markmap/
 *   node scripts/build_markmap_bundle.mjs --check   重建到临时目录并与磁盘产物比对
 *
 * 依赖版本全部锁定，保证重建字节一致；esbuild 只在构建期使用，不进分发产物。
 */
import { createHash } from 'node:crypto';
import { copyFile, mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const VENDOR_DIR = path.join(ROOT, 'web', 'vendor', 'markmap');
const OUTPUT = path.join(VENDOR_DIR, 'markmap-view.iife.min.js');
const LOCK_DIR = path.join(ROOT, 'scripts', 'markmap-bundle');

const PINNED = {
  'markmap-view': '0.18.13',
  'markmap-common': '0.18.9',
  d3: '7.9.2',
  '@babel/runtime': '7.29.15',
};
const ESBUILD_VERSION = '0.28.2';
const GLOBAL_NAME = 'Markmap';

const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

/**
 * 在临时目录里用仓库内的 package-lock.json 安装依赖（``npm ci``）。
 * 顶层与全部传递依赖（d3 的 30 个子包等）都由 lockfile 钉死，保证长期可复现；
 * 升级依赖时更新 ``scripts/markmap-bundle/`` 下的两个文件并重建产物。
 */
async function installDependencies(workDir) {
  await copyFile(path.join(LOCK_DIR, 'package.json'), path.join(workDir, 'package.json'));
  await copyFile(path.join(LOCK_DIR, 'package-lock.json'), path.join(workDir, 'package-lock.json'));
  const result = spawnSync('npm', ['ci', '--no-audit', '--no-fund'], {
    cwd: workDir, encoding: 'utf8', shell: true, timeout: 15 * 60 * 1000,
  });
  if (result.status !== 0) {
    throw new Error(`npm ci 失败：${result.stderr || result.stdout}`);
  }
  return path.join(workDir, 'node_modules');
}

/** 读取包的许可文件文本;常见命名都试一遍。 */
async function readLicense(nodeModules, name) {
  for (const candidate of ['LICENSE', 'LICENSE.md', 'LICENSE.txt', 'license']) {
    try {
      return await readFile(path.join(nodeModules, name, candidate), 'utf8');
    } catch { /* 换下一个 */ }
  }
  return null;
}

/** 打包:markmap-view + 传递依赖(d3/markmap-common/@babel/runtime) → 单 IIFE。 */
async function bundle(nodeModules) {
  const esbuild = await import(pathToFileURL(path.join(nodeModules, 'esbuild', 'lib', 'main.js')).toString());
  const result = await esbuild.build({
    entryPoints: [path.join(nodeModules, 'markmap-view', 'dist', 'index.js')],
    bundle: true,
    format: 'iife',
    globalName: GLOBAL_NAME,
    minify: true,
    charset: 'utf8',
    legalComments: 'none',
    write: false,
    logLevel: 'silent',
    metafile: true,
  });
  return { bytes: result.outputFiles[0].contents, metafile: result.metafile };
}

/** 实际打入产物的 npm 包（从 esbuild metafile 的 inputs 提取，排序去重）。 */
function bundledPackages(metafile) {
  const names = new Set();
  for (const input of Object.keys(metafile.inputs || {})) {
    const match = input.replace(/\\/g, '/').match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
    if (match) names.add(match[1]);
  }
  return [...names].sort();
}

async function readPackageVersion(nodeModules, name) {
  try {
    const data = JSON.parse(await readFile(path.join(nodeModules, name, 'package.json'), 'utf8'));
    return String(data.version || '');
  } catch {
    return '';
  }
}

/** 逐包聚合许可：只收录实际打入产物的包（metafile 口径）。 */
async function collectLicenses(nodeModules, metafile) {
  const sections = [];
  for (const name of bundledPackages(metafile)) {
    const version = await readPackageVersion(nodeModules, name);
    const text = await readLicense(nodeModules, name);
    sections.push(`## ${name}${version ? ` ${version}` : ''}\n\n`
      + `${(text || '（包内未携带许可文件，见 npm 元数据）').trim()}\n`);
  }
  return sections;
}

function renderReadme(bundleSha, bundleSize, licenseSha, licenseSize) {
  const lines = [
    '# markmap 交互脑图（随仓库分发的构建产物）',
    '',
    '| 项 | 值 |',
    '|---|---|',
    `| 打包入口 | markmap-view ${PINNED['markmap-view']}（npm 包无自包含浏览器产物，构建产物随仓库分发） |`,
    '| 实际打入 | markmap-view、markmap-common 与 d3 及其 30 个子包（共 33 个包；@babel/runtime 仅安装、未进入产物），逐包许可全文见同目录 LICENSE |',
    '| 版本锁定 | `scripts/markmap-bundle/package.json` + `package-lock.json`（顶层与全部传递依赖均钉死，`npm ci` 安装） |',
    `| 构建工具 | esbuild ${ESBUILD_VERSION}（仅构建期，不进分发产物） |`,
    '| 用途 | `web/js/components/mindmap.js` 的 `renderInteractiveMindmap()`：笔记「交互 / 静态」脑图切换的交互视图；首次切换时懒加载 |',
    '',
    '## 文件清单',
    '',
    '| 文件 | 大小（字节） | SHA-256 |',
    '|---|---|---|',
    `| \`markmap-view.iife.min.js\` | ${bundleSize} | \`${bundleSha}\` |`,
    `| \`LICENSE\` | ${licenseSize} | \`${licenseSha}\` |`,
    '',
    '## 重建与校验',
    '',
    '```powershell',
    'node scripts/build_markmap_bundle.mjs          # 重建（lockfile 钉死版本，字节一致）',
    'node scripts/build_markmap_bundle.mjs --check  # 校验磁盘产物与重建结果一致',
    '```',
    '',
    '依赖由 `scripts/markmap-bundle/package-lock.json` 钉死（`npm ci`）；'
    + '升级时更新该目录的两个文件、重建产物并修订本表。',
    '产物与许可均由 esbuild metafile 的实际 inputs 生成，避免漏登记传递依赖。',
    '',
    '## 已知边界',
    '',
    '- `initialExpandLevel` 不能作为选项传入（0.18 的 deriveOptions 白名单处理会崩，默认 -1 全展开）；',
    '- options 必须直接用 `deriveOptions(...)` 的结果，不能作为 `style:` 二次嵌套。',
    '',
    '## CSP 说明',
    '',
    '打包产物为纯渲染运行时，无字符串求值；节点数据由 `mindmap.js` 从既有树结构转换',
    '（`{content, children}`），样式经 markmap 的 `embedGlobalCSS` 内嵌在 SVG 里，',
    "主站 `script-src 'self'` 不变，懒加载同源注入。",
    '',
  ];
  return `${lines.join('\n')}\n`;
}

async function main() {
  const checkOnly = process.argv.includes('--check');
  const workDir = await mkdtemp(path.join(tmpdir(), 'markmap-bundle-'));
  try {
    const nodeModules = await installDependencies(workDir);
    const { bytes, metafile } = await bundle(nodeModules);
    const bundleSha = sha256(bytes);
    const bundleSize = bytes.length;

    if (checkOnly) {
      const disk = await readFile(OUTPUT);
      const diskSha = sha256(disk);
      if (diskSha !== bundleSha || disk.length !== bundleSize) {
        console.error(`产物与重建结果不一致：磁盘 ${disk.length}/${diskSha.slice(0, 12)}…，`
          + `重建 ${bundleSize}/${bundleSha.slice(0, 12)}…`);
        console.error('请运行 node scripts/build_markmap_bundle.mjs 重建并提交。');
        // 交给 finally 清理临时目录；不要用 process.exit（不会展开 finally）。
        process.exitCode = 1;
        return;
      }
      console.log(`--check 通过：${path.relative(ROOT, OUTPUT)}（${bundleSize} 字节，`
        + `SHA-256 ${bundleSha.slice(0, 12)}…）`);
      return;
    }

    await mkdir(VENDOR_DIR, { recursive: true });
    await writeFile(OUTPUT, bytes);

    const licenses = await collectLicenses(nodeModules, metafile);
    const licenseBytes = Buffer.from(`# 打包产物的依赖许可（按 esbuild metafile 逐包聚合）\n\n${licenses.join('\n')}\n`, 'utf8');
    await writeFile(path.join(VENDOR_DIR, 'LICENSE'), licenseBytes);

    await writeFile(path.join(VENDOR_DIR, 'README.md'),
      renderReadme(bundleSha, bundleSize, sha256(licenseBytes), licenseBytes.length));
    console.log(`已写出 ${path.relative(ROOT, OUTPUT)}（${bundleSize} 字节）`);
    console.log(`SHA-256 ${bundleSha}`);
  } finally {
    try {
      await rm(workDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
    } catch {
      // Windows 下 esbuild.exe 服务进程会短暂锁定文件;残留目录在系统临时区,
      // 交给系统清理,不影响构建结果。
    }
  }
}

main().catch(error => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
