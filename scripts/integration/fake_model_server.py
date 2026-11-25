"""部署联验用的极简 OpenAI 兼容假模型端点（标准库，无依赖）。

用途：`scripts/integration/deploy_accept.py` 第 5 步需要一个真实 HTTP 模型端点来
验证「Redis 队列 → arq Worker → 模型调用 → 任务完成」全链路；本服务返回固定
chat.completion 响应，不联网、不计费。

用法（宿主侧）：
    python3 scripts/integration/fake_model_server.py [--port 9123]

容器内经 docker 网桥网关访问宿主端口，网关 IP 查询：
    docker network inspect <project>_default \
        --format '{{(index .IPAM.Config 0).Gateway}}'
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

RESPONSE = json.dumps({
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "model": "fake-vision",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant",
                    "content": "1. Compute 7 * 6.\nA. 42\nB. 48\nC. 36\nD. 54\n答案：A"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
}).encode("utf-8")

MODELS = json.dumps({
    "object": "list",
    "data": [{"id": "fake-vision", "object": "model"}],
}).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _reply(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802 - http.server 命名约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._reply(RESPONSE)

    def do_GET(self):  # noqa: N802
        self._reply(MODELS)

    def log_message(self, *args):  # 静音：联验输出只保留验收脚本日志
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI 兼容假模型端点（部署联验）")
    parser.add_argument("--port", type=int, default=9123)
    options = parser.parse_args()
    server = HTTPServer(("0.0.0.0", options.port), Handler)
    print(f"fake model server on 0.0.0.0:{options.port} (model: fake-vision)")
    server.serve_forever()


if __name__ == "__main__":
    main()
