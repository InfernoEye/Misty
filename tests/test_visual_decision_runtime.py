"""Real managed subprocess + fake HTTP model, never a real LLM or desktop."""
import base64
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from app.brain.visual_decision import (
    VisualDecisionRuntime, VisualRuntimeConfig, VisualImage,
    VisualDecisionError, VisualDecisionCancelled, VisualDecisionTimeout,
)


_FAKE_SERVER = r'''
import argparse, base64, json, socket, time
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from PIL import Image
p=argparse.ArgumentParser()
p.add_argument('--port',type=int)
p.add_argument('--api-key')
p.add_argument('--state')
p.add_argument('--listen-fd',type=int)
a=p.parse_args()
state=json.loads(Path(a.state).read_text())
count=0
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def respond(self,value):
        data=json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer '+a.api_key:
            self.send_error(401); return
        if state.get('mode')=='hang_health': time.sleep(20)
        if self.path=='/health': self.respond({'status':'ok'})
        else: self.respond({'modalities':{'vision':state.get('mode')!='text_only'}})
    def do_POST(self):
        global count
        if self.headers.get('Authorization') != 'Bearer '+a.api_key:
            self.send_error(401); return
        count+=1
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with open(state['requests'],'a') as out: out.write(json.dumps(payload)+'\n')
        if count==1:
            url=payload['messages'][-1]['content'][-1]['image_url']['url']
            image=Image.open(BytesIO(base64.b64decode(url.split(',')[1])))
            color='red' if image.getpixel((0,0))[0]>100 else 'blue'
            if state.get('mode')=='probe_wrong': color='green'
            content=json.dumps({'kind':'ask','question':color})
        else:
            if state.get('mode')=='hang_decision': time.sleep(20)
            content=state.get('content',json.dumps({'kind':'act','tool':'click','arguments':{'x':5},'observation_id':'obs-1'}))
        result={'choices':[{'finish_reason':state.get('finish_reason','stop'),
                            'message':{'content':content}}]}
        if count>1 and state.get('mode','').startswith('wire_'):
            data=json.dumps(result).encode()
            mode=state['mode']
            length=len(data)+(5 if mode=='wire_truncated' else 0)
            headers=b'Content-Length: '+str(length).encode()+b'\r\n'
            if mode=='wire_duplicate_length': headers+=headers
            if mode=='wire_missing_length': headers=b''
            if mode=='wire_chunked': headers=b'Transfer-Encoding: chunked\r\n'
            if mode=='wire_large_headers': headers+=b'X-Filler: '+b'x'*17000+b'\r\n'
            if mode=='wire_large_body': headers=b'Content-Length: 2000000\r\n'; data=b''
            self.connection.sendall(b'HTTP/1.1 200 OK\r\n'+headers+b'Connection: close\r\n\r\n'+data)
            self.close_connection=True
            return
        if count>1 and state.get('mode')=='native_tool_call':
            result['choices'][0]['message']['tool_calls']=[{'id':'not-admitted'}]
        if count>1 and state.get('mode')=='truncated_decision':
            result['choices'][0]['finish_reason']='length'
        self.respond(result)
# The test parent passes a listening socket, avoiding a release/rebind race and
# making fake-child startup independent of ephemeral-port reuse or collisions.
server=HTTPServer(('127.0.0.1',a.port),Handler,bind_and_activate=False)
server.socket.close()
server.socket=socket.socket(fileno=a.listen_fd)
server.server_address=server.socket.getsockname()
server.server_name='localhost'
server.server_port=a.port
server.serve_forever()
'''

_SCHEMA = {"type": "function", "function": {"name": "click", "description": "Exact test schema",
    "parameters": {"type": "object", "properties": {"x": {"type": "integer", "minimum": 0}},
                   "required": ["x"], "additionalProperties": False}}}


def image_bytes(color="red", size=(12, 10)):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


