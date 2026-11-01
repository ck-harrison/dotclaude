#!/usr/bin/env python3
"""
DotClaude — browse, understand and edit every file Claude Code reads on your Mac.

Run:        python3 dotclaude.py            (opens in your browser)
Self-test:  python3 dotclaude.py --self-test

No installs needed: standard-library Python 3.9+ only.
The server listens on 127.0.0.1 only and every API call needs a per-run token.
Before every save, the previous version is copied to ~/.dotclaude/backups/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

APP_NAME = "DotClaude"
APP_VERSION = "1.2.0"
DOCS = "https://code.claude.com/docs/en/"
MAX_READ_BYTES = 10 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SKIP_SCAN_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    ".next", "target", ".cache", ".tox", ".mypy_cache", ".pytest_cache", "Library",
    ".Trash", "Pods", "DerivedData", ".gradle", ".idea", ".vscode", "vendor",
}

# --------------------------------------------------------------------------------------
# Catalog: what each file is for. This is the knowledge the UI shows next to each file.
# --------------------------------------------------------------------------------------

T_SETTINGS = '{\n  "permissions": {\n    "allow": [],\n    "deny": []\n  }\n}\n'

KINDS: Dict[str, dict] = {
    # ---------- global (~/.claude) ----------
    "user_claude_md": dict(
        label="CLAUDE.md", title="Personal instructions (all projects)",
        role="Your own instructions for Claude Code on this Mac. Use it for things that apply in every "
             "project: how you like to work, your coding style, tools you use.",
        loads="Start of every session, in every project", share="Personal (only on this Mac)",
        docs="memory.md", template="# My instructions for Claude Code\n\n- \n"),
    "user_settings": dict(
        label="settings.json", title="Personal settings (all projects)",
        role="Settings for every project: permissions (which commands and tools Claude may use without "
             "asking), hooks, environment variables, default model, status line. A project's own settings "
             "override these.",
        loads="Start of every session", share="Personal", docs="settings.md", template=T_SETTINGS),
    "keybindings": dict(
        label="keybindings.json", title="Keyboard shortcuts",
        role="Custom keyboard shortcuts for Claude Code's terminal interface. Ctrl+C, Ctrl+D, Ctrl+M and "
             "Caps Lock can't be rebound.",
        loads="Start of every session", share="Personal", docs="claude-directory.md",
        create_hint="Create this by running /keybindings inside Claude Code, which writes the correct format."),
    "user_rules": dict(
        label="rules/", title="Personal rules", new_label="New rule",
        role="Instructions split into one file per topic. A rule with no `paths:` frontmatter loads every "
             "session, like CLAUDE.md. A rule with `paths:` (file patterns such as \"**/*.py\") loads only "
             "when Claude works on matching files.",
        loads="Every session, or only for matching files if it has `paths:`", share="Personal",
        docs="memory.md",
        template="# {name}\n\n<!-- To load this rule only for certain files, add frontmatter at the top:\n"
                 "---\npaths:\n  - \"**/*.py\"\n---\n-->\n\n- \n"),
    "user_skills": dict(
        label="skills/", title="Personal skills", new_label="New skill",
        role="Reusable abilities. Claude uses a skill automatically when its `description` matches the task, "
             "or you run it with /skill-name. Each skill is a folder with a SKILL.md and can hold extra files "
             "or scripts.",
        loads="Name and description every session; full text only when used", share="Personal",
        docs="skills.md",
        template="---\nname: {name}\ndescription: What this skill does and when Claude should use it.\n---\n\n"
                 "# {name}\n\nStep-by-step instructions for Claude.\n"),
    "user_agents": dict(
        label="agents/", title="Personal subagents", new_label="New subagent",
        role="Specialist helpers that Claude can hand work to. Each one has its own instructions and can be "
             "limited to certain tools or a specific model. Claude picks one based on its `description`.",
        loads="Name and description every session; full text when delegated to", share="Personal",
        docs="sub-agents.md",
        template="---\nname: {name}\ndescription: When Claude should delegate to this agent.\n"
                 "# tools: Read, Grep, Glob   (optional: remove to allow all tools)\n---\n\n"
                 "You are a specialist in ...\n"),
    "user_commands": dict(
        label="commands/", title="Personal slash commands (legacy)", new_label="New command",
        role="Older-style slash commands: typing /filename sends this file's content as a prompt. They still "
             "work, but skills now do the same job and more. If a skill and a command share a name, the "
             "skill wins.",
        loads="When you type the /command", share="Personal", docs="claude-directory.md",
        template="---\ndescription: What this command does\n---\n\nPrompt text. Use $ARGUMENTS for anything "
                 "typed after the command.\n"),
    "user_output_styles": dict(
        label="output-styles/", title="Personal output styles", new_label="New output style",
        role="Change how Claude responds (tone, format, role). Switch styles with /output-style or the "
             "`outputStyle` setting.",
        loads="When selected", share="Personal", docs="claude-directory.md",
        template="---\nname: {name}\ndescription: What this style is for\n---\n\nHow Claude should respond.\n"),
    "user_workflows": dict(
        label="workflows/", title="Personal workflows",
        role="Saved multi-agent workflow scripts that Claude can run by name.",
        loads="When the workflow is run", share="Personal", docs="claude-directory.md"),
    "user_themes": dict(
        label="themes/", title="Colour themes",
        role="Custom colour themes for Claude Code's terminal interface.",
        loads="When selected with /theme", share="Personal", docs="claude-directory.md",
        create_hint="Create themes with /theme inside Claude Code."),
    "user_agent_memory": dict(
        label="agent-memory/", title="Subagent memory (personal)",
        role="Notes that subagents set to `memory: user` keep for themselves across all projects.",
        loads="When that subagent runs", share="Personal", docs="sub-agents.md"),
    "claude_json": dict(
        label="~/.claude.json", title="Claude Code app state", readonly=True,
        readonly_reason="Claude Code rewrites this file constantly, so hand edits can be lost or corrupt it. "
                        "Change it with `claude mcp ...` and /config instead.",
        role="Written by Claude Code itself: your login, the list of projects you've trusted, personal MCP "
             "servers, and choices made in /config. This app reads its project list from here.",
        loads="Always", share="Personal", docs="claude-directory.md"),
    "automem_orphan": dict(
        label="Auto memory", title="Auto memory (other folders)",
        role="Auto memory folders that don't match any project in the list. Usually projects that were moved "
             "or deleted, or ones Claude Code hasn't listed.",
        loads="Only when you work in that folder", share="Personal", docs="memory.md"),

    # ---------- managed (organisation) ----------
    "managed_claude_md": dict(
        label="CLAUDE.md", title="Organisation instructions", readonly=True,
        readonly_reason="Managed files belong to an administrator and need admin rights to change.",
        role="Instructions an organisation's IT department applies to everyone. They load in every session "
             "and can't be overridden. On a personal Mac this normally doesn't exist.",
        loads="Every session", share="Set by an administrator", docs="memory.md"),
    "managed_settings": dict(
        label="managed-settings.json", title="Organisation settings", readonly=True,
        readonly_reason="Managed files belong to an administrator and need admin rights to change.",
        role="Settings enforced by an administrator. They override every other settings file.",
        loads="Always, highest priority", share="Set by an administrator", docs="managed-settings.md"),
    "managed_mcp": dict(
        label="managed-mcp.json", title="Organisation MCP servers", readonly=True,
        readonly_reason="Managed files belong to an administrator and need admin rights to change.",
        role="MCP servers an administrator provides, or restricts, for everyone.",
        loads="Always", share="Set by an administrator", docs="managed-settings.md"),

    # ---------- Claude desktop app ----------
    "desktop_config": dict(
        label="claude_desktop_config.json", title="Desktop app: MCP servers",
        role="Configuration for the Claude desktop app's chat. Mainly `mcpServers`: local tools the chat can "
             "use. Quit and reopen the desktop app after you edit it. This is not Claude Code's config.",
        loads="When the desktop app starts", share="Personal",
        docs="https://modelcontextprotocol.io/quickstart/user",
        template='{\n  "mcpServers": {}\n}\n'),
    "desktop_app_config": dict(
        label="config.json", title="Desktop app: internal preferences", readonly=True,
        readonly_reason="The desktop app manages this file itself. Change these preferences in the app's Settings.",
        role="The desktop app's own preferences and state.",
        loads="When the desktop app starts", share="Personal", docs="https://support.claude.com"),

    # ---------- per project ----------
    "proj_claude_md": dict(
        label="CLAUDE.md", title="Project instructions (shared)",
        role="Instructions for this project: what it is, how to build and test it, conventions to follow. "
             "Meant to be committed to git so everyone (and every Claude session) gets them. Aim for under "
             "about 200 lines.",
        loads="Every session started in this folder or a subfolder", share="Shared (commit to git)",
        docs="memory.md", template="# {project}\n\n## What this is\n\n## How to run it\n\n## Conventions\n"),
    "proj_dot_claude_md": dict(
        label=".claude/CLAUDE.md", title="Project instructions (alternative location)",
        role="Same job as CLAUDE.md in the project root, stored inside .claude/ to keep the root tidy. Both "
             "load if both exist, so normally you use one or the other.",
        loads="Every session started in this folder or a subfolder", share="Shared (commit to git)",
        docs="memory.md", template="# {project}\n\n"),
    "proj_local_md": dict(
        label="CLAUDE.local.md", title="Your private project notes",
        role="Project instructions just for you, such as local URLs, test accounts or your personal "
             "workflow. Loads together with CLAUDE.md. Add it to .gitignore so it isn't shared. If this file "
             "exists, Claude Code ignores AGENTS.md.",
        loads="Every session in this project", share="Personal (keep out of git)", docs="memory.md",
        template="# My notes for this project\n\n- \n"),
    "proj_agents_md": dict(
        label="AGENTS.md", title="AGENTS.md (cross-tool instructions)",
        role="The instructions-file convention shared by other AI coding tools. Claude Code reads it only "
             "when there is no CLAUDE.md or CLAUDE.local.md in this folder or above.",
        loads="Only when no CLAUDE.md exists", share="Shared", docs="memory.md"),
    "proj_settings": dict(
        label=".claude/settings.json", title="Project settings (shared)",
        role="Settings for everyone working on this project: permissions, hooks, environment variables. "
             "Overrides your personal settings.json. Commit it to git.",
        loads="Start of every session in this project", share="Shared (commit to git)", docs="settings.md",
        template=T_SETTINGS),
    "proj_settings_local": dict(
        label=".claude/settings.local.json", title="Project settings (just you)",
        role="Your personal settings for this project. Highest priority after managed settings. When you "
             "answer \"Yes, and don't ask again\" in Claude Code, that approval is saved here. Claude Code "
             "keeps it out of git automatically.",
        loads="Start of every session in this project", share="Personal (not committed)",
        docs="settings.md", template=T_SETTINGS),
    "proj_mcp": dict(
        label=".mcp.json", title="Project MCP servers (shared)",
        role="MCP servers (external tools) for this project, shared with the team via git. Claude Code asks "
             "before using them the first time. Put secrets in environment variables using ${VAR}, never "
             "in this file.",
        loads="Start of every session in this project", share="Shared (commit to git)", docs="mcp.md",
        template='{\n  "mcpServers": {}\n}\n'),
    "proj_worktreeinclude": dict(
        label=".worktreeinclude", title="Files to copy into new worktrees",
        role="Lists git-ignored files (like .env) that should be copied into new git worktrees Claude Code "
             "creates. Same pattern syntax as .gitignore.",
        loads="When a worktree is created", share="Shared", docs="claude-directory.md"),
    "proj_rules": dict(KINDS_PLACEHOLDER=True),
    "proj_skills": dict(KINDS_PLACEHOLDER=True),
    "proj_agents": dict(KINDS_PLACEHOLDER=True),
    "proj_commands": dict(KINDS_PLACEHOLDER=True),
    "proj_output_styles": dict(KINDS_PLACEHOLDER=True),
    "proj_workflows": dict(KINDS_PLACEHOLDER=True),
    "proj_agent_memory": dict(
        label=".claude/agent-memory/", title="Subagent memory (shared)",
        role="Notes that subagents set to `memory: project` keep about this project.",
        loads="When that subagent runs", share="Shared", docs="sub-agents.md"),
    "proj_agent_memory_local": dict(
        label=".claude/agent-memory-local/", title="Subagent memory (just you)",
        role="Notes that subagents set to `memory: local` keep about this project, not shared.",
        loads="When that subagent runs", share="Personal", docs="sub-agents.md"),
    "automem_index": dict(
        label="Auto memory: MEMORY.md", title="Auto memory (Claude's own notes)",
        role="Notes Claude Code writes for itself about this project: build commands, gotchas, your "
             "preferences. You can edit or delete anything in it. Stored in your ~/.claude folder, not in the "
             "project. Turn it on or off with /memory.",
        loads="First 200 lines (max 25 KB) at the start of every session", share="Personal",
        docs="memory.md", template="# Memory\n\n"),
    "automem_topics": dict(
        label="Auto memory: topic files", title="Auto memory topic files",
        role="Extra notes Claude keeps for this project. MEMORY.md points to them, and Claude reads them only "
             "when needed.",
        loads="On demand", share="Personal", docs="memory.md"),
    "parent_md": dict(
        label="From parent folder", title="Inherited from a parent folder",
        role="A CLAUDE.md or CLAUDE.local.md in a folder above this project. Claude Code loads these too, so "
             "anything here also applies to this project (and to every other project under that folder).",
        loads="Every session in this project", share="Depends on the folder", docs="memory.md"),
    "sub_md": dict(
        label="Subfolder", title="Subfolder instructions",
        role="A CLAUDE.md inside a subfolder. It doesn't load at startup; Claude reads it the first time it "
             "works with files in that subfolder. Good for instructions that only apply to one part of the "
             "codebase.",
        loads="On demand, when Claude reads files in that subfolder", share="Shared (commit to git)",
        docs="memory.md"),
}

# Project folder kinds reuse the personal wording, adjusted for "shared".
for _p, _u in [("proj_rules", "user_rules"), ("proj_skills", "user_skills"), ("proj_agents", "user_agents"),
               ("proj_commands", "user_commands"), ("proj_output_styles", "user_output_styles"),
               ("proj_workflows", "user_workflows")]:
    _k = dict(KINDS[_u])
    _k["label"] = ".claude/" + _k["label"]
    _k["title"] = _k["title"].replace("Personal", "Project").replace("(legacy)", "(legacy, shared)")
    _k["share"] = "Shared (commit to git)"
    _k["role"] = _k["role"] + " These ones only apply in this project and are shared with anyone who has it."
    KINDS[_p] = _k

# File specs: (kind, path relative to scope root, show when missing)
GLOBAL_FILES = [("user_claude_md", "CLAUDE.md", True), ("user_settings", "settings.json", True),
                ("keybindings", "keybindings.json", True)]
# Folder specs: (kind, dir, layout, pattern, show when empty)
#   layout "flat": <dir>/<name><ext>   "nested": <dir>/<name>/SKILL.md   "deep": any depth
GLOBAL_FOLDERS = [
    ("user_rules", "rules", "deep", ".md", True),
    ("user_skills", "skills", "nested", "SKILL.md", True),
    ("user_agents", "agents", "flat", ".md", True),
    ("user_commands", "commands", "deep", ".md", True),
    ("user_output_styles", "output-styles", "flat", ".md", False),
    ("user_workflows", "workflows", "flat", ".js", False),
    ("user_themes", "themes", "flat", ".json", False),
    ("user_agent_memory", "agent-memory", "deep", ".md", False),
]
PROJECT_FILES = [
    ("proj_claude_md", "CLAUDE.md", True), ("proj_dot_claude_md", ".claude/CLAUDE.md", True),
    ("proj_local_md", "CLAUDE.local.md", True), ("proj_agents_md", "AGENTS.md", False),
    ("proj_settings", ".claude/settings.json", True), ("proj_settings_local", ".claude/settings.local.json", True),
    ("proj_mcp", ".mcp.json", True), ("proj_worktreeinclude", ".worktreeinclude", False),
]
PROJECT_FOLDERS = [
    ("proj_rules", ".claude/rules", "deep", ".md", True),
    ("proj_skills", ".claude/skills", "nested", "SKILL.md", True),
    ("proj_agents", ".claude/agents", "flat", ".md", True),
    ("proj_commands", ".claude/commands", "deep", ".md", True),
    ("proj_output_styles", ".claude/output-styles", "flat", ".md", False),
    ("proj_workflows", ".claude/workflows", "flat", ".js", False),
    ("proj_agent_memory", ".claude/agent-memory", "deep", ".md", False),
    ("proj_agent_memory_local", ".claude/agent-memory-local", "deep", ".md", False),
]
MANAGED_FILES = [("managed_claude_md", "CLAUDE.md", True), ("managed_settings", "managed-settings.json", True),
                 ("managed_mcp", "managed-mcp.json", True)]
DESKTOP_FILES = [("desktop_config", "claude_desktop_config.json", True), ("desktop_app_config", "config.json", False)]

DESKTOP_NOTE = ("Your claude.ai Projects (their instructions and knowledge files) are stored in your Anthropic "
                "account, not on this Mac, so you edit them at claude.ai/projects. Claude Code sessions you run "
                "from the desktop app use the same ~/.claude files and appear under Projects below.")


# --------------------------------------------------------------------------------------
# Environment (paths are injectable so the self-test can use a fake home folder)
# --------------------------------------------------------------------------------------

class Env:
    def __init__(self, home: Path, managed_dir: Optional[Path] = None, desktop_dir: Optional[Path] = None,
                 state_dir: Optional[Path] = None):
        self.home = Path(home)
        self.claude_dir = self.home / ".claude"
        self.claude_json = self.home / ".claude.json"
        self.managed_dir = managed_dir or Path("/Library/Application Support/ClaudeCode")
        self.desktop_dir = desktop_dir or (self.home / "Library/Application Support/Claude")
        self.state_dir = state_dir or (self.home / ".dotclaude")
        if state_dir is None:
            migrate_state_dir(self.home / ".claude-files", self.state_dir)
        self.config_path = self.state_dir / "config.json"
        self.backup_dir = self.state_dir / "backups"

    # ---- app config (folders you added) ----
    def load_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text())
        except Exception:
            return {"extra_folders": []}

    def save_config(self, cfg: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.config_path, json.dumps(cfg, indent=2) + "\n")


def migrate_state_dir(old: Path, new: Path) -> bool:
    """The app was called Claude Files; move its backups, reviews and folder list across once."""
    if old.is_dir() and not new.exists():
        try:
            os.rename(str(old), str(new))
            return True
        except OSError:
            return False
    return False


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cf-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tilde(env: Env, p) -> str:
    s = str(p)
    h = str(env.home)
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def encode_project_key(path: Path) -> str:
    """Claude Code names ~/.claude/projects/<key> by replacing every non-alphanumeric char with '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def git_root(path: Path, home: Path) -> Optional[Path]:
    p = path
    for _ in range(40):
        if (p / ".git").exists():
            return p
        if p == home or p.parent == p:
            return None
        p = p.parent
    return None


