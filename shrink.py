# shrink: cuts diffs, logs and file trees before they land in a chat.
# runs on its own (cli, pipe, serve, watch) and as mcp.
# cursor, vscode, claude code, claude desktop all eat the same stdio.
#
#   py -3 shrink.py diff
#   py -3 shrink.py log build.log
#   py -3 shrink.py tree
#   py -3 shrink.py serve
#   py -3 shrink.py setup cursor --write

import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 0.2 was mcp only. 0.3.1 was the laptop script. 0.4 is the one I'd actually hand someone
VER = "0.4.0"

# a huge log hung the process once. stop reading past this
MAX_BYTES = 2_000_000

junk_dirs = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".next",
    "coverage",
    ".shrink",
}
lockfiles = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "poetry.lock",
}
# images and minified junk just add noise to a diff
bin_suffixes = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".pdf", ".zip", ".gz", ".woff", ".woff2",
    ".min.js", ".min.css", ".map",
)
# lines with these stay. left "warn" out on purpose, there are too many of them
hot_words = ("error", "exception", "traceback", "fail", "fatal", "panic", "assert")

HERE = Path(__file__).resolve()

HELP = """\
shrink cuts long text. the first line is a rough before -> after guess.

on its own:
  py -3 shrink.py diff                 git changes
  py -3 shrink.py log build.log        long log. no file means stdin
  py -3 shrink.py tree [folder]
  type build.log | py -3 shrink.py     a pipe is a log, no subcommand needed
  py -3 shrink.py serve [port]         http://127.0.0.1:8765  this machine only
  py -3 shrink.py watch                rewrites .shrink/last.txt every 2s

hook up an editor (cursor / vscode / claude, same protocol):
  py -3 shrink.py setup
  py -3 shrink.py setup vscode --write
  py -3 shrink.py mcp                  the editor starts this, don't run it by hand
"""


class GiveUp(Exception):
    pass


def fix_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


# False = haven't looked yet. None = tiktoken isn't installed, keep going.
_enc = False


def tok_encoder():
    global _enc
    if _enc is not False:
        return _enc
    try:
        import tiktoken
        _enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _enc = None
    return _enc


def rough_tokens(text):
    # words = len(text.split())
    # return max(1, (len(text) + 3) // 4) if text else 0
    # chars/4 drifted from the bill. cl100k when tiktoken is there, else a split
    if not text:
        return 0
    enc = tok_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    bits = re.findall(r"\w+|[^\w\s]", text)
    return max(1, len(bits))


def report(before, body, note=""):
    tail = f" ({note})" if note else ""
    head = f"rough tokens: {rough_tokens(before)} -> {rough_tokens(body)}{tail}\n\n"
    return head + body.rstrip() + "\n"


def read_capped(path):
    raw = Path(path).read_bytes()
    cut = len(raw) > MAX_BYTES
    text = raw[:MAX_BYTES].decode("utf-8", "replace")
    return text, cut


def read_stdin_capped():
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    cut = len(raw) > MAX_BYTES
    text = raw[:MAX_BYTES].decode("utf-8", "replace")
    return text, cut


def git_root(start):
    cur = Path(start).resolve()
    for folder in (cur, *cur.parents):
        if (folder / ".git").exists():
            return folder
    return None


def run_git(root, args):
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise GiveUp("git is not on PATH")
    if proc.returncode != 0:
        err = (proc.stderr or "git failed").strip()
        raise GiveUp(err)
    return proc.stdout


def grab_diff(root):
    # was: git diff   (without HEAD it missed stuff already staged)
    text = run_git(root, ["diff", "HEAD"])
    names = run_git(root, ["ls-files", "--others", "--exclude-standard"])
    if names.strip():
        text += "\nUntracked files, contents left out:\n" + names
    return text


def path_from_block(lines):
    for line in lines:
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"')
    return ""


def boring(path):
    name = Path(path).name.lower()
    if name in lockfiles:
        return True
    return name.endswith(bin_suffixes)


