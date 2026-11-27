#!/usr/bin/env python3
"""Pass the ticket port to the listening command, inside the package manager's normal script environment."""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

PORT_FLAG = re.compile(r"(?:^|\s)(?:-p|--port)[ =](\d{2,5})\b")
SERVER_PORT = re.compile(r"""(?:^|\s)(?:-p|--port)(?:\s+|=)("[^"]*"|'[^']*'|[^\s<>;&|()]+)""")
# Keep quoted shell words intact when locating the simple commands in a script.
SHELL_PART = re.compile(r"""\\.|'[^']*'|"(?:\\.|[^"\\])*"|[;&|()\n]+""")
PREFIX = r"""\s*(?:(?:exec|env|cross-env|command)\s+|\w+=(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s+)*"""
MANAGER = re.compile(PREFIX + r"(?P<pm>(?:[\w./-]+/)?(?:npm|pnpm|yarn))(?=\s)")
SERVER = re.compile(PREFIX + r"(?:(?:npx|(?:npm|pnpm|yarn)\s+exec(?:\s+--)?)\s+)?(?:[\w./-]+/)?"
                    r"(?P<cli>vite(?!\s+(?:build|optimize)\b)(?:\s+(?:dev|serve|preview)\b)?"
                    r"|remix\s+vite:dev|react-router\s+dev|next\s+(?:dev|start))(?=\s|$)")
VITE_BUILD = re.compile(r"(?:\s+(?:--mode|-m|--config|-c|--logLevel|-l)(?:\s+|=)\S+)*\s+(?:build|optimize)\b")


def bind_port(body: str, port: str, forwarded: str = "") -> str:
    """Edit a listener's arguments in place. Package invocations bind their own script after resolving its directory."""
    edits = []
    servers = []
    fixed = None
    start = 0
    ends = [(m.start(), m.end()) for m in SHELL_PART.finditer(body) if m[0][0] in ";&|()\n"]
    for end, following in [*ends, (len(body), len(body))]:
        command = body[start:end]
        pm = MANAGER.match(command)
        if pm:
            pos = start + pm.start("pm")
            edits.append((pos, pos, shlex.quote(__file__) + " "))
        server = SERVER.match(command)
        flag = PORT_FLAG.search(command)
        if server and not (server["cli"] == "vite" and VITE_BUILD.match(command, server.end("cli"))):
            servers.append((start, command, server, SERVER_PORT.search(command, server.end("cli"))))
        if flag and fixed is None and "{port}" not in body:
            fixed = (start + flag.start(1), start + flag.end(1), port)
        start = following

    if servers:
        # Prefer the listener with the declared port. For a build followed by preview, bind the preview.
        offset, command, server, flag = next((s for s in servers if s[3]), servers[-1])
        pos = offset + server.end("cli")
        # npm appends forwarded arguments after the entire script, which may end in 'wait'.
        # Keep extra server options with the listener, replacing an explicit forwarded port too.
        flags = SERVER_PORT.sub("", forwarded) + ("" if flag else " --port " + port)
        forwarded = ""
        if not server["cli"].startswith("next") and "--strictPort" not in command:
            flags += " --strictPort"
        edits.append((pos, pos, flags))
        fixed = (offset + flag.start(1), offset + flag.end(1), port) if flag else None
    if fixed:
        edits.append(fixed)
    for begin, end, replacement in sorted(edits, reverse=True):
        body = body[:begin] + replacement + body[end:]
    return body + forwarded


def main():
    env = os.environ
    if sys.argv[1] == "-c":  # npm-compatible script-shell callback, including pre/post lifecycle hooks
        args = sys.argv[1:]
        if env.get("npm_lifecycle_event") == env["TP_DEV_SCRIPT"]:
            body = env.get("npm_lifecycle_script") or args[1]
            if not (args[1] == body or args[1].startswith(body + " ")):
                body = args[1]
            args[1] = bind_port(body, env["TP_DEV_PORT"], args[1][len(body):])
        shell = env["TP_DEV_SHELL"]
        for key, original in json.loads(env.pop("TP_DEV_SHELL_CONFIG")).items():
            if original is None:
                env.pop(key, None)
            else:
                env[key] = original
        os.execvp(shell, [shell, *args])

    manager, *args = sys.argv[1:]
    name = Path(manager).name
    run_index = next((i for i, arg in enumerate(args) if arg in ("run", "run-script")), None)
    if run_index is not None and run_index + 1 < len(args):
        script_index = run_index + 1
    elif args and not args[0].startswith("-") and (name != "npm" or args[0] == "start"):
        script_index = 0
    else:
        os.execvp(manager, [manager, *args])
    script = args[script_index]
    if name == "yarn" and not subprocess.check_output([manager, "--version"], text=True).startswith("1."):
        # Modern Yarn runs scripts in its portable shell and has no implicit pre/post hooks.
        prefix = args[:run_index] if run_index is not None else []
        cwd = Path(subprocess.check_output([manager, *prefix, "exec", "pwd"], text=True).strip()) if prefix else Path.cwd()
        body = json.loads((cwd / "package.json").read_text()).get("scripts", {}).get(script)
        if body is None:  # a built-in or a dependency's binary
            os.execvp(manager, [manager, *args])
        env["npm_lifecycle_event"] = script
        extra = args[script_index + 1:]
        if extra and not re.search(r"\$\{?[0-9@*]", body):
            body = bind_port(body, env["TP_DEV_PORT"], " " + shlex.join(extra))
            extra = []
        else:
            body = bind_port(body, env["TP_DEV_PORT"])
        os.execvp(manager, [manager, *prefix, "exec", body, *extra])

    options = ("npm_config_script_shell", "pnpm_config_script_shell")
    query = [manager, "config", "get", "script-shell"]
    if name == "npm":
        query.append("--workspaces=false")
    shell = subprocess.check_output(query, text=True).strip()
    env.update(TP_DEV_SCRIPT=script, TP_DEV_SHELL="/bin/sh" if shell in ("", "null", "undefined") else shell,
               TP_DEV_SHELL_CONFIG=json.dumps({key: env.get(key) for key in options}))
    for key in options:
        env[key] = __file__
    os.execvp(manager, [manager, *args])


if __name__ == "__main__":
    main()