def is_worktree_path(p: str) -> bool:
    return "/.claude/worktrees/" in p or "/.claude-worktrees/" in p


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------

def file_item(kind: str, path: Path, show_missing: bool, gid: str, label: Optional[str] = None,
              project_name: str = "") -> Optional[dict]:
    exists = path.is_file()
    if not exists and not show_missing:
        return None
    k = KINDS[kind]
    readonly = bool(k.get("readonly"))
    return {
        "type": "file", "kind": kind, "group": gid, "label": label or k["label"], "path": str(path),
        "exists": exists, "readonly": readonly,
        "creatable": (not exists) and (not readonly) and ("template" in k),
        "size": path.stat().st_size if exists else 0,
    }


def list_folder(d: Path, layout: str, pattern: str, limit: int = 300) -> List[Path]:
    if not d.is_dir():
        return []
    out: List[Path] = []
    if layout == "flat":
        out = [p for p in d.iterdir() if p.is_file() and p.name.endswith(pattern)]
    elif layout == "nested":
        out = [p / pattern for p in d.iterdir() if p.is_dir() and (p / pattern).is_file()]
    else:  # deep
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in files:
                if f.endswith(pattern):
                    out.append(Path(root) / f)
            if len(out) >= limit:
                break
    return sorted(out)[:limit]


def folder_item(kind: str, d: Path, layout: str, pattern: str, show_empty: bool, gid: str) -> Optional[dict]:
    files = list_folder(d, layout, pattern)
    if not files and not show_empty:
        return None
    k = KINDS[kind]
    children = []
    for f in files:
        rel = str(f.relative_to(d))
        children.append(file_item(kind, f, False, gid, label=rel))
    return {"type": "folder", "kind": kind, "group": gid, "label": k["label"], "path": str(d),
            "children": children, "canCreate": "template" in k, "newLabel": k.get("new_label", "New")}


def true_case(p: Path) -> Path:
    """On macOS the disk ignores case, so ~/Code/aihq and ~/Code/AIHQ are the same folder.
    Return the path spelled the way it is on disk."""
    parts = Path(p).parts
    cur = Path(parts[0])
    for name in parts[1:]:
        try:
            if not (cur / name).exists():
                return Path(p)
            names = os.listdir(cur)
        except OSError:
            return Path(p)
        if name in names:
            cur = cur / name
        else:
            match = [n for n in names if n.lower() == name.lower()]
            cur = cur / (match[0] if len(match) == 1 else name)
    return cur


def folder_identity(p: Path):
    """Same folder = same device + inode, whatever the spelling (case, symlinks)."""
    try:
        st = p.stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        return ("missing", str(p))


def project_paths(env: Env) -> List[dict]:
    """Projects Claude Code knows (from ~/.claude.json) plus folders you added.
    Paths that point to the same folder (e.g. AIHQ and aihq on macOS) are merged into one entry."""
    by_id: Dict[object, dict] = {}
    out = []
    hidden_worktrees = 0
    known = []
    try:
        data = json.loads(env.claude_json.read_text())
        known = list((data.get("projects") or {}).keys())
    except Exception:
        pass

    def add(p: str, source: str):
        rp = Path(p).expanduser()
        if source == "claude" and not rp.is_dir():
            return
        ident = folder_identity(rp)
        if ident in by_id:
            o = by_id[ident]
            if str(rp) not in o["aliases"]:
                o["aliases"].append(str(rp))
            if o["source"] != source and "both" not in o["source"]:
                o["source"] = "both"
            return
        canon = true_case(rp) if rp.is_dir() else rp
        o = {"path": canon, "source": source, "aliases": [str(rp)], "missing": not rp.is_dir()}
        by_id[ident] = o
        out.append(o)

    for p in known:
        if is_worktree_path(p):
            hidden_worktrees += 1
            continue
        add(p, "claude")
    for p in env.load_config().get("extra_folders", []):
        add(p, "added")
    for o in out:
        o["aliases"] = [a for a in o["aliases"] if a != str(o["path"])]
    out.sort(key=lambda o: (o["path"].name.lower(), str(o["path"])))
    return out, hidden_worktrees


def scan_subdir_claude_md(root: Path, max_depth: int = 4, max_dirs: int = 3000) -> List[Path]:
    found = []
    count = 0
    root_depth = len(root.parts)
    for cur, dirs, files in os.walk(root):
        count += 1
        if count > max_dirs:
            break
        depth = len(Path(cur).parts) - root_depth
        dirs[:] = sorted(d for d in dirs if d not in SKIP_SCAN_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirs[:] = []
        if Path(cur) == root:
            continue
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            if name in files:
                found.append(Path(cur) / name)
    return found


def discover(env: Env) -> dict:
    groups = []

    # Global
    g = {"id": "global", "scope": "global", "title": "Global (all projects)", "subtitle": tilde(env, env.claude_dir),
         "items": []}
    for kind, rel, show in GLOBAL_FILES:
        it = file_item(kind, env.claude_dir / rel, show, "global")
        if it:
            g["items"].append(it)
    for kind, rel, layout, pat, show in GLOBAL_FOLDERS:
        it = folder_item(kind, env.claude_dir / rel, layout, pat, show, "global")
        if it:
            g["items"].append(it)
    it = file_item("claude_json", env.claude_json, True, "global", label="~/.claude.json")
    g["items"].append(it)
    groups.append(g)

    # Projects (computed before orphan auto-memory so we know which memory dirs are matched)
    projs, hidden_wt = project_paths(env)
    matched_mem = set()
    project_groups = []
    for pr in projs:
        p = pr["path"]
        gid = "p:" + str(p)
        pg = {"id": gid, "scope": "project", "title": p.name or str(p), "subtitle": tilde(env, p),
              "path": str(p), "source": pr["source"], "items": [], "missing": bool(pr.get("missing"))}
        if pr.get("missing"):
            pg["note"] = "This folder no longer exists."
            project_groups.append(pg)
            continue
        if pr.get("aliases"):
            pg["note"] = ("Claude Code has this folder listed under more than one spelling (" +
                          ", ".join(tilde(env, a) for a in pr["aliases"]) +
                          "). macOS treats them as the same folder, so they're shown once here.")
        is_home = (p == env.home)
        if is_home:
            pg["note"] = ("This is your home folder. Its .claude/ folder is the Global section above, so only "
                          "the extra home-folder files are listed here.")
        for kind, rel, show in PROJECT_FILES:
            if is_home and rel.startswith(".claude/"):
                continue
            it = file_item(kind, p / rel, show, gid)
            if it:
                pg["items"].append(it)
        if not is_home:
            for kind, rel, layout, pat, show in PROJECT_FOLDERS:
                it = folder_item(kind, p / rel, layout, pat, show, gid)
                if it:
                    pg["items"].append(it)
        # Auto memory
        mem_dir = automem_dir(env, p, pr.get("aliases", []))
        matched_mem.add(str(mem_dir))
        for al in pr.get("aliases", []):  # Claude Code may have made a memory folder per spelling
            extra_mem = env.claude_dir / "projects" / encode_project_key(Path(al)) / "memory"
            if extra_mem.is_dir() and str(extra_mem) != str(mem_dir):
                matched_mem.add(str(extra_mem))
                pg.setdefault("extraMem", []).append(str(extra_mem))
        pg["memDir"] = str(mem_dir)
        it = file_item("automem_index", mem_dir / "MEMORY.md", True, gid)
        pg["items"].append(it)
        topics = [f for f in list_folder(mem_dir, "flat", ".md") if f.name != "MEMORY.md"]
        if topics:
            pg["items"].append({"type": "folder", "kind": "automem_topics", "group": gid,
                                "label": KINDS["automem_topics"]["label"], "path": str(mem_dir),
                                "children": [file_item("automem_topics", f, False, gid, label=f.name) for f in topics],
                                "canCreate": False})
        for em in pg.get("extraMem", []):
            files = list_folder(Path(em), "flat", ".md")
            if files:
                pg["items"].append({"type": "folder", "kind": "automem_topics", "group": gid,
                                    "label": "Auto memory (other spelling)", "path": em,
                                    "children": [file_item("automem_topics", f, False, gid,
                                                           label=Path(em).parent.name[-24:] + "/" + f.name)
                                                 for f in files], "canCreate": False})
                pg["note"] = (pg.get("note", "") + " Claude Code also keeps a separate auto-memory folder for the "
                              "other spelling, shown as 'Auto memory (other spelling)'. You may want to merge its "
                              "notes into the main MEMORY.md.").strip()
        # Parent folders' CLAUDE.md
        parents = []
        q = p.parent
        while True:
            for name in ("CLAUDE.md", "CLAUDE.local.md"):
                f = q / name
                if f.is_file():
                    parents.append(file_item("parent_md", f, False, gid, label=tilde(env, f)))
            if q == q.parent or len(q.parts) <= 1:
                break
            q = q.parent
        if parents:
            pg["items"].append({"type": "folder", "kind": "parent_md", "group": gid, "label": "Inherited from parent folders",
                                "path": str(p.parent), "children": parents, "canCreate": False})
        # Subfolder CLAUDE.md
        if not is_home:
            subs = scan_subdir_claude_md(p)
            if subs:
                pg["items"].append({"type": "folder", "kind": "sub_md", "group": gid, "label": "Subfolder CLAUDE.md files",
                                    "path": str(p), "children": [file_item("sub_md", f, False, gid, label=str(f.relative_to(p)))
                                                                for f in subs], "canCreate": False})
        project_groups.append(pg)

    # Orphan auto memory → into global group
    orphans = []
    projects_dir = env.claude_dir / "projects"
    if projects_dir.is_dir():
        for d in sorted(projects_dir.iterdir()):
            md = d / "memory"
            if md.is_dir() and str(md) not in matched_mem:
                for f in list_folder(md, "flat", ".md"):
                    orphans.append(file_item("automem_orphan", f, False, "global", label=f"{d.name}/{f.name}"))
    if orphans:
        g["items"].append({"type": "folder", "kind": "automem_orphan", "group": "global",
                           "label": "Auto memory (unmatched folders)", "path": str(projects_dir),
                           "children": orphans, "canCreate": False})

    # Claude desktop app
    dg = {"id": "desktop", "scope": "desktop", "title": "Claude desktop app", "subtitle": tilde(env, env.desktop_dir),
          "items": [], "note": DESKTOP_NOTE}
    for kind, rel, show in DESKTOP_FILES:
        it = file_item(kind, env.desktop_dir / rel, show, "desktop")
        if it:
            dg["items"].append(it)
    groups.append(dg)

    # Managed
    mg = {"id": "managed", "scope": "managed", "title": "Organisation-managed", "subtitle": str(env.managed_dir),
          "items": [], "collapsed": True,
          "note": "Set by an IT administrator and overrides everything. On a personal Mac these normally don't exist."}
    for kind, rel, show in MANAGED_FILES:
        mg["items"].append(file_item(kind, env.managed_dir / rel, show, "managed"))
    groups.append(mg)

    return {"groups": groups, "projects": project_groups, "hiddenWorktrees": hidden_wt,
            "kinds": KINDS,
            "home": str(env.home), "version": APP_VERSION, "docsBase": DOCS,
            "notReviewed": sorted(NOT_REVIEWED)}


def automem_dir(env: Env, project: Path, aliases: Optional[List[str]] = None) -> Path:
    base = env.claude_dir / "projects"
    candidates = []
    gr = git_root(project, env.home)
    if gr:
        candidates.append(base / encode_project_key(gr) / "memory")
    candidates.append(base / encode_project_key(project) / "memory")
    for al in aliases or []:
        candidates.append(base / encode_project_key(Path(al)) / "memory")
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def iter_files(tree: dict):
    for g in tree["groups"] + tree["projects"]:
        for it in g["items"]:
            if it["type"] == "file":
                yield it
            else:
                for c in it["children"]:
                    yield c


# --------------------------------------------------------------------------------------
# Actions (pure functions, used by the HTTP layer and the self-test)
# --------------------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, msg: str, extra: Optional[dict] = None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


def find_item(env: Env, path: str) -> dict:
    tree = discover(env)
    for it in iter_files(tree):
        if it["path"] == path and it["exists"]:
            return it
    raise ApiError(403, "That file isn't one of the Claude files this app manages.")


def read_file(env: Env, path: str) -> dict:
    it = find_item(env, path)
    p = Path(path)
    st = p.stat()
    if st.st_size > MAX_READ_BYTES:
        raise ApiError(413, f"File is too large to open here ({st.st_size // 1024} KB).")
    raw = p.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(415, "File isn't UTF-8 text.")
    return {"path": path, "content": content, "mtime": str(st.st_mtime_ns), "readonly": it["readonly"],
            "kind": it["kind"], "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1)}


def flat_name(p) -> str:
    return str(p).strip("/").replace("/", "__")


def backup(env: Env, p: Path) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    flat = flat_name(p)
    dest = env.backup_dir / stamp / flat
    n = 1
    while dest.exists():
        dest = env.backup_dir / f"{stamp}-{n}" / flat
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(p), str(dest))
    return str(dest)


# ---- Backup history ----------------------------------------------------------
# Layout: ~/.dotclaude/backups/<YYYYmmdd-HHMMSS[-n]>/<path with / replaced by __>
# A backup id is "<stamp dir>/<flat name>".

BACKUP_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?/[^/\\]+$")


def _backup_entry(env: Env, f: Path, original: Optional[str] = None) -> dict:
    stamp = f.parent.name
    try:
        t = time.strptime(stamp[:15], "%Y%m%d-%H%M%S")
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", t)
    except ValueError:
        iso = stamp
    orig = original or "/" + f.name.replace("__", "/")
    return {"id": f"{stamp}/{f.name}", "time": iso, "size": f.stat().st_size, "original": orig,
            "label": tilde(env, orig)}


def _backup_sort_key(e: dict):
    """Several saves in one second get dirs stamp, stamp-1 … stamp-10: compare the suffix as a number."""
    m = re.match(r"(\d{8}-\d{6})(?:-(\d+))?/", e["id"])
    return (m.group(1), int(m.group(2) or 0)) if m else (e["id"], 0)


def list_backups(env: Env, path: Optional[str] = None) -> dict:
    """All backups (newest first), or only those of one file."""
    out = []
    if env.backup_dir.is_dir():
        want = flat_name(path) if path else None
        for d in env.backup_dir.iterdir():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and (want is None or f.name == want):
                    out.append(_backup_entry(env, f, path if want else None))
    out.sort(key=_backup_sort_key, reverse=True)
    total = sum(e["size"] for e in out)
    return {"backups": out, "count": len(out), "bytes": total, "dir": tilde(env, env.backup_dir)}


def _resolve_backup(env: Env, bid: str) -> Path:
    if not bid or not BACKUP_ID_RE.match(bid) or ".." in bid:
        raise ApiError(400, "Bad backup id.")
    f = (env.backup_dir / bid).resolve()
    root = env.backup_dir.resolve()
    if root not in f.parents or not f.is_file():
        raise ApiError(404, "That backup doesn't exist.")
    return f


def read_backup(env: Env, bid: str) -> dict:
    f = _resolve_backup(env, bid)
    entry = _backup_entry(env, f)
    try:
        content = f.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ApiError(415, "Backup isn't UTF-8 text.")
    cur = Path(entry["original"])
    current = cur.read_text(encoding="utf-8") if cur.is_file() else None
    diff = ""
    if current is not None:
        diff = "".join(difflib.unified_diff(current.splitlines(True), content.splitlines(True),
                                            "current", "this backup", n=2))
    return {**entry, "content": content, "diff": diff, "current_exists": current is not None,
            "same_as_current": current == content}


def restore_backup(env: Env, bid: str, mtime: Optional[str]) -> dict:
    """Put a backup back. The current version is backed up first, so a restore can itself be undone."""
    f = _resolve_backup(env, bid)
    target = Path(_backup_entry(env, f)["original"])
    managed = {it["path"]: it for it in iter_files(discover(env))}
    it = managed.get(str(target))
    if not it:
        raise ApiError(403, "That file isn't one of the Claude files this app manages any more.")
    if it["readonly"]:
        raise ApiError(403, KINDS[it["kind"]].get("readonly_reason", "This file is read-only here."))
    content = f.read_text(encoding="utf-8")
    validate_content(str(target), content)
    bk = None
    if target.exists():
        if mtime and str(target.stat().st_mtime_ns) != mtime:
            raise ApiError(409, "This file changed on disk since you opened it. Reload, then restore.")
        bk = backup(env, target)
    atomic_write(target, content)
    return {"ok": True, "path": str(target), "mtime": str(target.stat().st_mtime_ns),
            "backup": tilde(env, bk) if bk else None}


def validate_content(path: str, content: str) -> None:
    if path.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise ApiError(400, f"Not saved: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}",
                           {"line": e.lineno, "col": e.colno})


def save_file(env: Env, path: str, content: str, mtime: Optional[str], force: bool = False) -> dict:
    it = find_item(env, path)
    if it["readonly"]:
        raise ApiError(403, KINDS[it["kind"]].get("readonly_reason", "This file is read-only here."))
    p = Path(path)
    cur = str(p.stat().st_mtime_ns)
    if mtime and cur != mtime and not force:
        raise ApiError(409, "This file changed on disk since you opened it (Claude Code may have updated it). "
                            "Reload to see the new version, or overwrite it.")
    validate_content(path, content)
    bk = backup(env, p)
    atomic_write(p, content)
    return {"ok": True, "mtime": str(p.stat().st_mtime_ns), "backup": tilde(env, bk)}


def create_file(env: Env, group_id: str, kind: str, name: Optional[str]) -> dict:
    tree = discover(env)
    group = next((g for g in tree["groups"] + tree["projects"] if g["id"] == group_id), None)
    if not group or group.get("missing"):
        raise ApiError(404, "Unknown group.")
    k = KINDS.get(kind)
    if not k or "template" not in k or k.get("readonly"):
        raise ApiError(400, k.get("create_hint", "This file type can't be created here.") if k else "Unknown type.")
    scope = group["scope"]
    root = {"global": env.claude_dir, "desktop": env.desktop_dir}.get(scope) or Path(group.get("path", ""))
    project_name = Path(group.get("path", "")).name if scope == "project" else ""

    target = None
    file_specs = {"global": GLOBAL_FILES, "desktop": DESKTOP_FILES, "project": PROJECT_FILES}.get(scope, [])
    folder_specs = {"global": GLOBAL_FOLDERS, "project": PROJECT_FOLDERS}.get(scope, [])
    for fk, rel, _ in file_specs:
        if fk == kind:
            target = root / rel
    if kind == "automem_index" and scope == "project":
        target = Path(group["memDir"]) / "MEMORY.md"
    for fk, rel, layout, pat, _ in folder_specs:
        if fk == kind:
            if not name or not NAME_RE.match(name):
                raise ApiError(400, "Use a short name with letters, numbers, dashes, dots or underscores only.")
            base = name[:-len(pat)] if layout != "nested" and name.endswith(pat) else name
            target = (root / rel / base / pat) if layout == "nested" else (root / rel / (base + pat))
            name = base
    if target is None:
        raise ApiError(400, "That file type doesn't belong in this section.")
    if target.exists():
        raise ApiError(409, "That file already exists.")
    content = k["template"].replace("{name}", name or "").replace("{project}", project_name or "Project")
    atomic_write(target, content)
    return {"ok": True, "path": str(target)}


def add_folder(env: Env, path: str) -> dict:
    p = Path(os.path.expanduser(path.strip())).resolve() if path and path.strip() else None
    if not p or not p.is_dir():
        raise ApiError(400, "That folder doesn't exist.")
    cfg = env.load_config()
    folders = cfg.setdefault("extra_folders", [])
    if str(p) not in folders:
        folders.append(str(p))
    env.save_config(cfg)
    return {"ok": True, "path": str(p)}


def remove_folder(env: Env, path: str) -> dict:
    cfg = env.load_config()
    cfg["extra_folders"] = [f for f in cfg.get("extra_folders", []) if f != path]
    env.save_config(cfg)
    return {"ok": True}


def pick_folder() -> dict:
    if sys.platform != "darwin":
        raise ApiError(501, "The folder picker only works on macOS. Type the path instead.")
    try:
        r = subprocess.run(["osascript", "-e",
                            'POSIX path of (choose folder with prompt "Choose a project folder for DotClaude")'],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"path": None}
    if r.returncode != 0:
        return {"path": None}  # cancelled
    return {"path": r.stdout.strip().rstrip("/") or None}


def reveal(env: Env, path: str) -> dict:
    p = Path(path)
    if sys.platform != "darwin":
        raise ApiError(501, "Reveal in Finder only works on macOS.")
    allowed = {it["path"] for it in iter_files(discover(env))}
    tree = discover(env)
    allowed |= {g.get("path") for g in tree["projects"]} | {it["path"] for g in tree["groups"] + tree["projects"]
                                                            for it in g["items"] if it["type"] == "folder"}
    if path not in allowed:
        raise ApiError(403, "Not a managed path.")
    target = p if p.exists() else p.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    subprocess.run(["open", "-R", str(target)] if target.is_file() else ["open", str(target)], check=False)
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Best-practice review: runs your own Claude Code CLI headless (`claude -p`) with only
# WebFetch/WebSearch enabled. It fetches Anthropic's current guidance and reviews the files.
# --------------------------------------------------------------------------------------

import difflib  # noqa: E402

GUIDANCE = {
    "memory": ("CLAUDE.md, rules and memory", "https://code.claude.com/docs/en/memory.md"),
    "best": ("Claude Code best practices", "https://code.claude.com/docs/en/best-practices.md"),
    "skills": ("Skills in Claude Code", "https://code.claude.com/docs/en/skills.md"),
    "skills_bp": ("Skill authoring best practices",
                  "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices"),
    "agents": ("Subagents", "https://code.claude.com/docs/en/sub-agents.md"),
    "settings": ("Settings", "https://code.claude.com/docs/en/settings.md"),
    "permissions": ("Permissions", "https://code.claude.com/docs/en/permissions.md"),
    "hooks": ("Hooks guide", "https://code.claude.com/docs/en/hooks-guide.md"),
    "mcp": ("MCP", "https://code.claude.com/docs/en/mcp.md"),
}
KIND_GUIDANCE = {
    "skills": {"user_skills", "proj_skills", "user_commands", "proj_commands"},
    "agents": {"user_agents", "proj_agents"},
    "settings": {"user_settings", "proj_settings", "proj_settings_local", "managed_settings"},
    "mcp": {"proj_mcp", "desktop_config", "managed_mcp"},
}
NOT_REVIEWED = {"claude_json", "desktop_app_config", "user_themes", "user_workflows", "proj_workflows"}
REVIEW_MAX_FILES = 60
REVIEW_MAX_BYTES = 300_000
REVIEW_TIMEOUT_S = 900
SECRET_KEY_RE = re.compile(r"(key|token|secret|password|passwd|auth|credential|bearer)", re.I)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sources_checked": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}, "fetched": {"type": "boolean"}},
            "required": ["title", "url", "fetched"]}},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "issue": {"type": "string"},
            "recommendation": {"type": "string"},
            "source_url": {"type": "string"}},
            "required": ["file", "severity", "issue", "recommendation", "source_url"]}},
        "rewrites": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"}, "explanation": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file", "explanation", "content"]}},
    },
    "required": ["summary", "sources_checked", "findings", "rewrites"],
}

