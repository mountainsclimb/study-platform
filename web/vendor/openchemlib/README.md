# OpenChemLib JS（SMILES 解析与 3D 构象）

| 项目 | 内容 |
|---|---|
| 名称 | OpenChemLib JS |
| 版本 | 9.26.0 |
| 上游 | <https://github.com/cheminfo/openchemlib-js>（npm 包 `openchemlib`） |
| 许可证 | BSD-3-Clause（全文见 `LICENSE`） |
| 用途 | `web/js/components/moleculeViewer.js`：把 SMILES 解析为分子并生成 3D 构象（`ConformerGenerator`），再交给 3Dmol.js 渲染；首次打开分子弹层时才加载 |
| 分发方式 | 随仓库分发（`web/vendor/**` 已由构建指纹与 `.dockerignore` 保留） |

## 文件校验

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `openchemlib.js`（ESM 构建） | 1,101,184 | `2d7fb693bc4ee83dbc8d3a2fd3cd7edcc0195ac2af3b5daac948b4f99cede4b3` |
| `resources.json`（构象生成所需的 COD 扭转数据子集） | 773,478 | `bd976efcd5ea199cb5d5ed847a6f44431cd95a0940b0df8151cb7131325edb53` |
| `LICENSE` | 1,515 | `38dc3aed3def8cc4dd15ac879daa4af9b0d71af86fef82611ca1752497c6f464` |

`resources.json` 由上游 `dist/resources.json` 仅保留 `/resources/cod/*` 键（构象生成需要；
力场/毒理/药物相似性资源不引入），可用下述命令重新生成并核对：

```bash
node -e "const fs=require('fs');const m=JSON.parse(fs.readFileSync('dist/resources.json','utf8'));const out={};for(const [k,v] of Object.entries(m)){if(k.startsWith('/resources/cod/'))out[k]=v};fs.writeFileSync('resources.json',JSON.stringify(out))"
```

## 边界

- `openchemlib.js` 是 ESM 产物，通过同源动态 `import()` 加载；不走 CDN。
- SMILES 只在内存中解析；原子数超过 120 时前端拒绝，避免构象生成拖慢页面。
- `resources.json` 由同源 `fetch` 加载，`connect-src 'self'` 已允许。
- 升级时替换文件、重新生成资源子集，并同步本表版本、大小与 SHA-256。
