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
APP_VERSION = "1.4.0"
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
    "user_settings_local": dict(
        label="settings.local.json", title="Home-folder settings (just you)",
        role="Claude Code treats your home folder like any other project, so this is the local settings file for "
             "sessions you start in ~ itself. It is not a second copy of your personal settings: it does nothing "
             "in your other projects.",
        loads="Only for sessions started in your home folder (~)", share="Personal", docs="settings.md"),
    "user_plugins": dict(
        label="plugins/installed_plugins.json", title="Installed plugins", readonly=True,
        readonly_reason="Claude Code manages this list. Install, update or remove plugins with /plugin. Turning a "
                        "plugin on or off is stored as `enabledPlugins` in settings.json.",
        role="The plugins installed on this Mac and where each came from. An enabled plugin can add skills, "
             "subagents, hooks and MCP servers to every session, so it is worth knowing what is here.",
        loads="Enabled plugins load at the start of every session", share="Personal", docs="plugins.md"),
    "user_scripts": dict(
        label="Hook & status line scripts", title="Scripts your personal settings run",
        role="Scripts in ~/.claude/hooks/ and any script your settings.json runs as a hook or status line. "
             "Hooks run automatically on events such as \"before a tool runs\", so they can block or change "
             "what Claude does. Editing one changes that behaviour in every project.",
        loads="When the hook's event fires, or whenever the status line redraws", share="Personal",
        docs="hooks-guide.md"),
    "proj_scripts": dict(
        label=".claude hook scripts", title="Scripts this project's settings run",
        role="Scripts in .claude/hooks/ and any script this project's settings files run as a hook or status "
             "line. They only run in this project.",
        loads="When the hook's event fires", share="Shared if committed", docs="hooks-guide.md"),
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