REVIEW_INSTRUCTIONS = """You are reviewing a person's Claude Code configuration files against Anthropic's CURRENT official guidance.

Step 1. Use WebFetch to read these official pages before judging anything (they are the source of truth, and may have changed since your training):
{urls}
List every official page you tried in sources_checked, with fetched=false if it failed to load. If a page fails, say so at the start of the summary. You may use WebSearch for newer official Anthropic guidance (code.claude.com, docs.claude.com, platform.claude.com, anthropic.com) but ignore unofficial blogs.

Step 2. Review the files in the input below. Each file is between <file path="..."> tags, with a note on its role. Check each one against the guidance: structure, length, specificity, frontmatter, where content belongs (e.g. CLAUDE.md vs rules vs skills vs settings), conflicts and duplication between files, permissions that are too broad, and anything the docs explicitly warn against.

Rules:
- Every finding MUST cite source_url: the specific official page that supports it, and you must actually have read that page in this session. If you can't tie it to a page you read, leave it out. No generic advice.
- Use the exact file path from the <file> tag in "file" (for a cross-file issue, use the main file it concerns).
- severity: high = likely makes Claude Code behave wrongly or is unsafe; medium = clearly against the guidance; low = polish.
- rewrites: only for files where a rewrite would materially help. Give the COMPLETE new file content (not a diff). Keep the person's own facts, preferences and intent; restructure, tighten or move content, don't invent new requirements. Never rewrite files marked rewrite="no". Valid JSON for .json files. Never return an empty or near-empty rewrite: if a file should be deleted, say so in a finding instead.
- If a file is already good, say so in the summary rather than inventing problems.
- Plain, direct English. No buzzwords.
"""


def find_claude_cli(home: Path) -> Optional[str]:
    override = os.environ.get("DOTCLAUDE_CLAUDE_BIN") or os.environ.get("CLAUDE_FILES_CLAUDE_BIN")
    if override:
        return override if Path(override).exists() else None
    w = shutil.which("claude")
    if w:
        return w
    cands = [home / ".claude/local/claude", home / ".local/bin/claude", Path("/opt/homebrew/bin/claude"),
             Path("/usr/local/bin/claude"), home / ".homebrew/bin/claude", home / ".npm-global/bin/claude"]
    nvm = home / ".nvm/versions/node"
    if nvm.is_dir():
        cands += sorted(nvm.glob("*/bin/claude"), reverse=True)
    for c in cands:
        if c.exists():
            return str(c)
    return None


def redact_json_secrets(text: str):
    """Replace values of secret-looking keys (and everything under `env`) with a placeholder."""
    try:
        data = json.loads(text)
    except Exception:
        return text, False
    hit = [False]

    def walk(o, under_env=False):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if isinstance(v, (str, int, float)) and not isinstance(v, bool) and (
                        under_env or SECRET_KEY_RE.search(str(k))):
                    out[k] = "<redacted>"
                    hit[0] = True
                else:
                    out[k] = walk(v, under_env or k in ("env", "headers"))
            return out
        if isinstance(o, list):
            return [walk(x, under_env) for x in o]
        return o

    red = walk(data)
    return (json.dumps(red, indent=2) if hit[0] else text), hit[0]


def review_targets(env: Env, path: Optional[str], group_id: Optional[str]) -> dict:
    tree = discover(env)
    files = []
    if path:
        it = next((i for i in iter_files(tree) if i["path"] == path and i["exists"]), None)
        if not it:
            raise ApiError(403, "That file isn't one of the Claude files this app manages.")
        if it["kind"] in NOT_REVIEWED:
            raise ApiError(400, "This file isn't something to review against best practice.")
        files = [it]
        g = next(g for g in tree["groups"] + tree["projects"] if g["id"] == it["group"])
        label = f'{it["label"]} ({g["title"]})'
        # Context: the project's main CLAUDE.md files, so the reviewer can spot conflicts (not rewritten)
        context = [i for i in iter_files({"groups": [g], "projects": []})
                   if i["exists"] and i["path"] != path and i["kind"] in
                   {"proj_claude_md", "proj_dot_claude_md", "proj_local_md", "user_claude_md"}]
    else:
        g = next((g for g in tree["groups"] + tree["projects"] if g["id"] == group_id), None)
        if not g or g.get("missing"):
            raise ApiError(404, "Unknown section.")
        files = [i for i in iter_files({"groups": [g], "projects": []})
                 if i["exists"] and i["kind"] not in NOT_REVIEWED]
        label = g["title"]
        context = []
        if g["scope"] == "project":  # global CLAUDE.md as context for conflicts
            gl = env.claude_dir / "CLAUDE.md"
            if gl.is_file():
                context = [file_item("user_claude_md", gl, False, "global")]
    if not files:
        raise ApiError(400, "There are no files here to review yet.")
    return {"files": files[:REVIEW_MAX_FILES], "context": context, "label": label,
            "truncated": len(files) > REVIEW_MAX_FILES}


def build_review_input(env: Env, targets: dict):
    kinds = set()
    parts = []
    total = 0
    meta = {}
    for role, items in (("review", targets["files"]), ("context", targets["context"])):
        for it in items:
            p = Path(it["path"])
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            redacted = False
            if p.suffix == ".json":
                text, redacted = redact_json_secrets(text)
            if total + len(text) > REVIEW_MAX_BYTES:
                text = text[: max(0, REVIEW_MAX_BYTES - total)] + "\n…[truncated]"
            total += len(text)
            k = KINDS[it["kind"]]
            rewrite_ok = role == "review" and not it["readonly"] and not redacted and it["kind"] not in (
                "parent_md",)
            if role == "review":
                kinds.add(it["kind"])
                meta[it["path"]] = {"rewrite": rewrite_ok, "redacted": redacted}
            ctx_attr = 'context-only="yes" ' if role == "context" else ""
            rw_attr = 'rewrite="yes"' if rewrite_ok else 'rewrite="no"'
            red_attr = ' secrets-redacted="yes"' if redacted else ""
            parts.append(
                f'<file path="{it["path"]}" role="{k["title"]}" loads="{k["loads"]}" '
                f'{ctx_attr}{rw_attr}{red_attr}>\n'
                f'<!-- {k["role"]} -->\n{text}\n</file>')
            if total >= REVIEW_MAX_BYTES:
                break
    urls = ["memory", "best"]
    for g, ks in KIND_GUIDANCE.items():
        if kinds & ks:
            urls.append(g)
    if "skills" in urls:
        urls.append("skills_bp")
    if kinds & KIND_GUIDANCE["settings"]:
        urls += ["permissions", "hooks"]
    url_lines = "\n".join(f"- {GUIDANCE[u][0]}: {GUIDANCE[u][1]}" for u in dict.fromkeys(urls))
    return REVIEW_INSTRUCTIONS.format(urls=url_lines), "\n\n".join(parts), meta