def split_diff(diff):
    blocks = []
    cur = []
    for line in diff.splitlines():
        if line.startswith("diff --git ") and cur:
            blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def squash_diff(diff):
    if not diff.strip():
        return "No changes."
    # short patches stay whole. the first cut mangled tiny edits
    if len(diff.splitlines()) <= 80:
        return diff.rstrip()

    pieces = []
    blocks = split_diff(diff)
    hidden_files = 0
    for lines in blocks:
        if len(pieces) >= 40:
            hidden_files += 1
            continue
        path = path_from_block(lines)
        if path and boring(path):
            pieces.append(f"# skipped junk file: {path}")
            continue
        # a git binary patch explodes into thousands of garbage lines
        if any(line.startswith("GIT binary patch") for line in lines):
            pieces.append(f"# binary patch, dropped the body: {path or '?'}")
            continue

        kept = []
        hidden = 0
        changes = 0
        for line in lines:
            header = line.startswith((
                "diff --git", "index ", "---", "+++", "@@",
                "new file", "deleted file", "rename ", "similarity ",
                "Binary files", "Untracked files",
            ))
            changed = (
                line.startswith(("+", "-"))
                and not line.startswith(("+++", "---"))
            )
            if header or (changed and changes < 80):
                kept.append(line)
                if changed:
                    changes += 1
            else:
                hidden += 1
        if hidden:
            kept.append(f"# lines hidden: {hidden}")
        pieces.append("\n".join(kept))

    if hidden_files:
        pieces.append(f"# more files not expanded: {hidden_files}")
    return "\n\n".join(pieces)


def squash_log(text):
    raw = text.splitlines()
    lines = []
    i = 0
    while i < len(raw):
        j = i + 1
        while j < len(raw) and raw[j] == raw[i]:
            j += 1
        n = j - i
        if n > 2:
            lines.append(f"{raw[i]}  x{n}")
        else:
            lines.extend(raw[i:j])
        i = j

    if len(lines) <= 80:
        return "\n".join(lines)

    hot = []
    for n, line in enumerate(lines):
        low = line.lower()
        if any(w in low for w in hot_words):
            hot.append(n)

    keep = set()
    if hot:
        for n in hot:
            keep.update(range(max(0, n - 2), min(len(lines), n + 4)))
        keep.update(range(max(0, len(lines) - 15), len(lines)))
    else:
        # no errors found. keep the start (usually the command) and the tail (usually the summary)
        keep.update(range(min(20, len(lines))))
        keep.update(range(max(0, len(lines) - 40), len(lines)))

    out = []
    hidden = 0
    for n, line in enumerate(lines):
        if n in keep:
            if hidden:
                out.append(f"... lines hidden: {hidden}")
                hidden = 0
            out.append(line)
        else:
            hidden += 1
    if hidden:
        out.append(f"... lines hidden: {hidden}")
    return "\n".join(out)


def squash_tree(root, depth_limit=3, per_dir=12):
    root = Path(root).resolve()
    lines = [root.name + "/"]
    seen = 0

    def walk(folder, depth):
        nonlocal seen
        if depth > depth_limit:
            return
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        dirs, files = [], []
        for entry in entries:
            if entry.name in junk_dirs:
                continue
            # if entry.name.startswith("."):
            #     continue
            # that dropped .github. don't blanket-skip dotted folders anymore
            if entry.is_dir():
                dirs.append(entry)
            elif entry.is_file():
                files.append(entry)
        dirs.sort(key=lambda p: p.name.lower())
        files.sort(key=lambda p: p.name.lower())
        pad = "  " * depth
        for entry in files[:per_dir]:
            seen += 1
            lines.append(f"{pad}{entry.name}")
        rest = len(files) - per_dir
        if rest > 0:
            seen += rest
            lines.append(f"{pad}... more files: {rest}")
        for entry in dirs:
            lines.append(f"{pad}{entry.name}/")
            walk(entry, depth + 1)

    walk(root, 1)
    lines.append(f"\nfiles in this list: {seen}")
    return "\n".join(lines)


def file_dump(folder, limit=4000):
    # names = [str(p) for p in Path(folder).rglob("*") if p.is_file()]
    # rglob wandered into node_modules and sat there for a minute. walk + skip
    names = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [n for n in dirnames if n not in junk_dirs]
        for name in filenames:
            names.append(str(Path(dirpath) / name))
            if len(names) >= limit:
                names.append(f"... stopped after {limit}, not going further")
                return "\n".join(names)
    return "\n".join(names)


def do_diff(path=None):
    start = Path(path) if path else Path.cwd()
    if path and not start.exists():
        raise GiveUp(f"folder not found: {path}")
    root = git_root(start)
    if root is None:
        raise GiveUp("not a git repo. cd into the project and run it again")
    before = grab_diff(root)
    return report(before, squash_diff(before))


def do_log(path=""):
    note = ""
    if path:
        fp = Path(path)
        if not fp.is_file():
            raise GiveUp(f"file not found: {path}")
        before, cut = read_capped(fp)
    else:
        before, cut = read_stdin_capped()
    if cut:
        note = "input truncated"
    return report(before, squash_log(before), note)


def do_tree(path=""):
    folder = Path(path) if path else Path.cwd()
    if not folder.is_dir():
        raise GiveUp(f"folder not found: {path}")
    return report(file_dump(folder), squash_tree(folder))


