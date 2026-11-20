"""swarmctl login: start a sign-in on the worker in the background and print only what a person must do.

Run on the worker, as root (a bot does it over Tailscale SSH with sudo):
  sudo swarmctl login codex                 Codex: prints a link and a code. Opens nothing on the server.
  sudo swarmctl login github                GitHub for the runner user: prints a link and a code.
  sudo swarmctl login claude                Claude: prints a link. The browser then shows a code; give it back with --finish.
  sudo swarmctl login claude --finish       reads that code from standard input (never from the command line)
  sudo swarmctl login claude --token-stdin  saves a token you already have (from a secret form), read from stdin
  sudo swarmctl login NAME --status         where the sign-in is now
  sudo swarmctl login NAME --cancel         stop it

Each start prints lines like "URL: ...", "CODE: ...", "EXPIRES: ...", "NEXT: ..." (or one JSON object with --json),
so a bot can pass them on. Nothing else from the sign-in program is shown. A Claude token is saved straight into
/etc/agent-swarm/agent.env as CLAUDE_CODE_OAUTH_TOKEN (root-owned, readable by the agent group) and is never printed.

Which program, which user:
  codex   `codex login --device-auth`                       as the agent user
  claude  `claude setup-token` (needs a Pro, Max, Ultra, Team or Enterprise plan)   as the agent user
  github  `gh auth login --web`, then `gh auth setup-git`  as the swarm-runner user (never the agent user)
"""
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import fcntl

PROVIDERS = ("codex", "claude", "github")
STATE_DIR = os.environ.get("SWARM_LOGIN_DIR", "/var/lib/agent-swarm/login")
AGENT_ENV = os.environ.get("SWARM_AGENT_ENV_FILE", "/etc/agent-swarm/agent.env")
AGENT_USER = os.environ.get("SWARM_AGENT_USER_NAME", "agent")
RUNNER_USER = os.environ.get("SWARM_RUNNER_USER_NAME", "swarm-runner")
LIFETIME = int(os.environ.get("SWARM_LOGIN_SECONDS", "1200"))   # a sign-in is given up after 20 minutes (the Codex code lasts 15)
WAIT_FOR_PROMPT = int(os.environ.get("SWARM_LOGIN_PROMPT_WAIT", "40"))

ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=!]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\x1b[()][A-Za-z0-9]")
OSC8_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9]+-[A-Za-z0-9_-]{24,}")


def strip_ansi(text):
    """Remove colors, cursor moves and hyperlink escapes; keep the words. Carriage returns become newlines."""
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def parse_codex(text):
    """From `codex login --device-auth` output: {'url', 'code', 'expires_minutes'} with whatever is known so far."""
    t = strip_ansi(text)
    out = {}
    m = re.search(r"https://auth\.openai\.com/[^\s]+", t)
    if m:
        out["url"] = m.group(0)
    m = re.search(r"one-time code[^\n]*\n\s*([A-Z0-9]{3,8}-[A-Z0-9]{3,8})\b", t)
    if m:
        out["code"] = m.group(1)
    m = re.search(r"expires in (\d+) minutes", t)
    if m:
        out["expires_minutes"] = int(m.group(1))
    return out


def parse_github(text):
    """From `gh auth login --web` output (the code line starts with 'First copy your one-time code:')."""
    t = strip_ansi(text)
    out = {}
    m = re.search(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})\b", t)
    if m:
        out["code"] = m.group(1)
    m = re.search(r"(?:Open this URL to continue in your web browser:|open\s)\s*(https://github\.com/login/device)\b", t)
    if m:
        out["url"] = m.group(1)
    elif "code" in out and "github.com/login/device" in t:
        out["url"] = "https://github.com/login/device"
    return out