class ReviewJobs:
    def __init__(self, env: Env):
        self.env = env
        self.jobs: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self, path: Optional[str], group_id: Optional[str], model: Optional[str] = None) -> dict:
        cli = find_claude_cli(self.env.home)
        if not cli:
            raise ApiError(424, "Couldn't find the Claude Code command (`claude`). Install Claude Code, or start "
                                "this app from a Terminal where `claude` works.")
        targets = review_targets(self.env, path, group_id)
        instructions, payload, meta = build_review_input(self.env, targets)
        jid = secrets.token_hex(6)
        job = {"id": jid, "status": "running", "label": targets["label"], "started": time.time(),
               "files": [f["path"] for f in targets["files"]], "meta": meta, "truncated": targets["truncated"],
               "proc": None, "result": None, "error": None}
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(job, cli, instructions, payload, model), daemon=True).start()
        return self.public(job)

    def _run(self, job, cli, instructions, payload, model):
        cmd = [cli, "-p", instructions, "--output-format", "json", "--json-schema", json.dumps(REVIEW_SCHEMA),
               "--tools", "WebFetch,WebSearch", "--allowedTools", "WebFetch,WebSearch",
               "--permission-mode", "dontAsk", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        workdir = tempfile.mkdtemp(prefix="dotclaude-review-")  # empty dir: no project CLAUDE.md gets loaded
        try:
            proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            job["proc"] = proc
            try:
                out, err = proc.communicate(payload, timeout=REVIEW_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise ApiError(504, f"The review took longer than {REVIEW_TIMEOUT_S // 60} minutes and was stopped.")
            if job["status"] == "cancelled":
                return
            job["result"] = self.parse(job, proc.returncode, out, err)
            job["status"] = "done"
            self.save_report(job)
        except ApiError as e:
            if job["status"] != "cancelled":
                job["status"], job["error"] = "error", e.msg
        except Exception as e:
            if job["status"] != "cancelled":
                job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
        finally:
            job["proc"] = None
            job["finished"] = time.time()
            shutil.rmtree(workdir, ignore_errors=True)

    def parse(self, job, code, out, err) -> dict:
        try:
            data = json.loads(out)
        except Exception:
            msg = (err or out or "").strip()[-800:]
            raise ApiError(502, f"Claude Code didn't return a result (exit code {code}). {msg}".strip())
        if isinstance(data, list):  # tolerate a stream-style array: take the final result message
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
        so = data.get("structured_output")
        if code != 0 or data.get("is_error") or not isinstance(so, dict):
            msg = str(data.get("result") or err or "").strip()[-800:]
            raise ApiError(502, "The review didn't complete" + (f": {msg}" if msg else "."))
        files = set(job["files"])
        findings = [f for f in so.get("findings", []) if isinstance(f, dict)]
        for f in findings:
            if f.get("file") not in files:
                f["file_unmatched"] = True
        rewrites, dropped = [], []
        for r in so.get("rewrites", []):
            if not isinstance(r, dict):
                continue
            p = r.get("file", "")
            m = job["meta"].get(p)
            if not m or not m["rewrite"] or not isinstance(r.get("content"), str):
                dropped.append(p)
                continue
            if not r["content"].strip():
                dropped.append(p)
                continue
            try:
                validate_content(p, r["content"])
            except ApiError:
                dropped.append(p)
                continue
            old = Path(p).read_text(encoding="utf-8") if Path(p).is_file() else ""
            if old == r["content"]:
                continue
            diff = "".join(difflib.unified_diff(old.splitlines(True), r["content"].splitlines(True),
                                                "current", "suggested", n=2))
            rewrites.append({"file": p, "explanation": r.get("explanation", ""), "content": r["content"],
                             "diff": diff, "base_mtime": str(Path(p).stat().st_mtime_ns)})
        order = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda f: order.get(f.get("severity"), 3))
        sources = [x for x in so.get("sources_checked", []) if isinstance(x, dict)]
        return {"summary": so.get("summary", ""), "sources": sources, "findings": findings,
                "guidance_failed": [x.get("url") for x in sources if x.get("fetched") is False],
                "rewrites": rewrites, "dropped_rewrites": dropped,
                "cost_usd": data.get("total_cost_usd"), "redacted": [p for p, m in job["meta"].items() if m["redacted"]]}

    def save_report(self, job):
        try:
            d = self.env.state_dir / "reviews"
            d.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^A-Za-z0-9]+", "-", job["label"])[:40] + ".json"
            atomic_write(d / name, json.dumps(self.public(job), indent=2))
        except Exception:
            pass

    def cancel(self, jid: str) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise ApiError(404, "Unknown review.")
        if job["status"] == "running":
            job["status"] = "cancelled"
            p = job.get("proc")
            if p:
                p.kill()
        return self.public(job)

    def get(self, jid: str) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise ApiError(404, "Unknown review.")
        return self.public(job)

    @staticmethod
    def public(job) -> dict:
        return {k: v for k, v in job.items() if k not in ("proc", "meta")} | {
            "elapsed": round((job.get("finished") or time.time()) - job["started"])}


def apply_rewrite(env: Env, path: str, content: str, base_mtime: Optional[str]) -> dict:
    """Save a suggested rewrite. save_file() backs up the current version first."""
    return save_file(env, path, content, base_mtime)


# --------------------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------------------

def make_handler(env: Env, token: str, port_ref: dict):
    jobs = ReviewJobs(env)
    class H(BaseHTTPRequestHandler):
        server_version = "DotClaude/" + APP_VERSION

        def log_message(self, *a):  # quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            port = port_ref["port"]
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, status: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> bool:
            # /health is the only route without a token, and it returns nothing private
            if not self._host_ok():
                self._send(403, {"error": "Bad host"})
                return False
            if self.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self._send(403, {"error": "Missing or wrong token"})
                return False
            return True

        def _run(self, fn):
            try:
                self._send(200, fn())
            except ApiError as e:
                self._send(e.status, {"error": e.msg, **e.extra})
            except Exception as e:  # pragma: no cover
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/health":  # no token: lets AIHQ (or a launcher) see that DotClaude is up
                self._send(200, {"app": "dotclaude", "version": APP_VERSION, "url": f"http://127.0.0.1:{port_ref['port']}/"})
            elif u.path == "/":
                html = PAGE.replace("__TOKEN__", token)
                self._send(200, html, "text/html")
            elif u.path == "/api/tree":
                self._run(lambda: discover(env))
            elif u.path == "/api/backups":
                self._run(lambda: list_backups(env, (q.get("path") or [None])[0]))
            elif u.path == "/api/backup":
                self._run(lambda: read_backup(env, (q.get("id") or [""])[0]))
            elif u.path == "/api/review/status":
                self._run(lambda: jobs.get((q.get("id") or [""])[0]))
            elif u.path == "/api/claude-cli":
                self._run(lambda: {"path": find_claude_cli(env.home)})
            elif u.path == "/api/file":
                self._run(lambda: read_file(env, (q.get("path") or [""])[0]))
            else:
                self._send(404, {"error": "Not found"})

        def do_POST(self):
            if not self._guard():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                return self._send(400, {"error": "Bad JSON body"})
            def request_shutdown():
                # Reply first, then stop: shutting down inside the request would deadlock.
                threading.Timer(0.3, port_ref["server"].shutdown).start()
                return {"ok": True}

            routes = {
                "/api/save": lambda: save_file(env, body.get("path", ""), body.get("content", ""), body.get("mtime"),
                                               bool(body.get("force"))),
                "/api/create": lambda: create_file(env, body.get("group", ""), body.get("kind", ""), body.get("name")),
                "/api/folders/add": lambda: add_folder(env, body.get("path", "")),
                "/api/folders/remove": lambda: remove_folder(env, body.get("path", "")),
                "/api/pick-folder": pick_folder,
                "/api/reveal": lambda: reveal(env, body.get("path", "")),
                "/api/shutdown": request_shutdown,
                "/api/backups/restore": lambda: restore_backup(env, body.get("id", ""), body.get("mtime")),
                "/api/review/start": lambda: jobs.start(body.get("path"), body.get("group"), body.get("model") or None),
                "/api/review/cancel": lambda: jobs.cancel(body.get("id", "")),
                "/api/review/apply": lambda: apply_rewrite(env, body.get("path", ""), body.get("content", ""),
                                                           body.get("base_mtime")),
            }
            fn = routes.get(urlparse(self.path).path)
            if not fn:
                return self._send(404, {"error": "Not found"})
            self._run(fn)

    return H


def start_server(env: Env, port: int = 0, token: Optional[str] = None):
    token = token or secrets.token_urlsafe(24)
    port_ref = {"port": 0}
    handler = make_handler(env, token, port_ref)
    last_err = None
    for p in ([port] if port else list(range(8765, 8785))):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError as e:
            last_err = e
    else:
        raise SystemExit(f"Couldn't open a port: {last_err}")
    port_ref["port"] = srv.server_address[1]
    port_ref["server"] = srv
    srv.stopped = threading.Event()

    def serve():
        try:
            srv.serve_forever()
        finally:
            srv.stopped.set()

    threading.Thread(target=serve, daemon=True).start()
    return srv, port_ref["port"], token


# --------------------------------------------------------------------------------------
# Self-test: builds a fake home folder and checks behaviour, including cases that must fail
# --------------------------------------------------------------------------------------

