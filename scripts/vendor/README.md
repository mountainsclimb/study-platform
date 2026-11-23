# 开发期 vendored 资产

本目录只放**开发/验收脚本**使用的第三方文件，不进入运行时、不进入镜像
（`.dockerignore` 已排除 `scripts/vendor/`），也不属于 `web/vendor/` 分发资产。

## axe-core（无障碍回归）

| 项目 | 内容 |
|---|---|
| 名称 | axe-core |
| 版本 | 4.14.1 |
| 上游 | <https://github.com/dequelabs/axe-core> |
| 许可证 | Mozilla Public License 2.0（全文见 `LICENSE-axe`） |
| 文件 | `axe.min.js` |
| 大小 | 653,780 字节 |
| SHA-256 | `29348999450cdf2849a8e70b7912fc2da85a849da39c0d0d5394f51d4a9ed95c` |
| 获取方式 | `npm pack axe-core@4.14.1`，取包内 `axe.min.js` 与 `LICENSE`，未修改 |
| 用途 | `scripts/ui_check_a11y.py`：桌面 1440 与手机 390 视口下的 WCAG 2A/2AA 自动扫描 |

MPL-2.0 属文件级弱 copyleft：本项目未修改 `axe.min.js`，只以源码形式在开发脚本中注入
页面执行；不随应用分发。若要升级，替换文件并同步本表中的版本、大小与 SHA-256。
