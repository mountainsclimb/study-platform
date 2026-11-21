# physics-in-motion（vendored physics core）

本目录是知序「物理演示」页使用的第三方物理内核与演示配置。

- 上游仓库：<https://github.com/xuxuezhou/physics-in-motion>
- 上游提交：`d35b9a167fc4406814f5731fab6b1116890171df`（2026-07-12）
- 许可证：MIT License，Copyright (c) 2026 xuxuezhou，全文见 [LICENSE](LICENSE)
- 上游定位：高中物理在线刷题平台；本目录**只取**其纯 TypeScript 物理层
  （`src/physics/**` 求解器 + 校验、`src/sim/**` 场景构建器 + `SimResult` 类型）
  与 10 个自研教学案例的配置，不包含其 React/Next 页面、题库与 localStorage 状态层。

## 文件

| 文件 | 说明 |
|---|---|
| `physics-core.js` | 由 `src/sim/registry.ts` 用 esbuild 打包的 ESM（含全部求解器与构建器，minified，约 38KB）；导出 `buildSim(name, params)`、`buildSimDynamic(name, params)` |
| `demos.js` | 10 个教学案例的题面、参数、答案、解析步骤与 `visualization`（builder + params）；不含预计算的动画帧，前端按参数即时计算 |
| `LICENSE` | 上游 MIT 许可证原文 |

## 重新生成

需要 Node 20+ 与上游源码（`git clone` 或解压 zip）：

```bash
cd physics-in-motion-main
npm install --no-audit --no-fund

# 1) 物理内核（浏览器 ESM，minified）
npx esbuild src/sim/registry.ts --bundle --format=esm --minify \
  --alias:@=./src --outfile=physics-core.js

# 2) 演示配置（本仓库 scripts/export-web-demos.ts 由上游内容导出）
npx esbuild scripts/export-web-demos.ts --bundle --platform=node \
  --format=cjs --alias:@=./src --outfile=scripts/export-web-demos.cjs
node scripts/export-web-demos.cjs   # 产物 exported-web/demos.js
```

把 `physics-core.js`、`exported-web/demos.js`、`LICENSE` 复制到本目录即可。
`scripts/export-web-demos.ts` 与知序侧的导出脚本等价；未修改上游任何物理计算代码。

## 使用边界

- 动画为辅助理解，物理关系按题设理想模型模拟，不替代解析结论与官方答案。
- 每个演示的自动验证结果（守恒律 / 事件顺序 / 答案一致性）随 `SimResult.checks` 下发并在页面展示。
- 上游题库中的真题转录尚未逐条对照官方原卷，因此本目录**只使用自研教学案例**，不导入其题目内容。