def self_test() -> int:
    import urllib.request
    import urllib.error

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(("  PASS  " if cond else "  FAIL  ") + name)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td).resolve()
        home = td / "home"
        cd = home / ".claude"
        (cd / "skills/tidy").mkdir(parents=True)
        (cd / "agents").mkdir(parents=True)
        (cd / "CLAUDE.md").write_text("# global\n")
        (cd / "settings.json").write_text('{"model": "opus"}\n')
        (cd / "skills/tidy/SKILL.md").write_text("---\nname: tidy\n---\n")
        (cd / "agents/reviewer.md").write_text("---\nname: reviewer\n---\n")
        code = home / "Code"
        proj = code / "proj1"
        (proj / ".git").mkdir(parents=True)
        (proj / ".claude").mkdir()
        (proj / "src/api").mkdir(parents=True)
        (proj / "node_modules/x").mkdir(parents=True)
        (proj / "CLAUDE.md").write_text("# proj1\n")
        (proj / ".claude/settings.local.json").write_text('{"permissions": {"allow": ["Bash(ls)"]}}\n')
        (proj / "src/api/CLAUDE.md").write_text("# api rules\n")
        (proj / "node_modules/x/CLAUDE.md").write_text("should be skipped\n")
        (code / "CLAUDE.md").write_text("# parent\n")
        mem = cd / "projects" / encode_project_key(proj) / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("- uses pytest\n")
        (mem / "debugging.md").write_text("- topic\n")
        orphan = cd / "projects" / "-old-gone-project" / "memory"
        orphan.mkdir(parents=True)
        (orphan / "MEMORY.md").write_text("old\n")
        extra = home / "Elsewhere"
        extra.mkdir()
        (home / ".claude.json").write_text(json.dumps({"projects": {
            str(proj): {}, str(home / "deleted"): {}, str(proj / ".claude/worktrees/feat-x"): {}}}))
        managed = td / "managed"
        desktop = home / "Library/Application Support/Claude"
        desktop.mkdir(parents=True)
        (desktop / "claude_desktop_config.json").write_text('{"mcpServers": {}}\n')
        env = Env(home, managed_dir=managed, desktop_dir=desktop)

        print("Discovery")
        tree = discover(env)
        allf = {it["path"]: it for it in iter_files(tree)}
        check("global CLAUDE.md found", allf.get(str(cd / "CLAUDE.md"), {}).get("exists"))
        check("global skill found", str(cd / "skills/tidy/SKILL.md") in allf)
        check("project from ~/.claude.json listed", any(p["path"] == str(proj) for p in tree["projects"]))
        check("deleted project not listed", not any(p["path"].endswith("/deleted") for p in tree["projects"]))
        check("worktree hidden and counted", tree["hiddenWorktrees"] == 1)
        check("missing CLAUDE.local.md shown as creatable",
              allf.get(str(proj / "CLAUDE.local.md"), {}).get("creatable") is True)
        check("subfolder CLAUDE.md found", str(proj / "src/api/CLAUDE.md") in allf)
        check("node_modules skipped", str(proj / "node_modules/x/CLAUDE.md") not in allf)
        check("parent folder CLAUDE.md found", str(code / "CLAUDE.md") in allf)
        check("auto memory matched to project", allf.get(str(mem / "MEMORY.md"), {}).get("exists"))
        check("auto memory topic file found", str(mem / "debugging.md") in allf)
        check("orphan auto memory listed", str(orphan / "MEMORY.md") in allf)
        check("~/.claude.json read-only", allf.get(str(home / ".claude.json"), {}).get("readonly") is True)
        check("desktop config found", allf.get(str(desktop / "claude_desktop_config.json"), {}).get("exists"))
        check("managed files not creatable", not allf.get(str(managed / "CLAUDE.md"), {}).get("creatable"))
        check("keybindings not creatable (needs /keybindings)",
              not allf.get(str(cd / "keybindings.json"), {}).get("creatable"))

        print("Duplicate spellings of one folder")
        aihq = code / "AIHQ"
        aihq.mkdir()
        (code / "aihq-link").symlink_to(aihq)  # Linux is case-sensitive, so a symlink stands in for AIHQ vs aihq
        (cd / "projects" / encode_project_key(code / "aihq-link") / "memory").mkdir(parents=True)
        (cd / "projects" / encode_project_key(code / "aihq-link") / "memory" / "MEMORY.md").write_text("x\n")
        cj = json.loads((home / ".claude.json").read_text())
        cj["projects"][str(aihq)] = {}
        cj["projects"][str(code / "aihq-link")] = {}
        (home / ".claude.json").write_text(json.dumps(cj))
        t1 = discover(env)
        one = [p for p in t1["projects"] if p["path"] in (str(aihq), str(code / "aihq-link"))][0]
        check("only-other-spelling memory used as main MEMORY.md",
              any(it["kind"] == "automem_index" and it["exists"] for it in one["items"]))
        (cd / "projects" / encode_project_key(aihq) / "memory").mkdir(parents=True)
        (cd / "projects" / encode_project_key(aihq) / "memory" / "MEMORY.md").write_text("main\n")
        t2 = discover(env)
        same = [p for p in t2["projects"] if p["path"] in (str(aihq), str(code / "aihq-link"))]
        check("two spellings of one folder shown once", len(same) == 1)
        check("note explains the merge", "more than one spelling" in (same[0].get("note") or ""))
        check("other spelling's auto memory listed, not orphaned",
              any(it.get("label") == "Auto memory (other spelling)" for it in same[0]["items"]) and not any(
                  "aihq-link" in c["path"] for g in t2["groups"] for it in g["items"] if it["type"] == "folder"
                  and it["kind"] == "automem_orphan" for c in it["children"]))
        other = code / "other"
        other.mkdir()
        cj["projects"][str(other)] = {}
        (home / ".claude.json").write_text(json.dumps(cj))
        t3 = discover(env)
        check("different folders stay separate", any(p["path"] == str(other) for p in t3["projects"]) and
              len([p for p in t3["projects"] if p["path"] in (str(aihq), str(code / "aihq-link"))]) == 1)
        check("true_case keeps real spelling", str(true_case(aihq)) == str(aihq))

        print("HTTP API")
        srv, port, token = start_server(env, port=0)
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, tok=token, host=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            if tok:
                req.add_header("X-Token", tok)
            if host:
                req.add_header("Host", host)
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    return e.code, json.loads(raw)
                except Exception:
                    return e.code, {}

        from urllib.parse import quote
        check("no token -> 403", call("GET", "/api/tree", tok=None)[0] == 403)
        check("wrong token -> 403", call("GET", "/api/tree", tok="nope")[0] == 403)
        check("foreign Host header -> 403 (DNS rebinding)", call("GET", "/api/tree", host="evil.com")[0] == 403)
        check("tree with token -> 200", call("GET", "/api/tree")[0] == 200)
        s, r = call("GET", "/api/file?path=" + quote(str(proj / "CLAUDE.md")))
        check("read project CLAUDE.md", s == 200 and r["content"] == "# proj1\n")
        check("read /etc/passwd refused", call("GET", "/api/file?path=/etc/passwd")[0] == 403)
        check("path traversal refused",
              call("GET", "/api/file?path=" + quote(str(proj) + "/../../.ssh/id_rsa"))[0] == 403)

        sl = str(proj / ".claude/settings.local.json")
        s, r = call("GET", "/api/file?path=" + quote(sl))
        m = r["mtime"]
        s, r = call("POST", "/api/save", {"path": sl, "content": '{"permissions": {', "mtime": m})
        check("invalid JSON refused", s == 400 and "invalid JSON" in r.get("error", ""))
        check("file unchanged after refused save", Path(sl).read_text().startswith('{"permissions": {"allow"'))
        new = '{\n  "permissions": {"allow": ["Bash(ls)", "Bash(git status)"]}\n}\n'
        s, r = call("POST", "/api/save", {"path": sl, "content": new, "mtime": m})
        check("valid save ok", s == 200 and Path(sl).read_text() == new)
        bks = list(env.backup_dir.rglob("*settings.local.json"))
        check("backup of previous version written",
              len(bks) == 1 and bks[0].read_text().startswith('{"permissions": {"allow": ["Bash(ls)"]}}'))
        s, r = call("POST", "/api/save", {"path": sl, "content": "{}", "mtime": m})
        check("stale mtime -> 409 conflict", s == 409)
        s, r = call("POST", "/api/save", {"path": sl, "content": "{}\n", "mtime": m, "force": True})
        check("force overwrite ok", s == 200 and Path(sl).read_text() == "{}\n")
        s, r = call("POST", "/api/save", {"path": str(home / ".claude.json"), "content": "{}", "mtime": None})
        check("save to ~/.claude.json refused", s == 403)
        s, r = call("POST", "/api/save", {"path": str(td / "evil.txt"), "content": "x"})
        check("save outside managed files refused", s == 403 and not (td / "evil.txt").exists())

        gid = "p:" + str(proj)
        s, r = call("POST", "/api/create", {"group": gid, "kind": "proj_skills", "name": "deploy"})
        sk = proj / ".claude/skills/deploy/SKILL.md"
        check("create project skill", s == 200 and sk.is_file() and "name: deploy" in sk.read_text())
        s, r = call("POST", "/api/create", {"group": gid, "kind": "proj_skills", "name": "deploy"})
        check("create duplicate -> 409", s == 409)
        s, r = call("POST", "/api/create", {"group": gid, "kind": "proj_skills", "name": "../evil"})
        check("create with ../ name refused", s == 400 and not (proj / ".claude/evil").exists())
        s, r = call("POST", "/api/create", {"group": gid, "kind": "proj_local_md"})
        check("create CLAUDE.local.md", s == 200 and (proj / "CLAUDE.local.md").is_file())
        s, r = call("POST", "/api/create", {"group": gid, "kind": "proj_agents", "name": "tester.md"})
        check("create agent strips .md", s == 200 and (proj / ".claude/agents/tester.md").is_file())
        s, r = call("POST", "/api/create", {"group": "global", "kind": "proj_local_md"})
        check("kind in wrong section refused", s == 400)
        s, r = call("POST", "/api/create", {"group": "global", "kind": "keybindings"})
        check("keybindings create refused with hint", s == 400 and "/keybindings" in r.get("error", ""))
        s, r = call("POST", "/api/create", {"group": "managed", "kind": "managed_settings"})
        check("managed create refused", s == 400 and not (managed / "managed-settings.json").exists())

        s, r = call("POST", "/api/folders/add", {"path": str(extra)})
        s2, t2 = call("GET", "/api/tree")
        check("added folder appears", s == 200 and any(p["path"] == str(extra) and p["source"] == "added"
                                                       for p in t2["projects"]))
        check("add nonexistent folder refused", call("POST", "/api/folders/add", {"path": str(td / 'nope')})[0] == 400)
        call("POST", "/api/folders/remove", {"path": str(extra)})
        s2, t2 = call("GET", "/api/tree")
        check("removed folder gone", not any(p["path"] == str(extra) for p in t2["projects"]))

        print("Best-practice review (with a fake `claude` CLI)")
        fake = td / "fake_claude.py"
        log = td / "fake_log.json"
        fake.write_text(r"""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
stdin = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
json.dump({"args": args, "stdin": stdin, "cwd": os.getcwd()}, open(os.environ["FAKE_LOG"], "w"))
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    print("Invalid API key · Please run /login", file=sys.stderr); sys.exit(1)
if mode == "nostruct":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "I could not comply"})); sys.exit(0)
target = os.environ["FAKE_TARGET"]; other = os.environ["FAKE_OTHER"]; js = os.environ["FAKE_JSON"]
so = {"summary": "Mostly fine.", "sources_checked": [{"title": "Memory", "url": "https://code.claude.com/docs/en/memory.md", "fetched": True},
                                                      {"title": "Hooks", "url": "https://code.claude.com/docs/en/hooks-guide.md", "fetched": False}],
      "findings": [{"file": target, "severity": "low", "issue": "Too vague", "recommendation": "Be specific",
                    "source_url": "https://code.claude.com/docs/en/memory.md"},
                   {"file": target, "severity": "high", "issue": "Conflict", "recommendation": "Fix",
                    "source_url": "https://code.claude.com/docs/en/memory.md"}],
      "rewrites": [{"file": target, "explanation": "Tighter", "content": "# proj1\n\n## Build\n- npm test\n"},
                   {"file": other, "explanation": "should be dropped", "content": "hacked"},
                   {"file": js, "explanation": "bad json", "content": "{not json"},
                   {"file": target, "explanation": "empty", "content": "  \n"}]}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "total_cost_usd": 0.12,
                  "structured_output": so}))
""")
        fake.chmod(0o755)
        target = str(proj / "CLAUDE.md")
        Path(target).write_text("# proj1\n")
        sl_path = proj / ".claude/settings.local.json"
        sl_path.write_text('{"env": {"OPENAI_API_KEY": "sk-SECRET123"}, "apiToken": "tok-SECRET456", "model": "opus"}\n')
        os.environ.update({"DOTCLAUDE_CLAUDE_BIN": str(fake), "FAKE_LOG": str(log), "FAKE_TARGET": target,
                           "FAKE_OTHER": str(td / "evil.txt"), "FAKE_JSON": str(sl_path), "FAKE_MODE": "ok"})

        def wait_job(jid, limit=20):
            for _ in range(limit * 10):
                s_, r_ = call("GET", "/api/review/status?id=" + jid)
                if r_.get("status") != "running":
                    return r_
                time.sleep(0.1)
            return r_

        s, r = call("POST", "/api/review/start", {"group": gid})
        check("review start -> running", s == 200 and r["status"] == "running")
        r = wait_job(r["id"])
        check("review finishes", r["status"] == "done")
        lg = json.loads(log.read_text())
        a_ = lg["args"]
        check("CLI gets only WebFetch/WebSearch tools",
              a_[a_.index("--tools") + 1] == "WebFetch,WebSearch" and "Read" not in " ".join(a_))
        check("CLI runs in dontAsk mode", a_[a_.index("--permission-mode") + 1] == "dontAsk")
        check("CLI gets the JSON schema", json.loads(a_[a_.index("--json-schema") + 1])["required"])
        check("prompt names official guidance URLs", "code.claude.com/docs/en/memory.md" in a_[1]
              and "settings.md" in a_[1])
        check("file content sent on stdin", "# proj1" in lg["stdin"])
        check("secrets redacted before sending", "SECRET123" not in lg["stdin"] and "SECRET456" not in lg["stdin"]
              and "<redacted>" in lg["stdin"])
        check("non-secret JSON values kept", '"opus"' in lg["stdin"])
        check("runs in an empty temp dir, not the project", str(proj) not in lg["cwd"])
        res = r["result"]
        check("findings sorted high first", [f["severity"] for f in res["findings"]] == ["high", "low"])
        check("rewrite for reviewed file kept with diff",
              len(res["rewrites"]) == 1 and res["rewrites"][0]["file"] == target and "+## Build" in res["rewrites"][0]["diff"])
        check("rewrite for outside file dropped", str(td / "evil.txt") in res["dropped_rewrites"])
        check("rewrite for redacted JSON dropped", str(sl_path) in res["dropped_rewrites"])
        check("empty rewrite dropped", res["dropped_rewrites"].count(target) == 1)
        check("unloaded guidance pages reported", res["guidance_failed"] == ["https://code.claude.com/docs/en/hooks-guide.md"])
        check("report saved to ~/.dotclaude/reviews", len(list((env.state_dir / "reviews").glob("*.json"))) == 1)
        check("file not changed by the review itself", Path(target).read_text() == "# proj1\n")
        rw = res["rewrites"][0]
        Path(target).write_text("# proj1 edited meanwhile\n")
        os.utime(target, ns=(time.time_ns(), time.time_ns() + 5_000_000))
        s, r = call("POST", "/api/review/apply", {"path": target, "content": rw["content"], "base_mtime": rw["base_mtime"]})
        check("apply refused if file changed since review", s == 409 and "meanwhile" in Path(target).read_text())
        cur_m = str(Path(target).stat().st_mtime_ns)
        nb = len(list(env.backup_dir.rglob("*CLAUDE.md")))
        s, r = call("POST", "/api/review/apply", {"path": target, "content": rw["content"], "base_mtime": cur_m})
        check("apply writes rewrite", s == 200 and Path(target).read_text() == rw["content"])
        check("apply backs up old version first", len(list(env.backup_dir.rglob("*CLAUDE.md"))) == nb + 1)
        s, r = call("POST", "/api/review/apply", {"path": str(home / ".claude.json"), "content": "{}"})
        check("apply to read-only file refused", s == 403)

        os.environ["FAKE_MODE"] = "fail"
        s, r = call("POST", "/api/review/start", {"path": target})
        r = wait_job(r["id"])
        check("CLI failure -> error shown with message", r["status"] == "error" and "login" in r["error"])
        os.environ["FAKE_MODE"] = "nostruct"
        s, r = call("POST", "/api/review/start", {"path": target})
        r = wait_job(r["id"])
        check("missing structured_output -> error", r["status"] == "error" and "could not comply" in r["error"])
        os.environ["FAKE_MODE"] = "slow"
        s, r = call("POST", "/api/review/start", {"path": target})
        time.sleep(0.5)
        s, r2 = call("POST", "/api/review/cancel", {"id": r["id"]})
        time.sleep(0.5)
        s, r3 = call("GET", "/api/review/status?id=" + r["id"])
        check("cancel stops a running review", r3["status"] == "cancelled")
        s, r = call("POST", "/api/review/start", {"path": str(home / ".claude.json")})
        check("~/.claude.json can't be reviewed", s == 400)
        s, r = call("POST", "/api/review/start", {"path": "/etc/passwd"})
        check("review of unmanaged file refused", s == 403)
        os.environ["DOTCLAUDE_CLAUDE_BIN"] = str(td / "does-not-exist")
        s, r = call("POST", "/api/review/start", {"path": target})
        check("missing claude CLI -> clear error", s == 424 and "claude" in r["error"])
        for k in ("DOTCLAUDE_CLAUDE_BIN", "FAKE_MODE", "FAKE_LOG", "FAKE_TARGET", "FAKE_OTHER", "FAKE_JSON"):
            os.environ.pop(k, None)

        print("Backup history")
        h_target = proj / "CLAUDE.md"
        h_path = str(h_target)
        s, r = call("GET", "/api/backups?path=" + quote(h_path))
        n0 = r["count"]
        s, f0 = call("GET", "/api/file?path=" + quote(h_path))
        call("POST", "/api/save", {"path": h_path, "content": "# v2\n", "mtime": f0["mtime"]})
        s, f1 = call("GET", "/api/file?path=" + quote(h_path))
        call("POST", "/api/save", {"path": h_path, "content": "# v3\n", "mtime": f1["mtime"]})
        s, r = call("GET", "/api/backups?path=" + quote(h_path))
        check("history lists this file's backups, newest first", s == 200 and r["count"] == n0 + 2 and
              all(b["original"] == h_path for b in r["backups"]) and
              _backup_sort_key(r["backups"][0]) > _backup_sort_key(r["backups"][1]))
        newest = r["backups"][0]
        s, b = call("GET", "/api/backup?id=" + quote(newest["id"]))
        check("read backup returns content + diff", s == 200 and b["content"] == "# v2\n" and "-# v3" in b["diff"])
        s, allb = call("GET", "/api/backups")
        check("all-backups list includes other files", allb["count"] > r["count"])
        s, f2 = call("GET", "/api/file?path=" + quote(h_path))
        s, rr = call("POST", "/api/backups/restore", {"id": newest["id"], "mtime": f2["mtime"]})
        check("restore puts the old version back", s == 200 and h_target.read_text() == "# v2\n")
        s, r2 = call("GET", "/api/backups?path=" + quote(h_path))
        s, b2 = call("GET", "/api/backup?id=" + quote(r2["backups"][0]["id"]))
        check("restore backs up the version it replaced", r2["count"] == r["count"] + 1 and b2["content"] == "# v3\n")
        s, rr = call("POST", "/api/backups/restore", {"id": newest["id"], "mtime": "123"})
        check("restore with stale mtime -> 409", s == 409)
        check("backup id with ../ refused", call("GET", "/api/backup?id=" + quote("../../etc/passwd"))[0] == 400)
        check("absolute backup id refused", call("GET", "/api/backup?id=" + quote("/etc/passwd"))[0] == 400)
        evil = env.backup_dir / "20260101-000000" / flat_name(td / "evil.txt")
        evil.parent.mkdir(parents=True, exist_ok=True)
        evil.write_text("x")
        s, rr = call("POST", "/api/backups/restore", {"id": "20260101-000000/" + evil.name})
        check("restore to an unmanaged path refused", s == 403 and not (td / "evil.txt").exists())
        cj_bk = env.backup_dir / "20260101-000001" / flat_name(home / ".claude.json")
        cj_bk.parent.mkdir(parents=True, exist_ok=True)
        cj_bk.write_text("{}")
        s, rr = call("POST", "/api/backups/restore", {"id": "20260101-000001/" + cj_bk.name})
        check("restore over a read-only file refused", s == 403)

        print("Health, stop and rename")
        with urllib.request.urlopen(base + "/health") as rr:
            hj = json.loads(rr.read())
        check("/health works without a token", hj.get("app") == "dotclaude" and hj["url"].endswith(f":{port}/"))
        check("/health refuses a foreign Host", call("GET", "/health", tok=None, host="evil.com")[0] == 403)
        check("/health leaks nothing private", set(hj) == {"app", "version", "url"})
        check("shutdown needs the token", call("POST", "/api/shutdown", {}, tok=None)[0] == 403)
        old, new = td / "mig/.claude-files", td / "mig/.dotclaude"
        (old / "backups").mkdir(parents=True)
        (old / "config.json").write_text('{"extra_folders": ["/x"]}')
        check("old ~/.claude-files moved to ~/.dotclaude", migrate_state_dir(old, new) and
              (new / "config.json").is_file() and not old.exists())
        old.mkdir()
        check("migration never overwrites an existing ~/.dotclaude", not migrate_state_dir(old, new) and old.is_dir())

        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req) as rr:
            html = rr.read().decode()
        check("page served with token injected", token in html and "__TOKEN__" not in html)
        s, r = call("POST", "/api/shutdown", {})
        check("shutdown with token stops the server", s == 200 and srv.stopped.wait(5))

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Browse and edit the files Claude Code reads.")
    ap.add_argument("--port", type=int, default=0, help="port (default: first free from 8765)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--self-test", action="store_true", help="run built-in checks and exit")
    ap.add_argument("--home", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    env = Env(Path(args.home) if args.home else Path.home())
    srv, port, token = start_server(env, args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} {APP_VERSION} running at {url}\nPress Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while not srv.stopped.wait(1):
            pass
        print("Stopped from the app.")
    except KeyboardInterrupt:
        print("\nStopped.")
        srv.shutdown()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DotClaude</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTQiIGZpbGw9IiNDMjQxMEMiLz48cGF0aCBkPSJNMTYgOWgyMmwxMiAxMnYzMXEwIDQtNCA0SDE2cS00IDAtNC00VjEzcTAtNCA0LTR6IiBmaWxsPSIjRkJGNkYxIi8+PHBhdGggZD0iTTM4IDl2OHEwIDQgNCA0aDh6IiBmaWxsPSIjRTlDOUI1Ii8+PGNpcmNsZSBjeD0iMjUiIGN5PSI0MiIgcj0iOCIgZmlsbD0iI0MyNDEwQyIvPjwvc3ZnPg==">
<style>
:root {
  --bg: #f7f6f3; --panel: #ffffff; --side: #efede8; --ink: #1f1e1c; --muted: #6b6862; --faint: #9a968e;
  --line: #e2dfd8; --accent: #c2410c; --accent-ink: #ffffff; --hover: #e6e3dc; --sel: #fde7dc;
  --ok: #15803d; --warn: #b45309; --err: #b91c1c; --code: #f3f1ec; --chip: #ebe8e1;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1b1a18; --panel: #232220; --side: #191816; --ink: #ebe8e2; --muted: #a8a49c; --faint: #77736b;
    --line: #34322e; --accent: #f08a5d; --accent-ink: #1b1a18; --hover: #2a2926; --sel: #3a2a22;
    --ok: #4ade80; --warn: #fbbf24; --err: #f87171; --code: #1e1d1b; --chip: #2e2c29;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--ink); font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; overflow: hidden; }
button { font: inherit; color: inherit; cursor: pointer; }
code, textarea, .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
a { color: var(--accent); }

/* Sidebar */
#side { width: 330px; min-width: 260px; background: var(--side); border-right: 1px solid var(--line); display: flex; flex-direction: column; }
#side header { padding: 14px 14px 10px; border-bottom: 1px solid var(--line); }
#side h1 { font-size: 15px; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; cursor: pointer; }
#side h1 .logo { width: 20px; height: 20px; display: inline-block; }
#search { width: 100%; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); color: var(--ink); }
.side-opts { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 12px; color: var(--muted); }
.side-opts label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
#nav { overflow-y: auto; flex: 1; padding: 6px 6px 40px; }
.sec-title { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--faint); padding: 14px 8px 4px; display: flex; justify-content: space-between; align-items: center; }
.grp { margin-bottom: 2px; }
.grp-head { display: flex; align-items: center; gap: 6px; width: 100%; text-align: left; background: none; border: 0; padding: 6px 8px; border-radius: 6px; }
.grp-head:hover { background: var(--hover); }
.grp-head .caret { width: 10px; color: var(--faint); font-size: 10px; transition: transform .12s; }
.grp.closed .caret { transform: rotate(-90deg); }
.grp.closed .grp-body { display: none; }
.grp-head .t { font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grp-head .n { font-size: 11px; color: var(--faint); }
.grp-sub { font-size: 11px; color: var(--faint); padding: 0 8px 4px 24px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; background: none; border: 0; padding: 4px 8px 4px 24px; border-radius: 6px; font-size: 13px; }
.row:hover { background: var(--hover); }
.row.sel { background: var(--sel); }
.row .lbl { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row.missing .lbl { color: var(--faint); }
.row .tag { font-size: 11px; color: var(--faint); }
.row.missing:hover .tag { color: var(--accent); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex: none; }
.row.missing .dot { background: transparent; border: 1.5px solid var(--faint); }
.dot.ro { background: var(--muted); }
.row.folder .lbl { font-weight: 500; }
.row.child { padding-left: 42px; }
.fold-ico { width: 8px; flex: none; color: var(--faint); font-size: 10px; }
.mini { font-size: 11px; border: 1px solid var(--line); background: var(--panel); border-radius: 5px; padding: 1px 7px; color: var(--muted); }
.mini:hover { color: var(--accent); border-color: var(--accent); }
.badge { font-size: 10px; padding: 1px 6px; border-radius: 9px; background: var(--chip); color: var(--muted); }

/* Main */
#main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
#content { flex: 1; overflow-y: auto; padding: 26px 34px 30px; display: flex; flex-direction: column; min-height: 0; }
.crumbs { font-size: 12px; color: var(--faint); margin-bottom: 4px; }
h2 { font-size: 22px; margin: 0 0 6px; letter-spacing: -.01em; }
.pathline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.pathline code { background: var(--code); border: 1px solid var(--line); padding: 2px 8px; border-radius: 6px; font-size: 12px; word-break: break-all; }
.facts { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 10px; margin-bottom: 18px; }
.fact { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.fact h4 { margin: 0 0 3px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--faint); font-weight: 600; }
.fact p { margin: 0; font-size: 13px; }
.fact code { background: var(--code); padding: 0 4px; border-radius: 4px; font-size: 12px; }
.editor-wrap { flex: 1; display: flex; flex-direction: column; min-height: 320px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.toolbar { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-bottom: 1px solid var(--line); font-size: 12px; }
.toolbar .status { color: var(--muted); }
.toolbar .status.dirty { color: var(--warn); font-weight: 600; }
.toolbar .status.ro { color: var(--muted); }
.toolbar .spacer { flex: 1; }
.btn { border: 1px solid var(--line); background: var(--panel); border-radius: 7px; padding: 5px 12px; font-size: 13px; }
.btn:hover { background: var(--hover); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
.btn.primary:disabled { opacity: .45; cursor: default; }
.btn:disabled { cursor: default; }
textarea#ed { flex: 1; border: 0; outline: none; resize: none; padding: 14px 16px; background: var(--panel); color: var(--ink); font-size: 13px; line-height: 1.55; tab-size: 2; }
textarea#ed[readonly] { color: var(--muted); }
.msg { padding: 7px 12px; font-size: 12px; border-bottom: 1px solid var(--line); }
.msg.err { color: var(--err); }
.msg.warn { color: var(--warn); }
.msg.ok { color: var(--ok); }
.empty-card { background: var(--panel); border: 1px dashed var(--line); border-radius: 10px; padding: 20px; }
.empty-card pre { background: var(--code); border: 1px solid var(--line); border-radius: 8px; padding: 12px; font-size: 12px; overflow: auto; max-height: 260px; }
.note { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 8px; padding: 10px 14px; font-size: 13px; color: var(--muted); margin-bottom: 16px; }
.list { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
.list button { display: flex; width: 100%; text-align: left; gap: 10px; align-items: center; background: none; border: 0; border-bottom: 1px solid var(--line); padding: 9px 14px; }
.list button:last-child { border-bottom: 0; }
.list button:hover { background: var(--hover); }
.welcome h2 { font-size: 24px; }
.welcome ol { padding-left: 20px; }
.welcome li { margin: 4px 0; }
.welcome .stats { display: flex; gap: 10px; margin: 14px 0 20px; }
.welcome .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 10px 16px; }
.welcome .stat b { font-size: 20px; display: block; }
.welcome section { max-width: 760px; }
.legend { display: flex; gap: 16px; align-items: center; font-size: 13px; color: var(--muted); flex-wrap: wrap; }
.legend span { display: flex; gap: 6px; align-items: center; }
.legend .row { padding: 0; width: auto; display: inline-flex; }
dialog { border: 1px solid var(--line); border-radius: 12px; background: var(--panel); color: var(--ink); padding: 20px; width: min(440px, 92vw); }
dialog::backdrop { background: rgba(0,0,0,.35); }
dialog h3 { margin: 0 0 8px; font-size: 16px; }
dialog p { margin: 0 0 14px; color: var(--muted); }
dialog input { width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 7px; background: var(--bg); color: var(--ink); margin-bottom: 14px; }
dialog .acts { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
.rv-btn { border: 1px solid var(--accent); color: var(--accent); background: var(--panel); border-radius: 7px; padding: 5px 12px; font-size: 13px; font-weight: 600; }
.rv-btn:hover { background: var(--sel); }
.rv-btn:disabled { opacity: .5; cursor: default; }
#rv-banner { display: none; margin-top: 8px; font-size: 12px; background: var(--sel); border-radius: 7px; padding: 6px 10px; cursor: pointer; }
#rv-banner.show { display: block; }
.rv-card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }
.rv-card h4 { margin: 0 0 4px; font-size: 14px; }
.rv-card p { margin: 4px 0; }
.sev { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; padding: 1px 7px; border-radius: 9px; margin-right: 6px; }
.sev.high { background: #fee2e2; color: #991b1b; } .sev.medium { background: #fef3c7; color: #92400e; } .sev.low { background: var(--chip); color: var(--muted); }
@media (prefers-color-scheme: dark) { .sev.high { background: #4c1d1d; color: #fca5a5; } .sev.medium { background: #4a3510; color: #fcd34d; } }
.rv-meta { font-size: 12px; color: var(--faint); }
.rv-file { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--muted); cursor: pointer; text-decoration: underline dotted; }
pre.diff { background: var(--code); border: 1px solid var(--line); border-radius: 8px; padding: 10px; font-size: 12px; overflow: auto; max-height: 420px; margin: 8px 0; }
pre.diff .a { color: var(--ok); } pre.diff .d { color: var(--err); } pre.diff .h { color: var(--faint); }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: -2px; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
#toast { position: fixed; bottom: 18px; right: 18px; background: var(--ink); color: var(--bg); padding: 9px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; max-width: 420px; }
#toast.show { opacity: 1; }
@media (max-width: 900px) {
  .facts { grid-template-columns: 1fr; }
  #side { width: 270px; }
  #content { padding: 18px; }
}
</style>
</head>
<body>
<aside id="side">
  <header>
    <h1 id="home-link"><img class="logo" alt="" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTQiIGZpbGw9IiNDMjQxMEMiLz48cGF0aCBkPSJNMTYgOWgyMmwxMiAxMnYzMXEwIDQtNCA0SDE2cS00IDAtNC00VjEzcTAtNCA0LTR6IiBmaWxsPSIjRkJGNkYxIi8+PHBhdGggZD0iTTM4IDl2OHEwIDQgNCA0aDh6IiBmaWxsPSIjRTlDOUI1Ii8+PGNpcmNsZSBjeD0iMjUiIGN5PSI0MiIgcj0iOCIgZmlsbD0iI0MyNDEwQyIvPjwvc3ZnPg==">DotClaude</h1>
    <input id="search" placeholder="Filter files…  (⌘K)" autocomplete="off">
    <div class="side-opts">
      <label><input type="checkbox" id="show-missing"> Show files you could add</label>
      <span><button class="mini" id="refresh" title="Rescan">↻ Rescan</button> <button class="mini" id="stop" title="Stop DotClaude">⏻ Stop</button></span>
    </div>
    <div id="rv-banner"></div>
  </header>
  <nav id="nav"></nav>
</aside>
<main id="main"><div id="content"></div></main>
<dialog id="dlg"></dialog>
<div id="toast"></div>

<script>
const TOKEN = "__TOKEN__";
const DIRTY_KINDS_LIMIT = { user_claude_md: 200, proj_claude_md: 200, proj_dot_claude_md: 200, proj_local_md: 200, automem_index: 200 };
let tree = null, cur = null, sel = null;
const S = { showMissing: true, closed: {} };
try { const s = JSON.parse(localStorage.getItem("cf-state") || "{}"); Object.assign(S, s); } catch (e) {}
function persist() { try { localStorage.setItem("cf-state", JSON.stringify(S)); } catch (e) {} }

const $ = (q, el = document) => el.querySelector(q);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = s => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
const tilde = p => tree && p.startsWith(tree.home) ? "~" + p.slice(tree.home.length) : p;
const docUrl = d => d.startsWith("http") ? d : tree.docsBase + d;

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) { const err = new Error(data.error || ("HTTP " + r.status)); err.status = r.status; err.data = data; throw err; }
  return data;
}
function toast(msg, ms = 2600) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms); }

function ask({ title, text = "", input = null, buttons }) {
  return new Promise(res => {
    const d = $("#dlg");
    d.innerHTML = `<h3>${esc(title)}</h3>${text ? `<p>${text}</p>` : ""}${input !== null ? `<input id="dlg-in" value="${esc(input.value || "")}" placeholder="${esc(input.placeholder || "")}">` : ""}<div class="acts"></div>`;
    const acts = $(".acts", d);
    buttons.forEach(b => {
      const el = document.createElement("button");
      el.className = "btn" + (b.primary ? " primary" : "");
      el.textContent = b.label;
      el.onclick = () => { const v = input !== null ? $("#dlg-in", d).value : undefined; d.close(); res({ value: b.value, input: v }); };
      acts.appendChild(el);
    });
    d.onclose = () => res(res._done ? undefined : { value: null });
    d.showModal();
    const inEl = $("#dlg-in", d);
    if (inEl) { inEl.focus(); inEl.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); const p = buttons.find(b => b.primary); if (p) { d.close(); res({ value: p.value, input: inEl.value }); } } }; }
  });
}

