#!/usr/bin/env python3
"""Pass the ticket port to the listening command, inside the package manager's normal script environment."""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Match whole shell words before interpreting options. Spans keep the original script's quoting and operators intact.
SHELL_WORD = r"""(?:\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*"|[^\s\\'"<>;&|()])+"""
SHELL_PART = re.compile(rf"(?P<comment>\#[^\n]*)|(?P<redirect>[0-9]*(?:<<-?|>>|[<>][&>]|>\||[<>]))"
                        rf"|(?P<separator>[;&|()\n]+)|(?P<word>{SHELL_WORD})")
PREFIX = r"""\s*(?:(?:exec|env|cross-env|command)\s+|\w+=(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s+)*"""
MANAGER = re.compile(PREFIX + r"(?P<pm>(?:[\w./-]+/)?(?:npm|pnpm|yarn))(?=\s)")
SERVER = re.compile(PREFIX + r"(?:(?:npx|(?:npm|pnpm|yarn)\s+exec(?:\s+--)?)\s+)?(?:[\w./-]+/)?"
                    r"(?P<cli>vite(?!\s+(?:build|optimize)\b)(?:\s+(?:dev|serve|preview)\b)?"
                    r"|remix\s+vite:dev|react-router\s+dev|next\s+(?:dev|start))(?=\s|$)")
VITE_BUILD = re.compile(r"(?:\s+(?:--mode|-m|--config|-c|--logLevel|-l)(?:\s+|=)\S+)*\s+(?:build|optimize)\b")


class PortOption(NamedTuple):
    start: int
    value_start: int
    end: int
    value: str
    prefix: str = ""


def _commands(body):
    start = 0
    for part in SHELL_PART.finditer(body):
        if part.lastgroup in ("separator", "comment"):
            yield start, body[start:part.start()]
            start = part.end()
    yield start, body[start:]


def _arguments(command, start=0, *, package=False):
    redirect = False
    for part in SHELL_PART.finditer(command, start):
        if part.lastgroup == "redirect":
            redirect = True
        elif part.lastgroup == "word":
            if redirect:
                redirect = False
                continue
            value = shlex.split(part[0])[0]
            if value == "--":
                if not package:
                    break
                package = False  # npm's separator introduces the script's options
            else:
                yield part, value


def _port_options(arguments):
    arguments = iter(arguments)
    for word, value in arguments:
        flag, equals, inline = value.partition("=")
        if flag not in ("-p", "--port"):
            continue
        if equals:
            yield PortOption(word.start(), word.start(), word.end(), inline, flag + "=")
        else:
            operand = next(arguments, None)
            if operand:
                token, port = operand
                yield PortOption(word.start(), token.start(), token.end(), port)


def _literal_port(options):
    return next((option for option in options if re.fullmatch(r"[0-9]{2,5}", option.value)), None)


def declared_port(body: str) -> int | None:
    for _, command in _commands(body):
        flag = _literal_port(_port_options(_arguments(command, package=bool(MANAGER.match(command)))))
        if flag:
            return int(flag.value)
    return None


def bind_port(body: str, port: str, forwarded: str = "") -> str:
    """Edit a listener's arguments in place. Package invocations bind their own script after resolving its directory."""
    edits = []
    servers = []
    fixed = None
    for start, command in _commands(body):
        pm = MANAGER.match(command)
        if pm:
            pos = start + pm.start("pm")
            edits.append((pos, pos, shlex.quote(__file__) + " "))
        server = SERVER.match(command)
        arguments = list(_arguments(command, server.end("cli") if server else 0, package=bool(pm)))
        options = list(_port_options(arguments))
        if server and not (server["cli"] == "vite" and VITE_BUILD.match(command, server.end("cli"))):
            servers.append((start, server, arguments, options[0] if options else None))
        if fixed is None and "{port}" not in body:
            flag = _literal_port(options)
            if flag:
                fixed = (start + flag.value_start, start + flag.end, flag.prefix + port)

    if servers:
        # Prefer the listener with the declared port. For a build followed by preview, bind the preview.
        offset, server, arguments, flag = next((s for s in servers if s[3]), servers[-1])
        pos = offset + server.end("cli")
        # npm appends forwarded arguments after the entire script, which may end in 'wait'.
        # Keep extra server options with the listener, replacing an explicit forwarded port too.
        for option in reversed(list(_port_options(_arguments(forwarded)))):
            forwarded = forwarded[:option.start] + forwarded[option.end:]
        flags = forwarded + ("" if flag else " --port " + port)
        forwarded = ""
        if not server["cli"].startswith("next") and not any(
                word.partition("=")[0] == "--strictPort" for _, word in arguments):
            flags += " --strictPort"
        edits.append((pos, pos, flags))
        fixed = (offset + flag.value_start, offset + flag.end, flag.prefix + port) if flag else None
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
