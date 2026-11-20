# Build the worker with nobody typing (cloud-init)

This is the default way to get a worker. Your Grok Bot creates the server through the provider's API.
The server sets itself up the first time it starts. You never open a terminal, never run `sudo`, and
never SSH in. You only approve things in a browser: the provider token form, the Tailscale approval, and
the sign-ins.

The by-hand way is still there as a fallback: [worker-setup.md](worker-setup.md).

**Plain words.** *cloud-init* is a standard feature of cloud servers: you hand the provider a small
file ("user data") when you create the server, and the server runs it on first boot. *Tailscale* is a
private network for your own devices. *Lockdown* means closing SSH to the public internet so the server
can only be reached over Tailscale.

## What happens

```
bot ── renders user data ──> provider API creates the server
                                   │ first boot
                                   ▼
        cloud-init: make the admin user, download the kit, start swarm-firstboot
                                   ▼
        scripts/firstboot.sh
          1. setup-worker.sh --yes --admin-user NAME [--auth-key FILE]   (users, tools, services, firewall)
          2. join Tailscale (a one-use key, or a login link saved for the bot)
          3. setup-secrets.sh --prepare   (SOPS, age, this worker's keys; public halves saved for the bot)
          4. start swarm-worker
          5. smoke-test.sh   (must say 0 failed, or it stops and public SSH stays open)
          6. lockdown: close public SSH once Tailscale is connected
                                   ▼
bot ── reaches the box over Tailscale SSH ──> logins, secrets repo, bots (see below)
```

Progress is one line per step in `/var/lib/agent-swarm/firstboot/status`. It ends with `finished` or
`stopped REASON`. The full log is `/var/log/agent-swarm-firstboot.log`.

## What the bot needs from you

| Need | How you give it | Notes |
| --- | --- | --- |
| A provider token | Grok Bot's secret request form (never chat) | DigitalOcean: a personal access token with Droplet create and read, plus read for images, sizes and regions. Revoke it when the server is up; the bot does not need it later |
| Your yes on the monthly cost | In chat | The bot says the rough price first |
| A Tailscale join | Either a one-use auth key through the secret form, or one click on a login link | See below |

**Tailscale, two ways.**

- **Auth key (no waiting).** In the Tailscale admin page, make a key that is *single use*, *expires in
  an hour*, and is *pre-approved* if your tailnet needs device approval. Give it through the secret form.
  It is written to a file that only root can read, used once, and deleted from the disk during setup.
- **Login link.** No key. The server runs `tailscale up --ssh`, saves the link, and the bot reads it and
  sends it to you. You open it and approve the machine. The bot reaches the server with its bootstrap
  key (below) while SSH is still open, so it can read the link.

## What the bot does

1. **Make a bootstrap key.** `ssh-keygen -t ed25519 -N "" -f bootstrap` in the bot's own scratch
   folder. Only the public half goes to the server. It exists so setup can prove an admin can log in
   (the setup script refuses to harden SSH otherwise) and so the bot can read progress before Tailscale
   is up. Delete the private half after lockdown. Day to day, the bot uses Tailscale SSH, which needs no
   key.