# --- mcp ---
# stdout here is json only, one line per reply. debug goes to stderr.
# cursor sends a json line. one old fork sent Content-Length like lsp,
# so the second branch is still here. not ripping it out in case it shows up again.

def tool_text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name, args):
    args = args or {}
    try:
        if name == "shrink_diff":
            return tool_text(do_diff(args.get("path") or ""))
        if name == "shrink_log":
            text = args.get("text") or ""
            note = ""
            if len(text.encode("utf-8")) > MAX_BYTES:
                text = text.encode("utf-8")[:MAX_BYTES].decode("utf-8", "replace")
                note = "input truncated"
            return tool_text(report(text, squash_log(text), note))
        if name == "shrink_tree":
            return tool_text(do_tree(args.get("path") or ""))
    except GiveUp as err:
        return tool_text(str(err), True)
    except Exception as err:
        return tool_text(str(err), True)
    return tool_text(f"no such tool: {name}", True)


TOOLS = [
    {
        "name": "shrink_diff",
        "description": "Short git diff plus a rough guess of how much text was cut.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "project folder, can be empty"},
            },
        },
    },
    {
        "name": "shrink_log",
        "description": "Keeps errors and the tail of a long log.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "shrink_tree",
        "description": "Short folder tree, node_modules and other junk left out.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
]


def mcp_reply(msg):
    method = msg.get("method", "")
    mid = msg.get("id")
    if method == "initialize":
        ver = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shrink", "version": VER},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    # vscode and claude ask for these. answering -32601 made the server look dead
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resourceTemplates": []}}
    if method == "logging/setLevel":
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": call_tool(params.get("name"), params.get("arguments")),
        }
    if mid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": "not implemented: %s" % method},
    }


