# Pyodide（浏览器内 Python，随仓库分发的核心运行时）

| 项 | 值 |
|---|---|
| 版本 | 0.28.0 |
| 许可证 | Pyodide 本体 Mozilla Public License 2.0（全文见同目录 `LICENSE`）；内置 CPython 为 PSF License，随 `python_stdlib.zip` 分发 |
| 来源 | `https://cdn.jsdelivr.net/pyodide/v0.28.0/full/`，未做改动 |
| 用途 | `web/lab/python.html` 的「Python 实验台」：独立 CSP 专用页，按需加载 WASM 运行时 |

## 文件清单

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `pyodide.js` | 16,811 | `564b1acbfeb07eda9ba849ee0a0ed1eb8587aab62d9f18082ed905ed36432384` |
| `pyodide.asm.js` | 1,072,130 | `da195d86c80caece821124dcd4f049e6465a7a73797f1b2b4147283e445d02a2` |
| `pyodide.asm.wasm` | 8,645,010 | `4ee3dc1deab639b454fdc6683651b4cf6c86fc587072e4f7b6c8eaed777d9db6` |
| `python_stdlib.zip` | 2,416,414 | `1cc90fccf52e5145bd1bd3ef3540f5e1dd8bf17c5fc66c3e037802ef06ea62af` |
| `pyodide-lock.json` | 108,703 | `6c6ba6e257a75193fc935a456963b0bfd3f9f4e307c52378b030e25b0b968227` |
| `LICENSE` | 16,725 | `1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5` |

核心资产约 **11.9MB**（未计按需包）。不随仓库分发 `pyodide-lock` 之外的包仓库与 source map。

## CSP 说明

「Python 实验台」是**独立策略专用页**，不走主站默认 CSP：
`script-src 'self' 'wasm-unsafe-eval'`（WebAssembly 编译所需），`connect-src 'self'`、
`worker-src 'none'`。主站 `script-src 'self'` 下 `loadPyodide` 无法完成加载，
因此不得把本页嵌入主站或课堂交互沙箱；课堂沙箱仍保持 `worker-src 'none'` 不放宽。