/* ---------------- Sidebar ---------------- */
function itemMatches(it, g, q) {
  if (!q) return true;
  const k = tree.kinds[it.kind] || {};
  return [it.label, it.path, k.title, g.title].join(" ").toLowerCase().includes(q);
}
function countFiles(g) { let n = 0; g.items.forEach(it => { if (it.type === "file") n += it.exists ? 1 : 0; else n += it.children.length; }); return n; }

function renderNav() {
  const q = $("#search").value.trim().toLowerCase();
  const nav = $("#nav");
  nav.innerHTML = "";
  const addSection = (title, extra) => { const d = document.createElement("div"); d.className = "sec-title"; d.innerHTML = `<span>${esc(title)}</span>`; if (extra) d.appendChild(extra); nav.appendChild(d); };

  const g0 = tree.groups.find(g => g.id === "global");
  addSection("This Mac");
  renderGroup(nav, g0, q);
  nav.appendChild(rowEl({ label: "Backups (every saved version)", cls: "folder", dot: false, onclick: () => openAllBackups(), selKey: "backups" }));

  const addBtn = document.createElement("button");
  addBtn.className = "mini"; addBtn.textContent = "+ Add folder"; addBtn.onclick = addFolder;
  addSection(`Projects (${tree.projects.length})`, addBtn);
  if (!tree.projects.length) { const p = document.createElement("div"); p.className = "grp-sub"; p.style.paddingLeft = "8px"; p.textContent = "No projects yet. Open a folder in Claude Code, or use + Add folder."; nav.appendChild(p); }
  tree.projects.forEach(g => renderGroup(nav, g, q));
  if (tree.hiddenWorktrees) { const p = document.createElement("div"); p.className = "grp-sub"; p.style.paddingLeft = "8px"; p.textContent = `${tree.hiddenWorktrees} Claude Code worktree folder(s) hidden`; nav.appendChild(p); }

  addSection("Apps & organisation");
  tree.groups.filter(g => g.id !== "global").forEach(g => renderGroup(nav, g, q));
}

function renderGroup(nav, g, q) {
  const showMissing = S.showMissing;
  const vis = g.items.map(it => {
    if (it.type === "file") return (it.exists || showMissing) && itemMatches(it, g, q) ? it : null;
    const kids = it.children.filter(c => itemMatches(c, g, q));
    const self = itemMatches(it, g, q);
    if (!kids.length && !(showMissing && it.canCreate && self) && !(self && it.children.length)) return null;
    return { ...it, _kids: self && !q ? it.children : (kids.length ? kids : it.children) };
  }).filter(Boolean);
  if (q && !vis.length) return;

  const closedDefault = g.collapsed || (g.scope === "project" && tree.projects.length > 3);
  const closed = q ? false : (S.closed[g.id] ?? closedDefault);
  const wrap = document.createElement("div");
  wrap.className = "grp" + (closed ? " closed" : "");
  const head = document.createElement("button");
  head.className = "grp-head";
  const srcBadge = g.source === "added" ? `<span class="badge">added</span>` : "";
  head.innerHTML = `<span class="caret">▼</span><span class="t">${esc(g.title)}</span>${srcBadge}<span class="n">${countFiles(g)}</span>`;
  head.onclick = () => { S.closed[g.id] = !wrap.classList.contains("closed"); persist(); wrap.classList.toggle("closed"); };
  head.ondblclick = () => openGroup(g);
  wrap.appendChild(head);
  const body = document.createElement("div");
  body.className = "grp-body";
  const sub = document.createElement("div");
  sub.className = "grp-sub"; sub.textContent = g.subtitle; sub.title = g.subtitle;
  body.appendChild(sub);
  const info = rowEl({ label: "About this section", cls: "folder", tag: "", dot: false, onclick: () => openGroup(g), selKey: "g:" + g.id });
  body.appendChild(info);
  vis.forEach(it => {
    if (it.type === "file") body.appendChild(fileRow(it));
    else {
      body.appendChild(rowEl({ label: it.label, cls: "folder", tag: it.children.length ? String(it.children.length) : (it.canCreate ? "+ new" : ""), fold: true, onclick: () => openFolder(it), selKey: "f:" + it.group + ":" + it.kind }));
      (it._kids || []).forEach(c => body.appendChild(fileRow(c, true)));
    }
  });
  wrap.appendChild(body);
  nav.appendChild(wrap);
}

function rowEl({ label, cls = "", tag = "", dot = true, roDot = false, fold = false, onclick, selKey, title }) {
  const b = document.createElement("button");
  b.className = "row " + cls + (sel === selKey ? " sel" : "");
  b.dataset.key = selKey;
  if (title) b.title = title;
  b.innerHTML = (fold ? `<span class="fold-ico">▸</span>` : dot ? `<span class="dot${roDot ? " ro" : ""}"></span>` : `<span class="fold-ico">ⓘ</span>`) + `<span class="lbl">${esc(label)}</span>` + (tag ? `<span class="tag">${esc(tag)}</span>` : "");
  b.onclick = onclick;
  return b;
}
function fileRow(it, child = false) {
  return rowEl({ label: it.label, cls: (it.exists ? "" : "missing ") + (child ? "child" : ""), tag: it.exists ? (it.readonly ? "read-only" : "") : (it.creatable ? "+ add" : "none"),
    roDot: it.readonly, onclick: () => openFile(it), selKey: "p:" + it.path, title: tilde(it.path) });
}
function markSel(key) { sel = key; document.querySelectorAll(".row").forEach(r => r.classList.toggle("sel", r.dataset.key === key)); }

