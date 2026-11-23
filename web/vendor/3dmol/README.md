# 3Dmol.js（分子 3D 查看）

| 项目 | 内容 |
|---|---|
| 名称 | 3Dmol.js |
| 版本 | 3.0.0 |
| 上游 | <https://github.com/3dmol/3Dmol.js>（npm 包 `3dmol`） |
| 许可证 | BSD-3-Clause（全文见 `LICENSE`） |
| 获取方式 | `npm pack 3dmol@3.0.0`，取包内 `build/3Dmol-min.js` 与 `LICENSE`，未修改 |
| 用途 | `web/js/components/moleculeViewer.js`：化学题「分子模型」弹层与学习附件 `molecule_3d` 的只读渲染；首次打开时才注入 |
| 分发方式 | 随仓库分发（`web/vendor/**` 已由构建指纹与 `.dockerignore` 保留） |

## 文件校验

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `3Dmol-min.js` | 580,276 | `a886e24944310cea8111915209efb2c1246e327858a898ad56ac90fccc314053` |
| `LICENSE` | 4,283 | `4c6eaaed856f3f28a3b1a98e74f4a8a71618de7d51ea4155c29f6f793bcef861` |

## 边界

- 只引入 UMD 浏览器产物；主站 CSP 为 `script-src 'self'`，脚本从同源 `/static/vendor/3dmol/` 注入。
- 依赖浏览器 WebGL；不可用时组件降级为中文文字提示，不伪造结构式。
- SMILES 只作为模型数据，不拼入 DOM HTML；标题与标签一律 `textContent` 写入。
- 升级时替换文件并同步本表版本、大小与 SHA-256；`tests/test_oss_vendor_assets.py` 会逐项校验。
