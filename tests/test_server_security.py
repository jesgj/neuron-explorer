"""Security regressions exercise actual server functions without model imports."""
import ast
import asyncio
import glob
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "explorer" / "server.py"


def load_functions(*names, **env):
    tree = ast.parse(SOURCE.read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = {"os": os, "glob": glob, "asyncio": asyncio, "json": json, **env}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class SessionSecurityTests(unittest.TestCase):
    def test_lookup_is_confined_and_existing_sessions_work(self):
        with tempfile.TemporaryDirectory() as root:
            projects = Path(root) / ".claude" / "projects"
            other = projects / "other"
            explorer = projects / "explorer"
            other.mkdir(parents=True)
            explorer.mkdir()
            (other / "outside.jsonl").write_text("private")
            (explorer / "old-session_1.jsonl").write_text("existing")
            (explorer / "linked.jsonl").symlink_to(other / "outside.jsonl")
            ns = load_functions("_session_file", AGENT_SESSIONS=str(explorer))
            old_home = os.environ.get("HOME")
            try:
                os.environ["HOME"] = root
                self.assertIsNone(ns["_session_file"]("outside"))
                self.assertEqual(ns["_session_file"]("old-session_1"), str(explorer / "old-session_1.jsonl"))
                for sid in ("*", "old?session_1", "old[ -]session_1", "../outside", "linked", ""):
                    with self.subTest(sid=sid):
                        self.assertIsNone(ns["_session_file"](sid))
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home


    def test_history_rewind_and_resume_use_existing_explorer_session(self):
        with tempfile.TemporaryDirectory() as root:
            explorer = Path(root) / "explorer"
            other = Path(root) / "other"
            explorer.mkdir()
            other.mkdir()
            message = json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n"
            (explorer / "legacy-id.jsonl").write_text(message)
            (other / "outside.jsonl").write_text(message)
            ns = load_functions("_session_file", "_user_text", "_shown_text", "_refusal", "_tool_results",
                                "agent_history", "agent_rewind", "_claude_cmd", AGENT_SESSIONS=str(explorer),
                                _meta=lambda: {}, _chats={}, ROOT=root, PORT=8791,
                                FILE_TOOLS=[], agent_prompt_file=lambda: "prompt", sys=__import__("sys"))
            self.assertEqual(ns["agent_history"]("legacy-id")["events"], [{"t": "user", "text": "hello"}])
            self.assertEqual(ns["agent_history"]("outside"), {"events": []})
            self.assertEqual(ns["_claude_cmd"]("legacy-id")[-2:], ["--resume", "legacy-id"])
            class Request:
                async def json(self):
                    return {"chat": "outside", "text": "hello"}
            self.assertEqual(asyncio.run(ns["agent_rewind"](Request())), {"ok": True, "cut": False})
            self.assertEqual((other / "outside.jsonl").read_text(), message)


class NavigationSecurityTests(unittest.TestCase):
    def test_cross_site_navigation_cannot_reach_api_but_pages_work(self):
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse

        ns = load_functions("local_only", LOCAL_HOSTS={"localhost:8791"}, PlainTextResponse=PlainTextResponse)
        async def next_handler(request):
            return PlainTextResponse("allowed")
        for site in ("cross-site", "same-site"):
            for path, expected in (("/", 200), ("/index.html", 200), ("/api/fires", 403), ("/api/agent/history", 403), ("/api", 403)):
                scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [(b"host", b"localhost:8791"), (b"sec-fetch-site", site.encode()), (b"sec-fetch-mode", b"navigate")]}
                with self.subTest(site=site, path=path):
                    response = asyncio.run(ns["local_only"](Request(scope), next_handler))
                    self.assertEqual(response.status_code, expected)


class FiresSecurityTests(unittest.TestCase):
    def test_fires_limits_plain_and_chat_tokens_before_model_execution(self):
        class Tokenizer:
            def __call__(self, text):
                return types.SimpleNamespace(input_ids=list(range(1100)))
            def decode(self, ids):
                return str(ids[0])
        class Runtime:
            tok = Tokenizer()
            def chat_ids(self, text):
                return list(range(1100))
            def all_activations(self, ids):
                self.seen = ids
                return {}
            def vocab_strings(self):
                return []
        with tempfile.NamedTemporaryFile() as scale:
            runtime = Runtime()
            ns = load_functions("fires", rt=runtime, ACT_SCALE=scale.name, MAX_INFER_TOKENS=1024,
                                np=types.SimpleNamespace(load=lambda path: {}), lens_index=lambda: None, names=lambda: {})
            for chat in (False, True):
                with self.subTest(chat=chat):
                    result = asyncio.run(ns["fires"]("text", chat=chat))
                    self.assertEqual(len(runtime.seen), 1024)
                    self.assertEqual(len(result["tokens"]), 1024)


if __name__ == "__main__":
    unittest.main()
