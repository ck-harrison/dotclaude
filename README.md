# DotClaude

A local web app for browsing and editing every file Claude Code reads on your Mac. Each file comes with an explanation of what it does, when it loads, and whether it's shared or personal.

## Run

From the Dock: build the Dock app once (it uses the icon in `icon.svg`):

```bash
bash ~/Code/dotclaude/make-dock-app.sh --add-to-dock
```

Clicking it starts DotClaude if it isn't running and opens it in your browser. It reuses a running copy instead of starting a second one. Stop it with the **⏻ Stop** button in the sidebar. Launcher log: `~/Library/Logs/DotClaude/launcher.log`.

From Terminal:

```bash
python3 ~/Code/dotclaude/dotclaude.py
```

It runs at `http://127.0.0.1:8765/` (or the next free port up to 8784). You don't need to install anything; it uses only the Python that ships with macOS (3.9+).

From AIHQ: **Workspace → DotClaude ↗** in the sidebar launches it if needed and opens it in a new tab. AIHQ doesn't start DotClaude by itself.

## What it shows

- **Global**: `~/.claude/` (CLAUDE.md, settings.json, keybindings.json, rules, skills, agents, commands, output styles, workflows, themes, subagent memory) and `~/.claude.json` (read-only)
- **Projects**: every folder you've opened Claude Code in (read from `~/.claude.json`), plus any folders you add. For each one: CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md, AGENTS.md, settings files, .mcp.json, rules/skills/agents/commands, auto memory, CLAUDE.md files inherited from parent folders, and subfolder CLAUDE.md files
- **Claude desktop app**: `claude_desktop_config.json` (MCP servers for the chat app). Your claude.ai Projects are stored in your account, not on your Mac, so they don't appear here
- **Organisation-managed**: `/Library/Application Support/ClaudeCode/` (read-only)

## Best-practice review

Click **✦ Review against best practice** on any file, or on a project's page to review all of its files together. The app runs Claude Code on your Mac (`claude -p`) with only the WebFetch and WebSearch tools enabled, so it can read the web but can't touch your files. It reads Anthropic's current guidance (memory, best practices, skills, subagents, settings, permissions, hooks), then reports:

- findings sorted by severity, each linked to the official page that supports it
- suggested rewrites, shown as a diff. **Apply** saves the rewrite and backs up the current file first; **Open in editor** lets you adjust it before saving

Notes:
- It uses your Claude plan's usage. A project review took about 45 seconds in testing
- Values in JSON files that look like keys or tokens (and everything under `env`) are replaced with `<redacted>` before anything is sent. Those files get findings but no rewrites
- If an official page couldn't be loaded, the results say so
- Reports are saved in `~/.dotclaude/reviews/`
- It needs the `claude` command to work in Terminal. If you start the app some other way, set `DOTCLAUDE_CLAUDE_BIN=/path/to/claude`

## Backups

Every save, applied review suggestion and restore first copies the old version to `~/.dotclaude/backups/<timestamp>/`.

- **History** (in each file's toolbar): that file's earlier versions, newest first, each with the changes compared to the current file. **Restore this version** puts it back, and backs up the current version first, so a restore can be undone too.
- **Backups** (sidebar, under Global): every saved version of every file, grouped by file. This includes files DotClaude no longer manages, which you can view and copy but not restore.

## Safety

- The app only listens on 127.0.0.1, and every request needs a random token that changes each time you start it
- It can only read or write the Claude files it lists, never anything else
- Before each save, the old version is copied to `~/.dotclaude/backups/<timestamp>/`
- JSON files aren't saved if they're invalid
- If Claude Code changed a file while you had it open, the app asks before overwriting it
- `~/.claude.json`, managed files, and the desktop app's `config.json` are read-only

Folders you add are stored in `~/.dotclaude/config.json`. If you used it when it was called Claude Files, `~/.claude-files` is moved to `~/.dotclaude` the first time it starts.

`GET /health` is the only request that works without the token. It returns only the app name, version and URL, so AIHQ and the Dock app can tell whether DotClaude is running.

## Self-test

```bash
python3 ~/Code/dotclaude/dotclaude.py --self-test
```

This builds a fake home folder in a temp directory and runs 94 checks (using a fake `claude` command for the review tests). They include cases that must fail: bad tokens, path traversal, invalid JSON, writes to read-only files, stale edits, a failing or missing `claude` command, rewrites aimed at files outside the review, a shutdown without the token, backup ids that try to escape the backups folder, restores onto read-only or unmanaged files, and the move from the old ~/.claude-files folder.
