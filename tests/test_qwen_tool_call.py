import json
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "third_party/llama.cpp/build/bin/llama-server"
MODEL = ROOT / "models/vision/ministral-3-8b/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
LOG = ROOT / "tests/llama-server-test.log"
BASE = "http://127.0.0.1:9931"

tools = [
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a known user folder in the desktop file manager.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "enum": ["Downloads", "Documents", "Desktop"]
                    }
                },
                "required": ["folder"],
                "additionalProperties": False
            }
        }
    }
]

def request_json(url, data=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())

def main():
    if not SERVER.is_file() or not MODEL.is_file():
        raise FileNotFoundError("llama-server or Ministral model was not found.")

    log_file = LOG.open("w")
    process = subprocess.Popen(
        [
            str(SERVER),
            "-m", str(MODEL),
            "-ngl", "99",
            "-c", "4096",
            "-n", "256",
            "-np", "1",
            "--host", "127.0.0.1",
            "--port", "9931",
            "--jinja",
        ],
        cwd=ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    try:
        print("[Misty test] Starting local Ministral server...")
        for _ in range(180):
            if process.poll() is not None:
                raise RuntimeError("llama-server exited during startup.")
            try:
                request_json(BASE + "/health", timeout=2)
                break
            except (OSError, urllib.error.URLError):
                time.sleep(1)
        else:
            raise TimeoutError("Ministral server did not become ready in 180 seconds.")

        print("[Misty test] Server ready. Sending tool-selection request...")

        payload = {
            "model": "local-model",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Misty, a local desktop assistant. "
                        "Use available tools when they directly perform the "
                        "user's requested action. Do not invent tool names "
                        "or arguments. Do not claim an action was completed "
                        "before receiving its tool result."
                    )
                },
                {
                    "role": "user",
                    "content": "Open my Downloads folder."
                }
            ],
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": 0,
            "max_tokens": 256
        }

        result = request_json(
            BASE + "/v1/chat/completions",
            payload,
            timeout=120
        )

        message = result["choices"][0]["message"]

        print("\n========== MODEL RESPONSE ==========")
        print(json.dumps(message, indent=2, ensure_ascii=False))
        print("====================================")

        calls = message.get("tool_calls") or []
        if calls:
            print(f"\n[SUCCESS] Model returned {len(calls)} tool call(s).")
            for call in calls:
                print("Tool:", call["function"]["name"])
                print("Arguments:", call["function"]["arguments"])
        else:
            print("\n[WARNING] No structured tool call was returned.")
            print("Check the server log for chat-template information.")

    finally:
        print("\n[Misty test] Stopping server and releasing model resources...")
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log_file.close()
        print("[Misty test] Server stopped.")

if __name__ == "__main__":
    main()