# Why you'd want each file ("why", shown on every page) and what you lose without it ("missing",
# shown when the file doesn't exist or the folder is empty). Public UI copy: no em-dashes.
KIND_WHY: Dict[str, tuple] = {
    "user_claude_md": (
        "It saves you repeating yourself. Anything you'd otherwise tell Claude at the start of every session "
        "(how you like answers, tools you use, things to never do) goes here once.",
        "Every session starts knowing nothing about you, so you correct the same habits again and again. "
        "Add a short list of the preferences you find yourself repeating."),
    "user_settings": (
        "Fewer permission prompts and consistent behaviour everywhere. Allow the safe commands you always "
        "approve, block the risky ones, and set hooks or a default model once for every project.",
        "Claude asks permission for the same safe commands in every project, and there's nowhere to set "
        "hooks or defaults that apply everywhere. Add one when the prompts start to feel repetitive."),
    "keybindings": (
        "Only useful if Claude Code's default shortcuts clash with your terminal or your muscle memory.",
        "Nothing is lost: the default shortcuts apply. Create it only if a shortcut gets in your way."),
    "user_rules": (
        "Keeps your personal CLAUDE.md short. Topic files are easier to maintain, and a rule with `paths:` "
        "only loads for matching files, so it costs nothing in sessions that don't need it.",
        "Everything personal has to live in one CLAUDE.md, which gets long and is followed less reliably. "
        "Move a topic here when CLAUDE.md grows past a screen or two, or when advice only applies to one "
        "kind of file."),
    "user_skills": (
        "Turns a procedure you repeat into something Claude does the same way every time, in any project. "
        "Only the short description loads until it's needed, so a skill costs almost no context.",
        "Repeated procedures (a review checklist, a release routine) have to be explained from scratch each "
        "time. Write a skill the second time you explain the same steps."),
    "user_agents": (
        "Hands focused jobs (code review, research, testing) to a helper with its own instructions and a "
        "clean context, so your main conversation stays short and on topic.",
        "Every task runs in your main conversation and fills it up. Add a subagent for a job you hand off "
        "often and want done the same way each time."),
    "user_commands": (
        "Only worth keeping if you already have commands that work. Skills do the same job and more.",
        "Nothing is lost. Write a skill instead: it does everything a command does and can also be used "
        "automatically."),
    "user_output_styles": (
        "Changes Claude's whole manner for a kind of work, such as a teaching style or a terse reviewer, "
        "without rewriting your instructions.",
        "You get Claude Code's default style, which suits most coding. Add one only if you want a "
        "different voice or role across sessions."),
    "user_workflows": (
        "Saves a multi-agent job you run often so you can start it by name instead of describing it again.",
        "Nothing is lost until you run the same multi-agent job twice. Save it here when you do."),
    "user_themes": (
        "Purely visual: makes Claude Code's terminal match your colour scheme.",
        "Nothing is lost: the built-in themes apply."),
    "user_agent_memory": (
        "Lets a subagent remember what it learned across projects, so it improves over time instead of "
        "starting fresh.",
        "Your subagents forget everything between sessions. This fills in automatically once a subagent "
        "has `memory: user` set."),
    "user_settings_local": (
        "Only matters if you start Claude Code in your home folder itself, rather than in a project.",
        "Nothing is lost for normal project work. Claude Code creates it if you approve something "
        "permanently while working in your home folder."),
    "user_plugins": (
        "Shows what plugins are adding to every session, which matters because each one can add skills, "
        "hooks and tools you didn't write.",
        "No plugins are installed, so nothing extra is loading into your sessions."),
    "user_scripts": (
        "Hooks are the only way to make something happen every time, guaranteed: block dangerous commands, "
        "run checks after edits, log activity. Instructions in CLAUDE.md are advice; hooks are enforced.",
        "Everything relies on Claude following instructions. Add a hook for any rule that must never be "
        "broken, such as blocking deletes outside the project."),
    "proj_scripts": (
        "Enforces this project's rules automatically, such as running the formatter after every edit or "
        "blocking writes to production config.",
        "This project's rules are advice only. Add a hook script when one of them must always happen, "
        "such as formatting or tests after edits."),
    "claude_json": (
        "Claude Code needs it to run. It's useful here to see which projects and MCP servers it knows about.",
        "Claude Code creates this itself the first time it runs."),
    "automem_orphan": (
        "Usually leftovers. Worth a look: notes from a moved project may still be useful, and notes from a "
        "deleted one can go.",
        "No leftover memory folders. Nothing to tidy."),
    "managed_claude_md": (
        "Lets an organisation give every user the same baseline instructions that can't be switched off.",
        "Normal on a personal Mac. Only an IT administrator would install this."),
    "managed_settings": (
        "Lets an organisation enforce security settings that no user or project can loosen.",
        "Normal on a personal Mac. Only an IT administrator would install this."),
    "managed_mcp": (
        "Lets an organisation control which external tools everyone can connect.",
        "Normal on a personal Mac. Only an IT administrator would install this."),
    "desktop_config": (
        "Gives the Claude desktop app's chat access to local tools, such as your files or a database, "
        "through MCP servers.",
        "The desktop chat can only use built-in features and connectors from your claude.ai account. Add "
        "this when you want it to reach something on your Mac."),
    "desktop_app_config": (
        "The desktop app manages this itself. It's shown so you know it exists.",
        "The desktop app creates this itself."),
    "proj_claude_md": (
        "The single most useful file for a project. It tells every session how to build, test and work in "
        "this codebase, so Claude stops guessing commands and breaking conventions.",
        "Every session in this project starts blind: Claude has to rediscover how to build and test, and "
        "guesses at your conventions. Add one with the build and test commands and the few rules that "
        "matter most. Running /init in Claude Code drafts one from the code."),
    "proj_dot_claude_md": (
        "Same benefit as a root CLAUDE.md, with a tidier project root.",
        "Nothing is lost if the project has a CLAUDE.md in its root. Use one location or the other, not "
        "both."),
    "proj_local_md": (
        "Keeps your personal details for this project (local URLs, test logins, how you like to work on it) "
        "out of the shared CLAUDE.md and out of git.",
        "Personal notes either go in the shared CLAUDE.md, where teammates see them, or nowhere. Add one if "
        "you have project details that are only true on your machine."),
    "proj_agents_md": (
        "Useful when other AI tools (Codex, Cursor and others) work on this project too: one instructions "
        "file they all read.",
        "Nothing is lost if only Claude Code works on this project. If other tools do, keep instructions in "
        "AGENTS.md and make CLAUDE.md a single line, `@AGENTS.md`, so both stay in step."),
    "proj_settings": (
        "Gives everyone on the project the same permissions and hooks, so safe project commands (build, "
        "test, lint) run without prompts and risky ones stay blocked.",
        "Claude asks permission for this project's routine commands every time, and nothing project-wide is "
        "enforced. Add one to allow the build and test commands and to hold any project hooks."),
    "proj_settings_local": (
        "Holds your own permission approvals for this project without affecting anyone else.",
        "Nothing is lost. Claude Code creates it the first time you answer \"Yes, and don't ask again\"."),
    "proj_mcp": (
        "Connects this project to the external tools it needs (a database, an issue tracker, a browser), "
        "and shares that setup with everyone who works on it.",
        "Claude can only use tools you've connected personally. Add one when this project depends on an "
        "external service Claude should be able to query."),
    "proj_worktreeinclude": (
        "Makes parallel worktree sessions work straight away, by copying the git-ignored files (like .env) "
        "they need to run.",
        "New worktrees start without your .env and other ignored files, so builds or tests may fail there. "
        "Add one if you use worktrees and the project needs such files."),
    "proj_rules": (
        "Keeps the project CLAUDE.md short and lets advice for one area (tests, the API, the frontend) load "
        "only when Claude works on those files.",
        "All project guidance sits in CLAUDE.md and loads every session, relevant or not. Add rules when "
        "CLAUDE.md grows long or has sections that only apply to certain files."),
    "proj_skills": (
        "Packages this project's repeat procedures (deploy, release, data refresh) so Claude follows them "
        "exactly, and anyone with the project gets them too.",
        "Project procedures have to be explained each time or crammed into CLAUDE.md. Add a skill for any "
        "multi-step routine you run more than once here."),
    "proj_agents": (
        "Gives this project specialist helpers that know its codebase, such as a reviewer that checks "
        "your conventions, each with its own clean context.",
        "Specialist jobs run in your main conversation. Add a subagent for a review or check you want done "
        "the same way every time in this project."),
    "proj_commands": (
        "Only worth keeping if this project already has commands that work. Skills do the same job and more.",
        "Nothing is lost. Write a project skill instead."),
    "proj_output_styles": (
        "Sets a manner that suits this project, such as explanatory for a teaching repo.",
        "You get the default style, which suits most projects. Add one only if this project needs a "
        "different voice."),
    "proj_workflows": (
        "Saves a multi-agent job this project runs often so anyone can start it by name.",
        "Nothing is lost until you run the same multi-agent job here twice."),
    "proj_agent_memory": (
        "Lets a subagent build up knowledge of this project and share it with everyone who has the project.",
        "Subagents forget this project between sessions. This fills in automatically once a subagent has "
        "`memory: project` set."),
    "proj_agent_memory_local": (
        "Lets a subagent remember this project privately, for you only.",
        "Subagents forget this project between sessions. This fills in automatically once a subagent has "
        "`memory: local` set."),
    "automem_index": (
        "Claude keeps what it learns about this project (commands that work, mistakes to avoid, your "
        "corrections) and reads it back next session, so it doesn't repeat mistakes.",
        "Claude hasn't saved anything about this project yet, so lessons from past sessions are lost. It "
        "fills in by itself as you work; you can also ask Claude to \"remember\" something."),
    "automem_topics": (
        "Holds the detail behind MEMORY.md, loaded only when relevant, so memory can grow without filling "
        "the context at startup.",
        "Nothing is lost while MEMORY.md is short. Claude creates topic files as its notes grow."),
    "parent_md": (
        "Shares instructions across every project under one folder, such as all your work repos, without "
        "copying them into each one.",
        "Nothing is inherited, so shared conventions have to be repeated in each project. Add a CLAUDE.md "
        "to a parent folder if several projects there follow the same rules."),
    "sub_md": (
        "Gives instructions for one part of the codebase only when Claude works there, keeping startup "
        "context small.",
        "All guidance loads at startup. Add one in a subfolder whose rules differ from the rest of the "
        "project, such as a frontend or a legacy module."),
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

for _w, (_why, _missing) in KIND_WHY.items():
    KINDS[_w]["why"], KINDS[_w]["missing"] = _why, _missing

# Plain-English names for the sidebar; the file name is shown beside them in small type.
# "section" groups files on overview pages.
KIND_SHORT: Dict[str, tuple] = {
    "user_claude_md": ("Your instructions", "Instructions"),
    "user_settings": ("Settings", "Settings and tools"),
    "keybindings": ("Keyboard shortcuts", "Settings and tools"),
    "user_rules": ("Rules", "Instructions"),
    "user_skills": ("Skills", "Skills and helpers"),
    "user_agents": ("Subagents", "Skills and helpers"),
    "user_commands": ("Old-style slash commands", "Skills and helpers"),
    "user_output_styles": ("Output styles", "Skills and helpers"),
    "user_workflows": ("Workflows", "Skills and helpers"),
    "user_themes": ("Colour themes", "Settings and tools"),
    "user_agent_memory": ("Subagent memory", "Memory"),
    "user_settings_local": ("Home-folder settings", "Settings and tools"),
    "user_plugins": ("Installed plugins", "Settings and tools"),
    "user_scripts": ("Hook scripts", "Settings and tools"),
    "proj_scripts": ("Hook scripts", "Settings and tools"),
    "claude_json": ("Claude Code app state", "Settings and tools"),
    "automem_orphan": ("Leftover memory folders", "Memory"),
    "managed_claude_md": ("Organisation instructions", "Instructions"),
    "managed_settings": ("Organisation settings", "Settings and tools"),
    "managed_mcp": ("Organisation MCP servers", "Settings and tools"),
    "desktop_config": ("MCP servers for the chat", "Settings and tools"),
    "desktop_app_config": ("App preferences", "Settings and tools"),
    "proj_claude_md": ("Instructions", "Instructions"),
    "proj_dot_claude_md": ("Instructions (in .claude)", "Instructions"),
    "proj_local_md": ("Your private notes", "Instructions"),
    "proj_agents_md": ("Instructions for other AI tools", "Instructions"),
    "proj_settings": ("Settings (shared)", "Settings and tools"),
    "proj_settings_local": ("Settings (just you)", "Settings and tools"),
    "proj_mcp": ("MCP servers", "Settings and tools"),
    "proj_worktreeinclude": ("Files copied to worktrees", "Settings and tools"),
    "proj_rules": ("Rules", "Instructions"),
    "proj_skills": ("Skills", "Skills and helpers"),
    "proj_agents": ("Subagents", "Skills and helpers"),
    "proj_commands": ("Old-style slash commands", "Skills and helpers"),
    "proj_output_styles": ("Output styles", "Skills and helpers"),
    "proj_workflows": ("Workflows", "Skills and helpers"),
    "proj_agent_memory": ("Subagent memory (shared)", "Memory"),
    "proj_agent_memory_local": ("Subagent memory (just you)", "Memory"),
    "automem_index": ("Claude's memory", "Memory"),
    "automem_topics": ("Memory notes", "Memory"),
    "parent_md": ("From parent folders", "Instructions"),
    "sub_md": ("Subfolder instructions", "Instructions"),
}
for _w, (_short, _section) in KIND_SHORT.items():
    KINDS[_w]["short"], KINDS[_w]["section"] = _short, _section

# File specs: (kind, path relative to scope root, show when missing)
GLOBAL_FILES = [("user_claude_md", "CLAUDE.md", True), ("user_settings", "settings.json", True),
                ("user_settings_local", "settings.local.json", False),
                ("keybindings", "keybindings.json", True),
                ("user_plugins", "plugins/installed_plugins.json", False)]
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
    it = scripts_item("user_scripts", env.claude_dir, env.home, None, "global")
    if it:
        g["items"].append(it)
    it = file_item("claude_json", env.claude_json, True, "global", label="~/.claude.json")
    g["items"].append(it)
    groups.append(g)

    # Projects (computed before orphan auto-memory so we know which memory dirs are matched)
    projs, hidden_wt = project_paths(env)
    matched_mem = set()
    project_groups = []
    hidden_set = set(env.load_config().get("hidden_projects", []))
    hidden_projects = []
    for pr in projs:
        p = pr["path"]
        if str(p) in hidden_set:
            # Hidden: not listed, but its memory folders still count as matched (not "unmatched")
            hidden_projects.append({"path": str(p), "title": p.name or str(p), "subtitle": tilde(env, p)})
            for cand in [p] + [Path(a) for a in pr.get("aliases", [])]:
                matched_mem.add(str(env.claude_dir / "projects" / encode_project_key(cand) / "memory"))
            matched_mem.add(str(automem_dir(env, p, pr.get("aliases", []))))
            continue
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
            it = scripts_item("proj_scripts", p / ".claude", env.home, p, gid)
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
            "hiddenProjects": hidden_projects,
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
# Hook and status line scripts: files in .claude/hooks/ plus scripts a settings file runs
# --------------------------------------------------------------------------------------

SCRIPT_EXTS = {".sh", ".bash", ".zsh", ".fish", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl"}


def settings_commands(path: Path) -> List[str]:
    """Every `command` string under `hooks` and `statusLine` in a settings file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "command" and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    if isinstance(data, dict):
        walk(data.get("hooks"))
        walk(data.get("statusLine"))
    return out


def command_scripts(commands: List[str], home: Path, project: Optional[Path]) -> List[Path]:
    """Script files named in hook/status line commands. Only text scripts inside the home folder count,
    so a command like `/usr/bin/env` or a script elsewhere on the disk never becomes editable here."""
    import shlex
    found: List[Path] = []
    for cmd in commands:
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        for t in toks:
            t = t.replace("${HOME}", str(home)).replace("$HOME", str(home))
            if project is not None:
                t = t.replace("${CLAUDE_PROJECT_DIR}", str(project)).replace("$CLAUDE_PROJECT_DIR", str(project))
            if t.startswith("~/"):
                t = str(home) + t[1:]
            if "/" not in t:
                continue
            p = Path(t)
            if not p.is_absolute():
                if project is None:
                    continue
                p = project / p
            p = Path(os.path.normpath(str(p)))
            try:
                real = p.resolve()
            except OSError:
                continue
            if (p.is_file() and p.suffix in SCRIPT_EXTS and home.resolve() in real.parents
                    and p not in found):
                found.append(p)
    return found


def scripts_item(kind: str, claude_dir: Path, home: Path, project: Optional[Path], gid: str) -> Optional[dict]:
    hooks_dir = claude_dir / "hooks"
    files = [f for f in list_folder(hooks_dir, "deep", "") if f.suffix in SCRIPT_EXTS]
    cmds: List[str] = []
    for name in ("settings.json", "settings.local.json"):
        cmds += settings_commands(claude_dir / name)
    for f in command_scripts(cmds, home, project):
        if f not in files:
            files.append(f)
    if not files:
        return None
    k = KINDS[kind]
    children = []
    for f in files:
        try:
            label = str(f.relative_to(claude_dir))
        except ValueError:
            label = "~" + str(f)[len(str(home)):] if str(f).startswith(str(home) + os.sep) else str(f)
        children.append(file_item(kind, f, False, gid, label=label))
    return {"type": "folder", "kind": kind, "group": gid, "label": k["label"], "path": str(hooks_dir),
            "children": children, "canCreate": False}


# --------------------------------------------------------------------------------------
# What loads at the start of a session (rules from code.claude.com/docs/en/memory.md,
# checked 2026-11-01): CLAUDE.md files in the folder and every folder above it, @imports up
# to 4 hops (not inside code spans or fenced blocks), AGENTS.md only when none of CLAUDE.md,
# .claude/CLAUDE.md or CLAUDE.local.md exists in the folder or above it (~/.claude/CLAUDE.md
# doesn't count for that check), rules without `paths:`, the first 200 lines / 25 KB of
# MEMORY.md, and only the frontmatter of skills and subagents. HTML block comments are
# stripped before loading. Token counts are an estimate: characters / 4.
# --------------------------------------------------------------------------------------

IMPORT_MAX_HOPS = 4
MEMORY_MAX_LINES = 200
MEMORY_MAX_BYTES = 25 * 1024
CHARS_PER_TOKEN = 4
IMPORT_RE = re.compile(r"(?:(?<=\s)|^)@((?:\\ |[^\s\"'`])+)")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
INSTRUCTION_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")


def _lines_outside_code(text: str) -> List[str]:
    out, fence = [], None
    for line in text.splitlines():
        s = line.lstrip()
        if fence:
            if s.startswith(fence):
                fence = None
            continue
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            continue
        out.append(re.sub(r"`[^`]*`", "", line))
    return out


def find_imports(text: str, base: Path, home: Path) -> List[str]:
    """Raw import targets (as written) in a memory file."""
    found = []
    for line in _lines_outside_code(text):
        for m in IMPORT_RE.finditer(line):
            raw = m.group(1).replace("\\ ", " ").rstrip(".,;:)]}")
            if raw:
                found.append(raw)
    return found


def resolve_import(raw: str, base: Path, home: Path) -> Path:
    if raw.startswith("~/"):
        return home / raw[2:]
    p = Path(raw)
    return p if p.is_absolute() else base / p


def frontmatter(text: str) -> Optional[str]:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else None


def _read_text(p: Path) -> Optional[str]:
    try:
        if p.stat().st_size > MAX_READ_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def startup_load(env: Env, group_id: str) -> dict:
    tree = discover(env)
    g = next((x for x in tree["groups"] + tree["projects"] if x["id"] == group_id), None)
    if not g or g.get("missing") or g["scope"] not in ("global", "project"):
        raise ApiError(404, "Unknown section.")
    entries: List[dict] = []
    warnings: List[str] = []
    seen = set()
    user_md = env.claude_dir / "CLAUDE.md"

    def add(p: Path, how: str, via: Optional[Path] = None, hops: int = 0):
        try:
            key = p.resolve()
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        text = _read_text(p)
        if text is None:
            return
        loaded = HTML_COMMENT_RE.sub("", text)
        entries.append({"path": str(p), "label": tilde(env, p), "how": how, "chars": len(loaded),
                        "via": tilde(env, via) if via else None})
        if hops >= IMPORT_MAX_HOPS:
            return
        for raw in find_imports(loaded, p.parent, env.home):
            target = resolve_import(raw, p.parent, env.home)
            if target.is_file():
                add(target, "Imported with @", via=p, hops=hops + 1)
            elif "/" in raw or raw.endswith(".md"):
                warnings.append(f"{tilde(env, p)} imports @{raw}, but that file doesn't exist.")

    def add_rules(rules_dir: Path, how: str):
        for f in list_folder(rules_dir, "deep", ".md"):
            fm = frontmatter(_read_text(f) or "")
            if fm is None or not re.search(r"^paths\s*:", fm, re.M):
                add(f, how)

    def add_descriptions(claude_dir: Path, who: str):
        total, n = 0, 0
        for f in list_folder(claude_dir / "skills", "nested", "SKILL.md") + list_folder(claude_dir / "agents", "flat", ".md"):
            fm = frontmatter(_read_text(f) or "")
            if fm is None or re.search(r"^disable-model-invocation\s*:\s*true", fm, re.M):
                continue
            total += len(fm)
            n += 1
        if n:
            entries.append({"path": str(claude_dir), "label": f"{who} skills and subagents ({n})",
                            "how": "Names and descriptions only, until one is used", "chars": total, "via": None})

    # Loaded in every project
    if user_md.is_file():
        add(user_md, "Your personal CLAUDE.md, in every project")
    add_rules(env.claude_dir / "rules", "Personal rule without paths:, in every project")
    add_descriptions(env.claude_dir, "Personal")

    if g["scope"] == "project":
        p = Path(g["path"])
        chain = [p] + list(p.parents)
        has_md = False
        for d in chain:
            for name in INSTRUCTION_NAMES:
                f = d / name
                if f.is_file() and not (user_md.is_file() and f.resolve() == user_md.resolve()):
                    has_md = True
                    add(f, "This project's instructions" if d == p else "From a parent folder")
        if not has_md:
            for d in chain:
                for name in ("AGENTS.md", ".claude/AGENTS.md"):
                    f = d / name
                    if f.is_file():
                        add(f, "AGENTS.md: loads because there's no CLAUDE.md here or in any folder above")
                        warnings.append(f"No CLAUDE.md in this folder or above, so Claude Code loads "
                                        f"{tilde(env, f)} instead.")
        if p != env.home:
            add_rules(p / ".claude/rules", "Project rule without paths:")
            add_descriptions(p / ".claude", "Project")
        mem = Path(g["memDir"]) / "MEMORY.md"
        if mem.is_file():
            text = _read_text(mem) or ""
            lines = text.splitlines(True)
            loaded = "".join(lines[:MEMORY_MAX_LINES]).encode("utf-8")[:MEMORY_MAX_BYTES].decode("utf-8", "ignore")
            entries.append({"path": str(mem), "label": tilde(env, mem), "how": "Auto memory (first 200 lines, max 25 KB)",
                            "chars": len(loaded), "via": None})
            if len(loaded) < len(text):
                warnings.append(f"{tilde(env, mem)} is longer than what loads: Claude only sees the first "
                                f"{MEMORY_MAX_LINES} lines (max 25 KB).")

    for e in entries:
        e["tokens"] = round(e["chars"] / CHARS_PER_TOKEN)
    total = sum(e["chars"] for e in entries)
    return {"group": group_id, "entries": entries, "chars": total, "tokens": round(total / CHARS_PER_TOKEN),
            "warnings": list(dict.fromkeys(warnings)),
            "estimate": "Characters divided by 4, a rough rule of thumb for English text.",
            "not_counted": "Claude Code's own instructions, MCP tool lists, and skills that plugins add."}


# --------------------------------------------------------------------------------------
# Drift: instruction files that are mostly copies of each other but no longer match
# (for example CLAUDE.md and AGENTS.md kept side by side). On this Mac, copy pairs scored
# 0.76 to 1.00 and every unrelated pair under 0.30 (measured 2026-11-01), hence 0.6.
# --------------------------------------------------------------------------------------

DRIFT_KINDS = {"user_claude_md", "user_rules", "proj_claude_md", "proj_dot_claude_md", "proj_local_md",
               "proj_agents_md", "proj_rules", "parent_md", "sub_md"}
DRIFT_MIN_RATIO = 0.6
DRIFT_MIN_LINES = 5


def _mirror_candidates(a: Path, b: Path) -> bool:
    """Copies meant to say the same thing sit side by side: in one folder (CLAUDE.md and AGENTS.md), or in
    a folder and its .claude/ subfolder (CLAUDE.md and .claude/CLAUDE.md; ~/AGENTS.md and
    ~/.claude/CLAUDE.md). Similar files in different folders or projects are usually one template filled
    in differently on purpose, so they don't count."""
    return a.parent == b.parent or a.parent == b.parent / ".claude" or b.parent == a.parent / ".claude"


def find_drift(env: Env) -> dict:
    files: Dict[str, dict] = {}
    for it in iter_files(discover(env)):
        if it["exists"] and it["kind"] in DRIFT_KINDS:
            files.setdefault(str(Path(it["path"]).resolve()), it)  # same file under two names counts once
    texts = {k: (_read_text(Path(it["path"])) or "").splitlines() for k, it in files.items()}
    pairs = []
    keys = sorted(files)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            la, lb = texts[a], texts[b]
            if len(la) < DRIFT_MIN_LINES or len(lb) < DRIFT_MIN_LINES or la == lb:
                continue
            if not _mirror_candidates(Path(files[a]["path"]), Path(files[b]["path"])):
                continue
            sm = difflib.SequenceMatcher(None, la, lb, autojunk=False)
            if sm.real_quick_ratio() < DRIFT_MIN_RATIO or sm.quick_ratio() < DRIFT_MIN_RATIO:
                continue
            r = sm.ratio()
            if r < DRIFT_MIN_RATIO:
                continue
            changed = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
            pa, pb = files[a]["path"], files[b]["path"]
            pairs.append({"a": pa, "b": pb, "a_label": tilde(env, pa), "b_label": tilde(env, pb),
                          "ratio": round(r, 2), "changed_lines": changed})
    pairs.sort(key=lambda x: (-x["ratio"], x["a_label"]))
    return {"pairs": pairs, "checked": len(files)}


def compare_files(env: Env, a: str, b: str) -> dict:
    managed = {it["path"] for it in iter_files(discover(env)) if it["exists"]}
    if a not in managed or b not in managed:
        raise ApiError(403, "Both files must be Claude files this app manages.")
    ta, tb = _read_text(Path(a)) or "", _read_text(Path(b)) or ""
    diff = "".join(difflib.unified_diff(ta.splitlines(True), tb.splitlines(True), tilde(env, a), tilde(env, b), n=2))
    return {"a": a, "b": b, "a_label": tilde(env, a), "b_label": tilde(env, b), "diff": diff, "same": ta == tb}


# --------------------------------------------------------------------------------------
# Tidy up: hand-made copies sitting next to a Claude file, such as settings.json.bak-2026…
# or CLAUDE.md.v1-backup. They are moved (never deleted) to ~/.dotclaude/archive/ and can be
# put back. A copy that an instruction file mentions by name is flagged, because moving it
# would break that reference.
# --------------------------------------------------------------------------------------

STRAY_BASE_KINDS = {"user_claude_md", "user_settings", "user_settings_local", "keybindings", "proj_claude_md",
                    "proj_dot_claude_md", "proj_local_md", "proj_agents_md", "proj_settings",
                    "proj_settings_local", "proj_mcp", "desktop_config"}
STRAY_SEPARATORS = ".-_~ "
ARCHIVE_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?/[^/\\]+$")


def find_strays(env: Env) -> List[dict]:
    tree = discover(env)
    managed = {it["path"] for it in iter_files(tree)}
    bases: Dict[Path, set] = {}
    for it in iter_files(tree):
        if it["kind"] in STRAY_BASE_KINDS:
            p = Path(it["path"])
            bases.setdefault(p.parent, set()).add(p.name)
    out: Dict[str, dict] = {}
    for d, names in bases.items():
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file() or f.is_symlink() or str(f) in managed:
                continue
            for n in names:
                if len(f.name) > len(n) and f.name.startswith(n) and f.name[len(n)] in STRAY_SEPARATORS:
                    st = f.stat()
                    out[str(f)] = {"path": str(f), "label": tilde(env, f), "of": tilde(env, d / n),
                                   "size": st.st_size, "mtime": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                                               time.localtime(st.st_mtime))}
                    break
    texts = [(it["path"], _read_text(Path(it["path"])) or "") for it in iter_files(tree)
             if it["exists"] and it["kind"] in DRIFT_KINDS]
    for s in out.values():
        name = Path(s["path"]).name
        s["referenced_in"] = [tilde(env, p) for p, t in texts if name in t]
    return sorted(out.values(), key=lambda s: s["label"].lower())


def _archive_index_path(env: Env) -> Path:
    return env.state_dir / "archive" / "index.json"


def _archive_index(env: Env) -> List[dict]:
    try:
        data = json.loads(_archive_index_path(env).read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def list_tidy(env: Env) -> dict:
    archived = [{**e, "label": tilde(env, e["original"])} for e in reversed(_archive_index(env))]
    return {"strays": find_strays(env), "archived": archived, "dir": tilde(env, env.state_dir / "archive")}


def archive_strays(env: Env, paths: List[str]) -> dict:
    if not isinstance(paths, list) or not paths:
        raise ApiError(400, "Pick at least one file.")
    allowed = {s["path"] for s in find_strays(env)}
    bad = [p for p in paths if p not in allowed]
    if bad:
        raise ApiError(403, "Only the old copies DotClaude lists can be archived.", {"refused": bad})
    d = _archive_dir(env)
    idx = _archive_index(env)
    for p in dict.fromkeys(paths):
        dest = d / flat_name(p)
        shutil.move(p, str(dest))
        idx.append({"id": f"{d.name}/{dest.name}", "original": p, "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
        atomic_write(_archive_index_path(env), json.dumps(idx, indent=2) + "\n")  # after each move, so none is lost
    return {"ok": True, "moved": len(dict.fromkeys(paths)), "dir": tilde(env, d)}


def _archive_dir(env: Env) -> Path:
    root = env.state_dir / "archive"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = root / stamp
    n = 1
    while d.exists():
        d = root / f"{stamp}-{n}"
        n += 1
    d.mkdir(parents=True)
    return d


def delete_file(env: Env, path: str, mtime: Optional[str]) -> dict:
    """Delete a managed file by moving it to the archive, so Tidy up's Put back can undo it."""
    it = find_item(env, path)
    if it["readonly"]:
        raise ApiError(403, KINDS[it["kind"]].get("readonly_reason", "This file is read-only here."))
    p = Path(path)
    if p.is_symlink():
        raise ApiError(403, "This file is a link to another file, so it isn't deleted here.")
    if mtime and str(p.stat().st_mtime_ns) != mtime:
        raise ApiError(409, "This file changed on disk since you opened it. Reload, then delete.")
    d = _archive_dir(env)
    dest = d / flat_name(p)
    shutil.move(str(p), str(dest))
    idx = _archive_index(env)
    idx.append({"id": f"{d.name}/{dest.name}", "original": str(p), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "deleted": True})
    atomic_write(_archive_index_path(env), json.dumps(idx, indent=2) + "\n")
    return {"ok": True, "id": f"{d.name}/{dest.name}", "dir": tilde(env, d)}


def restore_archived(env: Env, aid: str) -> dict:
    if not aid or not ARCHIVE_ID_RE.match(aid) or ".." in aid:
        raise ApiError(400, "Bad archive id.")
    idx = _archive_index(env)
    e = next((x for x in idx if x.get("id") == aid), None)
    root = (env.state_dir / "archive").resolve()
    src = (env.state_dir / "archive" / aid).resolve()
    if not e or root not in src.parents or not src.is_file():
        raise ApiError(404, "That archived file doesn't exist.")
    orig = Path(e["original"])
    if orig.exists():
        raise ApiError(409, "A file with that name is already back in place, so nothing was moved.")
    if not orig.parent.is_dir():
        raise ApiError(409, "The folder it came from doesn't exist any more.")
    shutil.move(str(src), str(orig))
    idx = [x for x in idx if x.get("id") != aid]
    atomic_write(_archive_index_path(env), json.dumps(idx, indent=2) + "\n")
    return {"ok": True, "path": str(orig)}


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


def set_project_hidden(env: Env, path: str, hidden: bool) -> dict:
    """Hide a project from DotClaude's lists, or show it again. Only changes ~/.dotclaude/config.json."""
    cfg = env.load_config()
    cur = cfg.get("hidden_projects", [])
    if hidden:
        known = {str(pr["path"]) for pr in project_paths(env)[0]}
        if path not in known:
            raise ApiError(404, "That project isn't in the list.")
        if path not in cur:
            cur.append(path)
    else:
        if path not in cur:
            raise ApiError(404, "That project isn't hidden.")
        cur = [p for p in cur if p != path]
    cfg["hidden_projects"] = cur
    env.save_config(cfg)
    return {"ok": True, "hidden": cur}


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
    # The .md form: the HTML page changes on every fetch (checked 2026-11-01), the .md form doesn't
    "skills_bp": ("Skill authoring best practices",
                  "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices.md"),
    "agents": ("Subagents", "https://code.claude.com/docs/en/sub-agents.md"),
    "settings": ("Settings", "https://code.claude.com/docs/en/settings.md"),
    "permissions": ("Permissions", "https://code.claude.com/docs/en/permissions.md"),
    "hooks": ("Hooks guide", "https://code.claude.com/docs/en/hooks-guide.md"),
    "mcp": ("MCP", "https://code.claude.com/docs/en/mcp.md"),
}
KIND_GUIDANCE = {
    "skills": {"user_skills", "proj_skills", "user_commands", "proj_commands"},
    "agents": {"user_agents", "proj_agents"},
    "settings": {"user_settings", "user_settings_local", "proj_settings", "proj_settings_local", "managed_settings"},
    "mcp": {"proj_mcp", "desktop_config", "managed_mcp"},
}
NOT_REVIEWED = {"claude_json", "desktop_app_config", "user_themes", "user_workflows", "proj_workflows",
                "user_plugins", "user_scripts", "proj_scripts"}
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
    url_lines = "\n".join(f"- {GUIDANCE[u][0]}: {GUIDANCE[u][1]}" for u in guidance_keys_for(kinds))
    return REVIEW_INSTRUCTIONS.format(urls=url_lines), "\n\n".join(parts), meta


def guidance_keys_for(kinds) -> List[str]:
    """The official pages a review of these file kinds is judged against (the benchmark)."""
    kinds = set(kinds)
    urls = ["memory", "best"]
    for g, ks in KIND_GUIDANCE.items():
        if kinds & ks:
            urls.append(g)
    if "skills" in urls:
        urls.append("skills_bp")
    if kinds & KIND_GUIDANCE["settings"]:
        urls += ["permissions", "hooks"]
    return list(dict.fromkeys(urls))


# --------------------------------------------------------------------------------------
# Guidance tracking: DotClaude keeps a copy of each benchmark page in ~/.dotclaude/guidance/
# and compares on every check, so it can say when Anthropic changed a page and which of your
# files were last edited before that. It can only see changes from its first check onward.
# --------------------------------------------------------------------------------------

GUIDANCE_STALE_S = 12 * 3600       # the app re-checks on start if the last check is older
LAUNCH_LABEL = "com.dotclaude.guidance-check"


def _guidance_dir(env: Env) -> Path:
    return env.state_dir / "guidance"


def _guidance_state(env: Env) -> dict:
    try:
        d = json.loads((_guidance_dir(env) / "state.json").read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def http_fetch(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"DotClaude/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _normalise(text: str) -> str:
    return "\n".join(l.rstrip() for l in text.strip().splitlines()) + "\n"


def check_guidance(env: Env, fetch=None) -> dict:
    """Fetch every benchmark page, compare with the saved copy, record what changed."""
    import hashlib
    fetch = fetch or http_fetch
    d = _guidance_dir(env)
    d.mkdir(parents=True, exist_ok=True)
    state = _guidance_state(env)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    changed, failed = [], []
    for key, (title, url) in GUIDANCE.items():
        s = state.setdefault(key, {})
        s.update({"title": title, "url": url, "checked": now})
        try:
            text = _normalise(fetch(url))
            if len(text) < 200:
                raise ValueError("page came back nearly empty")
        except Exception as e:
            s["error"] = f"{type(e).__name__}: {e}"[:200]
            failed.append(key)
            continue
        s.pop("error", None)
        h = hashlib.sha256(text.encode()).hexdigest()
        cur = d / f"{key}.md"
        if s.get("url_hashed") != url:          # first sight, or the page moved: new baseline, not a change
            s.update({"hash": h, "first_seen": now, "changed": None, "url_hashed": url})
            atomic_write(cur, text)
            (d / f"{key}.prev.md").unlink(missing_ok=True)
        elif s.get("hash") != h:
            if cur.is_file():
                os.replace(cur, d / f"{key}.prev.md")
            atomic_write(cur, text)
            s.update({"hash": h, "changed": now})
            changed.append(key)
    atomic_write(d / "state.json", json.dumps(state, indent=2) + "\n")
    return {"checked": now, "changed": changed, "failed": failed}


def _reviewable_files(env: Env, tree: dict) -> List[dict]:
    return [it for it in iter_files(tree) if it["exists"] and it["kind"] not in NOT_REVIEWED
            and not it["readonly"]]


# Claude writes these notes itself, so flagging them "to review" after a guidance change is noise.
NOT_BEHIND = {"automem_topics", "automem_index", "automem_orphan"}


def guidance_status(env: Env) -> dict:
    state = _guidance_state(env)
    tree = discover(env)
    used: Dict[str, set] = {k: set() for k in GUIDANCE}
    for kind in KINDS:
        if kind in NOT_REVIEWED or KINDS[kind].get("KINDS_PLACEHOLDER"):
            continue
        for g in guidance_keys_for({kind}):
            if g not in ("memory", "best"):     # those two apply to every review; the page says so
                used[g].add(KINDS[kind]["title"])
    pages = []
    for key, (title, url) in GUIDANCE.items():
        s = state.get(key, {})
        pages.append({"key": key, "title": title, "url": url, "checked": s.get("checked"),
                      "first_seen": s.get("first_seen"), "changed": s.get("changed"), "error": s.get("error"),
                      "has_diff": (_guidance_dir(env) / f"{key}.prev.md").is_file(),
                      "used_for": sorted(used[key])})
    behind = []
    for it in _reviewable_files(env, tree):
        if it["kind"] in NOT_BEHIND:
            continue
        mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(Path(it["path"]).stat().st_mtime))
        newer = [k for k in guidance_keys_for({it["kind"]}) if (state.get(k) or {}).get("changed")
                 and state[k]["changed"] > mtime]
        if newer:
            behind.append({"path": it["path"], "label": tilde(env, it["path"]), "edited": mtime,
                           "pages": [GUIDANCE[k][0] for k in newer],
                           "latest": max(state[k]["changed"] for k in newer)})
    behind.sort(key=lambda b: b["latest"], reverse=True)
    checks = [s.get("checked") for s in state.values() if s.get("checked")]
    return {"pages": pages, "behind": behind, "last_check": max(checks) if checks else None,
            "notify": notify_status(env)}


def guidance_diff(env: Env, key: str) -> dict:
    if key not in GUIDANCE:
        raise ApiError(404, "Unknown page.")
    d = _guidance_dir(env)
    prev, cur = d / f"{key}.prev.md", d / f"{key}.md"
    if not prev.is_file() or not cur.is_file():
        raise ApiError(404, "No earlier copy of this page to compare with yet.")
    diff = "".join(difflib.unified_diff(prev.read_text().splitlines(True), cur.read_text().splitlines(True),
                                        "before", "now", n=2))
    return {"key": key, "title": GUIDANCE[key][0], "url": GUIDANCE[key][1], "diff": diff}


def maybe_check_guidance(env: Env) -> None:
    """On app start: re-check in the background if the last check is old."""
    st = _guidance_state(env)
    last = max((s.get("checked") or "" for s in st.values()), default="")
    try:
        age = time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) if last else None
    except ValueError:
        age = None
    if age is None or age > GUIDANCE_STALE_S:
        threading.Thread(target=lambda: check_guidance(env), daemon=True).start()


# ---- macOS notifications: a LaunchAgent runs `dotclaude.py --check-guidance --notify` daily ----

def _launch_agent_path(env: Env) -> Path:
    return env.home / "Library/LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def notify_status(env: Env) -> dict:
    return {"on": _launch_agent_path(env).is_file(), "time": "09:00 every day",
            "log": tilde(env, env.home / "Library/Logs/DotClaude/guidance-check.log")}


def launch_agent_plist(env: Env, script: Path, python: str) -> str:
    import plistlib
    log = str(env.home / "Library/Logs/DotClaude/guidance-check.log")
    return plistlib.dumps({
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [python, str(script), "--check-guidance", "--notify"],
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
        "StandardOutPath": log, "StandardErrorPath": log,
    }).decode()


def set_notify(env: Env, on: bool, run=None) -> dict:
    """Install or remove the daily check. `run` executes launchctl (swapped out by the self-test)."""
    run = run or (lambda args: subprocess.run(args, capture_output=True, text=True))
    p = _launch_agent_path(env)
    domain = f"gui/{os.getuid()}"
    if p.is_file():
        run(["launchctl", "bootout", domain, str(p)])
    if on:
        (env.home / "Library/Logs/DotClaude").mkdir(parents=True, exist_ok=True)
        atomic_write(p, launch_agent_plist(env, Path(__file__).resolve(), sys.executable))
        r = run(["launchctl", "bootstrap", domain, str(p)])
        if getattr(r, "returncode", 0) != 0:
            p.unlink(missing_ok=True)
            raise ApiError(500, "macOS refused the daily check: " + (getattr(r, "stderr", "") or "").strip()[:300])
    else:
        p.unlink(missing_ok=True)
    return notify_status(env)


def notify_mac(title: str, message: str) -> None:
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def notification_text(env: Env, result: dict) -> Optional[str]:
    """What the daily check says, or None when nothing changed."""
    if not result["changed"]:
        return None
    names = [GUIDANCE[k][0] for k in result["changed"]]
    behind = guidance_status(env)["behind"]
    msg = "Updated: " + ", ".join(names) + "."
    if behind:
        msg += f" {len(behind)} of your files predate it. Open DotClaude to review."
    return msg


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
            if u.path == "/health":  # no token: lets a launcher or another local tool see that DotClaude is up
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
            elif u.path == "/api/load":
                self._run(lambda: startup_load(env, (q.get("group") or [""])[0]))
            elif u.path == "/api/drift":
                self._run(lambda: find_drift(env))
            elif u.path == "/api/compare":
                self._run(lambda: compare_files(env, (q.get("a") or [""])[0], (q.get("b") or [""])[0]))
            elif u.path == "/api/tidy":
                self._run(lambda: list_tidy(env))
            elif u.path == "/api/guidance":
                self._run(lambda: guidance_status(env))
            elif u.path == "/api/guidance/diff":
                self._run(lambda: guidance_diff(env, (q.get("key") or [""])[0]))
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
                "/api/projects/hide": lambda: set_project_hidden(env, body.get("path", ""), True),
                "/api/projects/unhide": lambda: set_project_hidden(env, body.get("path", ""), False),
                "/api/pick-folder": pick_folder,
                "/api/reveal": lambda: reveal(env, body.get("path", "")),
                "/api/shutdown": request_shutdown,
                "/api/backups/restore": lambda: restore_backup(env, body.get("id", ""), body.get("mtime")),
                "/api/review/start": lambda: jobs.start(body.get("path"), body.get("group"), body.get("model") or None),
                "/api/review/cancel": lambda: jobs.cancel(body.get("id", "")),
                "/api/tidy/archive": lambda: archive_strays(env, body.get("paths")),
                "/api/tidy/restore": lambda: restore_archived(env, body.get("id", "")),
                "/api/delete": lambda: delete_file(env, body.get("path", ""), body.get("mtime")),
                "/api/guidance/check": lambda: {**check_guidance(env), **guidance_status(env)},
                "/api/notify": lambda: set_notify(env, bool(body.get("on"))),
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

    print("File type descriptions")
    no_short = [k for k, v in KINDS.items() if not v.get("short") or not v.get("section")]
    check(f"every file type has a plain-English name and a section (missing: {no_short})", not no_short)
    no_why = [k for k, v in KINDS.items() if not (v.get("why") or "").strip() or not (v.get("missing") or "").strip()]
    check(f"every file type says why you'd want it and what's lost without it (missing: {no_why})", not no_why)
    dashed = [k for k, v in KINDS.items() if any("—" in str(v.get(f, "")) for f in
                                                  ("role", "why", "missing", "loads", "share", "create_hint"))]
    check(f"no em-dashes in file type descriptions (found in: {dashed})", not dashed)

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

        print("What loads at startup, drift, scripts, tidy (separate fake home)")
        h2 = td / "home2"
        c2 = h2 / ".claude"
        sh = h2 / "shared"
        for d in (c2 / "rules", c2 / "skills/a", c2 / "skills/b", c2 / "hooks", c2 / "plugins", sh, h2 / "bin"):
            d.mkdir(parents=True)
        comment = "<!-- maintainer note " + "x" * 400 + " -->\n"
        (c2 / "CLAUDE.md").write_text("# me\n@~/shared/team.md\n`@~/shared/ignored.md`\n```\n@~/shared/fenced.md\n```\n"
                                      "@~/shared/missing.md\nmail a@b.com\nold copy: settings.json.bak-1\n" + comment)
        (sh / "team.md").write_text("team\n@deep1.md\n")
        for i in range(1, 5):
            (sh / f"deep{i}.md").write_text(f"deep{i}\n@deep{i + 1}.md\n@team.md\n")  # also a cycle back to team.md
        (sh / "ignored.md").write_text("never\n")
        (sh / "fenced.md").write_text("never\n")
        (c2 / "rules/always.md").write_text("always\n")
        (c2 / "rules/py.md").write_text('---\npaths:\n  - "**/*.py"\n---\npy only\n')
        (c2 / "skills/a/SKILL.md").write_text("---\nname: a\ndescription: does a\n---\n" + "body\n" * 300)
        (c2 / "skills/b/SKILL.md").write_text("---\nname: b\ndescription: manual\ndisable-model-invocation: true\n---\n")
        outside = td / "outside.sh"
        outside.write_text("echo hi\n")
        (c2 / "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": 'python3 "$HOME/.claude/hooks/guard.py"'},
                {"type": "command", "command": f"bash {outside}"},
                {"type": "command", "command": "/usr/bin/env true"}]}]},
            "statusLine": {"type": "command", "command": "bash ~/bin/status.sh"}}))
        (c2 / "hooks/guard.py").write_text("print('ok')\n")
        (h2 / "bin/status.sh").write_text("echo status\n")
        (c2 / "plugins/installed_plugins.json").write_text('{"version": 2, "plugins": {}}\n')
        (c2 / "settings.local.json").write_text('{"permissions": {"allow": []}}\n')
        (c2 / "settings.json.bak-1").write_text("{}\n")
        (c2 / "CLAUDE.md.old-copy").write_text("old\n")
        (c2 / "settings.json5").write_text("not a copy\n")
        base_md = "".join(f"line {i}\n" for i in range(8))
        pa, pb, pd = h2 / "Code/a", h2 / "Code/b", h2 / "Code/d"
        for d in (pa / "sys1", pa / "sys2", pb, pd):
            d.mkdir(parents=True)
        (pa / "CLAUDE.md").write_text(base_md)
        (pa / "AGENTS.md").write_text(base_md.replace("line 3", "line 3 changed"))
        (pa / "CLAUDE.md~").write_text("editor backup\n")
        (pa / "sys1/CLAUDE.md").write_text(base_md + "icp\n")
        (pa / "sys2/CLAUDE.md").write_text(base_md + "oisy\n")
        (pb / "AGENTS.md").write_text("".join(f"other {i}\n" for i in range(8)))
        (pd / "CLAUDE.md").write_text(base_md)
        (pd / "AGENTS.md").write_text(base_md)
        mem2 = c2 / "projects" / encode_project_key(pa) / "memory"
        mem2.mkdir(parents=True)
        (mem2 / "MEMORY.md").write_text("".join(f"note {i}\n" for i in range(250)))
        (h2 / ".claude.json").write_text(json.dumps({"projects": {str(pa): {}, str(pb): {}, str(pd): {}}}))
        env2 = Env(h2, managed_dir=td / "managed2", desktop_dir=td / "desktop2", state_dir=td / "state2")

        L = startup_load(env2, "global")
        names = {Path(e["path"]).name for e in L["entries"]}
        check("load: global CLAUDE.md and its @import counted", {"CLAUDE.md", "team.md"} <= names)
        check("load: imports followed exactly 4 hops", {"deep1.md", "deep2.md", "deep3.md"} <= names
              and "deep4.md" not in names)
        check("load: import cycle doesn't hang or double count",
              sum(1 for e in L["entries"] if e["path"].endswith("team.md")) == 1)
        check("load: imports in code spans and fenced blocks ignored", not names & {"ignored.md", "fenced.md"})
        check("load: missing import reported", any("missing.md" in w for w in L["warnings"]))
        check("load: email address not taken for an import", not any("b.com" in w for w in L["warnings"]))
        md_entry = next(e for e in L["entries"] if e["path"] == str(c2 / "CLAUDE.md"))
        check("load: HTML comments not counted", md_entry["chars"] < len((c2 / "CLAUDE.md").read_text()) - 400)
        check("load: rule without paths counted, rule with paths not", "always.md" in names and "py.md" not in names)
        sk = next((e for e in L["entries"] if "skills and subagents" in e["label"]), {})
        check("load: only skill frontmatter counted, manual-only skill skipped",
              sk.get("label", "").endswith("(1)") and 0 < sk.get("chars", 0) < 100)
        check("load: tokens are characters / 4", L["tokens"] == round(L["chars"] / 4))
        La = startup_load(env2, "p:" + str(pa))
        na = [e["path"] for e in La["entries"]]
        check("load: project CLAUDE.md counted, its AGENTS.md not", str(pa / "CLAUDE.md") in na
              and str(pa / "AGENTS.md") not in na)
        me = next((e for e in La["entries"] if e["path"].endswith("MEMORY.md")), {})
        check("load: auto memory capped at 200 lines", me.get("chars") == len("".join(f"note {i}\n" for i in range(200)))
              and any("first 200 lines" in w for w in La["warnings"]))
        Lb = startup_load(env2, "p:" + str(pb))
        check("load: AGENTS.md loads when there's no CLAUDE.md, with a warning",
              str(pb / "AGENTS.md") in [e["path"] for e in Lb["entries"]] and any("AGENTS.md" in w for w in Lb["warnings"]))
        try:
            startup_load(env2, "p:/nope")
            check("load: unknown section refused", False)
        except ApiError as e:
            check("load: unknown section refused", e.status == 404)

        D = find_drift(env2)
        dp = {(Path(p["a"]).relative_to(h2).as_posix(), Path(p["b"]).relative_to(h2).as_posix()) for p in D["pairs"]}
        check("drift: CLAUDE.md vs AGENTS.md that differ is flagged", ("Code/a/AGENTS.md", "Code/a/CLAUDE.md") in dp)
        check("drift: identical copies not flagged", not any("Code/d" in a for pair in dp for a in pair))
        check("drift: same-named template siblings not flagged", not any("sys1" in a for pair in dp for a in pair))
        check("drift: unrelated files not flagged", not any("Code/b" in a for pair in dp for a in pair))
        cmp = compare_files(env2, str(pa / "CLAUDE.md"), str(pa / "AGENTS.md"))
        check("compare: diff shows the changed line", "+line 3 changed" in cmp["diff"] and not cmp["same"])
        try:
            compare_files(env2, str(pa / "CLAUDE.md"), str(outside))
            check("compare: unmanaged file refused", False)
        except ApiError as e:
            check("compare: unmanaged file refused", e.status == 403)

        t2 = discover(env2)
        all2 = {it["path"]: it for it in iter_files(t2)}
        check("scripts: hook in ~/.claude/hooks listed", all2.get(str(c2 / "hooks/guard.py"), {}).get("kind") == "user_scripts")
        check("scripts: status line script outside .claude listed", str(h2 / "bin/status.sh") in all2)
        check("scripts: script outside the home folder not listed", str(outside) not in all2)
        check("home-folder settings.local.json listed", all2.get(str(c2 / "settings.local.json"), {}).get("kind")
              == "user_settings_local")
        pl = str(c2 / "plugins/installed_plugins.json")
        check("plugins list shown read-only", all2.get(pl, {}).get("readonly") is True)
        try:
            save_file(env2, pl, "{}", None)
            check("plugins list can't be saved", False)
        except ApiError as e:
            check("plugins list can't be saved", e.status == 403)
        r2 = read_file(env2, str(h2 / "bin/status.sh"))
        check("scripts: can be opened", r2["content"] == "echo status\n")

        st = {s["label"]: s for s in find_strays(env2)}
        check("tidy: hand-made copies found", set(st) == {"~/.claude/settings.json.bak-1", "~/.claude/CLAUDE.md.old-copy",
                                                         "~/Code/a/CLAUDE.md~"})
        check("tidy: settings.json5 isn't taken for a copy", "~/.claude/settings.json5" not in st)
        check("tidy: copy mentioned in CLAUDE.md is flagged",
              st["~/.claude/settings.json.bak-1"]["referenced_in"] == ["~/.claude/CLAUDE.md"]
              and st["~/.claude/CLAUDE.md.old-copy"]["referenced_in"] == [])
        try:
            archive_strays(env2, [str(c2 / "CLAUDE.md")])
            check("tidy: archiving a live Claude file refused", False)
        except ApiError as e:
            check("tidy: archiving a live Claude file refused", e.status == 403 and (c2 / "CLAUDE.md").is_file())
        try:
            archive_strays(env2, [])
            check("tidy: empty selection refused", False)
        except ApiError as e:
            check("tidy: empty selection refused", e.status == 400)
        bak, oldc = c2 / "settings.json.bak-1", c2 / "CLAUDE.md.old-copy"
        ar = archive_strays(env2, [str(bak), str(oldc)])
        lt = list_tidy(env2)
        check("tidy: archive moves files, nothing deleted", ar["moved"] == 2 and not bak.exists() and not oldc.exists()
              and len(lt["archived"]) == 2 and all((td / "state2/archive" / a["id"]).is_file() for a in lt["archived"]))
        a_bak = next(a for a in lt["archived"] if a["original"] == str(bak))
        a_old = next(a for a in lt["archived"] if a["original"] == str(oldc))
        restore_archived(env2, a_bak["id"])
        check("tidy: put back restores the file", bak.read_text() == "{}\n" and len(list_tidy(env2)["archived"]) == 1)
        try:
            restore_archived(env2, a_bak["id"])
            check("tidy: putting back twice refused", False)
        except ApiError as e:
            check("tidy: putting back twice refused", e.status == 404)
        oldc.write_text("someone made a new one\n")
        try:
            restore_archived(env2, a_old["id"])
            check("tidy: put back never overwrites", False)
        except ApiError as e:
            check("tidy: put back never overwrites", e.status == 409 and oldc.read_text() == "someone made a new one\n"
                  and (td / "state2/archive" / a_old["id"]).is_file())
        try:
            restore_archived(env2, "../../etc/passwd")
            check("tidy: bad archive id refused", False)
        except ApiError as e:
            check("tidy: bad archive id refused", e.status == 400)

        live = c2 / "CLAUDE.md"
        live_text = live.read_text()
        for name, args, status in [("delete: read-only file refused", (pl, None), 403),
                                   ("delete: unmanaged file refused", (str(h2 / ".bashrc"), None), 403),
                                   ("delete: stale edit refused", (str(live), "1"), 409)]:
            try:
                delete_file(env2, *args)
                check(name, False)
            except ApiError as e:
                check(name, e.status == status and live.is_file() and Path(pl).is_file())
        dr = delete_file(env2, str(live), str(live.stat().st_mtime_ns))
        a_del = next((a for a in list_tidy(env2)["archived"] if a["id"] == dr["id"]), {})
        check("delete: file moves to the archive, marked deleted", not live.exists() and a_del.get("deleted") is True
              and (td / "state2/archive" / dr["id"]).read_text() == live_text
              and str(live) not in {i["path"] for i in iter_files(discover(env2)) if i["exists"]})
        restore_archived(env2, dr["id"])
        check("delete: put back restores it", live.read_text() == live_text)

        pa_path = next(g["path"] for g in discover(env2)["projects"] if g["title"] == pa.name)
        try:
            set_project_hidden(env2, str(h2 / "not-a-project"), True)
            check("hide: a folder that isn't a project refused", False)
        except ApiError as e:
            check("hide: a folder that isn't a project refused", e.status == 404)
        set_project_hidden(env2, pa_path, True)
        t_h = discover(env2)
        orphan_paths = {c["path"] for g in t_h["groups"] for it in g["items"] if it["kind"] == "automem_orphan"
                        for c in it.get("children", [])}
        check("hide: project leaves the list and shows under hidden", pa_path not in {g["path"] for g in t_h["projects"]}
              and [h["path"] for h in t_h["hiddenProjects"]] == [pa_path])
        check("hide: its memory isn't reported as unmatched", not any(str(mem2) in o for o in orphan_paths))
        check("hide: no files touched", (pa / "CLAUDE.md").is_file() and (mem2 / "MEMORY.md").is_file())
        set_project_hidden(env2, pa_path, False)
        check("hide: showing it again restores it", pa_path in {g["path"] for g in discover(env2)["projects"]}
              and not discover(env2)["hiddenProjects"])
        try:
            set_project_hidden(env2, pa_path, False)
            check("hide: showing a project that isn't hidden refused", False)
        except ApiError as e:
            check("hide: showing a project that isn't hidden refused", e.status == 404)

        print("Best-practice benchmark tracking (fake web)")
        pages = {url: f"# {title}\n" + "guidance line\n" * 30 for title, url in GUIDANCE.values()}
        fake = lambda url: pages[url]
        g1 = check_guidance(env2, fetch=fake)
        st1 = guidance_status(env2)
        check("guidance: first check is a baseline, not a change", g1["changed"] == [] and
              all(p["first_seen"] and not p["changed"] for p in st1["pages"]))
        check("guidance: nothing changed means nothing reported", check_guidance(env2, fetch=fake)["changed"] == [])
        check("guidance: no notification when nothing changed", notification_text(env2, {"changed": []}) is None)
        check("guidance: every page lists what it's used for (memory and best: every review)",
              all(p["used_for"] for p in st1["pages"] if p["key"] in ("settings", "skills"))
              and all(not p["used_for"] for p in st1["pages"] if p["key"] in ("memory", "best")))
        old_mtime = time.time() - 86400                     # a file edited yesterday...
        os.utime(c2 / "settings.json", (old_mtime, old_mtime))
        new_mtime = time.time() + 120                       # ...and one edited after the change
        os.utime(pa / "CLAUDE.md", (new_mtime, new_mtime))
        set_url = GUIDANCE["settings"][1]
        pages[set_url] = pages[set_url].replace("guidance line\n", "NEW RULE about permissions\n", 1)
        g2 = check_guidance(env2, fetch=fake)
        st2 = guidance_status(env2)
        check("guidance: a changed page is detected", g2["changed"] == ["settings"])
        behind = {b["path"] for b in st2["behind"]}
        check("guidance: file edited before the change is flagged", str(c2 / "settings.json") in behind)
        check("guidance: file edited after the change isn't", str(pa / "CLAUDE.md") not in behind)
        check("guidance: file the changed page doesn't cover isn't flagged", str(c2 / "CLAUDE.md") not in behind)
        dg = guidance_diff(env2, "settings")
        check("guidance: 'what changed' shows the new line", "+NEW RULE about permissions" in dg["diff"])
        check("guidance: notification names the page", "Settings" in (notification_text(env2, g2) or ""))
        try:
            guidance_diff(env2, "memory")
            check("guidance: no diff before a page has changed", False)
        except ApiError as e:
            check("guidance: no diff before a page has changed", e.status == 404)
        def broken(url):
            raise OSError("offline")
        g3 = check_guidance(env2, fetch=broken)
        st3 = guidance_status(env2)
        check("guidance: being offline isn't reported as a change", g3["changed"] == [] and
              len(g3["failed"]) == len(GUIDANCE) and all(p["error"] for p in st3["pages"]))
        check("guidance: being offline keeps the last known change",
              next(p for p in st3["pages"] if p["key"] == "settings")["changed"] == st2["pages"][
                  [p["key"] for p in st2["pages"]].index("settings")]["changed"])
        g4 = check_guidance(env2, fetch=lambda url: "tiny")
        check("guidance: a near-empty page counts as a failure, not a change", g4["changed"] == [] and g4["failed"])
        ran = []
        ns = set_notify(env2, True, run=lambda a: (ran.append(a), type("R", (), {"returncode": 0, "stderr": ""})())[1])
        plist = _launch_agent_path(env2).read_text()
        check("notify: switching on installs a daily check", ns["on"] and "--check-guidance" in plist and "--notify" in plist
              and str(Path(__file__).resolve()) in plist and any(a[1] == "bootstrap" for a in ran))
        try:
            set_notify(env2, True, run=lambda a: type("R", (), {"returncode": 5, "stderr": "denied"})())
            check("notify: a refusal from macOS is reported, not hidden", False)
        except ApiError as e:
            check("notify: a refusal from macOS is reported, not hidden", "denied" in e.msg
                  and not _launch_agent_path(env2).exists())
        ns = set_notify(env2, False, run=lambda a: type("R", (), {"returncode": 0, "stderr": ""})())
        check("notify: switching off removes it", not ns["on"] and not _launch_agent_path(env2).exists())
        check("review: benchmark keys for settings include permissions and hooks",
              guidance_keys_for({"user_settings"}) == ["memory", "best", "settings", "permissions", "hooks"])

        s, r = call("GET", "/api/load?group=global")
        check("API: /api/load works", s == 200 and "tokens" in r)
        s, r = call("GET", "/api/drift")
        check("API: /api/drift works", s == 200 and "pairs" in r)
        s, r = call("POST", "/api/tidy/archive", {"paths": [str(cd / "CLAUDE.md")]})
        check("API: archiving a live file refused", s == 403 and (cd / "CLAUDE.md").is_file())
        s, r = call("POST", "/api/delete", {"path": str(cd / "CLAUDE.md")}, tok=None)
        check("API: delete without the token refused", s == 403 and (cd / "CLAUDE.md").is_file())
        s, r = call("POST", "/api/delete", {"path": str(home / ".claude.json")})
        check("API: deleting ~/.claude.json refused", s == 403 and (home / ".claude.json").is_file())
        s, r = call("GET", "/api/compare?a=" + quote(str(cd / "CLAUDE.md")) + "&b=" + quote("/etc/hosts"))
        check("API: compare with an unmanaged file refused", s == 403)
        check("API: new routes need the token", call("GET", "/api/tidy", tok=None)[0] == 403)

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

    readme = Path(__file__).with_name("README.md")
    if readme.is_file():
        m = re.search(r"runs (\d+) checks", readme.read_text(encoding="utf-8"))
        stated = int(m.group(1)) if m else None
        # +1 counts this check itself
        check(f"README states the check count ({stated} vs {len(results) + 1})", stated == len(results) + 1)

    failed =[n for n, ok in results if not ok]
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
    ap.add_argument("--check-guidance", action="store_true",
                    help="check Anthropic's best-practice pages for changes, print what changed, and exit")
    ap.add_argument("--notify", action="store_true", help="with --check-guidance: show a macOS notification on changes")
    ap.add_argument("--home", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    env = Env(Path(args.home) if args.home else Path.home())
    if args.check_guidance:
        res = check_guidance(env)
        print(time.strftime("%Y-%m-%d %H:%M"), "checked", len(GUIDANCE), "pages; changed:",
              ", ".join(res["changed"]) or "none", "; failed:", ", ".join(res["failed"]) or "none")
        msg = notification_text(env, res)
        if msg and args.notify:
            notify_mac("Claude Code best practice changed", msg)
        sys.exit(1 if len(res["failed"]) == len(GUIDANCE) else 0)
    maybe_check_guidance(env)
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
.sec-title { font-size: 12px; font-weight: 600; color: var(--muted); padding: 16px 8px 4px; display: flex; justify-content: space-between; align-items: center; }
.grp { margin-bottom: 2px; }
.grp-head { display: flex; align-items: center; gap: 2px; width: 100%; padding: 0 8px 0 2px; border-radius: 6px; }
.grp-head:hover { background: var(--hover); }
.grp-head.sel { background: var(--sel); }
.grp-head button { background: none; border: 0; padding: 6px 4px; text-align: left; }
.grp-head .caret { width: 22px; color: var(--faint); font-size: 10px; transition: transform .12s; flex: none; text-align: center; }
.grp-head .caret:hover { color: var(--ink); }
.grp.closed .caret { transform: rotate(-90deg); }
.grp.closed .grp-body { display: none; }
.grp-head .t { font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grp-head .n { font-size: 11px; color: var(--faint); }
.row .fname { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; color: var(--faint); margin-left: 7px; }
.row.tool { padding-left: 8px; }
.row.tool .lbl { font-weight: 500; }
.row.faint .lbl { color: var(--muted); font-weight: 400; }
.tool-ico { width: 16px; flex: none; text-align: center; color: var(--muted); font-size: 12px; }
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
.fact.wide { grid-column: 1 / -1; }
.why-missing { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 10px; padding: 12px 14px; margin: 0 0 14px; }
.why-missing h4 { margin: 0 0 4px; font-size: 13px; color: var(--ink); font-weight: 600; }
.about { margin: 0 0 12px; }
.about > summary { cursor: pointer; font-size: 13px; color: var(--muted); padding: 4px 0; width: fit-content; }
.about > summary:hover { color: var(--ink); }
.about[open] > summary { margin-bottom: 8px; }
.about .facts { margin-bottom: 4px; }
.flag { font-size: 13px; color: var(--muted); background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--warn); border-radius: 8px; padding: 7px 12px; margin-bottom: 8px; }
.why-missing p { margin: 0; font-size: 14px; }
.why-missing code { background: var(--code); padding: 0 4px; border-radius: 4px; font-size: 12px; }
.fact h4 { margin: 0 0 3px; font-size: 12px; color: var(--muted); font-weight: 600; }
.fact p { margin: 0; font-size: 13px; }
.fact code { background: var(--code); padding: 0 4px; border-radius: 4px; font-size: 12px; }
.editor-wrap { flex: 1; display: flex; flex-direction: column; min-height: 420px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.toolbar { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-bottom: 1px solid var(--line); font-size: 12px; }
.toolbar .status { color: var(--muted); }
.toolbar .status.dirty { color: var(--warn); font-weight: 600; }
.toolbar .status.ro { color: var(--muted); }
.toolbar .spacer { flex: 1; }
.btn { border: 1px solid var(--line); background: var(--panel); border-radius: 7px; padding: 5px 12px; font-size: 13px; }
.btn:hover { background: var(--hover); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
.btn.primary:disabled { opacity: .45; cursor: default; }
.btn.danger { color: var(--err); }
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
.list > button { display: flex; width: 100%; text-align: left; gap: 10px; align-items: center; background: none; border: 0; border-bottom: 1px solid var(--line); padding: 9px 14px; }
.list > button:last-child { border-bottom: 0; }
.list > button:hover { background: var(--hover); }
.lede { color: var(--muted); margin: 0 0 14px; max-width: 72ch; }
h3.sec { font-size: 15px; margin: 24px 0 8px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 4px 0 6px; }
.actions:empty { display: none; }
.list .fname, .sugg .fname { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; color: var(--faint); }
.list button .ent { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.list button .preview { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.list button .when { font-size: 11px; color: var(--faint); text-align: right; max-width: 34%; }
.list button .why-line { font-size: 12px; color: var(--muted); max-width: 55%; }
.list .count { min-width: 22px; height: 18px; border-radius: 9px; background: var(--chip); color: var(--muted); font-size: 11px; display: inline-flex; align-items: center; justify-content: center; flex: none; }
.dot.hollow { background: transparent; border: 1.5px solid var(--faint); }
.sugg { display: flex; flex-direction: column; gap: 8px; }
.sugg-row { display: flex; gap: 14px; align-items: center; background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 10px; padding: 12px 14px; }
.sugg-row > div { flex: 1; }
.sugg-row p { margin: 4px 0 0; font-size: 13px; color: var(--muted); }
.sugg-row .btn { flex: none; }
details.more { margin-top: 22px; }
details.more > summary { cursor: pointer; font-weight: 600; font-size: 15px; padding: 4px 0; }
details.more[open] > summary { margin-bottom: 10px; }
.att-row { display: flex; gap: 14px; align-items: flex-start; padding: 12px 14px; border-bottom: 1px solid var(--line); }
.att-row:last-child { border-bottom: 0; }
.att-text { flex: 1; min-width: 0; }
.att-text .rv-meta { display: block; margin-top: 2px; font-size: 13px; }
.att-text details { margin-top: 8px; }
.att-text details > summary { cursor: pointer; font-size: 13px; color: var(--accent); }
.att-text details .list { margin-top: 8px; }
.att-row > .btn { flex: none; }
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
.load-total { display: flex; align-items: baseline; gap: 10px; margin: 6px 0 10px; }
.load-total b { font-size: 22px; }
.load-row { display: grid; grid-template-columns: 1fr 90px; gap: 2px 12px; padding: 8px 14px; border-bottom: 1px solid var(--line); font-size: 13px; }
.load-row:last-child { border-bottom: 0; }
.load-row .how { grid-column: 1; font-size: 12px; color: var(--muted); }
.load-row .tok { grid-column: 2; grid-row: 1 / span 2; text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
.load-bar { grid-column: 1 / span 2; height: 4px; background: var(--chip); border-radius: 2px; overflow: hidden; }
.load-bar i { display: block; height: 100%; background: var(--accent); }
.drift-row { display: flex; gap: 10px; align-items: center; padding: 9px 14px; border-bottom: 1px solid var(--line); font-size: 13px; }
.drift-row:last-child { border-bottom: 0; }
.drift-row span.files { flex: 1; word-break: break-all; }
.tidy-row { display: flex; gap: 10px; align-items: flex-start; padding: 9px 14px; border-bottom: 1px solid var(--line); font-size: 13px; }
.tidy-row:last-child { border-bottom: 0; }
.tidy-row .lab { flex: 1; word-break: break-all; }
.list .drift-row .btn, .list .tidy-row .btn { display: inline-block; width: auto; flex: none; border: 1px solid var(--line); padding: 5px 12px; }
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
      <label title="Also list files that don't exist yet, so you can create them"><input type="checkbox" id="show-missing"> Show missing files</label>
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
  return [it.label, it.path, k.title, k.short, g.title].join(" ").toLowerCase().includes(q);
}
function countFiles(g) { let n = 0; g.items.forEach(it => { if (it.type === "file") n += it.exists ? 1 : 0; else n += it.children.length; }); return n; }

function renderNav() {
  const q = $("#search").value.trim().toLowerCase();
  const nav = $("#nav");
  nav.innerHTML = "";
  const addSection = (title, extra) => { const d = document.createElement("div"); d.className = "sec-title"; d.innerHTML = `<span>${esc(title)}</span>`; if (extra) d.appendChild(extra); nav.appendChild(d); };

  if (!q) {
    const att = attentionItems().length;
    nav.appendChild(rowEl({ label: "Home", cls: "tool", icon: "⌂", tag: att ? `${att} to look at` : "", onclick: () => goHome(), selKey: "home" }));
    nav.appendChild(rowEl({ label: "Best-practice benchmark", cls: "tool", icon: "✦", tag: GUIDE && GUIDE.behind.length ? `${GUIDE.behind.length} to review` : "", onclick: () => openGuidance(), selKey: "guidance", title: "The official pages reviews are judged against, and when they last changed" }));
    nav.appendChild(rowEl({ label: "Backups", cls: "tool", icon: "↺", onclick: () => openAllBackups(), selKey: "backups", title: "Every saved version of every file" }));
    if (TIDY && (TIDY.strays.length || TIDY.archived.length)) nav.appendChild(rowEl({ label: "Tidy up and deleted files", cls: "tool", icon: "⌫", tag: TIDY.strays.length ? String(TIDY.strays.length) : "", onclick: () => openTidy(), selKey: "tidy" }));
  }

  const g0 = tree.groups.find(g => g.id === "global");
  addSection("Every project");
  renderGroup(nav, g0, q);

  const addBtn = document.createElement("button");
  addBtn.className = "mini"; addBtn.textContent = "+ Add folder"; addBtn.title = "Add a folder you haven't opened Claude Code in yet"; addBtn.onclick = addFolder;
  addSection(`Projects (${tree.projects.length})`, addBtn);
  if (!tree.projects.length) { const p = document.createElement("div"); p.className = "grp-sub"; p.style.paddingLeft = "8px"; p.textContent = "No projects yet. Open a folder in Claude Code, or use + Add folder."; nav.appendChild(p); }
  tree.projects.forEach(g => renderGroup(nav, g, q));
  if (!q && tree.hiddenProjects.length) nav.appendChild(rowEl({ label: `Hidden projects (${tree.hiddenProjects.length})`, cls: "tool faint", icon: "◌", onclick: () => openHidden(), selKey: "hidden" }));

  addSection("Apps and organisation");
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
  const head = document.createElement("div");
  head.className = "grp-head" + (sel === "g:" + g.id ? " sel" : "");
  head.dataset.key = "g:" + g.id;
  const srcBadge = g.source === "added" ? `<span class="badge">added</span>` : "";
  head.innerHTML = `<button class="caret" title="Show or hide its files" aria-label="Show or hide ${esc(g.title)} files">▼</button><button class="t" title="${esc(g.subtitle)}">${esc(g.title)}</button>${srcBadge}<span class="n" title="Files that exist">${countFiles(g)}</span>`;
  const toggle = open => { const c = open === undefined ? !wrap.classList.contains("closed") : !open; S.closed[g.id] = c; persist(); wrap.classList.toggle("closed", c); };
  $(".caret", head).onclick = () => toggle();
  $(".t", head).onclick = () => { toggle(true); openGroup(g); };
  wrap.appendChild(head);
  const body = document.createElement("div");
  body.className = "grp-body";
  vis.forEach(it => {
    if (it.type === "file") body.appendChild(fileRow(it));
    else {
      const k = tree.kinds[it.kind];
      body.appendChild(rowEl({ label: k.short || it.label, sub: it.label, cls: "folder", tag: it.children.length ? String(it.children.length) : (it.canCreate ? "+ new" : ""), fold: true, onclick: () => openFolder(it), selKey: "f:" + it.group + ":" + it.kind, title: tilde(it.path) }));
      (it._kids || []).forEach(c => body.appendChild(fileRow(c, true)));
    }
  });
  wrap.appendChild(body);
  nav.appendChild(wrap);
}

const isFileName = s => /[./]/.test(s) && !/\s/.test(s);  // "settings.json" yes; "Auto memory: MEMORY.md" no
function rowEl({ label, sub = "", cls = "", tag = "", roDot = false, fold = false, icon = "", onclick, selKey, title }) {
  const b = document.createElement("button");
  b.className = "row " + cls + (sel === selKey ? " sel" : "");
  b.dataset.key = selKey;
  if (title) b.title = title;
  const lead = icon ? `<span class="tool-ico">${esc(icon)}</span>` : fold ? `<span class="fold-ico">▸</span>` : `<span class="dot${roDot ? " ro" : ""}"></span>`;
  const subHtml = sub && sub !== label && isFileName(sub) ? `<span class="fname">${esc(sub)}</span>` : "";
  b.innerHTML = lead + `<span class="lbl">${esc(label)}${subHtml}</span>` + (tag ? `<span class="tag">${esc(tag)}</span>` : "");
  b.onclick = onclick;
  return b;
}
function fileRow(it, child = false) {
  const k = tree.kinds[it.kind];
  const label = child ? it.label : (k.short || it.label);
  return rowEl({ label, sub: child ? "" : it.label, cls: (it.exists ? "" : "missing ") + (child ? "child" : ""), tag: it.exists ? (it.readonly ? "read-only" : "") : (it.creatable ? "+ add" : ""),
    roDot: it.readonly, onclick: () => openFile(it), selKey: "p:" + it.path, title: tilde(it.path) });
}
function markSel(key) { sel = key; document.querySelectorAll("#nav [data-key]").forEach(r => r.classList.toggle("sel", r.dataset.key === key)); }

/* ---------------- Main pane ---------------- */
function groupOf(it) { return [...tree.groups, ...tree.projects].find(g => g.id === it.group); }
function factsHtml(k) {
  return `<div class="facts">
    <div class="fact"><h4>What it's for</h4><p>${md(k.role)}</p></div>
    <div class="fact"><h4>When it loads</h4><p>${md(k.loads)}</p></div>
    <div class="fact"><h4>Shared or personal</h4><p>${md(k.share)}</p><p style="margin-top:6px"><a href="${esc(docUrl(k.docs))}" target="_blank" rel="noopener">Docs ↗</a></p></div>
    <div class="fact wide"><h4>Why you'd want it</h4><p>${md(k.why)}</p></div>
  </div>`;
}
function aboutHtml(k, open) {
  return `<details class="about"${open ? " open" : ""}><summary>What this is for, when it loads, and why you'd want it</summary>${factsHtml(k)}</details>`;
}
function missingHtml(k, g, what) {
  const where = g.scope === "project" ? `<b>${esc(g.title)}</b> doesn't have ${what} yet.` : `You don't have ${what} yet.`;
  return `<div class="why-missing"><h4>Why it could help</h4><p>${where} ${md(k.missing)}</p></div>`;
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
  const head = `<div class="crumbs">${esc(g.title)} › ${esc(k.short || it.label)}</div><h2>${esc(k.title)}</h2>${pathLine(it.path)}`;
  if (!it.exists) {
    c.innerHTML = head + missingHtml(k, g, `<span class="mono">${esc(it.label)}</span>`) + `<div class="empty-card">
      ${it.creatable ? `<p style="margin-top:0"><button class="btn primary" id="create">Create ${esc(it.label)}</button> <span class="rv-meta">It starts with this template, which you can then edit:</span></p><pre>${esc(fillTpl(k.template, g, ""))}</pre>`
        : `<b>This file doesn't exist yet.</b><p>${md(k.create_hint || (k.readonly ? (k.readonly_reason || "This file can't be created here.") : "This file is created by Claude Code itself."))}</p>`}
    </div>` + `<div style="margin-top:14px">${aboutHtml(k, true)}</div>`;
    wirePathButtons(c);
    const b = $("#create", c);
    if (b) b.onclick = () => doCreate(g.id, it.kind, null);
    return;
  }
  const fd = driftFor([it.path]);
  const driftNote = fd.length ? fd.map((p, i) => `<div class="flag">Looks like a copy of <b class="mono">${esc(p.a === it.path ? p.b_label : p.a_label)}</b> (${Math.round(p.ratio * 100)}% the same) but they differ. <button class="mini" data-cmp="${i}">Compare</button></div>`).join("") : "";
  const gb = GUIDE ? GUIDE.behind.find(b => b.path === it.path) : null;
  const guideNote = gb ? `<div class="flag">Anthropic changed its guidance (${esc(gb.pages.join(", "))}) on ${esc(fmtTime(gb.latest))}, after you last edited this. <button class="mini" id="gb-what">What changed</button> Then run <b>✦ Review</b> below.</div>` : "";
  c.innerHTML = head + driftNote + guideNote + aboutHtml(k, false) + `<div class="editor-wrap"><div class="toolbar"><span class="status" id="st">Loading…</span><span id="lines" style="color:var(--faint)"></span><span class="spacer"></span><button class="btn" id="hist-btn" title="Earlier versions of this file">History</button>${tree.notReviewed.includes(it.kind) ? "" : `<button class="rv-btn" id="rv-file" title="Check this file against Anthropic's official guidance">✦ Review</button>`}${it.readonly ? "" : `<button class="btn danger" id="del-btn" title="Move this file to DotClaude's archive">Delete</button>`}<button class="btn" id="revert" disabled>Revert</button><button class="btn primary" id="save" disabled>Save  ⌘S</button></div><div id="msgs"></div><textarea id="ed" spellcheck="false"></textarea></div>`;
  wirePathButtons(c);
  wireDrift(c, fd);
  const gw = $("#gb-what", c); if (gw) gw.onclick = () => openGuidance();
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
  const db = $("#del-btn"); if (db) db.onclick = () => deleteFile(it);
  api("GET", "/api/backups?path=" + encodeURIComponent(it.path)).then(b => {
    const hb = $("#hist-btn"); if (hb && cur && cur.item.path === it.path) hb.textContent = `History (${b.count})`;
  }).catch(() => {});
  $("#revert").onclick = () => { ed.value = cur.original; cur.dirty = false; updateBar(); };
  updateBar();
}

async function deleteFile(it) {
  if (!cur || cur.item.path !== it.path) return;
  const unsaved = cur.dirty ? " Your unsaved changes will be lost." : "";
  const a = await ask({ title: `Delete ${it.label}?`, text: `<code>${esc(tilde(it.path))}</code> moves to DotClaude's archive, so Claude Code stops reading it. You can put it back from <b>Tidy up and deleted files</b> in the sidebar.${unsaved}`, buttons: [{ label: "Cancel", value: null }, { label: "Delete", value: "go", primary: true }] });
  if (!a || a.value !== "go") return;
  try {
    await api("POST", "/api/delete", { path: it.path, mtime: cur.mtime });
    cur.dirty = false; cur = null;
    toast(`Deleted ${it.label}. Put it back from Tidy up and deleted files.`, 4000);
    await load(); welcome();
  } catch (e) { toast(e.message, 4500); }
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
    if (cur.fromRewrite) { RV_STATE[cur.fromRewrite] = "edited"; cur.fromRewrite = null; }
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
  c.innerHTML = `<div class="crumbs">${esc(g.title)} › ${esc(k.short || it.label)}</div><h2>${esc(k.title)}</h2>${pathLine(it.path)}${it.children.length ? "" : missingHtml(k, g, `anything in <span class="mono">${esc(it.label)}</span>`)}${aboutHtml(k, !it.children.length)}
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
  const canHide = g.scope === "project" && g.source !== "added";
  const sugg = suggestions(g);
  const other = missing.filter(it => !sugg.includes(it) && it.creatable);
  const entries = g.items.filter(it => it.type === "file" ? it.exists : it.children.length);
  const sections = SECTION_ORDER.map(s => [s, entries.filter(it => (tree.kinds[it.kind].section || "Settings and tools") === s)]).filter(([, a]) => a.length);
  c.innerHTML = `<div class="crumbs">${esc(g.scope === "project" ? "Project" : "Section")}</div><h2>${esc(g.title)}</h2>${g.path ? pathLine(g.path) : `<div class="pathline"><code>${esc(g.subtitle)}</code></div>`}
    <p class="lede">${md(desc)}</p>
    ${g.note ? `<div class="note">${md(g.note)}</div>` : ""}
    <div class="actions">
      ${exist.length ? `<button class="rv-btn" id="rv-group" title="Checks all these files together against Anthropic's official pages, read fresh each time">✦ Review ${g.scope === "project" ? "this project" : "these files"} against best practice</button>` : ""}
      ${canHide ? `<button class="btn" id="hide">Hide this project</button>` : ""}
      ${g.source === "added" ? `<button class="btn" id="rm">Remove from list</button>` : ""}
    </div>
    ${sugg.length ? `<h3 class="sec">Worth adding</h3><div class="sugg">${sugg.map((it, i) => `<div class="sugg-row"><div><b>${esc(tree.kinds[it.kind].short)}</b> <span class="fname">${esc(it.label)}</span><p>${md(tree.kinds[it.kind].missing)}</p></div><button class="btn primary" data-sugg="${i}">Add it</button></div>`).join("")}</div>` : ""}
    <div id="drift-g"></div>
    ${sections.map(([s, arr]) => `<h3 class="sec">${esc(s)}</h3><div class="list">${arr.map(it => entryHtml(it)).join("")}</div>`).join("") || (g.missing ? "" : `<p class="lede">No Claude files here yet.</p>`)}
    <div id="load-box"></div>
    ${other.length ? `<details class="more"><summary>Other files you could add (${other.length})</summary><div class="list">${other.map(it => entryHtml(it, true)).join("")}</div></details>` : ""}
    ${g.source === "claude" || g.source === "both" ? `<p class="rv-meta" style="margin-top:18px">Listed because you've opened Claude Code in this folder.</p>` : ""}`;
  if (g.path) wirePathButtons(c);
  c.querySelectorAll("[data-entry]").forEach(b => { const it = entryByKey(g, b.dataset.entry); if (it) b.onclick = () => it.type === "folder" ? openFolder(it) : openFile(it); });
  c.querySelectorAll("[data-sugg]").forEach(b => b.onclick = () => { const it = sugg[+b.dataset.sugg]; openFile(it); });
  if (g.scope === "global" || (g.scope === "project" && !g.missing)) renderLoad(g);
  const dp = driftFor(exist.map(i => i.path));
  if (dp.length) { const d = $("#drift-g", c); d.innerHTML = `<h3 class="sec">Drifted apart (${dp.length})</h3>` + driftListHtml(dp); wireDrift(d, dp); }
  const rvg = $("#rv-group", c); if (rvg) rvg.onclick = () => startReview({ group: g.id });
  const hb = $("#hide", c); if (hb) hb.onclick = () => hideProject(g);
  const rm = $("#rm", c);
  if (rm) rm.onclick = async () => { await api("POST", "/api/folders/remove", { path: g.path }); toast("Removed " + g.title + " from the list"); await load(); welcome(); };
}

const SECTION_ORDER = ["Instructions", "Settings and tools", "Skills and helpers", "Memory"];
function entryKey(it) { return it.type === "folder" ? "f:" + it.kind : "p:" + it.path; }
function entryByKey(g, key) { return g.items.find(it => entryKey(it) === key); }
function entryHtml(it, missing = false) {
  const k = tree.kinds[it.kind];
  const n = it.type === "folder" ? it.children.length : 0;
  const preview = n ? it.children.slice(0, 3).map(ch => ch.label.replace(/\/SKILL\.md$|\.md$/, "")).join(", ") + (n > 3 ? `, and ${n - 3} more` : "") : "";
  const dot = missing ? `<span class="dot hollow"></span>` : it.type === "folder" ? `<span class="count">${n}</span>` : `<span class="dot${it.readonly ? " ro" : ""}"></span>`;
  const right = missing ? `<span class="why-line">${md(k.why)}</span>` : `<span class="when">${esc(k.loads)}</span>`;
  return `<button data-entry="${esc(entryKey(it))}">${dot}<span class="ent"><span><b>${esc(k.short || it.label)}</b> ${isFileName(it.label) ? `<span class="fname">${esc(it.label)}</span>` : ""}${it.readonly ? ` <span class="badge">read-only</span>` : ""}</span>${preview ? `<span class="preview">${esc(preview)}</span>` : ""}</span>${right}</button>`;
}
/* Files genuinely worth adding: an instructions file where there's none, and shared settings. */
function suggestions(g) {
  if (g.missing || !["project", "global"].includes(g.scope)) return [];
  const has = kind => g.items.some(it => it.kind === kind && (it.type === "file" ? it.exists : it.children.length));
  const find = kind => g.items.find(it => it.kind === kind && it.type === "file" && !it.exists && it.creatable);
  const want = g.scope === "global"
    ? [["user_claude_md", !has("user_claude_md")], ["user_settings", !has("user_settings")]]
    : [["proj_claude_md", !has("proj_claude_md") && !has("proj_dot_claude_md") && !has("proj_agents_md")], ["proj_settings", !has("proj_settings")]];
  return want.filter(([, w]) => w).map(([k]) => find(k)).filter(Boolean);
}

async function hideProject(g) {
  const a = await ask({ title: `Hide ${g.title}?`, text: `It disappears from DotClaude only. No files change, and Claude Code works there as before. You can show it again from <b>Hidden projects</b> at the bottom of the project list.`, buttons: [{ label: "Cancel", value: null }, { label: "Hide", value: "go", primary: true }] });
  if (!a || a.value !== "go") return;
  try { await api("POST", "/api/projects/hide", { path: g.path }); toast(`Hid ${g.title}`); await load(); welcome(); }
  catch (e) { toast(e.message, 4500); }
}
async function openHidden() {
  if (!(await guardDirty())) return;
  cur = null; markSel("hidden");
  const c = $("#content");
  const h = tree.hiddenProjects;
  c.innerHTML = `<div class="crumbs">Projects</div><h2>Hidden projects</h2><p class="lede">These are left out of DotClaude's lists. Their files haven't changed, and Claude Code still uses them.</p>
    ${h.length ? `<div class="list">${h.map((p, i) => `<div class="tidy-row"><span class="lab"><b>${esc(p.title)}</b><br><span class="rv-meta">${esc(p.subtitle)}</span></span><button class="btn" data-show="${i}">Show again</button></div>`).join("")}</div>` : `<p class="lede">Nothing is hidden.</p>`}`;
  c.querySelectorAll("[data-show]").forEach(b => b.onclick = async () => {
    const p = h[+b.dataset.show];
    try { await api("POST", "/api/projects/unhide", { path: p.path }); toast(`${p.title} is back in the list`); await load(); openHidden(); }
    catch (e) { toast(e.message, 4500); }
  });
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
  cur = null; markSel("home");
  let files = 0;
  [...tree.groups, ...tree.projects].forEach(g => files += countFiles(g));
  const att = attentionItems();
  const loading = !DRIFT || !TIDY || !GUIDE;
  $("#content").innerHTML = `<div class="welcome"><section>
    <h2>DotClaude</h2>
    <p class="lede">Every file Claude Code reads on this Mac: ${files} file${files === 1 ? "" : "s"} across ${tree.projects.length} project${tree.projects.length === 1 ? "" : "s"}. Pick one on the left to read or edit it. Every save keeps a backup of the old version.</p>
    <h3 class="sec">Needs your attention</h3>
    ${att.length ? `<div class="list att">${att.map((a, i) => `<div class="att-row"><div class="att-text">${a.html}${a.detail ? `<details><summary>${esc(a.detailLabel)}</summary>${a.detail}</details>` : ""}</div>${a.btn ? `<button class="btn" data-att="${i}">${esc(a.btn)}</button>` : ""}</div>`).join("")}</div>`
      : `<p class="lede">${loading ? "Checking…" : "Nothing needs your attention."}</p>`}
    <details class="more"><summary>How Claude Code reads these files</summary>
    <div class="legend"><span><span class="dot"></span> exists</span><span><span class="dot ro"></span> read-only</span><span><span class="dot hollow"></span> missing (shown when <b>Show missing files</b> is ticked)</span></div>
    <h3 style="margin-top:18px">What Claude Code loads when a session starts</h3>
    <ol>
      <li><b>Organisation files</b> (if an administrator installed any)</li>
      <li><b>Your global</b> <code>~/.claude/CLAUDE.md</code> and <code>~/.claude/rules/</code></li>
      <li><b>CLAUDE.md files in parent folders</b> above the project</li>
      <li><b>The project's</b> <code>CLAUDE.md</code> / <code>.claude/CLAUDE.md</code>, plus your private <code>CLAUDE.local.md</code>. If there's none of these in the folder or any folder above it, <code>AGENTS.md</code> loads instead</li>
      <li><b>Project rules</b> in <code>.claude/rules/</code> (rules with <code>paths:</code> wait until matching files come up)</li>
      <li><b>Auto memory</b>: the first 200 lines of the project's <code>MEMORY.md</code></li>
      <li><b>Skills and subagents</b>: only their names and descriptions, until one is used</li>
    </ol>
    <p>Later, when it's needed: <b>subfolder CLAUDE.md files</b>, auto-memory topic files, and the full text of skills.</p>
    <h3 style="margin-top:22px">Which settings win</h3>
    <p>Organisation → command-line flags → <code>.claude/settings.local.json</code> → <code>.claude/settings.json</code> → <code>~/.claude/settings.json</code>. Lists such as permission rules are combined across all of them.</p>
    <p>Click any project's name on the left to see how much of this loads there, file by file.</p>
    <p style="color:var(--faint);font-size:12px;margin-top:18px">Based on the Claude Code docs: <a href="https://code.claude.com/docs/en/memory.md" target="_blank" rel="noopener">Memory</a>, <a href="https://code.claude.com/docs/en/settings.md" target="_blank" rel="noopener">Settings</a> and <a href="https://code.claude.com/docs/en/claude-directory.md" target="_blank" rel="noopener">The .claude directory</a></p>
    </details>
  </section></div>`;
  const c = $("#content");
  c.querySelectorAll("[data-att]").forEach(b => b.onclick = () => att[+b.dataset.att].go());
  c.querySelectorAll("[data-goto]").forEach(b => b.onclick = e => { e.preventDefault(); const g = tree.projects.find(p => p.id === b.dataset.goto); if (g) openGroup(g); });
  if (DRIFT && DRIFT.pairs.length) wireDrift(c, DRIFT.pairs);
}
async function goHome() { if (await guardDirty()) { if (cur) cur.dirty = false; welcome(); } }

/* Everything worth a look, in one list. Each item: html, optional button + action, optional expandable detail. */
function attentionItems() {
  if (!tree) return [];
  const out = [];
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  if (GUIDE && GUIDE.behind.length) out.push({ html: `<b>${plural(GUIDE.behind.length, "file was", "files were")} last edited before Anthropic changed its guidance.</b><span class="rv-meta">Worth a review against the new version.</span>`, btn: "See which", go: () => openGuidance() });
  if (DRIFT && DRIFT.pairs.length) out.push({ html: `<b>${plural(DRIFT.pairs.length, "pair of files has", "pairs of files have")} drifted apart.</b><span class="rv-meta">Each pair looks like one set of instructions kept under two names, such as CLAUDE.md and AGENTS.md, but they no longer match, so different tools get different instructions.</span>`, detailLabel: "Show them", detail: driftListHtml(DRIFT.pairs) });
  const bare = tree.projects.filter(g => suggestions(g).some(it => it.kind === "proj_claude_md"));
  if (bare.length) out.push({ html: `<b>${plural(bare.length, "project has", "projects have")} no instructions file.</b><span class="rv-meta">Claude starts every session there without knowing how to build or test: ${bare.map(g => `<a href="#" data-goto="${esc(g.id)}">${esc(g.title)}</a>`).join(", ")}.</span>` });
  if (TIDY && TIDY.strays.length) out.push({ html: `<b>${plural(TIDY.strays.length, "old copy is", "old copies are")} sitting next to Claude files.</b><span class="rv-meta">Hand-made backups like <code>settings.json.bak</code>. Claude Code ignores them.</span>`, btn: "Tidy up", go: () => openTidy() });
  tree.projects.filter(g => g.missing).forEach(g => out.push({ html: `<b>${esc(g.title)} no longer exists.</b><span class="rv-meta">${esc(g.subtitle)} has gone, but Claude Code still lists it.</span>`, btn: g.source === "added" ? "Remove" : "Hide", go: () => g.source === "added" ? api("POST", "/api/folders/remove", { path: g.path }).then(load).then(welcome) : hideProject(g) }));
  return out;
}

/* ---------------- What loads, drift, tidy ---------------- */
async function renderLoad(g) {
  const box = $("#load-box"); if (!box) return;
  const title = `<h4 style="margin:18px 0 4px">What loads at the start of every session${g.scope === "global" ? ", in every project" : " here"}</h4>`;
  box.innerHTML = title + `<div class="rv-meta">Measuring…</div>`;
  let L;
  try { L = await api("GET", "/api/load?group=" + encodeURIComponent(g.id)); } catch (e) { box.innerHTML = title + `<div class="rv-meta">${esc(e.message)}</div>`; return; }
  if ($("#load-box") !== box) return;  // user moved on
  const max = Math.max(1, ...L.entries.map(e => e.chars));
  box.innerHTML = title + `<div class="load-total"><b>≈ ${L.tokens.toLocaleString()} tokens</b><span class="rv-meta">${fmtSize(L.chars)} of text, before you type anything</span></div>
    ${L.warnings.map(w => `<div class="msg warn" style="border:0;padding:2px 0 6px">${md(w)}</div>`).join("")}
    <div class="list">${L.entries.map((e, i) => `<div class="load-row" data-i="${i}" style="${findItem(e.path) ? "cursor:pointer" : ""}"><span class="mono" style="word-break:break-all">${esc(e.label)}</span><span class="tok">≈ ${e.tokens.toLocaleString()}</span><span class="how">${esc(e.how)}${e.via ? ` · from ${esc(e.via)}` : ""}</span><span class="load-bar"><i style="width:${(100 * e.chars / max).toFixed(1)}%"></i></span></div>`).join("") || `<div style="padding:14px;color:var(--faint)">Nothing loads here.</div>`}</div>
    <p class="rv-meta" style="margin-top:6px">Estimate: ${esc(L.estimate)} Not counted: ${esc(L.not_counted)}</p>`;
  box.querySelectorAll(".load-row").forEach(r => { const it = findItem(L.entries[+r.dataset.i].path); if (it) r.onclick = () => openFile(it); });
}

async function openCompare(p) {
  if (!(await guardDirty())) return;
  cur = null; markSel("compare");
  const c = $("#content");
  c.innerHTML = `<div class="crumbs">Drift</div><h2>Compare two files</h2>
    <div class="note">These look like one set of instructions kept under two names, but they've drifted apart. Lines starting with <b>-</b> are only in the first file, lines starting with <b>+</b> only in the second. Bring the out-of-date one up to date, or keep a single copy: for CLAUDE.md and AGENTS.md, Anthropic's docs suggest a CLAUDE.md that just says <code>@AGENTS.md</code>.</div>
    <p><button class="btn" id="oa">Open ${esc(p.a_label)}</button> <button class="btn" id="ob">Open ${esc(p.b_label)}</button></p><div id="cmp">Loading…</div>`;
  $("#oa").onclick = () => { const it = findItem(p.a); if (it) openFile(it); };
  $("#ob").onclick = () => { const it = findItem(p.b); if (it) openFile(it); };
  try {
    const r = await api("GET", `/api/compare?a=${encodeURIComponent(p.a)}&b=${encodeURIComponent(p.b)}`);
    $("#cmp").innerHTML = r.same ? `<p>They're identical now.</p>` : `<pre class="diff" style="max-height:none">${diffHtml(r.diff)}</pre>`;
  } catch (e) { $("#cmp").innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`; }
}

async function openTidy() {
  if (!(await guardDirty())) return;
  cur = null; markSel("tidy");
  const c = $("#content");
  c.innerHTML = `<div class="crumbs">Tools</div><h2>Tidy up and deleted files</h2>
    <div class="note">Copies made by hand next to a Claude file, such as <code>settings.json.bak-…</code>. Claude Code ignores them, and DotClaude already keeps a backup every time you save. Moving them to the archive gets them out of the way. Nothing is deleted, and you can put any of them back.</div><div id="tidy">Loading…</div>`;
  let t;
  try { t = await api("GET", "/api/tidy"); } catch (e) { $("#tidy").textContent = e.message; return; }
  const box = $("#tidy");
  box.innerHTML = (t.strays.length ? `<div class="list">${t.strays.map((s, i) => `<label class="tidy-row"><input type="checkbox" data-i="${i}" ${s.referenced_in.length ? "" : "checked"}><span class="lab"><span class="mono">${esc(s.label)}</span><br><span class="rv-meta">Copy of ${esc(s.of)} · ${fmtSize(s.size)} · ${esc(fmtTime(s.mtime))}</span>${s.referenced_in.length ? `<br><span style="color:var(--warn);font-size:12px">Mentioned by name in ${esc(s.referenced_in.join(", "))}. Moving it would break that reference, so it isn't ticked.</span>` : ""}</span></label>`).join("")}</div>
      <p style="margin-top:10px"><button class="btn primary" id="arch">Move ticked files to the archive</button> <span class="rv-meta">They go to <code>${esc(t.dir)}</code>.</span></p>` : `<div class="note">No old copies found.</div>`)
    + (t.archived.length ? `<h4 style="margin:22px 0 8px">In the archive (${t.archived.length})</h4><div class="list">${t.archived.map((a, i) => `<div class="tidy-row"><span class="lab"><span class="mono">${esc(a.label)}</span><br><span class="rv-meta">${a.deleted ? "Deleted" : "Archived"} ${esc(fmtTime(a.time))}</span></span><button class="btn" data-put="${i}">Put back</button></div>`).join("")}</div>` : "");
  const arch = $("#arch", box);
  if (arch) arch.onclick = async () => {
    const paths = [...box.querySelectorAll("input[data-i]:checked")].map(x => t.strays[+x.dataset.i].path);
    if (!paths.length) { toast("Tick at least one file."); return; }
    const a = await ask({ title: `Move ${paths.length} file${paths.length === 1 ? "" : "s"} to the archive?`, text: `They move to <code>${esc(t.dir)}</code>. You can put each one back from this page.`, buttons: [{ label: "Cancel", value: null }, { label: "Move", value: "go", primary: true }] });
    if (!a || a.value !== "go") return;
    try { const r = await api("POST", "/api/tidy/archive", { paths }); toast(`Moved ${r.moved} to the archive`); await load(); openTidy(); }
    catch (e) { toast(e.message, 4500); }
  };
  box.querySelectorAll("[data-put]").forEach(b => b.onclick = async () => {
    const a = t.archived[+b.dataset.put];
    try { await api("POST", "/api/tidy/restore", { id: a.id }); toast("Put back " + a.label); await load(); openTidy(); }
    catch (e) { toast(e.message, 4500); }
  });
}

/* ---------------- Best-practice benchmark ---------------- */
async function openGuidance(showKey) {
  if (!(await guardDirty())) return;
  cur = null; markSel("guidance");
  const c = $("#content");
  c.innerHTML = `<div class="crumbs">This Mac</div><h2>Best-practice benchmark</h2>
    <div class="note"><b>What your files are judged against.</b> When you click <b>✦ Review against best practice</b>, DotClaude asks Claude Code to read the official Anthropic pages below, fresh at that moment, and to judge your files only against them. Every finding has to cite one of these pages; advice that no page supports is dropped. A review only uses the pages that apply to the files being reviewed (the "Used for" column).</div>
    <div id="gd">Loading…</div>`;
  let g;
  try { g = await api("GET", "/api/guidance"); GUIDE = g; } catch (e) { $("#gd").textContent = e.message; return; }
  renderGuidance(g, showKey);
}

function renderGuidance(g, showKey) {
  const box = $("#gd"); if (!box) return;
  const n = g.notify;
  const pageRows = g.pages.map(p => `<div class="tidy-row"><span class="lab"><a href="${esc(p.url)}" target="_blank" rel="noopener"><b>${esc(p.title)}</b> ↗</a><br>
      <span class="rv-meta">Used for: ${esc(p.used_for.join(", ") || "every review")}</span><br>
      <span class="rv-meta">${p.error ? `<span style="color:var(--err)">Last check failed: ${esc(p.error)}</span>` : p.changed ? `<b style="color:var(--warn)">Changed ${esc(fmtTime(p.changed))}</b>` : p.first_seen ? `No change since DotClaude started tracking it (${esc(fmtTime(p.first_seen))})` : "Not checked yet"}</span></span>
      ${p.has_diff ? `<button class="btn" data-diff="${esc(p.key)}">What changed</button>` : ""}</div>`).join("");
  box.innerHTML = `
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px">
      <button class="btn" id="gcheck">Check for updates now</button>
      <span class="rv-meta">${g.last_check ? "Last checked " + esc(fmtTime(g.last_check)) : "Never checked"}. DotClaude can only see changes from its first check onward.</span>
    </div>
    <div class="list" style="margin-bottom:14px"><label class="tidy-row" style="cursor:pointer"><input type="checkbox" id="gnotify" ${n.on ? "checked" : ""}><span class="lab"><b>Notify me when these pages change</b><br><span class="rv-meta">A Mac notification after a daily check at ${esc(n.time)}, even when DotClaude isn't open. Log: <code>${esc(n.log)}</code></span></span></label></div>
    ${g.behind.length ? `<h4 style="margin:18px 0 8px">Files last edited before the guidance changed (${g.behind.length})</h4><div class="list">${g.behind.map((b, i) => `<div class="tidy-row"><span class="lab"><span class="mono">${esc(b.label)}</span><br><span class="rv-meta">Edited ${esc(fmtTime(b.edited))} · ${esc(b.pages.join(", "))} changed ${esc(fmtTime(b.latest))}</span></span><button class="btn" data-open="${i}">Open</button></div>`).join("")}</div>` : ""}
    <h4 style="margin:18px 0 8px">The official pages (${g.pages.length})</h4><div class="list">${pageRows}</div>
    <div id="gdiff"></div>`;
  $("#gcheck").onclick = async () => {
    $("#gcheck").disabled = true; $("#gcheck").textContent = "Checking…";
    try { const r = await api("POST", "/api/guidance/check"); GUIDE = r; renderNav(); markSel("guidance"); renderGuidance(r);
      toast(r.changed.length ? `${r.changed.length} page${r.changed.length === 1 ? "" : "s"} changed` : r.failed.length ? `${r.failed.length} page(s) couldn't be loaded` : "No changes", 3500); }
    catch (e) { toast(e.message, 4000); $("#gcheck").disabled = false; $("#gcheck").textContent = "Check for updates now"; }
  };
  $("#gnotify").onchange = async e => {
    try { const r = await api("POST", "/api/notify", { on: e.target.checked }); toast(r.on ? "Notifications on: daily check at " + r.time : "Notifications off"); }
    catch (err) { e.target.checked = !e.target.checked; toast(err.message, 4500); }
  };
  box.querySelectorAll("[data-open]").forEach(b => b.onclick = () => { const it = findItem(g.behind[+b.dataset.open].path); if (it) openFile(it); });
  box.querySelectorAll("[data-diff]").forEach(b => b.onclick = () => showGuidanceDiff(b.dataset.diff));
  if (showKey) showGuidanceDiff(showKey);
}

async function showGuidanceDiff(key) {
  const d = $("#gdiff"); if (!d) return;
  d.innerHTML = "Loading…";
  try {
    const r = await api("GET", "/api/guidance/diff?key=" + encodeURIComponent(key));
    d.innerHTML = `<h4 style="margin:18px 0 8px">What changed: ${esc(r.title)}</h4><p class="rv-meta">Lines starting with - were removed, + were added, compared with the copy from the previous check.</p><pre class="diff">${diffHtml(r.diff)}</pre>`;
    d.scrollIntoView({ behavior: "smooth" });
  } catch (e) { d.innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`; }
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
          st === "edited" ? `<span style="color:var(--ok);font-weight:600">✓ Saved with your edits. The old version is in ~/.dotclaude/backups/</span> <button class="btn" data-open="${esc(w.file)}">Open the saved file</button>` :
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
  $("#ed").value = w.content; cur.dirty = w.content !== cur.original; cur.fromRewrite = RV.id + ":" + i; updateBar();
  $("#msgs").insertAdjacentHTML("afterbegin", `<div class="msg warn">The suggested rewrite is loaded but not saved yet. Edit it if you want, then Save (the current version gets backed up first), or Revert to throw it away.</div>`);
}

let DRIFT = null, TIDY = null, GUIDE = null;  // filled in the background after each scan
async function load() {
  tree = await api("GET", "/api/tree"); renderNav();
  DRIFT = null; TIDY = null;
  api("GET", "/api/guidance").then(g => { GUIDE = g; onExtras(); }).catch(() => { GUIDE = { behind: [] }; onExtras(); });
  api("GET", "/api/drift").then(d => { DRIFT = d; onExtras(); }).catch(() => { DRIFT = { pairs: [] }; onExtras(); });
  api("GET", "/api/tidy").then(t => { TIDY = t; onExtras(); }).catch(() => { TIDY = { strays: [], archived: [] }; onExtras(); });
}
function onExtras() { renderNav(); if (!cur && sel === "home" && $(".welcome")) welcome(); }
function driftFor(paths) { return DRIFT ? DRIFT.pairs.filter(p => paths.includes(p.a) || paths.includes(p.b)) : []; }
function driftListHtml(pairs) {
  return `<div class="list">${pairs.map((p, i) => `<div class="drift-row"><span class="files"><span class="mono">${esc(p.a_label)}</span> and <span class="mono">${esc(p.b_label)}</span><br><span class="rv-meta">${Math.round(p.ratio * 100)}% the same · ${p.changed_lines} line${p.changed_lines === 1 ? "" : "s"} differ</span></span><button class="btn" data-cmp="${i}">Compare</button></div>`).join("")}</div>`;
}
function wireDrift(root, pairs) { root.querySelectorAll("[data-cmp]").forEach(b => b.onclick = () => openCompare(pairs[+b.dataset.cmp])); }

$("#show-missing").checked = S.showMissing;
$("#show-missing").onchange = e => { S.showMissing = e.target.checked; persist(); renderNav(); };
$("#search").oninput = renderNav;
$("#refresh").onclick = async () => { await load(); toast("Rescanned"); };
$("#stop").onclick = async () => {
  if (RV && RV.status === "running") { toast("A review is running. Cancel it or wait before stopping."); return; }
  if (!(await guardDirty())) return;
  const a = await ask({ title: "Stop DotClaude?", text: "This closes the app. Open it again from the Dock or from Terminal.", buttons: [{ label: "Cancel", value: null }, { label: "Stop", value: "stop", primary: true }] });
  if (!a || a.value !== "stop") return;
  try { await api("POST", "/api/shutdown"); } catch (e) {}
  if (cur) cur.dirty = false;
  document.body.innerHTML = `<div style="margin:auto;text-align:center;color:var(--muted)"><h2 style="color:var(--ink)">DotClaude has stopped</h2><p>You can close this tab.</p></div>`;
};
$("#home-link").onclick = goHome;
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