2. **Render the user data.**

   ```bash
   python3 scripts/render-cloud-init.py --admin-user ops --ssh-public-key-file bootstrap.pub \
     --auth-key-file ts.key --out user-data.yaml        # drop --auth-key-file for the login-link way
   ```

   Options: `--lockdown bot` (leave public SSH open until the bot closes it itself, see "Safer
   lockdown" below), `--no-claude`, `--no-codex`, `--kit-url`, `--kit-sha256`. Without `--kit-url` the
   server downloads this version's release tag from GitHub if your checkout has that tag, otherwise the
   commit your checkout is on, otherwise the main branch. The script prints which one it chose. If you
   changed the kit and did not push the commit, GitHub does not have it: push it or pass `--kit-url`. Every value is checked
   against a strict pattern. The output holds the Tailscale key if you used one, so it is saved with mode
   600 and refused on screen. Delete it once the server is up.
3. **Create the server (DigitalOcean).** One call to the Droplets API. `user_data` is the file's text,
   at most 64 KiB. `jq` builds the JSON, so nothing needs quoting by hand:

   ```bash
   jq -n --rawfile ud user-data.yaml \
     '{name:"swarm-worker", region:"nyc3", size:"s-4vcpu-8gb", image:"ubuntu-24-04-x64", monitoring:true, user_data:$ud}' \
     | curl -sS -X POST https://api.digitalocean.com/v2/droplets \
         -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" -H "Content-Type: application/json" -d @-
   ```

   The reply is `202` with the droplet's `id`. Poll `GET /v2/droplets/ID` until `status` is `active`,
   then read the public address from `networks.v4` (the entry with `"type": "public"`). Check the size
   slug with `GET /v2/sizes` and the region with `GET /v2/regions` rather than trusting the ones above.
   The token comes from the secret form into an environment variable or a header file, never onto the
   command line in chat or into a log.
4. **Wait.** First boot takes about 10 to 15 minutes. The bot reads the status file, over Tailscale SSH
   once the machine is on your tailnet, or over public SSH with the bootstrap key before that
   (`ssh -i bootstrap ops@PUBLIC-IP cat /var/lib/agent-swarm/firstboot/status`).
5. **Relay the Tailscale link** if you chose the login-link way: the status file points at
   `/var/lib/agent-swarm/firstboot/tailscale-login-url`.
6. **Confirm it works.** When the status says `finished`, the bot opens a Tailscale SSH session
   (`ssh ops@HOSTNAME`) and checks `sudo swarmctl status`.
7. **Clean up.** Delete `user-data.yaml`, the auth key file and the bootstrap private key. Ask you to
   revoke the provider token.

Your Tailscale access rules must let you (and so your bot's machine) reach the worker with Tailscale
SSH as the admin user. The sample policy is in [worker-setup.md](worker-setup.md#tailscale-access-rules).

### Other providers

Hetzner Cloud, Vultr, Linode (Akamai) and most others take the same cloud-init file in a `user_data`
field of their create-server call; some call it a startup script. The steps are the same: render, create
with Ubuntu 24.04, poll until the server has an address, wait for the status file. Only the endpoint and
the token form differ. Only DigitalOcean is written out above, from its API reference. The others are
**not** tested with the kit.

## After the server is up (all over Tailscale SSH, run by the bot)

| Step | Command (on the worker) | You do |
| --- | --- | --- |
| Codex sign-in | `sudo swarmctl login codex` | Open the link, type the code. Turn on device-code login first ([troubleshooting](troubleshooting.md#sign-in)) |
| Claude sign-in | `sudo swarmctl login claude`, then `--finish` | Open the link, sign in, give the bot the code the page shows (secret form) |
| GitHub sign-in | `sudo swarmctl login github` | Open the link, type the code |
| Secrets repo | read `/var/lib/agent-swarm/firstboot/worker-age-public-key.txt` and `worker-deploy-key.pub.txt`, add them to your secrets repo, then `sudo bash /opt/agent-swarm-kit/current/scripts/setup-secrets.sh --link REPO_URL` | Approve adding the deploy key (read only) in GitHub |
| Bots | [bot-setup-checklist.md](../templates/bot-setup-checklist.md) | Tokens and keys through the secret form |

How the sign-ins work is in [worker-setup.md](worker-setup.md#5-sign-in-the-model-clis-with-your-own-subscriptions).

## Safer lockdown

By default the server closes public SSH on its own once Tailscale is connected and has a network
address and the smoke test passed. It cannot prove that *you* can reach it over Tailscale: that depends
on your Tailscale access rules. If the rules are wrong you would have to use the provider's web console
to get back in.

For a first server, or if you are unsure of your rules, render with `--lockdown bot`. Public SSH stays
open. The bot proves it can log in over Tailscale SSH, then closes it:

```bash
sudo bash /opt/agent-swarm-kit/current/scripts/setup-worker.sh --lockdown --yes --admin-user ops
```

## Safety notes

- **User data is not a safe place for long-lived secrets.** The provider stores it, and any process on the
  server can read it from the provider's metadata address. That is why the Tailscale key must be single
  use and short lived, and why nothing else secret goes in it. The age and deploy keys are made on the
  server and never travel.
- **The admin user has passwordless sudo.** Nobody is there to type a password. Who can use it is decided
  by your Tailscale access rules (and, before lockdown, by the bootstrap key). Keep the rules narrow:
  only your admin group, never `root`.
- The kit is downloaded over HTTPS from GitHub (the release tag, or a commit or the main branch when there is
  no tag, see step 2). Pass `--kit-sha256` to pin its hash.
- The `agent` user still has no sudo and no tokens. Nothing here changes the trust model in
  [secrets.md](secrets.md).

## Tested, and not

Tested here: the user data renders, passes `cloud-init schema`, and every input that could break out of
the file is rejected. `firstboot.sh` runs every step in order, handles a failed smoke test, no-key
Tailscale and a setup script that finishes with warnings, using stand-ins for the setup script, Tailscale
and systemd. `setup-worker.sh --yes` and the lockdown guard are covered by tests.

**Install test 5 (2026-11-20, 0.6.0 on a fresh DigitalOcean Ubuntu 24.04 droplet)** created the droplet
through the API and ran the real first boot. It found three problems, fixed in 0.6.1:

- Stock DigitalOcean Ubuntu 24.04 images mark root's password "must be changed". Every `sudo -u agent`
  then failed ("Account or password is expired") and first boot stopped at the Claude and Codex installs.
  `setup-worker.sh` now clears that flag before anything runs as `agent`. It sets no password. The test
  used `chage -d $(date +%F) root` by hand; 0.6.1 does the same.
- `setup-secrets.sh --prepare` never installed SOPS: its checksum line was rewritten with an extra space,
  so `sha256sum -c` refused every download. Fixed, and checked against the real SOPS 3.10.2 checksums file.
- The default kit download was the `v0.6.0` tag, which did not exist, so first boot got a 404. See step 2.

With those worked around, setup, the smoke test and the SSH and firewall hardening passed.

**Not tested on a real server:** the real Tailscale join with a key, the real login link, the automatic
lockdown, linking a real secrets repo, and the 0.6.1 fixes themselves on a new droplet (they are covered
by tests here). Run it on a server you can throw away first.