def parse_claude(text):
    """From `claude setup-token` output: the sign-in link. Prefers the hyperlink target, falls back to the visible text."""
    m = OSC8_RE.search(text)
    if m and "oauth/authorize" in m.group(1):
        return {"url": m.group(1)}
    t = strip_ansi(text)
    m = re.search(r"https://claude\.(?:com|ai)/[^\s]*oauth/authorize\?[^\s]+", t)
    return {"url": m.group(0)} if m else {}


def find_claude_token(text):
    """The long-lived token `claude setup-token` prints after the code is accepted, or None."""
    m = TOKEN_RE.search(strip_ansi(text))
    return m.group(0) if m else None


PARSERS = {"codex": parse_codex, "github": parse_github, "claude": parse_claude}


def needs(provider, info):
    """Is the information a person needs complete?"""
    if provider == "claude":
        return bool(info.get("url"))
    return bool(info.get("url") and info.get("code"))


def set_env_value(path, name, value, group=None):
    """Put NAME=VALUE in an env file (replacing an older line), mode 640, atomically. Never prints the value."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or "\n" in value or "\r" in value:
        raise ValueError("bad env entry")
    lines = []
    if os.path.exists(path):
        with open(path) as fh:
            lines = [ln.rstrip("\n") for ln in fh if not ln.startswith(name + "=")]
    lines.append(f"{name}={value}")
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o640)
    if group and os.getuid() == 0:
        import grp
        os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
    os.replace(tmp, path)


# ---------- state files ----------

def sdir(provider):
    return os.path.join(STATE_DIR, provider)


def read_status(provider):
    try:
        with open(os.path.join(sdir(provider), "status.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_status(provider, **fields):
    d = sdir(provider)
    cur = read_status(provider) or {}
    cur.update(fields, updated=int(time.time()))
    tmp = os.path.join(d, "status.json.new")
    with open(tmp, "w") as fh:
        json.dump(cur, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, os.path.join(d, "status.json"))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------- what to run ----------

def plan(provider):
    """(argv, user, home, use_pty) for a provider. Tests can override with SWARM_LOGIN_FAKE_<NAME>=json argv."""
    fake = os.environ.get("SWARM_LOGIN_FAKE_" + provider.upper())
    if fake:
        return json.loads(fake), os.environ.get("SWARM_LOGIN_FAKE_USER", ""), os.environ.get("HOME", "/tmp"), provider == "claude"
    if provider == "codex":
        return ["codex", "login", "--device-auth"], AGENT_USER, None, False
    if provider == "claude":
        return ["claude", "setup-token"], AGENT_USER, None, True
    return ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", "--skip-ssh-key"], RUNNER_USER, None, False


def child_env(home, provider):
    # A clean environment: no leftover tokens, and no browser to open on a server.
    return {"HOME": home, "USER": os.path.basename(home), "LOGNAME": os.path.basename(home), "LANG": "C.UTF-8",
            "TERM": "xterm-256color", "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", "BROWSER": "false"}


def drop_to(user):
    import pwd
    pw = pwd.getpwnam(user)
    os.initgroups(user, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    return pw.pw_dir


def spawn(provider):
    """Start the sign-in program. Returns (pid, master_fd or None, read_fd). Runs inside the background process."""
    argv, user, home, use_pty = plan(provider)
    if user and os.getuid() == 0:
        import pwd
        home = pwd.getpwnam(user).pw_dir
    env = child_env(home or "/tmp", provider)
    if use_pty:
        pid, master = pty.fork()
        if pid == 0:
            try:
                if user and os.getuid() == 0:
                    drop_to(user)
                os.chdir(home or "/tmp")
                os.execvpe(argv[0], argv, env)
            except Exception as e:  # noqa: BLE001
                os.write(2, f"cannot start {argv[0]}: {e}\n".encode())
            os._exit(127)
        # A very wide window, so a long link or token is never broken over two lines.
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
        return pid, master, master
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(w, 1)
            os.dup2(w, 2)
            os.close(r)
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.setsid()
            if user and os.getuid() == 0:
                drop_to(user)
            os.chdir(home or "/tmp")
            os.execvpe(argv[0], argv, env)
        except Exception as e:  # noqa: BLE001
            os.write(2, f"cannot start {argv[0]}: {e}\n".encode())
        os._exit(127)
    os.close(w)
    return pid, None, r


def last_line(raw):
    """The last line of output that says something, with anything token-shaped removed. For failure messages."""
    for ln in reversed(strip_ansi(raw).split("\n")):
        ln = TOKEN_RE.sub("[token]", ln).strip()
        if ln:
            return ln[:200]
    return ""


def finish_success(provider, raw):
    """Run after the sign-in program exits 0. Returns (ok, message)."""
    if provider == "claude":
        token = find_claude_token(raw)
        if not token:
            return False, "the sign-in finished but no token was found in its output"
        try:
            set_env_value(AGENT_ENV, "CLAUDE_CODE_OAUTH_TOKEN", token, group=AGENT_USER)
        except (OSError, ValueError, KeyError) as e:
            return False, f"could not save the token: {e}"
        return True, f"saved CLAUDE_CODE_OAUTH_TOKEN in {AGENT_ENV} (not shown). New tasks use it."
    if provider == "github":
        if os.environ.get("SWARM_LOGIN_FAKE_GITHUB"):
            return True, "signed in (test)"
        r = subprocess.run(["sudo", "-u", RUNNER_USER, "-H", "gh", "auth", "setup-git"], capture_output=True, text=True)
        if r.returncode != 0:
            return False, "signed in, but 'gh auth setup-git' failed: " + (r.stderr or r.stdout).strip()[-200:]
        return True, f"signed in as the {RUNNER_USER} user and git is set up to use it"
    return True, "signed in"


def background(provider):
    """The long-running part, in its own process: run the sign-in, keep the log, take the pasted code, record the result."""
    d = sdir(provider)
    log_path = os.path.join(d, "raw.log")
    pid, master, rfd = spawn(provider)
    write_status(provider, state="starting", daemon=os.getpid(), child=pid)
    start, raw, shown, sent_code = time.time(), "", False, False
    code_file = os.path.join(d, "code")
    exit_code = None
    with open(log_path, "ab") as log:
        os.fchmod(log.fileno(), 0o600)
        while True:
            if master is not None and os.path.exists(code_file) and not sent_code:
                with open(code_file) as fh:
                    code = fh.read().strip()
                os.remove(code_file)
                os.write(master, code.encode() + b"\r")
                sent_code = True
                write_status(provider, state="checking-code")
            ready, _, _ = select.select([rfd], [], [], 1.0)
            if ready:
                try:
                    chunk = os.read(rfd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    log.write(chunk)
                    log.flush()
                    raw += chunk.decode("utf-8", "replace")
                    raw = raw[-200000:]
                    if not shown:
                        info = PARSERS[provider](raw)
                        if needs(provider, info):
                            shown = True
                            write_status(provider, state="waiting", **info)
                    continue
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                exit_code = os.waitstatus_to_exitcode(status)
                break
            if time.time() - start > LIFETIME:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                time.sleep(1)
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
                write_status(provider, state="expired", message="nobody finished the sign-in in time; start again")
                return
    # drain what is left, then judge
    if exit_code == 0:
        ok, msg = finish_success(provider, raw)
    else:
        ok, msg = False, f"the sign-in program stopped with code {exit_code}: {last_line(raw)}"
    # The raw log can hold a token. Keep it only when something failed, and even then it stays root-only and short-lived.
    write_status(provider, state="done" if ok else "failed", message=msg)
    if ok:
        try:
            os.remove(log_path)
        except OSError:
            pass


def start_background(provider):
    """Fork a detached root process that runs background(provider). Returns after the parent has forked."""
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return
    os.setsid()
    if os.fork():
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        background(provider)
    finally:
        os._exit(0)


# ---------- the swarmctl command ----------

def say(a_json, info, lines):
    if a_json:
        print(json.dumps(info))
    else:
        for ln in lines:
            print(ln)


def describe(provider, st):
    """Lines for a person or a bot, from a status record. Never contains a token."""
    state = st.get("state")
    if state == "waiting":
        lines = [f"URL: {st['url']}"]
        if st.get("code"):
            lines.append(f"CODE: {st['code']}")
        if st.get("expires_minutes"):
            lines.append(f"EXPIRES: {st['expires_minutes']} minutes")
        if provider == "claude":
            lines.append("NEXT: the owner opens the URL and signs in. The browser then shows a code. "
                         "Get that code through the secret request form and run: swarmctl login claude --finish (code on stdin)")
        else:
            lines.append("NEXT: the owner opens the URL, signs in, and types the code. Then run: swarmctl login "
                         f"{provider} --status")
        return lines
    if state in ("done", "failed", "expired"):
        return [f"RESULT: {state}", f"MESSAGE: {st.get('message', '')}"]
    return [f"STATE: {state or 'unknown'}"]


def cmd_login(a):
    provider = a.provider
    if os.getuid() != 0 and not os.environ.get("SWARM_LOGIN_ALLOW_NON_ROOT"):
        sys.exit("swarmctl login: run with sudo on the worker")
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    d = sdir(provider)
    os.makedirs(d, mode=0o700, exist_ok=True)

    if a.token_stdin:
        if provider != "claude":
            sys.exit("swarmctl login: --token-stdin is only for claude")
        token = sys.stdin.read().strip()
        if not TOKEN_RE.fullmatch(token):
            sys.exit("swarmctl login: that is not a Claude token (sk-ant-...). Nothing saved.")
        set_env_value(AGENT_ENV, "CLAUDE_CODE_OAUTH_TOKEN", token, group=AGENT_USER)
        print(f"saved CLAUDE_CODE_OAUTH_TOKEN in {AGENT_ENV} (not shown). New tasks use it. Running tasks keep what they had.")
        return

    st = read_status(provider) or {}
    alive = bool(st.get("daemon")) and pid_alive(st["daemon"]) and st.get("state") in ("starting", "waiting", "checking-code")

    if a.cancel:
        if alive:
            for pid in (st.get("child"), st.get("daemon")):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, TypeError):
                    pass
        write_status(provider, state="cancelled", message="cancelled")
        print("cancelled")
        return

    if a.finish:
        if provider != "claude":
            sys.exit("swarmctl login: --finish is only for claude")
        if not alive:
            sys.exit("swarmctl login: no Claude sign-in is waiting. Start one: swarmctl login claude")
        code = sys.stdin.read().strip()
        if not code or len(code) > 500 or not re.fullmatch(r"[A-Za-z0-9._#~/+=-]+", code):
            sys.exit("swarmctl login: that does not look like the code from the browser. Nothing sent.")
        fd = os.open(os.path.join(d, "code"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(code)
        end = time.time() + 60
        while time.time() < end:
            st = read_status(provider) or {}
            if st.get("state") in ("done", "failed", "expired"):
                break
            time.sleep(1)
        say(a.json, st, describe(provider, st))
        return

    if a.status:
        if not st:
            sys.exit("swarmctl login: nothing started for " + provider)
        say(a.json, st, describe(provider, st))
        return

    # start (or show the sign-in that is already waiting)
    if not alive:
        for name in ("status.json", "raw.log", "code"):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
        start_background(provider)
    end = time.time() + WAIT_FOR_PROMPT
    st = read_status(provider) or {}
    while time.time() < end and st.get("state") not in ("waiting", "done", "failed", "expired"):
        time.sleep(0.5)
        st = read_status(provider) or {}
    if st.get("state") not in ("waiting", "done", "failed", "expired"):
        sys.exit(f"swarmctl login: no link or code appeared in {WAIT_FOR_PROMPT} seconds. See docs/troubleshooting.md, \"Sign-in\".")
    say(a.json, st, describe(provider, st))
    if st.get("state") in ("failed", "expired"):
        sys.exit(1)
