import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

# otherwise `py -3 tests\test_shrink.py` can't see shrink.py one level up
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shrink


class SquashTests(unittest.TestCase):
    def test_short_diff_stays(self):
        src = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertIn("+b", shrink.squash_diff(src))
        self.assertNotIn("hidden", shrink.squash_diff(src))

    def test_lockfile_skipped(self):
        body = []
        body.append("diff --git a/package-lock.json b/package-lock.json")
        body.append("--- a/package-lock.json")
        body.append("+++ b/package-lock.json")
        for i in range(120):
            body.append("+%s" % i)
        out = shrink.squash_diff("\n".join(body))
        self.assertIn("skipped", out)
        self.assertNotIn("+119", out)

    def test_long_log_keeps_error(self):
        lines = ["debug %s" % i for i in range(200)]
        lines[120] = "Error: database connection refused"
        out = shrink.squash_log("\n".join(lines))
        self.assertIn("database connection refused", out)
        self.assertIn("lines hidden", out)
        self.assertNotIn("debug 10\n", out)

    def test_repeats(self):
        out = shrink.squash_log("\n".join(["ok"] * 8))
        self.assertIn("x8", out)

    def test_empty_diff_message(self):
        self.assertEqual(shrink.squash_diff("  \n"), "No changes.")

    def test_tokens_not_zero_on_code(self):
        # self.assertEqual(shrink.rough_tokens("a+b"), 1)  # back when I counted words
        self.assertGreater(shrink.rough_tokens("a+b+c+d+e"), 1)

    def test_tree_hides_node_modules(self):
        tmp = Path(tempfile.mkdtemp(prefix="shrink-"))
        try:
            (tmp / "main.py").write_text("print(1)\n", encoding="utf-8")
            junk = tmp / "node_modules" / "left-pad"
            junk.mkdir(parents=True)
            (junk / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
            text = shrink.squash_tree(tmp)
            self.assertIn("main.py", text)
            self.assertNotIn("left-pad", text)
            self.assertNotIn("node_modules", text)
        finally:
            shutil.rmtree(tmp)

    # def test_tree_on_whole_disk(self):
    #     shrink.squash_tree("C:/")
    # ran this once out of curiosity, not doing it again


class McpTests(unittest.TestCase):
    def test_initialize_echoes_version(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        ans = shrink.mcp_reply(msg)
        self.assertEqual(ans["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(ans["result"]["serverInfo"]["name"], "shrink")
        self.assertEqual(ans["result"]["serverInfo"]["version"], shrink.VER)

    def test_prompts_list_is_empty_not_an_error(self):
        # answering "not implemented" here made vscode drop the server
        ans = shrink.mcp_reply({"jsonrpc": "2.0", "id": 9, "method": "prompts/list"})
        self.assertEqual(ans["result"]["prompts"], [])
        ans = shrink.mcp_reply({"jsonrpc": "2.0", "id": 10, "method": "resources/list"})
        self.assertEqual(ans["result"]["resources"], [])

    def test_notification_is_quiet(self):
        ans = shrink.mcp_reply({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(ans)

    def test_unknown_tool(self):
        ans = shrink.mcp_reply({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "shrink_nope", "arguments": {}},
        })
        self.assertTrue(ans["result"]["isError"])

    def test_framed_header(self):
        payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        buf = io.StringIO("\r\n" + payload)
        # feed stdin, read_framed reads it directly
        old = shrink.sys.stdin
        shrink.sys.stdin = buf
        try:
            raw = shrink.read_framed("Content-Length: %s" % len(payload))
        finally:
            shrink.sys.stdin = old
        self.assertEqual(json.loads(raw)["method"], "ping")


class HttpTests(unittest.TestCase):
    def test_log_endpoint(self):
        httpd = shrink.make_server(0)
        port = httpd.server_address[1]
        thr = threading.Thread(target=httpd.serve_forever, daemon=True)
        thr.start()
        try:
            lines = ["noise %s" % i for i in range(150)]
            lines[40] = "FAIL test_login"
            data = "\n".join(lines).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:%s/log" % port,
                data=data,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
            self.assertIn("FAIL test_login", body)
            self.assertIn("rough tokens", body)

            health = urllib.request.urlopen(
                "http://127.0.0.1:%s/health" % port, timeout=5
            )
            info = json.loads(health.read().decode("utf-8"))
            self.assertTrue(info["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()


class SetupTests(unittest.TestCase):
    def test_merge_does_not_drop_other_server(self):
        tmp = Path(tempfile.mkdtemp(prefix="shrink-cfg-"))
        try:
            path = tmp / "mcp.json"
            path.write_text(json.dumps({
                "mcpServers": {"other": {"command": "nope", "args": ["1"]}}
            }), encoding="utf-8")
            shrink.merge_json(path, {
                "mcpServers": {"shrink": {"command": "py", "args": ["-3", "shrink.py", "mcp"]}}
            })
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["mcpServers"]["other"]["command"], "nope")
            self.assertEqual(data["mcpServers"]["shrink"]["command"], "py")
        finally:
            shutil.rmtree(tmp)


def git_repo():
    if shutil.which("git") is None:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="shrink-git-"))
    def g(*args):
        subprocess.run(
            ["git", *args],
            cwd=tmp,
            check=True,
            capture_output=True,
            text=True,
        )
    try:
        g("init", "-b", "main")
        g("config", "user.email", "dev@local")
        g("config", "user.name", "dev")
        g("config", "core.autocrlf", "false")
    except subprocess.CalledProcessError:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    note = tmp / "note.txt"
    note.write_text("one\n", encoding="utf-8", newline="\n")
    g("add", "note.txt")
    g("commit", "-m", "init")
    note.write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    return tmp


class GitTests(unittest.TestCase):
    def test_diff_and_snapshot_on_a_real_repo(self):
        repo = git_repo()
        if repo is None:
            self.skipTest("git init failed")
        try:
            text = shrink.do_diff(str(repo))
            self.assertIn("+two", text)
            self.assertIn("rough tokens", text)
            out, body = shrink.snapshot(repo)
            self.assertTrue(out.is_file())
            self.assertIn("+two", body)
            disk = out.read_text(encoding="utf-8")
            self.assertEqual(disk, body)
            # second pass with the same text should not leave a tmp behind
            shrink.snapshot(repo, prev=body)
            self.assertFalse(out.with_suffix(".txt.tmp").exists())
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class StdioTests(unittest.TestCase):
    def test_mcp_process_answers(self):
        proc = subprocess.Popen(
            [sys.executable, str(shrink.HERE), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        def ask(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read()
                self.fail("no reply from mcp\n" + err)
            return json.loads(line)

        try:
            hello = ask({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            })
            self.assertEqual(hello["result"]["serverInfo"]["version"], shrink.VER)
            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }) + "\n")
            proc.stdin.flush()
            tools = ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = [item["name"] for item in tools["result"]["tools"]]
            self.assertIn("shrink_diff", names)
            self.assertIn("shrink_log", names)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


class CliTests(unittest.TestCase):
    def test_version_flag(self):
        # captured nothing, just the exit. printing is fine
        self.assertEqual(shrink.main(["shrink", "--version"]), 0)
        self.assertEqual(shrink.main(["shrink", "version"]), 0)


if __name__ == "__main__":
    unittest.main()