def write_mcp(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_framed(first_line):
    # Content-Length: 123\r\n
    # \r\n
    # {json}
    try:
        n = int(first_line.split(":", 1)[1].strip())
    except ValueError:
        return ""
    while True:
        extra = sys.stdin.readline()
        if extra == "" or extra.strip() == "":
            break
    return sys.stdin.read(n)


def mcp_loop():
    while True:
        line = sys.stdin.readline()
        if line == "":
            return
        if line.lower().startswith("content-length:"):
            raw = read_framed(line)
        else:
            raw = line.strip()
            if not raw:
                continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            sys.stderr.write("bad json, skipped the line\n")
            continue
        ans = mcp_reply(msg)
        if ans is not None:
            write_mcp(ans)


# --- local http, for anything that can't speak mcp ---

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "shrink/" + VER
    # was: leave Python/x.y.z in the header. no reason to advertise the interpreter
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write((fmt % args) + "\n")

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # browser chats on localhost get blocked without this
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path != "/health":
            self._send(404, "nope\n")
            return
        body = json.dumps({"ok": True, "ver": VER})
        self._send(200, body, "application/json; charset=utf-8")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(400, "bad Content-Length\n")
            return
        if n > MAX_BYTES:
            self._send(413, "request too big\n")
            return
        raw = self.rfile.read(n)
        path = self.path.split("?", 1)[0]
        try:
            if path == "/log":
                text = raw.decode("utf-8", "replace")
                self._send(200, report(text, squash_log(text)))
                return
            if path != "/shrink":
                self._send(404, "nope\n")
                return
            payload = json.loads(raw.decode("utf-8"))
            # op = payload["kind"]
            op = payload.get("op") or payload.get("kind") or ""
            if op == "log":
                text = payload.get("text") or ""
                self._send(200, report(text, squash_log(text)))
            elif op == "diff":
                self._send(200, do_diff(payload.get("path") or ""))
            elif op == "tree":
                self._send(200, do_tree(payload.get("path") or ""))
            else:
                self._send(400, "op must be log, diff or tree\n")
        except GiveUp as err:
            self._send(400, str(err) + "\n")
        except json.JSONDecodeError:
            self._send(400, "not json. raw text goes to /log\n")
        except Exception as err:
            self._send(500, str(err) + "\n")


def make_server(port):
    # ThreadingHTTPServer.allow_reuse_address = True
    # on windows the port stuck around after tests. serve stays as-is, tests use port 0
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


def serve(port):
    # httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    # once this was reachable from a cafe wifi. localhost only
    httpd = make_server(port)
    bound = httpd.server_address[1]
    sys.stderr.write(f"listening on http://127.0.0.1:{bound}\n")
    sys.stderr.write("POST /log with raw text, POST /shrink json {op,text|path}, GET /health\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
    finally:
        httpd.server_close()


def snapshot(root, prev=None):
    before = grab_diff(root)
    body = report(before, squash_diff(before))
    out = Path(root) / ".shrink" / "last.txt"
    if body == prev and out.exists():
        return out, body
    out.parent.mkdir(exist_ok=True)
    # out.write_text(body, encoding="utf-8")
    # the agent read a half file once. write aside, then rename
    tmp = out.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(out)
    return out, body


def watch(path, every):
    root = git_root(Path(path) if path else Path.cwd())
    if root is None:
        raise GiveUp("watch only works inside a git repo")
    sys.stderr.write("writing %s every %ss, ctrl+c to stop\n" % (root / ".shrink" / "last.txt", every))
    prev = None
    try:
        while True:
            _out, body = snapshot(root, prev)
            if body != prev:
                prev = body
                sys.stderr.write("updated\n")
            time.sleep(every)
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")


def launcher():
    # C:\Windows\System32\python is the store stub. cursor launched it and sat there silent
    if os.name == "nt":
        return ["py", "-3"]
    return [sys.executable]


def snippet(kind):
    cmd = launcher()
    script = str(HERE)
    if kind == "vscode":
        return {
            "servers": {
                "shrink": {
                    "type": "stdio",
                    "command": cmd[0],
                    "args": [*cmd[1:], script, "mcp"],
                }
            }
        }
    block = {
        "mcpServers": {
            "shrink": {
                "command": cmd[0],
                "args": [*cmd[1:], script, "mcp"],
            }
        }
    }
    return block


def config_path(kind):
    home = Path.home()
    if kind == "cursor":
        return home / ".cursor" / "mcp.json"
    if kind == "vscode":
        return Path.cwd() / ".vscode" / "mcp.json"
    if kind == "claude":
        return Path.cwd() / ".mcp.json"
    if kind == "claude-desktop":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    raise GiveUp("unknown target: " + kind + ". use cursor, vscode, claude, claude-desktop")


def merge_json(path, fresh):
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise GiveUp(f"{path} is broken json, fix it yourself, I won't touch it")
        if not isinstance(data, dict):
            raise GiveUp(f"{path} is not a json object")
    # used to write the fresh blob over the file and wipe other servers. not doing that again
    for key, val in fresh.items():
        if isinstance(val, dict):
            slot = data.get(key)
            if not isinstance(slot, dict):
                slot = {}
            slot.update(val)
            data[key] = slot
        else:
            data[key] = val
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def setup(kind, write):
    kind = kind or "cursor"
    body = snippet(kind)
    text = json.dumps(body, ensure_ascii=False, indent=2)
    if not write:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        where = config_path(kind)
        sys.stderr.write(f"\nthis is for {kind}. the file usually lives at: {where}\n")
        sys.stderr.write("to merge it in with the other servers, add --write\n")
        return
    path = config_path(kind)
    merge_json(path, body)
    sys.stderr.write(f"added shrink to {path}\n")
    sys.stderr.write("restart the editor, it does not always pick the config up live\n")


def pick_port(argv):
    if "--port" in argv:
        i = argv.index("--port")
        try:
            return int(argv[i + 1])
        except (IndexError, ValueError):
            raise GiveUp("--port needs a number")
    if len(argv) >= 3 and argv[2].isdigit():
        return int(argv[2])
    return 8765


def emit(text):
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()


def main(argv=None):
    if argv is None:
        argv = sys.argv
    argv = list(argv)
    fix_stdio()

    try:
        if len(argv) < 2:
            if not sys.stdin.isatty():
                emit(do_log(""))
                return 0
            sys.stdout.write(HELP)
            return 0

        cmd = argv[1]
        if cmd in ("-v", "--version", "version"):
            sys.stdout.write(VER + "\n")
            return 0
        if cmd in ("-h", "--help", "help"):
            sys.stdout.write(HELP)
            return 0
        if cmd == "diff":
            emit(do_diff(argv[2] if len(argv) > 2 else ""))
        elif cmd == "log":
            emit(do_log(argv[2] if len(argv) > 2 else ""))
        elif cmd == "tree":
            emit(do_tree(argv[2] if len(argv) > 2 else ""))
        elif cmd == "mcp":
            mcp_loop()
        elif cmd == "serve":
            serve(pick_port(argv))
        elif cmd == "watch":
            every = 2
            if "--every" in argv:
                every = float(argv[argv.index("--every") + 1])
            target = ""
            for item in argv[2:]:
                if item.startswith("--"):
                    break
                if item.isdigit():
                    continue
                target = item
                break
            watch(target, every)
        elif cmd == "setup":
            kind = "cursor"
            write = False
            for item in argv[2:]:
                if item == "--write":
                    write = True
                elif not item.startswith("-"):
                    kind = item
            setup(kind, write)
        else:
            sys.stderr.write(f"unknown command {cmd}\n\n")
            sys.stdout.write(HELP)
            return 1
    except GiveUp as err:
        sys.stderr.write(str(err) + "\n")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
