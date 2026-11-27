import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
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

    def test_short_lockfile_still_skipped(self):
        # self.assertNotIn("skipped", out)  # back when anything under 80 lines was sacred
        body = ["diff --git a/package-lock.json b/package-lock.json"]
        body += ["+%s" % i for i in range(30)]
        out = shrink.squash_diff("\n".join(body))
        self.assertIn("skipped", out)
        self.assertNotIn("+29", out)

    def test_minified_line_gets_clipped(self):
        fat = "diff --git a/app.js b/app.js\n+++ b/app.js\n+" + ("x" * 20000)
        out = shrink.squash_diff(fat)
        self.assertIn("clipped", out)
        self.assertLess(len(out), 2000)

    def test_failover_is_not_a_crash(self):
        lines = ["noise %s" % i for i in range(120)]
        lines[60] = "pod failover to node b"
        out = shrink.squash_log("\n".join(lines))
        self.assertNotIn("failover", out)

    def test_quoted_image_path(self):
        body = ['diff --git "a/my icon.png" "b/my icon.png"']
        body += ["+%s" % i for i in range(90)]
        out = shrink.squash_diff("\n".join(body))
        self.assertIn("my icon.png", out)
        self.assertIn("skipped", out)

    def test_read_stops_early(self):
        tmp = Path(tempfile.mkdtemp(prefix="shrink-cap-"))
        try:
            fp = tmp / "big.log"
            fp.write_bytes(b"abcdefghij")
            text, cut = shrink.read_capped(fp, limit=4)
            self.assertTrue(cut)
            self.assertEqual(text, "abcd")
        finally:
            shutil.rmtree(tmp)

    def test_deep_tree_says_so(self):
        tmp = Path(tempfile.mkdtemp(prefix="shrink-deep-"))
        try:
            folder = tmp
            for name in ("a", "b", "c", "d", "e"):
                folder = folder / name
            folder.mkdir(parents=True)
            (folder / "leaf.txt").write_text("x\n", encoding="utf-8")
            text = shrink.squash_tree(tmp)
            self.assertIn("deeper folders not opened", text)
            self.assertNotIn("leaf.txt", text)
        finally:
            shutil.rmtree(tmp)

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
            egg = tmp / "demo.egg-info"
            egg.mkdir()
            (egg / "PKG-INFO").write_text("Name: demo\n", encoding="utf-8")
            text = shrink.squash_tree(tmp)
            self.assertIn("main.py", text)
            self.assertNotIn("left-pad", text)
            self.assertNotIn("node_modules", text)
            self.assertNotIn("PKG-INFO", text)
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
        self.assertEqual(ans["result"]["serverInfo"]["name"], "shrink-mcp")
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
        payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode("utf-8")
        buf = io.BytesIO(b"\r\n" + payload)
        raw = shrink.read_framed(b"Content-Length: %d" % len(payload), buf)
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

    def test_other_routes(self):
        httpd = shrink.make_server(0)
        port = httpd.server_address[1]
        thr = threading.Thread(target=httpd.serve_forever, daemon=True)
        thr.start()
        try:
            payload = json.dumps({"kind": "log", "text": "Error: via kind"}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%s/shrink" % port,
                data=payload,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertIn("via kind", resp.read().decode("utf-8"))

            bad = urllib.request.Request(
                "http://127.0.0.1:%s/shrink" % port,
                data=b"not-json",
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(bad, timeout=5)
            self.assertEqual(caught.exception.code, 400)

            nope = urllib.request.Request(
                "http://127.0.0.1:%s/nope" % port,
                data=b"x",
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(nope, timeout=5)
            self.assertEqual(caught.exception.code, 404)

            opt = urllib.request.Request("http://127.0.0.1:%s/log" % port, method="OPTIONS")
            with urllib.request.urlopen(opt, timeout=5) as resp:
                self.assertIn(resp.status, (200, 204))
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

    def test_codex_appends_and_does_not_duplicate(self):
        tmp = Path(tempfile.mkdtemp(prefix="shrink-codex-"))
        try:
            path = tmp / "config.toml"
            path.write_text('model = "x"\n', encoding="utf-8")
            block = shrink.codex_text()
            self.assertIn("[mcp_servers.\"shrink-mcp\"]", block)
            self.assertTrue(block.strip().endswith("mcp\"]") or "mcp" in block)
            self.assertTrue(shrink.merge_codex(path, block))
            text = path.read_text(encoding="utf-8")
            self.assertIn('model = "x"', text)
            self.assertEqual(text.count("[mcp_servers.\"shrink-mcp\"]"), 1)
            self.assertFalse(shrink.merge_codex(path, block))
            self.assertEqual(path.read_text(encoding="utf-8").count("[mcp_servers.\"shrink-mcp\"]"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_this_checkout_can_be_diffed(self):
        # a drive that doesn't store file owners makes git refuse. we don't touch git config
        root = shrink.git_root(Path(__file__).resolve().parent)
        if root is None:
            self.skipTest("not a git checkout")
        argv = shrink.git_argv(root, ["status"])
        self.assertTrue(any(part.startswith("safe.directory=") for part in argv))
        # if git still refused the folder, this raises instead of returning a diff
        text = shrink.do_diff(str(root))
        self.assertTrue(text.startswith("rough tokens"))


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

    def test_repo_with_no_commit_lists_the_new_file(self):
        if shutil.which("git") is None:
            self.skipTest("no git")
        tmp = Path(tempfile.mkdtemp(prefix="shrink-new-"))
        try:
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=tmp,
                check=True,
                capture_output=True,
            )
            (tmp / "note.txt").write_text("hi\n", encoding="utf-8")
            text = shrink.do_diff(str(tmp))
            self.assertIn("note.txt", text)
            self.assertNotIn("ambiguous argument", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_new_file_still_listed_next_to_a_long_diff(self):
        repo = git_repo()
        if repo is None:
            self.skipTest("git init failed")
        try:
            note = repo / "note.txt"
            note.write_text("one\n" + "\n".join("line %s" % i for i in range(100)), encoding="utf-8", newline="\n")
            (repo / "other.txt").write_text("brand new\n", encoding="utf-8")
            text = shrink.do_diff(str(repo))
            self.assertIn("other.txt", text)
            self.assertIn("+line 1", text)
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

    def test_bad_flags_dont_traceback(self):
        self.assertEqual(shrink.main(["shrink", "serve", "99999"]), 1)
        self.assertEqual(shrink.main(["shrink", "watch", "--every"]), 1)
        self.assertEqual(shrink.main(["shrink", "watch", "--every", "nope"]), 1)


class LogShapes(unittest.TestCase):
    def test_ansi_and_npm_and_pytest(self):
        lines = ["ok %s" % i for i in range(100)]
        lines[30] = "\x1b[31mERROR\x1b[0m: disk full"
        lines[55] = "Found 0 errors"
        lines[70] = "npm ERR! code ELIFECYCLE"
        out = shrink.squash_log("\n".join(lines))
        self.assertIn("disk full", out)
        self.assertIn("ELIFECYCLE", out)
        self.assertNotIn("0 errors", out)

        tail = ["passed %s" % i for i in range(40)]
        tail += ["E   assert 1 == 2", "FAILED tests/test_a.py::test_bar"]
        tail += ["done %s" % i for i in range(40)]
        out = shrink.squash_log("\n".join(tail))
        self.assertIn("assert 1 == 2", out)
        self.assertIn("FAILED", out)

    def test_utf16_notepad_and_a_png(self):
        tmp = Path(tempfile.mkdtemp(prefix="shrink-log-"))
        try:
            logf = tmp / "build.log"
            body = "\n".join(["noise %s" % i for i in range(60)] + ["Error: from notepad"])
            logf.write_bytes(body.encode("utf-16"))
            self.assertIn("from notepad", shrink.do_log(str(logf)))
            png = tmp / "shot.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
            with self.assertRaises(shrink.GiveUp):
                shrink.do_log(str(png))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uv_lock_and_stringy_mcp_args(self):
        body = ["diff --git a/uv.lock b/uv.lock"] + ["+%s" % i for i in range(15)]
        self.assertIn("skipped", shrink.squash_diff("\n".join(body)))
        ans = shrink.call_tool("shrink_log", json.dumps({"text": "Error: from a string"}))
        self.assertFalse(ans["isError"])
        self.assertIn("from a string", ans["content"][0]["text"])

    def test_editor_config_shapes(self):
        # cursor and claude share mcpServers. vscode insists on servers + type
        cur = shrink.snippet("cursor")
        vs = shrink.snippet("vscode")
        claude = shrink.snippet("claude")
        desktop = shrink.snippet("claude-desktop")
        self.assertEqual(cur["mcpServers"]["shrink-mcp"]["args"][-1], "mcp")
        self.assertEqual(claude["mcpServers"]["shrink-mcp"]["args"][-1], "mcp")
        self.assertEqual(desktop["mcpServers"]["shrink-mcp"]["args"][-1], "mcp")
        self.assertEqual(vs["servers"]["shrink-mcp"]["type"], "stdio")
        self.assertEqual(vs["servers"]["shrink-mcp"]["args"][-1], "mcp")

    def test_real_failure_shapes(self):
        def pile(mid):
            lines = ["step %s ok" % i for i in range(120)]
            for i, line in enumerate(mid):
                lines[60 + i] = line
            return "\n".join(lines)

        tb = pile([
            "Traceback (most recent call last):",
            '  File "app.py", line 3, in <module>',
            '  File "app.py", line 9, in main',
            '  File "app.py", line 14, in run',
            '  File "app.py", line 20, in inner',
            '  File "app.py", line 28, in deep',
            "SystemExit: 2",
        ])
        out = shrink.squash_log(tb)
        self.assertIn("SystemExit: 2", out)
        self.assertLess(len(out), len(tb))

        jest = pile([
            "FAIL src/a.test.js",
            "  ● adds",
            "    Expected: 1",
            "    Received: 2",
        ])
        out = shrink.squash_log(jest)
        self.assertIn("Received: 2", out)

        green = pile(["errors: 0, warnings: 12"])
        self.assertNotIn("errors: 0", shrink.squash_log(green))
        green = pile(["0 failures, 40 passed"])
        self.assertNotIn("0 failures", shrink.squash_log(green))

        crash = pile(["segmentation fault (core dumped)"])
        self.assertIn("segmentation fault", shrink.squash_log(crash))
        oom = pile(["process out of memory"])
        self.assertIn("out of memory", shrink.squash_log(oom))

        more = {
            "error[E0308]: mismatched types": "E0308",
            "--- FAIL: TestParse (0.01s)": "TestParse",
            "npm ERR! code ELIFECYCLE": "ELIFECYCLE",
            "main.c:10:5: error: 'x' undeclared": "undeclared",
            "    + FullyQualifiedErrorId : NativeCommandError": "NativeCommandError",
            "PHP Fatal error:  Uncaught Error: call to undefined": "Fatal error",
            "error MSB3027: Could not copy the file": "MSB3027",
            "ERROR in ./src/index.js": "index.js",
        }
        for line, needle in more.items():
            out = shrink.squash_log(pile([line]))
            self.assertIn(needle, out, line)

    def test_http_utf16_and_binary_mcp_line(self):
        httpd = shrink.make_server(0)
        port = httpd.server_address[1]
        thr = threading.Thread(target=httpd.serve_forever, daemon=True)
        thr.start()
        try:
            body = "\n".join(["n %s" % i for i in range(90)] + ["Error: utf16 http"])
            req = urllib.request.Request(
                "http://127.0.0.1:%s/log" % port,
                data=body.encode("utf-16"),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode("utf-8")
            self.assertIn("utf16 http", text)
        finally:
            httpd.shutdown()
            httpd.server_close()

        proc = subprocess.Popen(
            [sys.executable, str(shrink.HERE), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            msg = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "cursor", "version": "0"},
                },
            }).encode("utf-8")
            proc.stdin.write(msg + b"\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            self.assertTrue(line.endswith(b"\n"))
            self.assertFalse(line.endswith(b"\r\n"))
            hello = json.loads(line)
            self.assertEqual(hello["result"]["serverInfo"]["name"], "shrink-mcp")

            log = "\n".join(["ok %s" % i for i in range(80)] + ["panic: from framed"])
            call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "shrink_log", "arguments": {"text": log}},
            }
            raw = json.dumps(call).encode("utf-8")
            proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
            proc.stdin.flush()
            line = proc.stdout.readline()
            ans = json.loads(line)
            self.assertIn("from framed", ans["result"]["content"][0]["text"])
        finally:
            proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


class ConnectTests(unittest.TestCase):
    def test_cursor_style_session_calls_the_log_tool(self):
        proc = subprocess.Popen(
            [sys.executable, str(shrink.HERE), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        def ask(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                self.fail(proc.stderr.read())
            return json.loads(line)

        log = "\n".join(["compile %s" % i for i in range(80)] + ["Error: cannot find module left-pad"])
        try:
            hello = ask({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "cursor", "version": "1.0"},
                },
            })
            self.assertEqual(hello["result"]["protocolVersion"], "2025-06-18")
            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }) + "\n")
            proc.stdin.flush()
            # prompts/list is what vscode sends before tools. must not be an error
            prompts = ask({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
            self.assertEqual(prompts["result"]["prompts"], [])
            called = ask({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "shrink_log", "arguments": {"text": log}},
            })
            text = called["result"]["content"][0]["text"]
            self.assertIn("left-pad", text)
            self.assertIn("rough tokens", text)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
