#!/usr/bin/env python3
"""
Wolfram Engine HTTP 服务 — 在宿主机运行
Docker 中的 bot 通过 HTTP 调用此服务执行 Wolfram 计算

启动方式: python3 wolfram_server.py
默认监听: 0.0.0.0:9876
"""

import json
import uuid
import os
import re
import base64
import subprocess
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = int(os.getenv("WOLFRAM_SERVER_PORT", "9876"))
HOST = os.getenv("WOLFRAM_SERVER_HOST", "0.0.0.0")
WOLFRAM_EXECUTABLE = os.path.expanduser(
    os.getenv("WOLFRAM_EXECUTABLE", "~/mma/mma_new/Executables/WolframKernel")
)
PASSWORD_FILE = os.path.expanduser(
    os.getenv("WOLFRAM_PASSWORD_FILE", str(Path.home() / ".WolframEngine/Licensing/mathpass"))
)
TEMP_IMG_DIR = Path("data/wolfram_images")
TEMP_IMG_DIR.mkdir(parents=True, exist_ok=True)
ENABLE_IMAGE_EXPORT = os.getenv("WOLFRAM_EXPORT_IMAGE", "0") == "1"
IMAGE_EXPORT_TIMEOUT = int(os.getenv("WOLFRAM_IMAGE_EXPORT_TIMEOUT", "30"))


def run_wolfram(code: str) -> dict:
    """同步执行 Wolfram 代码，返回文本结果和图片(base64)"""
    img_filename = f"{uuid.uuid4()}.jpg"
    img_path = TEMP_IMG_DIR / img_filename
    wolfram_img_path = str(img_path).replace("\\", "/")

    kernel_input = (
        f'val=({code});\n'
        f'Print[val];\n'
    )
    if ENABLE_IMAGE_EXPORT:
        kernel_input += (
            f'Quiet[Check[TimeConstrained[Export["{wolfram_img_path}", val, "JPEG", ImageSize->2000, CompressionLevel->0], {IMAGE_EXPORT_TIMEOUT}, Null], Null]];\n'
        )
    kernel_input += 'Quit[]\n'

    env = os.environ.copy()
    if "DISPLAY" in env:
        del env["DISPLAY"]

    cmd = [
        WOLFRAM_EXECUTABLE,
        "-noprompt",
        "-pwfile", PASSWORD_FILE,
        "-J-Djava.awt.headless=true",
    ]

    try:
        result = subprocess.run(
            cmd,
            input=kernel_input,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        text_output = ""
        if result.stdout:
            clean_lines = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if "Wolfram Language" in line or "Copyright" in line:
                    continue
                if line == "" or line == ">":
                    continue
                clean_line = re.sub(r"^(In|Out)\[\d+\][:=]+\s*", "", line)
                if clean_line:
                    clean_lines.append(clean_line)
            text_output = "\n".join(clean_lines)

        # 读取图片转 base64
        image_b64 = None
        if img_path.exists() and img_path.stat().st_size > 0:
            image_b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
            img_path.unlink()  # 清理临时文件

        return {
            "text": f"In: {code}\nOut: {text_output}",
            "image_base64": image_b64,
        }

    except subprocess.TimeoutExpired:
        return {"text": "故障: 计算超时", "image_base64": None}
    except Exception as e:
        return {"text": f"故障: {e}", "image_base64": None}


class WolframHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/run":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
            code = data.get("code", "")
        except (json.JSONDecodeError, KeyError):
            self.send_error(400, "Invalid JSON, need {\"code\": \"...\"}")
            return

        print(f"\033[36m[Wolfram-Server]\033[0m 收到请求: {code}")
        result = run_wolfram(code)
        print(f"\033[36m[Wolfram-Server]\033[0m 结果: {result['text'][:100]}")

        response = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), WolframHandler)
    print(f"\033[32m[Wolfram-Server]\033[0m 启动在 http://{HOST}:{PORT}")
    print(f"\033[32m[Wolfram-Server]\033[0m Wolfram: {WOLFRAM_EXECUTABLE}")
    print(f"\033[32m[Wolfram-Server]\033[0m Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Wolfram-Server] 已停止")
        server.server_close()
