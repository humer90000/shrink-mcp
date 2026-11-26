# shrink

Cuts git diffs, test logs, and folder trees before they land in an agent chat. The first line is a rough before -> after count.

Use it from a terminal, over localhost HTTP, or as an MCP server. Cursor, VS Code, Claude Code, and Claude Desktop all speak MCP. Only the config file changes.

Python 3.9 or newer. No required packages. MIT license.

On this Windows box `python` is the store stub and does nothing. Use `py -3`. If your `python` actually runs, use that instead.

## install

From a checkout:

```
py -3 -m pip install -e .
shrink --version
```

# was: python setup.py install
# there is no setup.py anymore

Without installing, the same thing is `py -3 shrink.py ...` from this folder.

Optional, closer token counts (cl100k, same family most chat APIs use):

```
py -3 -m pip install -e ".[tokens]"
```

Without that extra the count is a split on words and punctuation. It is still a guess, not the invoice.

## use it

```
shrink diff
shrink log build.log
shrink tree
type build.log | shrink
```

`diff` has to run inside a git repo. Lockfiles, images and binary patches are not expanded. A short diff is left alone. Cutting those made small edits worse.

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
```

Merge it in. Other servers in that file stay put:

```
shrink setup cursor --write
shrink setup vscode --write
shrink setup claude --write
shrink setup claude-desktop --write
```

Where `--write` goes:

- cursor: `%USERPROFILE%\.cursor\mcp.json`
- vscode: `.vscode/mcp.json` in the current folder
- claude code: `.mcp.json` in the current folder
- claude desktop: `%APPDATA%\Claude\claude_desktop_config.json`

Restart the editor after that. It does not always reload a config that is already open.

The tools are `shrink_diff`, `shrink_log`, `shrink_tree`. Don't start `shrink mcp` yourself. The editor does.

## what it will not do

It does not call a model. It does not apply the diff back. It does not open a port on the network. A log bigger than about 2 MB is cut. The token line is an estimate unless you installed the `tokens` extra.

## tests

```
py -3 tests\test_shrink.py
```

GitHub runs the same file on push.
