/* Pyodide 实验台（§9 专用页）：按需加载本地 WASM 运行时并执行文本框里的代码。
 *
 * 边界：
 * - 本页由 /lab/python 专用路由下发独立 CSP（script-src 'self' 'wasm-unsafe-eval'），
 *   不接入主站 SPA，也不嵌入课堂交互沙箱；
 * - 运行时完全同源（/static/vendor/pyodide/），无 CDN、无内联脚本；
 * - 输出一律 textContent，用户代码的异常文本不会被当作 HTML 解析。
 */
const RUN_URL = '/static/vendor/pyodide/pyodide.js';
const INDEX_URL = '/static/vendor/pyodide/';
const LOAD_TIMEOUT_MS = 30000;

const codeNode = document.getElementById('code');
const outputNode = document.getElementById('output');
const statusNode = document.getElementById('status');
const runButton = document.getElementById('run-code');
const clearCodeButton = document.getElementById('clear-code');
const clearOutputButton = document.getElementById('clear-output');

let runtimePromise = null;
let runtime = null;
let running = false;

function setStatus(text, state = '') {
  statusNode.textContent = text;
  if (state) statusNode.dataset.state = state;
  else delete statusNode.dataset.state;
}

function appendOutput(text, isError = false) {
  const value = String(text ?? '');
  if (!value) return;
  const node = document.createElement('span');
  if (isError) node.className = 'lab-error';
  node.textContent = value.endsWith('\n') ? value : `${value}\n`;
  outputNode.append(node);
  outputNode.scrollTop = outputNode.scrollHeight;
}

function clearOutput() {
  outputNode.replaceChildren();
}

function loadRuntime() {
  if (runtime) return Promise.resolve(runtime);
  if (runtimePromise) return runtimePromise;
  runtimePromise = new Promise((resolve, reject) => {
    setStatus('正在加载 Python 运行时（约 12MB，首次较慢）…');
    const timer = setTimeout(() => {
      reject(new Error('Python 运行时加载超时，请检查网络或刷新重试。'));
    }, LOAD_TIMEOUT_MS);
    const script = document.createElement('script');
    script.src = RUN_URL;
    script.async = true;
    script.onload = async () => {
      try {
        const loaded = await window.loadPyodide({ indexURL: INDEX_URL });
        loaded.setStdout({ batched: text => appendOutput(text) });
        loaded.setStderr({ batched: text => appendOutput(text, true) });
        clearTimeout(timer);
        runtime = loaded;
        setStatus('运行时已就绪。', 'ready');
        resolve(loaded);
      } catch (error) {
        clearTimeout(timer);
        reject(error);
      }
    };
    script.onerror = () => {
      clearTimeout(timer);
      reject(new Error('Python 运行时加载失败，请刷新重试。'));
    };
    document.head.append(script);
  }).catch(error => {
    runtimePromise = null;
    setStatus(error?.message || 'Python 运行时加载失败。', 'error');
    throw error;
  });
  return runtimePromise;
}

async function runCode() {
  if (running) return;
  running = true;
  runButton.disabled = true;
  runButton.textContent = '运行中…';
  try {
    const pyodide = await loadRuntime();
    setStatus('正在执行…');
    const result = await pyodide.runPythonAsync(codeNode.value);
    if (result !== undefined && result !== null) appendOutput(String(result));
    setStatus('执行完成。', 'ready');
  } catch (error) {
    appendOutput(error?.message || String(error), true);
    setStatus('执行失败，请检查代码或输出。', 'error');
  } finally {
    running = false;
    runButton.disabled = false;
    runButton.textContent = '运行';
  }
}

runButton.addEventListener('click', runCode);
clearCodeButton.addEventListener('click', () => {
  codeNode.value = '';
  codeNode.focus();
});
clearOutputButton.addEventListener('click', clearOutput);
codeNode.addEventListener('keydown', event => {
  if (event.key === 'Tab') {
    event.preventDefault();
    const start = codeNode.selectionStart;
    const end = codeNode.selectionEnd;
    codeNode.value = `${codeNode.value.slice(0, start)}    ${codeNode.value.slice(end)}`;
    codeNode.selectionStart = codeNode.selectionEnd = start + 4;
  }
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    runCode();
  }
});