class FakeRuntime(VisualDecisionRuntime):
    def __init__(self, config, script, state):
        super().__init__(config)
        self.script, self.state_file = script, state
        self.stderr_file = state.with_suffix(".stderr")

    def start(self, cancel=None, deadline=None):
        try:
            return super().start(cancel=cancel, deadline=deadline)
        except VisualDecisionError as error:
            # Only this synthetic server is diagnosed here; production never
            # persists private model prompts, screenshots or stderr contents.
            detail = self.stderr_file.read_text()[:2000] if self.stderr_file.exists() else ""
            if detail.strip():
                raise type(error)(str(error) + "; fake child stderr: " + detail.strip()) from error
            raise

    def _command(self):
        return [sys.executable, str(self.script), "--port", str(self._port),
                "--api-key=" + self._api_key, "--state", str(self.state_file)]

    def _launch(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self._port = listener.getsockname()[1]
            with self.stderr_file.open("wb") as errors:
                return subprocess.Popen(
                    [*self._command(), "--listen-fd", str(listener.fileno())],
                    pass_fds=(listener.fileno(),), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errors,
                    start_new_session=True,
                )


class VisualRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_model.py"
        self.script.write_text(_FAKE_SERVER)
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
        self.tmp.cleanup()

    def runtime(self, mode="normal", content=None, **overrides):
        requests = self.root / f"requests-{len(self.runtimes)}.jsonl"
        state_path = self.root / f"state-{len(self.runtimes)}.json"
        state = {"mode": mode, "requests": str(requests)}
        if content is not None:
            state["content"] = content
        state_path.write_text(json.dumps(state))
        config = replace(VisualRuntimeConfig(), startup_timeout=4, request_timeout=2,
                         cleanup_timeout=0.2, **overrides)
        runtime = FakeRuntime(config, self.script, state_path)
        self.runtimes.append(runtime)
        return runtime, requests

    def decide(self, runtime, **overrides):
        kwargs = {"goal": "Click target", "observation_id": "obs-1",
                  "images": [VisualImage(image_bytes())], "tool_schemas": [_SCHEMA]}
        kwargs.update(overrides)
        return runtime.decide(**kwargs)

    def test_constructor_never_starts_a_process_and_readiness_required(self):
        runtime, _ = self.runtime()
        self.assertIsNone(runtime._process)
        with self.assertRaisesRegex(VisualDecisionError, "readiness"):
            self.decide(runtime)

    def test_real_images_and_exact_schemas_reach_task_scoped_process(self):
        runtime, requests = self.runtime()
        ready = runtime.start()
        process = runtime._process
        self.assertTrue(ready.image_input)
        self.assertFalse(ready.grounding_verified)
        self.assertEqual(self.decide(runtime)["tool"], "click")
        self.assertIs(runtime._process, process)
        self.assertEqual(runtime.start(), ready)
        calls = [json.loads(line) for line in requests.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        payload = calls[-1]
        blocks = payload["messages"][-1]["content"]
        context = json.loads(blocks[0]["text"])
        self.assertEqual(context["tool_schemas"], [_SCHEMA])
        self.assertEqual(base64.b64decode(blocks[1]["image_url"]["url"].split(",")[1]), image_bytes())
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        runtime.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(runtime.ready)

    def test_random_key_with_leading_hyphens_has_non_option_prefix(self):
        with patch("app.brain.visual_decision.secrets.token_urlsafe", return_value="--random-test-key"):
            runtime, _ = self.runtime()
        self.assertEqual(runtime._api_key, "misty---random-test-key")
        runtime.start()
        self.assertEqual(self.decide(runtime)["tool"], "click")

    def test_fake_http_child_accepts_an_option_looking_key(self):
        runtime, _ = self.runtime()
        runtime._api_key = "--option-looking-test-key"
        runtime.start()
        self.assertEqual(self.decide(runtime)["tool"], "click")

    def test_text_only_and_failed_probe_servers_are_not_ready(self):
        for mode in ("text_only", "probe_wrong"):
            with self.subTest(mode=mode):
                runtime, _ = self.runtime(mode)
                with self.assertRaises(VisualDecisionError):
                    runtime.start()
                self.assertFalse(runtime.ready)
                self.assertIsNone(runtime._process)

    def test_startup_timeout_is_bounded_and_reaps_owned_process(self):
        runtime, _ = self.runtime("hang_health")
        before = time.monotonic()
        with self.assertRaises(VisualDecisionTimeout):
            runtime.start(deadline=time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - before, 2)
        self.assertIsNone(runtime._process)

    def test_inference_cancel_interrupts_http_and_reaps_process(self):
        runtime, _ = self.runtime("hang_decision")
        runtime.start()
        process = runtime._process
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        try:
            before = time.monotonic()
            with self.assertRaises(VisualDecisionCancelled):
                self.decide(runtime, cancel=cancel)
            self.assertLess(time.monotonic() - before, 2)
            self.assertIsNotNone(process.poll())
        finally:
            timer.cancel()

    def test_inference_deadline_is_bounded(self):
        runtime, _ = self.runtime("hang_decision")
        runtime.start()
        with self.assertRaises(VisualDecisionTimeout):
            self.decide(runtime, deadline=time.monotonic() + 0.15)
        self.assertIsNone(runtime._process)

    def test_close_from_another_thread_cancels_request(self):
        runtime, _ = self.runtime("hang_decision")
        runtime.start()
        errors = []
        entered = threading.Event()
        original = runtime._http
        def http(*args):
            entered.set()
            return original(*args)
        runtime._http = http
        def decide():
            try:
                self.decide(runtime)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=decide)
        worker.start()
        self.assertTrue(entered.wait(1))
        runtime.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], VisualDecisionCancelled)
        self.assertIsNone(runtime._process)

    def test_bad_decisions_never_escape_as_actions(self):
        decisions = [
            '```json\n{"kind":"ask","question":"x"}\n```',
            '{"kind":"ask","kind":"act","question":"x"}',
            '[]',
            json.dumps({"kind": "act", "tool": "unknown", "arguments": {}, "observation_id": "obs-1"}),
            json.dumps({"kind": "act", "tool": "click", "arguments": {"x": -1}, "observation_id": "obs-1"}),
            json.dumps({"kind": "act", "tool": "click", "arguments": {"x": 2}, "observation_id": "stale"}),
            json.dumps({"kind": "act", "tool": "click", "arguments": {"x": 2}, "observation_id": "obs-1", "safe": True}),
            json.dumps({"kind": "finish", "evidence_ids": ["old"], "summary": "done"}),
        ]
        # Reuse one managed fake server; mutate only fake response state between independent instances.
        for content in decisions:
            with self.subTest(content=content):
                runtime, _ = self.runtime(content=content)
                runtime.start()
                with self.assertRaises(VisualDecisionError):
                    self.decide(runtime)
                runtime.close()

    def test_image_validation_fails_before_inference(self):
        runtime, requests = self.runtime(max_image_pixels=1024)
        runtime.start()
        bad = [VisualImage(b"not an image"), VisualImage(image_bytes(), "image/jpeg"),
               VisualImage(image_bytes(size=(40, 40))), VisualImage(bytearray(image_bytes()))]
        for image in bad:
            with self.subTest(image_type=type(image.data)):
                with self.assertRaises(VisualDecisionError):
                    self.decide(runtime, images=[image])
        self.assertEqual(len(requests.read_text().splitlines()), 1)
        with self.assertRaises(VisualDecisionError):
            self.decide(runtime, images=[])

    def test_external_schema_references_and_duplicates_are_rejected(self):
        runtime, _ = self.runtime()
        runtime.start()
        for schemas in ([{"name": "net", "inputSchema": {"$ref": "http://example.invalid/schema"}}],
                        [_SCHEMA, _SCHEMA]):
            with self.assertRaises(VisualDecisionError):
                self.decide(runtime, tool_schemas=schemas)

    def test_mcp_catalog_is_supported_without_changing_its_schema(self):
        runtime, requests = self.runtime()
        runtime.start()
        mcp = {"name": "click", "inputSchema": _SCHEMA["function"]["parameters"]}
        self.decide(runtime, tool_schemas=[mcp])
        payload = json.loads(requests.read_text().splitlines()[-1])
        self.assertEqual(json.loads(payload["messages"][-1]["content"][0]["text"])["tool_schemas"], [mcp])

    def test_valid_ask_and_finish_envelopes_are_preserved(self):
        for decision in ({"kind": "ask", "question": "Which target?"},
                         {"kind": "finish", "evidence_ids": ["obs-1"], "summary": "Target visibly selected"}):
            with self.subTest(kind=decision["kind"]):
                runtime, _ = self.runtime(content=json.dumps(decision))
                runtime.start()
                self.assertEqual(self.decide(runtime, tool_schemas=[]), decision)
                runtime.close()

    def test_http_authentication_is_required_without_disclosing_key(self):
        runtime, requests = self.runtime()
        runtime.start()
        runtime._api_key = "wrong-test-credential"
        with self.assertRaisesRegex(VisualDecisionError, "HTTP 401"):
            self.decide(runtime)
        # The rejected call never reaches fake inference or its request log.
        self.assertEqual(len(requests.read_text().splitlines()), 1)

    def test_complete_content_length_framing_is_required(self):
        for mode in ("wire_truncated", "wire_duplicate_length", "wire_missing_length",
                     "wire_chunked", "wire_large_headers", "wire_large_body"):
            with self.subTest(mode=mode):
                runtime, _ = self.runtime(mode)
                runtime.start()
                with self.assertRaises(VisualDecisionError):
                    self.decide(runtime)
                runtime.close()

    def test_model_truncation_or_native_tool_calls_do_not_bypass_envelope(self):
        for mode in ("native_tool_call", "truncated_decision"):
            with self.subTest(mode=mode):
                runtime, _ = self.runtime(mode)
                runtime.start()
                with self.assertRaisesRegex(VisualDecisionError, "complete strict JSON"):
                    self.decide(runtime)
                runtime.close()

    def test_request_body_limit_fails_before_task_inference(self):
        runtime, requests = self.runtime(max_request_bytes=8000)
        runtime.start()
        # Probe fits; the JSON history fits context but not the HTTP body cap.
        with self.assertRaisesRegex(VisualDecisionError, "bounded JSON"):
            self.decide(runtime, history=[{"text": "x" * 9000}])
        self.assertEqual(len(requests.read_text().splitlines()), 1)

    def test_canceled_schema_validation_is_not_reclassified_as_bad_model_output(self):
        runtime, _ = self.runtime()
        cancel = threading.Event()
        validators = runtime._catalog([_SCHEMA], cancel, time.monotonic() + 2)
        cancel.set()
        with self.assertRaises(VisualDecisionCancelled):
            runtime._validate_decision({"kind": "act", "tool": "click", "arguments": {"x": 1},
                                        "observation_id": "obs-1"}, "obs-1", validators)

    def test_launch_failure_is_content_free_and_leaves_no_process(self):
        runtime, _ = self.runtime()
        def fail_launch():
            raise OSError("private-path-and-command")
        runtime._launch = fail_launch
        with self.assertRaisesRegex(VisualDecisionError, "could not be started") as raised:
            runtime.start()
        self.assertNotIn("private-path-and-command", str(raised.exception))
        self.assertIsNone(runtime._process)

    def test_occupied_endpoint_fails_closed_and_reaps_startup_child(self):
        runtime, _ = self.runtime()
        launched = []
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            def collide():
                runtime._port = occupied.getsockname()[1]
                child = subprocess.Popen([sys.executable, "-c",
                    "import socket; s=socket.socket(); s.bind(('127.0.0.1', " + str(runtime._port) + "))"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                launched.append(child)
                return child
            runtime._launch = collide
            with self.assertRaisesRegex(VisualDecisionError, "exited during startup"):
                runtime.start()
        self.assertEqual(len(launched), 1)  # No retry conceals a failed bind.
        self.assertIsNotNone(launched[0].poll())
        self.assertIsNone(runtime._process)

    def test_closed_runtime_cannot_restart(self):
        runtime, _ = self.runtime()
        runtime.start()
        process = runtime._process
        runtime.close()
        with self.assertRaises(VisualDecisionCancelled):
            runtime.start()
        self.assertIsNotNone(process.poll())

    def test_invalid_image_media_type_and_hard_budget_configuration(self):
        runtime, _ = self.runtime()
        with self.assertRaises(VisualDecisionError):
            runtime._image_blocks([VisualImage(image_bytes(), {})])
        for config in ({"device": 123}, {"max_request_bytes": 100_000_000},
                       {"max_response_bytes": 100_000_000}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                replace(VisualRuntimeConfig(), **config)

    def test_missing_artifacts_fail_without_launching_model(self):
        runtime = VisualDecisionRuntime(VisualRuntimeConfig(server_path=self.root / "absent"))
        self.runtimes.append(runtime)
        with self.assertRaisesRegex(VisualDecisionError, "artifact"):
            runtime.start()
        self.assertIsNone(runtime._process)

    def test_invalid_deadlines_do_not_lock_runtime(self):
        runtime, _ = self.runtime()
        with self.assertRaises(ValueError):
            runtime.start(deadline=float("nan"))
        runtime.start()
        with self.assertRaises(ValueError):
            self.decide(runtime, deadline=float("inf"))
        self.assertEqual(self.decide(runtime)["tool"], "click")


if __name__ == "__main__":
    unittest.main()
