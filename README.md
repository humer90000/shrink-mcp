# shrink-mcp

Experimental project. The package name is shrink-mcp. The command is still `shrink`. Fewer tokens for coding agents. Shrinks a git diff, a test log, or a repo tree when the context window is getting fat and the model starts forgetting. The first line is a rough before -> after token count. It is useful, and it is not a finished product.

Use it from a terminal, over localhost HTTP, or as an MCP server. Cursor, Claude Code, Codex, VS Code, and Claude Desktop can all call it. Only the config file changes.

Python 3.9 or newer. No required packages. MIT license.

On this Windows box `python` is the store stub and does nothing. Use `py -3`. If your `python` actually runs, use that instead.

## examples

Both were `py -3 shrink.py log build.log` on this machine, no tiktoken. The first line of the answer is the before -> after count.

### Same line, 200 times

The file had 201 lines. Line 1 through 200 were identical. Line 201 was the error.

```
compile ok
compile ok
compile ok
... 197 more "compile ok" ...
Error: disk full, cannot write C:/build/out.bin
```

415 tokens in. The program printed only this:

```
rough tokens: 415 -> 18

compile ok  x200
Error: disk full, cannot write C:/build/out.bin
```

`x200` means the line was there 200 times. The error sentence is unchanged.

### 120 different lines, one error in the middle

Nothing repeats. `file_001` through `file_120`, and at `file_071` the compiler error:

```
compiled src/file_001.ts
compiled src/file_002.ts
compiled src/file_003.ts
... file_004 through file_070, each name different ...
src/pay.ts(40,12): error TS2304: Cannot find name total
compiled src/file_072.ts
... file_073 through file_120 ...
```

732 tokens in, 176 back. `file_001` through `file_068` are gone. The error is still there, with the two lines above it, and the end of the file is still there:

```
rough tokens: 732 -> 176

... lines hidden: 68
compiled src/file_069.ts
compiled src/file_070.ts
src/pay.ts(40,12): error TS2304: Cannot find name total
compiled src/file_072.ts
compiled src/file_073.ts
compiled src/file_074.ts
compiled src/file_075.ts
compiled src/file_076.ts
compiled src/file_077.ts
compiled src/file_078.ts
... lines hidden: 27
compiled src/file_106.ts
compiled src/file_107.ts
compiled src/file_108.ts
compiled src/file_109.ts
compiled src/file_110.ts
compiled src/file_111.ts
compiled src/file_112.ts
compiled src/file_113.ts
compiled src/file_114.ts
compiled src/file_115.ts
compiled src/file_116.ts
compiled src/file_117.ts
compiled src/file_118.ts
compiled src/file_119.ts
compiled src/file_120.ts
```

`... lines hidden: 68` is `file_001` to `file_068`. The second hidden block is `file_079` to `file_105`. What a person needs from this log is the `TS2304` line, and it survived.

## install

From PyPI:

```
py -3 -m pip install shrink-mcp
shrink --version
```

From a checkout of this repo:

```
py -3 -m pip install -e .
```

# was: python setup.py install
# there is no setup.py anymore

Without installing, the same thing is `py -3 shrink.py ...` from this folder.

Optional, closer token counts (cl100k, same family most chat APIs use):

```
py -3 -m pip install "shrink-mcp[tokens]"
```

Without that extra the count is a split on words and punctuation. It is still a guess, not the invoice.

## use it

```
shrink diff
shrink log build.log
shrink tree
type build.log | shrink
```

`shrink log` with no file and no pipe used to wait forever. It now says so. A UTF-16 log from Notepad is read, including when it is posted to `/log`. Repeated lines collapse (`compile ok  x200`). When the lines are all different and the file is long, it keeps the error, a few lines around it, and the tail. That is the second example above.

A line counts as an error for pytest (`FAIL`, `assert`), npm (`npm ERR!`), Rust (`error[E0308]`, `panicked`), Go (`FAIL`), a Python traceback (the exception at the bottom stays, not just the word Traceback), and a shell crash (`segmentation fault`). A line that only says `0 errors` or `errors: 0` does not.

`diff` has to run inside a git repo. Lockfiles, images and binary patches are not expanded, even when the diff is short. A long file keeps the header and the first chunk of changes, then a line saying how many were hidden. A repo that has no commit yet lists the new files instead of dying on `HEAD`. A short normal diff is left alone. Cutting those made small edits worse.

`tree` skips `node_modules`, `.git`, `.venv`, `venv`, `dist`, `build`, `target`, `__pycache__`, `.next`, `coverage` and `*.egg-info`. It opens three levels. Anything deeper is one note, `deeper folders not opened`, not a list of names. A directory with a lot of files shows the first dozen and a count of the rest.

```
shrink serve
curl.exe -s --data-binary @build.log http://127.0.0.1:8765/log
```

Listens on 127.0.0.1 only. `GET /health`. `POST /shrink` takes `{"op":"log","text":"..."}` or `diff` / `tree` with `path`. The field `kind` still works. The draft called it that.

Other port: `shrink serve 9000`.

`shrink watch` rewrites `.shrink/last.txt` every two seconds, for an agent that can read a file and cannot start MCP. Don't commit `.shrink`.

## editor

Print the snippet:

```
shrink setup
shrink setup vscode
shrink setup claude
shrink setup claude-desktop
shrink setup codex
```

Merge it in. Other servers in that file stay put. Codex is toml, so `--write` appends a block and will not add a second one:

```
shrink setup cursor --write
shrink setup vscode --write
shrink setup claude --write
shrink setup claude-desktop --write
shrink setup codex --write
```

Where `--write` goes:

- cursor: `%USERPROFILE%\.cursor\mcp.json`
- vscode: `.vscode/mcp.json` in the current folder
- claude code: `.mcp.json` in the current folder
- claude desktop: `%APPDATA%\Claude\claude_desktop_config.json`
- codex: `%USERPROFILE%\.codex\config.toml`

Restart the editor after that. It does not always reload a config that is already open.

The tools are `shrink_diff`, `shrink_log`, `shrink_tree`. Don't start `shrink mcp` yourself. The editor does.

## what it will not do

It does not call a model. It does not apply the diff back. It does not open a port on the network. A log bigger than about 2 MB is cut. The token line is an estimate unless you installed the `tokens` extra.
