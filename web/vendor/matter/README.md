# Matter.js（物理自由沙盒）

| 项目 | 内容 |
|---|---|
| 名称 | Matter.js |
| 版本 | 0.20.0 |
| 上游 | <https://github.com/liabru/matter-js>（npm 包 `matter-js`） |
| 许可证 | MIT License（全文见 `LICENSE`） |
| 获取方式 | `npm pack matter-js@0.20.0`，取包内 `build/matter.min.js` 与 `LICENSE`，未修改 |
| 用途 | `web/js/components/physicsSandbox.js`：物理题「自由沙盒」与学习附件 `physics_sandbox` 的探索模式渲染；首次打开时才注入 |
| 分发方式 | 随仓库分发（`web/vendor/**` 已由构建指纹与 `.dockerignore` 保留） |

## 文件校验

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `matter.min.js` | 83,476 | `72d30be0f579eb02ce1e0b6f9d359a4f392e6837e5a26ba8be5dbee7f88e24ae` |
| `LICENSE` | 1,092 | `ed182087be5b26734aa6d4789743de3a97417950e8c1e3ff2e3d19c6462720d3` |

## 边界

- **只用于探索模式**：引擎的逐步积分不保证跨运行逐位一致，页面会明确提示「每次运行结果可能有细微差异」；
  需要回放与对账的教学演示继续使用 `web/vendor/physics-in-motion/` 的确定性物理内核。
- 主站 CSP 为 `script-src 'self'`，脚本从同源 `/static/vendor/matter/` 注入；不引入 CDN。
- 场景数据只存可重建画面的参数（重力、刚体、约束），不序列化 Matter 引擎对象。
- 升级时替换文件并同步本表版本、大小与 SHA-256；`tests/test_oss_vendor_assets.py` 会逐项校验。
