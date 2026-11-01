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

- **Global**: `~/.claude/` (CLAUDE.md, settings.json, settings.local.json, keybindings.json, rules, skills, agents, commands, output styles, workflows, themes, subagent memory, hook and status line scripts), the installed plugins list (read-only) and `~/.claude.json` (read-only)
- **Projects**: every folder you've opened Claude Code in (read from `~/.claude.json`), plus any folders you add. For each one: CLAUDE.md, .claude/CLAUDE.md, CLAUDE.local.md, AGENTS.md, settings files, .mcp.json, rules/skills/agents/commands, hook scripts, auto memory, CLAUDE.md files inherited from parent folders, and subfolder CLAUDE.md files
- **Claude desktop app**: `claude_desktop_config.json` (MCP servers for the chat app). Your claude.ai Projects are stored in your account, not on your Mac, so they don't appear here
- **Organisation-managed**: `/Library/Application Support/ClaudeCode/` (read-only)

Hook and status line scripts are the files in `.claude/hooks/` plus any script a settings file runs as a hook or status line. Only scripts inside your home folder are listed.

## What loads at startup

Open a project (or **About this section** under Global) to see what Claude Code loads at the start of every session there, file by file, with a size estimate. It follows the rules in Anthropic's [memory docs](https://code.claude.com/docs/en/memory.md): CLAUDE.md files in the folder and every folder above it, `@imports` up to 4 hops deep, AGENTS.md only when there's no CLAUDE.md in the folder or above, rules without `paths:`, the first 200 lines of auto memory, and only the descriptions of skills and subagents. It warns about imports that point to missing files.

The token count is an estimate (characters ÷ 4). It leaves out Claude Code's own instructions, MCP tool lists and skills that plugins add.

## Best-practice benchmark

**Best-practice benchmark** (sidebar, under Global) shows exactly what reviews judge your files against: nine official Anthropic pages (memory, best practices, skills, skill authoring, subagents, settings, permissions, hooks, MCP). A review reads the ones that apply to the files being reviewed, fresh each time, and every finding has to cite one of them.

DotClaude keeps a copy of each page in `~/.dotclaude/guidance/` and compares on every check, so it can tell you:

- when a page last changed, and **What changed** (a diff against the previous copy)
- which of your files were last edited before a page that applies to them changed. Those files also show a note when you open them

It checks when the app starts (if the last check is over 12 hours old) and when you click **Check for updates now**. It can only see changes from its first check onward.

**Notifications:** tick **Notify me when these pages change** to install a daily check at 09:00 (a macOS LaunchAgent, `~/Library/LaunchAgents/com.dotclaude.guidance-check.plist`). It runs even when DotClaude isn't open and shows a Mac notification only when a page changed. Untick it to remove the LaunchAgent. Log: `~/Library/Logs/DotClaude/guidance-check.log`. From Terminal:

```bash
python3 ~/Code/dotclaude/dotclaude.py --check-guidance          # check now, print what changed
python3 ~/Code/dotclaude/dotclaude.py --check-guidance --notify # same, plus a notification on changes
```

## Drift

The home page lists instruction files that look like one file kept under two names but no longer match: CLAUDE.md beside AGENTS.md, CLAUDE.md beside .claude/CLAUDE.md, or `~/.claude/CLAUDE.md` beside `~/AGENTS.md`. **Compare** shows the differences. Similar files in different folders (one template filled in per project) aren't flagged.

## Tidy up old copies

**Tidy up old copies** (sidebar, under Global) lists hand-made copies sitting next to a Claude file, such as `settings.json.bak-2026…` or `CLAUDE.md.v1-backup`. Ticked files move to `~/.dotclaude/archive/`, never deleted, and **Put back** returns any of them. A copy that one of your instruction files mentions by name starts unticked, because moving it would break that reference.

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

This builds fake home folders in a temp directory and runs 155 checks (using a fake `claude` command for the review tests). They include cases that must fail: bad tokens, path traversal, invalid JSON, writes to read-only files, stale edits, a failing or missing `claude` command, rewrites aimed at files outside the review, a shutdown without the token, backup ids that try to escape the backups folder, restores onto read-only or unmanaged files, the move from the old ~/.claude-files folder, imports past 4 hops or inside code blocks, rules with `paths:`, drift between unrelated or identical files, scripts outside the home folder, saving the plugins list, archiving a live Claude file, putting an archived file back over a newer one, a first guidance check reported as a change, being offline reported as a change, and macOS refusing the daily check.