/* ---------------- Main pane ---------------- */
function groupOf(it) { return [...tree.groups, ...tree.projects].find(g => g.id === it.group); }
function factsHtml(k) {
  return `<div class="facts">
    <div class="fact"><h4>What it's for</h4><p>${md(k.role)}</p></div>
    <div class="fact"><h4>When it loads</h4><p>${md(k.loads)}</p></div>
    <div class="fact"><h4>Shared or personal</h4><p>${md(k.share)}</p><p style="margin-top:6px"><a href="${esc(docUrl(k.docs))}" target="_blank" rel="noopener">Docs ↗</a></p></div>
  </div>`;
}
function pathLine(path) {
  return `<div class="pathline"><code>${esc(tilde(path))}</code><button class="mini" data-copy="${esc(path)}">Copy path</button><button class="mini" data-reveal="${esc(path)}">Show in Finder</button></div>`;
}
function wirePathButtons(root) {
  root.querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.copy); toast("Path copied"); } catch (e) { toast("Couldn't copy"); } });
  root.querySelectorAll("[data-reveal]").forEach(b => b.onclick = async () => { try { await api("POST", "/api/reveal", { path: b.dataset.reveal }); } catch (e) { toast(e.message); } });
}

async function guardDirty() {
  if (!cur || !cur.dirty) return true;
  const r = await ask({ title: "Unsaved changes", text: `You have unsaved changes in <b>${esc(cur.item.label)}</b>.`, buttons: [{ label: "Cancel", value: "cancel" }, { label: "Discard", value: "discard" }, { label: "Save", value: "save", primary: true }] });
  if (!r || r.value === "cancel" || r.value === null) return false;
  if (r.value === "save") return await save();
  return true;
}

async function openFile(it, force = false) {
  if (!force && !(await guardDirty())) return;
  markSel("p:" + it.path);
  const k = tree.kinds[it.kind], g = groupOf(it);
  const c = $("#content");
  cur = null;
  const head = `<div class="crumbs">${esc(g.title)} › ${esc(it.label)}</div><h2>${esc(k.title)}</h2>${pathLine(it.path)}${factsHtml(k)}`;
  if (!it.exists) {
    c.innerHTML = head + `<div class="empty-card">
      <b>This file doesn't exist yet.</b>
      ${it.creatable ? `<p>Create it with this starting template, then edit it:</p><pre>${esc(fillTpl(k.template, g, ""))}</pre><button class="btn primary" id="create">Create ${esc(it.label)}</button>`
        : `<p>${md(k.create_hint || (k.readonly ? (k.readonly_reason || "This file can't be created here.") : "This file is created by Claude Code itself."))}</p>`}
    </div>`;
    wirePathButtons(c);
    const b = $("#create", c);
    if (b) b.onclick = () => doCreate(g.id, it.kind, null);
    return;
  }
  c.innerHTML = head + `<div class="editor-wrap"><div class="toolbar"><span class="status" id="st">Loading…</span><span id="lines" style="color:var(--faint)"></span><span class="spacer"></span><button class="btn" id="hist-btn" title="Earlier versions of this file">History</button>${tree.notReviewed.includes(it.kind) ? "" : `<button class="rv-btn" id="rv-file">✦ Review against best practice</button>`}<button class="btn" id="revert" disabled>Revert</button><button class="btn primary" id="save" disabled>Save  ⌘S</button></div><div id="msgs"></div><textarea id="ed" spellcheck="false"></textarea></div>`;
  wirePathButtons(c);
  let f;
  try { f = await api("GET", "/api/file?path=" + encodeURIComponent(it.path)); }
  catch (e) { $("#st").textContent = e.message; $("#st").style.color = "var(--err)"; return; }
  cur = { item: it, original: f.content, mtime: f.mtime, readonly: f.readonly, dirty: false };
  const ed = $("#ed");
  ed.value = f.content;
  ed.readOnly = f.readonly;
  ed.oninput = () => { cur.dirty = ed.value !== cur.original; updateBar(); };
  ed.onkeydown = e => {
    if (e.key === "Tab" && !ed.readOnly) { e.preventDefault(); const s = ed.selectionStart, en = ed.selectionEnd; ed.setRangeText("  ", s, en, "end"); ed.oninput(); }
  };
  $("#save").onclick = () => save();
  const rvb = $("#rv-file"); if (rvb) rvb.onclick = () => startReview({ path: it.path });
  $("#hist-btn").onclick = () => openHistory(it);
  api("GET", "/api/backups?path=" + encodeURIComponent(it.path)).then(b => {
    const hb = $("#hist-btn"); if (hb && cur && cur.item.path === it.path) hb.textContent = `History (${b.count})`;
  }).catch(() => {});
  $("#revert").onclick = () => { ed.value = cur.original; cur.dirty = false; updateBar(); };
  updateBar();
}

function updateBar() {
  if (!cur) return;
  const ed = $("#ed"), st = $("#st");
  const v = ed.value;
  const lines = v ? v.split("\n").length - (v.endsWith("\n") ? 1 : 0) : 0;
  $("#lines").textContent = `· ${lines} line${lines === 1 ? "" : "s"} · ${(() => { const n = new Blob([v]).size; return n < 1024 ? n + " bytes" : (n / 1024).toFixed(1) + " KB"; })()}`;
  if (cur.readonly) { st.className = "status ro"; st.textContent = "🔒 Read-only"; }
  else if (cur.dirty) { st.className = "status dirty"; st.textContent = "● Unsaved changes"; }
  else { st.className = "status"; st.textContent = "Saved"; }
  $("#save").disabled = cur.readonly || !cur.dirty;
  $("#revert").disabled = cur.readonly || !cur.dirty;
  const msgs = [];
  const k = tree.kinds[cur.item.kind];
  if (cur.readonly && k.readonly_reason) msgs.push(["warn", k.readonly_reason]);
  if (cur.item.path.endsWith(".json")) { try { JSON.parse(v); } catch (e) { msgs.push(["err", "Invalid JSON: " + e.message + " (it won't save until this is fixed)"]); } }
  const lim = DIRTY_KINDS_LIMIT[cur.item.kind];
  if (lim && lines > lim) msgs.push(["warn", cur.item.kind === "automem_index" ? `Over ${lim} lines: only the first ${lim} lines (max 25 KB) load at the start of a session.` : `Over ${lim} lines. Long instruction files get followed less reliably, so think about moving topics into .claude/rules/.`]);
  $("#msgs").innerHTML = msgs.map(([c, t]) => `<div class="msg ${c}">${md(t)}</div>`).join("");
}

async function save(force = false) {
  if (!cur || cur.readonly) return false;
  const ed = $("#ed");
  try {
    const r = await api("POST", "/api/save", { path: cur.item.path, content: ed.value, mtime: cur.mtime, force });
    cur.original = ed.value; cur.mtime = r.mtime; cur.dirty = false; updateBar();
    toast("Saved. Old version backed up in ~/.dotclaude/backups/", 3000);
    return true;
  } catch (e) {
    if (e.status === 409) {
      const a = await ask({ title: "File changed on disk", text: esc(e.message), buttons: [{ label: "Cancel", value: "c" }, { label: "Load disk version", value: "reload" }, { label: "Overwrite", value: "over", primary: true }] });
      if (a && a.value === "over") return save(true);
      if (a && a.value === "reload") { cur.dirty = false; openFile(cur.item, true); }
      return false;
    }
    $("#msgs").innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    return false;
  }
}

function fillTpl(t, g, name) { return (t || "").replaceAll("{name}", name || "my-name").replaceAll("{project}", g.scope === "project" ? g.title : "Project"); }

async function doCreate(groupId, kind, name) {
  try {
    const r = await api("POST", "/api/create", { group: groupId, kind, name });
    toast("Created " + tilde(r.path));
    await load();
    const it = findItem(r.path);
    if (it) { const g = groupOf(it); S.closed[g.id] = false; renderNav(); openFile(it, true); }
  } catch (e) { toast(e.message, 4000); }
}
function findItem(path) {
  for (const g of [...tree.groups, ...tree.projects]) for (const it of g.items) {
    if (it.type === "file" && it.path === path) return it;
    if (it.type === "folder") for (const c of it.children) if (c.path === path) return c;
  }
  return null;
}

async function newInFolder(it) {
  const k = tree.kinds[it.kind];
  const r = await ask({ title: (k.new_label || "New file"), text: `Name only: letters, numbers and dashes, for example <code>code-review</code>.`, input: { placeholder: "name" }, buttons: [{ label: "Cancel", value: null }, { label: "Create", value: "ok", primary: true }] });
  if (!r || r.value !== "ok" || !r.input) return;
  doCreate(it.group, it.kind, r.input.trim());
}

async function openFolder(it) {
  if (!(await guardDirty())) return;
  cur = null;
  markSel("f:" + it.group + ":" + it.kind);
  const k = tree.kinds[it.kind], g = groupOf(it);
  const c = $("#content");
  c.innerHTML = `<div class="crumbs">${esc(g.title)} › ${esc(it.label)}</div><h2>${esc(k.title)}</h2>${pathLine(it.path)}${factsHtml(k)}
    ${it.canCreate ? `<p><button class="btn primary" id="new">+ ${esc(k.new_label || "New")}</button></p>` : (k.create_hint ? `<div class="note">${md(k.create_hint)}</div>` : "")}
    <div class="list" id="flist">${it.children.length ? "" : `<div style="padding:14px;color:var(--faint)">Nothing here yet.</div>`}</div>`;
  wirePathButtons(c);
  const list = $("#flist", c);
  it.children.forEach(ch => { const b = document.createElement("button"); b.innerHTML = `<span class="dot"></span><span class="mono" style="font-size:13px">${esc(ch.label)}</span>`; b.onclick = () => openFile(ch); list.appendChild(b); });
  const nb = $("#new", c); if (nb) nb.onclick = () => newInFolder(it);
}

