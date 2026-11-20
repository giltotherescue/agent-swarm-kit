#!/usr/bin/env python3
"""render-cloud-init.py: fill in scripts/cloud-init.yaml and print (or save) the user-data for a new server.

Usage:
  render-cloud-init.py --admin-user NAME --ssh-public-key-file FILE.pub [options] > user-data.yaml

Options:
  --auth-key-file FILE    a Tailscale auth key (tskey-auth-...) read from FILE and never printed. Make it single-use,
                          expiring within an hour, pre-approved. Without it the server asks for a browser approval.
  --kit-url URL           where the server downloads the kit (default: this version's tag on GitHub)
  --kit-sha256 HEX        check the download against this hash
  --lockdown auto|bot     auto: close public SSH once Tailscale is connected (default). bot: leave it for the bot
  --no-claude, --no-codex skip installing one model CLI
  --out FILE              write to FILE with mode 600 instead of printing. Required when --auth-key-file is set.
Every value is checked against a strict pattern, so nothing can break out of the YAML or the shell lines.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "cloud-init.yaml")
DEFAULT_URL = "https://github.com/giltotherescue/agent-swarm-kit/archive/refs/tags/v{version}.tar.gz"
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED = {"root", "agent", "swarm-runner", "swarm"}
KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+( [A-Za-z0-9@._:-]+)?$")
AUTH_RE = re.compile(r"^tskey-[A-Za-z0-9_-]{8,200}$")
URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/?&=%+@-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class Bad(Exception):
    pass


def kit_version():
    with open(os.path.join(HERE, "..", "VERSION")) as fh:
        return fh.read().strip()


def render(admin_user, ssh_public_key, auth_key=None, kit_url=None, kit_sha256=None, lockdown="auto",
           with_claude=True, with_codex=True):
    """Return the user-data text. Raises Bad with a plain message for any value that does not pass its check."""
    if not USER_RE.match(admin_user) or admin_user in RESERVED:
        raise Bad("--admin-user must be a plain lowercase user name and not root, agent, swarm or swarm-runner")
    ssh_public_key = ssh_public_key.strip()
    if not KEY_RE.match(ssh_public_key):
        raise Bad("the SSH public key must be one line like 'ssh-ed25519 AAAA... comment' (a PUBLIC key, never the private one)")
    if lockdown not in ("auto", "bot"):
        raise Bad("--lockdown must be auto or bot")
    kit_url = kit_url or DEFAULT_URL.format(version=kit_version())
    if not URL_RE.match(kit_url):
        raise Bad("--kit-url must be an https address")
    if kit_sha256 is not None and not SHA_RE.match(kit_sha256):
        raise Bad("--kit-sha256 must be 64 lowercase hex characters")
    if auth_key is not None:
        auth_key = auth_key.strip()
        if not AUTH_RE.match(auth_key):
            raise Bad("that does not look like a Tailscale auth key (tskey-...)")
    with open(TEMPLATE) as fh:
        text = fh.read()
    if auth_key:
        block = ("      AUTH_KEY_FILE=/root/.swarm-tailscale-authkey\n"
                 "  - path: /root/.swarm-tailscale-authkey\n"
                 "    owner: root:root\n"
                 '    permissions: "0600"\n'
                 f"    content: {auth_key}")
    else:
        block = "      # No Tailscale auth key: the server saves a login link for the bot to relay"
    sha_line = (f'      echo "{kit_sha256}  $work/kit.tar.gz" | sha256sum -c -' if kit_sha256
                else "      # no --kit-sha256 given: the download is checked only by HTTPS")
    subs = {"ADMIN_USER": admin_user, "SSH_PUBLIC_KEY": ssh_public_key, "LOCKDOWN": lockdown,
            "WITH_CLAUDE": "1" if with_claude else "0", "WITH_CODEX": "1" if with_codex else "0",
            "AUTH_KEY_BLOCK": block, "KIT_URL": kit_url, "SHA_LINE": sha_line}
    for k, v in subs.items():
        text = text.replace(f"@@{k}@@", v)
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise Bad("unfilled placeholders: " + ", ".join(left))
    return text


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--admin-user", required=True)
    p.add_argument("--ssh-public-key-file", required=True)
    p.add_argument("--auth-key-file")
    p.add_argument("--kit-url")
    p.add_argument("--kit-sha256")
    p.add_argument("--lockdown", default="auto")
    p.add_argument("--no-claude", action="store_true")
    p.add_argument("--no-codex", action="store_true")
    p.add_argument("--out")
    a = p.parse_args(argv)
    try:
        key = open(a.ssh_public_key_file).read()
        auth = open(a.auth_key_file).read() if a.auth_key_file else None
        if auth and not a.out:
            raise Bad("refusing to print a file that holds an auth key; use --out FILE")
        text = render(a.admin_user, key, auth, a.kit_url, a.kit_sha256, a.lockdown, not a.no_claude, not a.no_codex)
    except (OSError, Bad) as e:
        print(f"render-cloud-init: {e}", file=sys.stderr)
        return 1
    if a.out:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(a.out, 0o600)
        print(f"wrote {a.out} (mode 600). Delete it once the server is up.")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