async function openGroup(g) {
  if (!(await guardDirty())) return;
  cur = null;
  markSel("g:" + g.id);
  const c = $("#content");
  const exist = [], missing = [];
  g.items.forEach(it => { if (it.type === "file") (it.exists ? exist : missing).push(it); else it.children.forEach(ch => exist.push(ch)); });
  const desc = {
    global: "Files in your ~/.claude folder apply to every project on this Mac. Project files override them where they conflict.",
    project: "Files Claude Code reads when you start a session in this folder: the project's own files, your private ones, auto memory, and CLAUDE.md files inherited from parent folders.",
    desktop: "The Claude desktop app's own configuration. This is separate from Claude Code.",
    managed: "Files an IT administrator installs. They take priority over everything else.",
  }[g.scope];
  c.innerHTML = `<div class="crumbs">${esc(g.scope === "project" ? "Project" : "Section")}</div><h2>${esc(g.title)}</h2>${g.path ? pathLine(g.path) : `<div class="pathline"><code>${esc(g.subtitle)}</code></div>`}
    <div class="note">${md(desc)}${g.note ? "<br><br>" + md(g.note) : ""}</div>
    ${g.source === "added" ? `<p><button class="btn" id="rm">Remove from list</button> <span style="color:var(--faint);font-size:12px">Only removes it from this app. No files are touched.</span></p>` : ""}
    ${g.source === "claude" || g.source === "both" ? `<p style="color:var(--faint);font-size:12px">Listed because you've opened Claude Code in this folder (from ~/.claude.json).</p>` : ""}
    ${exist.length ? `<p><button class="rv-btn" id="rv-group">✦ Review ${g.scope === "project" ? "this project" : "this section"} against best practice</button> <span style="color:var(--faint);font-size:12px">Fetches Anthropic's current guidance and checks all these files together.</span></p>` : ""}
    <h4 style="margin:18px 0 8px">Files that exist (${exist.length})</h4><div class="list" id="ex"></div>
    ${missing.length ? `<h4 style="margin:18px 0 8px">Files you could add (${missing.length})</h4><div class="list" id="mi"></div>` : ""}`;
  if (g.path) wirePathButtons(c);
  const fill = (id, arr) => { const l = $("#" + id, c); if (!l) return; if (!arr.length) l.innerHTML = `<div style="padding:14px;color:var(--faint)">None.</div>`;
    arr.forEach(it => { const k = tree.kinds[it.kind]; const b = document.createElement("button"); b.innerHTML = `<span class="dot${it.exists ? (it.readonly ? " ro" : "") : ""}" style="${it.exists ? "" : "background:transparent;border:1.5px solid var(--faint)"}"></span><span style="flex:1"><span class="mono" style="font-size:13px">${esc(it.label)}</span><br><span style="font-size:12px;color:var(--muted)">${esc(k.title)}</span></span><span class="tag" style="font-size:11px;color:var(--faint)">${esc(k.loads)}</span>`; b.onclick = () => openFile(it); l.appendChild(b); }); };
  fill("ex", exist); fill("mi", missing);
  const rvg = $("#rv-group", c); if (rvg) rvg.onclick = () => startReview({ group: g.id });
  const rm = $("#rm", c);
  if (rm) rm.onclick = async () => { await api("POST", "/api/folders/remove", { path: g.path }); toast("Removed from list"); await load(); welcome(); };
}

async function addFolder() {
  let path = null;
  try { const r = await api("POST", "/api/pick-folder"); path = r.path; if (!path) return; }
  catch (e) {
    const r = await ask({ title: "Add a project folder", text: "Type the full path to the folder:", input: { placeholder: "~/Code/my-project" }, buttons: [{ label: "Cancel", value: null }, { label: "Add", value: "ok", primary: true }] });
    if (!r || r.value !== "ok" || !r.input) return;
    path = r.input.trim();
  }
  try {
    const r = await api("POST", "/api/folders/add", { path });
    await load();
    S.closed["p:" + r.path] = false; persist(); renderNav();
    const g = tree.projects.find(p => p.path === r.path);
    if (g) openGroup(g);
    toast("Added " + tilde(r.path));
  } catch (e) { toast(e.message, 4000); }
}

function welcome() {
  if (cur && cur.dirty) return;
  cur = null; markSel(null);
  let files = 0;
  [...tree.groups, ...tree.projects].forEach(g => files += countFiles(g));
  $("#content").innerHTML = `<div class="welcome"><section>
    <h2>DotClaude</h2>
    <p style="color:var(--muted)">Every file Claude Code reads on this Mac, with an explanation of what each one does. Pick a file on the left to read or edit it. A backup of the old version is saved in <code>~/.dotclaude/backups/</code> every time you save.</p>
    <div class="stats"><div class="stat"><b>${files}</b>files found</div><div class="stat"><b>${tree.projects.length}</b>projects</div></div>
    <div class="legend"><span><span class="dot"></span> exists</span><span><span class="dot ro"></span> read-only</span><span><span class="dot" style="background:transparent;border:1.5px solid var(--faint)"></span> doesn't exist yet (you can add it)</span></div>
    <h3 style="margin-top:26px">What Claude Code loads when a session starts</h3>
    <ol>
      <li><b>Organisation files</b> (if an administrator installed any)</li>
      <li><b>Your global</b> <code>~/.claude/CLAUDE.md</code> and <code>~/.claude/rules/</code></li>
      <li><b>CLAUDE.md files in parent folders</b> above the project</li>
      <li><b>The project's</b> <code>CLAUDE.md</code> / <code>.claude/CLAUDE.md</code>, plus your private <code>CLAUDE.local.md</code></li>
      <li><b>Project rules</b> in <code>.claude/rules/</code> (rules with <code>paths:</code> wait until matching files come up)</li>
      <li><b>Auto memory</b>: the first 200 lines of the project's <code>MEMORY.md</code></li>
      <li><b>Skills and subagents</b>: only their names and descriptions, until one is used</li>
    </ol>
    <p>Later, when it's needed: <b>subfolder CLAUDE.md files</b>, auto-memory topic files, and the full text of skills.</p>
    <h3 style="margin-top:22px">Which settings win</h3>
    <p>Organisation → command-line flags → <code>.claude/settings.local.json</code> → <code>.claude/settings.json</code> → <code>~/.claude/settings.json</code>. Lists such as permission rules are combined across all of them.</p>
    <p style="color:var(--faint);font-size:12px;margin-top:26px">Based on the Claude Code docs: <a href="https://code.claude.com/docs/en/memory.md" target="_blank" rel="noopener">Memory</a> · <a href="https://code.claude.com/docs/en/settings.md" target="_blank" rel="noopener">Settings</a> · <a href="https://code.claude.com/docs/en/claude-directory.md" target="_blank" rel="noopener">The .claude directory</a></p>
  </section></div>`;
}

/* ---------------- Backups ---------------- */
const fmtTime = iso => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" }); };
const fmtSize = n => n < 1024 ? n + " bytes" : (n / 1024).toFixed(1) + " KB";

async function openHistory(it, selectId) {
  if (!(await guardDirty())) return;
  cur = null; markSel("p:" + it.path);
  const k = tree.kinds[it.kind], g = groupOf(it);
  const c = $("#content");
  c.innerHTML = `<div class="crumbs">${esc(g.title)} › ${esc(it.label)} › History</div><h2>History: ${esc(k.title)}</h2>${pathLine(it.path)}
    <p><button class="btn" id="back-file">← Back to file</button></p><div id="hist">Loading…</div>`;
  wirePathButtons(c);
  $("#back-file").onclick = () => openFile(it);
  let data;
  try { data = await api("GET", "/api/backups?path=" + encodeURIComponent(it.path)); }
  catch (e) { $("#hist").textContent = e.message; return; }
  if (!data.count) { $("#hist").innerHTML = `<div class="note">No backups yet. One is saved every time you save this file in DotClaude, apply a review suggestion, or restore a version.</div>`; return; }
  $("#hist").innerHTML = `<p class="rv-meta">${data.count} saved version${data.count === 1 ? "" : "s"}, newest first. Each is the file as it was just <b>before</b> a change.</p>
    <div style="display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:14px;align-items:start"><div class="list" id="hist-list"></div><div id="hist-view"></div></div>`;
  const list = $("#hist-list");
  data.backups.forEach((b, i) => {
    const el = document.createElement("button");
    el.dataset.id = b.id;
    el.innerHTML = `<span style="flex:1"><span style="font-size:13px">${esc(fmtTime(b.time))}</span><br><span class="rv-meta">${fmtSize(b.size)}${i === 0 ? " · most recent" : ""}</span></span>`;
    el.onclick = () => showBackup(b.id, it);
    list.appendChild(el);
  });
  showBackup(selectId || data.backups[0].id, it);
}

async function showBackup(id, it) {
  document.querySelectorAll("#hist-list button, #all-list button").forEach(b => b.style.background = b.dataset.id === id ? "var(--sel)" : "");
  const v = $("#hist-view");
  v.innerHTML = "Loading…";
  let b;
  try { b = await api("GET", "/api/backup?id=" + encodeURIComponent(id)); }
  catch (e) { v.innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`; return; }
  const managed = findItem(b.original);
  const canRestore = managed && !managed.readonly;
  v.innerHTML = `<div class="rv-card"><h4>${esc(fmtTime(b.time))}</h4>
    <p class="rv-meta">${esc(b.label)} · ${fmtSize(b.size)}</p>
    ${b.same_as_current ? `<p>This is identical to the current file.</p>` : b.current_exists ? `<details open><summary style="cursor:pointer;font-size:13px">Changes from the current file to this version</summary><pre class="diff">${diffHtml(b.diff)}</pre></details>` : `<p>The file doesn't exist any more. Restoring recreates it.</p>`}
    <details><summary style="cursor:pointer;font-size:13px">Full text of this version</summary><pre class="diff">${esc(b.content)}</pre></details>
    <p style="margin-top:12px">${b.same_as_current ? "" : canRestore ? `<button class="btn primary" id="restore">Restore this version</button> <span class="rv-meta">The current version is backed up first, so you can undo this.</span>` : `<span class="rv-meta">${managed ? "This file is read-only in DotClaude." : "This file is no longer one DotClaude manages, so it can't be restored here. You can copy the text above."}</span>`}</p></div>`;
  const r = $("#restore");
  if (r) r.onclick = async () => {
    const a = await ask({ title: "Restore this version?", text: `<b>${esc(b.label)}</b> goes back to how it was at ${esc(fmtTime(b.time))}. The current version is saved as a new backup first.`, buttons: [{ label: "Cancel", value: null }, { label: "Restore", value: "go", primary: true }] });
    if (!a || a.value !== "go") return;
    let mtime = null;
    try { if (managed.exists) mtime = (await api("GET", "/api/file?path=" + encodeURIComponent(b.original))).mtime; } catch (e) {}
    try {
      await api("POST", "/api/backups/restore", { id, mtime });
      toast("Restored. The version it replaced is now the newest backup.", 3500);
      await load();
      const fresh = findItem(b.original);
      if (fresh) openFile(fresh, true);
    } catch (e) { toast(e.message, 4500); }
  };
}

async function openAllBackups() {
  if (!(await guardDirty())) return;
  cur = null; markSel("backups");
  const c = $("#content");
  c.innerHTML = `<div class="crumbs">This Mac</div><h2>Backups</h2><div id="all">Loading…</div>`;
  let data;
  try { data = await api("GET", "/api/backups"); } catch (e) { $("#all").textContent = e.message; return; }
  if (!data.count) { $("#all").innerHTML = `<div class="note">No backups yet. DotClaude saves one every time you save a file, apply a review suggestion, or restore a version.</div>`; return; }
  const byFile = {};
  data.backups.forEach(b => (byFile[b.original] = byFile[b.original] || []).push(b));
  const files = Object.keys(byFile).sort((a, b) => byFile[b][0].id.localeCompare(byFile[a][0].id));
  $("#all").innerHTML = `<p class="rv-meta">${data.count} backups of ${files.length} file${files.length === 1 ? "" : "s"} · ${fmtSize(data.bytes)} in <code>${esc(data.dir)}</code>. Sorted by most recently changed.</p>
    <div style="display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:14px;align-items:start"><div class="list" id="all-list"></div><div id="hist-view"><div class="rv-meta" style="padding:14px">Pick a version on the left.</div></div></div>`;
  const list = $("#all-list");
  files.forEach(f => {
    const head = document.createElement("div");
    const it = findItem(f);
    head.style.cssText = "padding:9px 14px 4px;border-bottom:1px solid var(--line);font-size:12px;font-weight:600;word-break:break-all";
    head.innerHTML = esc(it ? groupOf(it).title + " › " + it.label : byFile[f][0].label) + (it ? "" : ` <span class="badge">not managed</span>`);
    list.appendChild(head);
    byFile[f].forEach(b => {
      const el = document.createElement("button");
      el.dataset.id = b.id;
      el.innerHTML = `<span style="font-size:13px;flex:1">${esc(fmtTime(b.time))}</span><span class="rv-meta">${fmtSize(b.size)}</span>`;
      el.onclick = () => showBackup(b.id, it);
      list.appendChild(el);
    });
  });
}

/* ---------------- Best-practice review ---------------- */
let RV = null;          // current/last job
let rvTimer = null;
const RV_STATE = {};    // per-rewrite UI state: applied

async function startReview(target) {
  if (RV && RV.status === "running") { const a = await ask({ title: "A review is already running", text: "Wait for it to finish, or cancel it first.", buttons: [{ label: "Show it", value: "show", primary: true }] }); if (a && a.value === "show") showReview(); return; }
  const scope = target.path ? "this file" : "these files";
  const a = await ask({ title: "Review against best practice", text: `This runs Claude Code on your Mac. It reads Anthropic's current guidance from the docs, then reviews ${scope} and suggests changes. It uses your Claude plan's usage and usually takes 1–4 minutes.<br><br>Your file contents go to Claude. Values in JSON files that look like API keys or tokens (and anything under <code>env</code>) are replaced with &lt;redacted&gt; first.<br><br>Nothing changes until you choose to apply a suggestion, and applying one backs up the current file first.`, buttons: [{ label: "Cancel", value: null }, { label: "Start review", value: "go", primary: true }] });
  if (!a || a.value !== "go") return;
  if (!(await guardDirty())) return;
  try { RV = await api("POST", "/api/review/start", target); }
  catch (e) { await ask({ title: "Couldn't start the review", text: esc(e.message), buttons: [{ label: "OK", value: 1, primary: true }] }); return; }
  updateBanner(); showReview(); pollReview();
}
function pollReview() {
  clearTimeout(rvTimer);
  rvTimer = setTimeout(async () => {
    try { RV = await api("GET", "/api/review/status?id=" + RV.id); } catch (e) {}
    updateBanner();
    if (sel === "review") renderReview();
    if (RV.status === "running") pollReview();
    else if (RV.status === "done" && sel !== "review") toast("Review finished: click the banner on the left to see it", 5000);
  }, 2000);
}
function updateBanner() {
  const b = $("#rv-banner");
  if (!RV) { b.classList.remove("show"); return; }
  b.classList.add("show");
  b.innerHTML = RV.status === "running" ? `<span class="spinner"></span>Reviewing ${esc(RV.label)}… ${fmtSecs(RV.elapsed)}` :
    RV.status === "done" ? `✦ Review ready: ${esc(RV.label)}` : RV.status === "error" ? `⚠ Review failed: ${esc(RV.label)}` : `Review cancelled`;
  b.onclick = showReview;
}
const fmtSecs = s => s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
async function showReview() { if (!RV) return; if (!(await guardDirty())) return; cur = null; markSel("review"); renderReview(); }
function diffHtml(d) { return d.split("\n").map(l => `<span class="${l.startsWith("+++") || l.startsWith("---") || l.startsWith("@@") ? "h" : l.startsWith("+") ? "a" : l.startsWith("-") ? "d" : ""}">${esc(l)}</span>`).join("\n"); }
function fileLabel(p) { const it = findItem(p); return it ? (groupOf(it).title + " › " + it.label) : tilde(p); }

function renderReview() {
  const c = $("#content");
  const head = `<div class="crumbs">Best-practice review</div><h2>${esc(RV.label)}</h2>`;
  if (RV.status === "running") {
    c.innerHTML = head + `<div class="rv-card"><p><span class="spinner"></span><b>Reviewing ${RV.files.length} file${RV.files.length === 1 ? "" : "s"}…</b> ${fmtSecs(RV.elapsed)}</p><p class="rv-meta">Claude Code is reading Anthropic's current guidance, then checking your files. You can keep browsing; the banner on the left shows when it's done.</p><p><button class="btn" id="rv-cancel">Cancel review</button></p></div>`;
    $("#rv-cancel").onclick = async () => { RV = await api("POST", "/api/review/cancel", { id: RV.id }); updateBanner(); renderReview(); };
    return;
  }
  if (RV.status === "cancelled") { c.innerHTML = head + `<div class="note">Review cancelled.</div>`; return; }
  if (RV.status === "error") { c.innerHTML = head + `<div class="rv-card"><p style="color:var(--err)"><b>The review didn't finish.</b></p><p class="mono" style="font-size:12px;white-space:pre-wrap">${esc(RV.error)}</p><p class="rv-meta">If the message mentions logging in, run <code>claude</code> once in Terminal and sign in, then try again.</p></div>`; return; }
  const r = RV.result;
  const counts = { high: 0, medium: 0, low: 0 }; r.findings.forEach(f => counts[f.severity] = (counts[f.severity] || 0) + 1);
  let h = head + `<p class="rv-meta">${RV.files.length} file(s) reviewed in ${fmtSecs(RV.elapsed)}${r.cost_usd != null ? ` · about $${Number(r.cost_usd).toFixed(2)} of API-equivalent usage` : ""}${RV.truncated ? " · only the first 60 files were included" : ""}. Saved in <code>~/.dotclaude/reviews/</code>.</p>
    <div class="rv-card"><h4>Summary</h4><p>${md(r.summary)}</p>
    <p class="rv-meta" style="margin-top:8px">${counts.high} high · ${counts.medium} medium · ${counts.low} low · ${r.rewrites.length} suggested rewrite(s)</p></div>`;
  if (r.guidance_failed && r.guidance_failed.length) h += `<div class="note" style="border-left-color:var(--warn)"><b>Some official pages couldn't be loaded</b> (${r.guidance_failed.length} of ${r.sources.length}), so findings that rely on them were left out. Run the review again later for a full check.</div>`;
  if (r.redacted && r.redacted.length) h += `<div class="note">Secret-looking values were hidden from Claude in: ${r.redacted.map(p => `<code>${esc(tilde(p))}</code>`).join(", ")}. Those files get findings but no rewrites.</div>`;
  if (r.rewrites.length) {
    h += `<h3 style="margin:22px 0 8px">Suggested rewrites</h3>`;
    r.rewrites.forEach((w, i) => {
      const st = RV_STATE[RV.id + ":" + i];
      h += `<div class="rv-card"><h4><span class="rv-file" data-open="${esc(w.file)}">${esc(fileLabel(w.file))}</span></h4><p>${md(w.explanation)}</p>
        <details><summary style="cursor:pointer;font-size:13px">Show changes</summary><pre class="diff">${diffHtml(w.diff)}</pre></details>
        <p style="margin-top:10px">${st === "applied" ? `<span style="color:var(--ok);font-weight:600">✓ Applied. The old version is in ~/.dotclaude/backups/</span>` :
          `<button class="btn primary" data-apply="${i}">Apply (backs up current file)</button> <button class="btn" data-tweak="${i}">Open in editor to adjust first</button>`}</p></div>`;
    });
  }
  if (r.dropped_rewrites && r.dropped_rewrites.length) h += `<p class="rv-meta">${r.dropped_rewrites.length} suggested rewrite(s) were left out because they were empty, for a read-only file or one with hidden secrets, or invalid JSON.</p>`;
  h += `<h3 style="margin:22px 0 8px">Findings (${r.findings.length})</h3>`;
  if (!r.findings.length) h += `<div class="rv-card">No issues found.</div>`;
  r.findings.forEach(f => {
    h += `<div class="rv-card"><p><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span><span class="rv-file" data-open="${esc(f.file)}">${esc(fileLabel(f.file))}</span></p>
      <p><b>${md(f.issue)}</b></p><p>${md(f.recommendation)}</p>
      <p class="rv-meta">Source: <a href="${esc(f.source_url)}" target="_blank" rel="noopener">${esc(f.source_url)}</a></p></div>`;
  });
  if (r.sources && r.sources.length) h += `<h3 style="margin:22px 0 8px">Guidance checked</h3><div class="rv-card">${r.sources.map(s => `<p>${s.fetched === false ? `<span style="color:var(--warn)">✗ couldn't load</span>` : `<span style="color:var(--ok)">✓</span>`} <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a></p>`).join("")}</div>`;
  c.innerHTML = h;
  c.querySelectorAll("[data-open]").forEach(el => el.onclick = () => { const it = findItem(el.dataset.open); if (it) openFile(it); });
  c.querySelectorAll("[data-apply]").forEach(el => el.onclick = () => applyRewrite(+el.dataset.apply));
  c.querySelectorAll("[data-tweak]").forEach(el => el.onclick = () => tweakRewrite(+el.dataset.tweak));
}

async function applyRewrite(i) {
  const w = RV.result.rewrites[i];
  try {
    await api("POST", "/api/review/apply", { path: w.file, content: w.content, base_mtime: w.base_mtime });
    RV_STATE[RV.id + ":" + i] = "applied"; toast("Applied. Old version backed up in ~/.dotclaude/backups/", 3500); renderReview();
  } catch (e) {
    await ask({ title: "Not applied", text: esc(e.status === 409 ? "This file changed after the review ran, so applying could overwrite newer edits. Use 'Open in editor to adjust first' to merge by hand, or run the review again." : e.message), buttons: [{ label: "OK", value: 1, primary: true }] });
  }
}
async function tweakRewrite(i) {
  const w = RV.result.rewrites[i];
  const it = findItem(w.file); if (!it) return;
  await openFile(it);
  if (!cur || cur.item.path !== w.file) return;
  $("#ed").value = w.content; cur.dirty = w.content !== cur.original; updateBar();
  $("#msgs").insertAdjacentHTML("afterbegin", `<div class="msg warn">The suggested rewrite is loaded but not saved yet. Edit it if you want, then Save (the current version gets backed up first), or Revert to throw it away.</div>`);
}

async function load() { tree = await api("GET", "/api/tree"); renderNav(); }

$("#show-missing").checked = S.showMissing;
$("#show-missing").onchange = e => { S.showMissing = e.target.checked; persist(); renderNav(); };
$("#search").oninput = renderNav;
$("#refresh").onclick = async () => { await load(); toast("Rescanned"); };
$("#stop").onclick = async () => {
  if (RV && RV.status === "running") { toast("A review is running. Cancel it or wait before stopping."); return; }
  if (!(await guardDirty())) return;
  const a = await ask({ title: "Stop DotClaude?", text: "This closes the app. Open it again from the Dock or from AIHQ.", buttons: [{ label: "Cancel", value: null }, { label: "Stop", value: "stop", primary: true }] });
  if (!a || a.value !== "stop") return;
  try { await api("POST", "/api/shutdown"); } catch (e) {}
  if (cur) cur.dirty = false;
  document.body.innerHTML = `<div style="margin:auto;text-align:center;color:var(--muted)"><h2 style="color:var(--ink)">DotClaude has stopped</h2><p>You can close this tab.</p></div>`;
};
$("#home-link").onclick = async () => { if (await guardDirty()) { if (cur) cur.dirty = false; welcome(); } };
document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
  if ((e.metaKey || e.ctrlKey) && e.key === "k") { e.preventDefault(); $("#search").focus(); $("#search").select(); }
});
window.addEventListener("beforeunload", e => { if (cur && cur.dirty) { e.preventDefault(); e.returnValue = ""; } });
load().then(welcome).catch(e => { $("#content").innerHTML = `<p style="color:var(--err)">Couldn't load: ${esc(e.message)}</p>`; });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
