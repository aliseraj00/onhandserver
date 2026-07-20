# OnHandServer

Telegram bot for monitoring CPU, RAM, and disk on Linux servers. Run the bot on one host; optionally install an agent on other hosts to monitor them too.

## Requirements

- Linux with Python 3
- Root (or sudo) for the installer / systemd
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Quick install

One-liner  — no manual clone required. Run as **root**:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aliseraj00/onhandserver/main/install.sh)
```

If you use a non-root user: `sudo -i` first, then run the command above (`sudo bash <(curl …)` often fails with process substitution).

This clones the repo to `/opt/onhandserver-src`, then runs the interactive installer into `/opt/onhandserver`.

Quick update (keeps `.env` and data files):

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aliseraj00/onhandserver/main/update.sh)
```

## Install the bot

On the machine that should run the Telegram bot (interactive prompts):

```bash
# Preferred: quick install above, or from a clone:
git clone https://github.com/aliseraj00/onhandserver.git
cd onhandserver
sudo bash install.sh
```

Choose **Bot server**, then enter:

| Prompt | What to enter |
|--------|----------------|
| Telegram bot token | From @BotFather |
| Admin chat ID(s) | Your Telegram chat ID (comma-separated for several admins) |
| Display name | Optional label for this host |
| Monitor this machine? | Usually yes |
| Disk path | Usually `/` |
| Shell command execution? | Yes only if admins should run commands from Telegram |
| systemd service? | Yes to start automatically |

Default install path: `/opt/onhandserver`.

### Get your chat ID

1. Start the bot in Telegram (`/start`).
2. Tap **My ID** (or use [@userinfobot](https://t.me/userinfobot)).
3. Put that ID in `ADMIN_CHAT_IDS` during install (or reconfigure).

## Install a remote agent

On each extra server you want to monitor — same quick install one-liner, then choose **Agent**:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aliseraj00/onhandserver/main/install.sh)
```

Note the generated **token** and listen **port** (default `8765`).

Open the port on the agent host firewall so the bot host can reach it, e.g. `http://AGENT_IP:8765`.

## Use the bot

Open your bot in Telegram and send `/start` (or `/help`).

### Main menu

| Button | What it does |
|--------|----------------|
| **Status** | Resource snapshot for your selected server |
| **All servers** | Short status for every monitored host |
| **Servers** | Pick a server, then view status (admins can also run commands if enabled) |
| **Settings** | Alerts; admins also get Users, Manage servers, and Update |
| **My ID** | Your Telegram chat ID |
| **Backup path** | Admin only — zip a local path or Linux logs and send to Telegram |

Under **Settings**:

| Button | What it does |
|--------|----------------|
| **Alerts** | Per-server CPU / RAM / disk alerts and global check timing |
| **Users** | Admin only — allow or remove users |
| **Manage servers** | Admin only — add, rename, or remove remote agents |
| **Update** | Admin only — update the bot server, then all agents |

### Add a remote server (admin)

1. **Settings** → **Manage servers** → **Add server**
2. Send: `name | url | token`

Example:

```text
web1 | http://x.x.x.x:xxxx | YOUR_AGENT_TOKEN
```

The token must match the agent’s `AGENT_TOKEN`. If you omit the token, one is generated — then you must set the same value on the agent.

### Authorize another user (admin)

1. They send `/start` and tap **My ID**, then send you the number.
2. **Settings** → **Users** → **Add user** → paste their chat ID.

Admins (`ADMIN_CHAT_IDS`) always have full access. Allowed users can view status and settings; only admins manage users, servers, backups, and remote commands.

### Alerts

Under **Settings** → **Alerts**, pick a server and toggle CPU / RAM / disk alerts, thresholds, and (for CPU) how many consecutive checks must stay high before alerting. **Global timing** sets how often to check and the cooldown between repeated alerts.

Alerts are sent to admin and allowed-user chats when a threshold is exceeded.

### Backup (admin, bot host only)

1. **Backup path** → **Set path** → send an existing file or folder path on the **bot** machine.
2. **Run now**, or set an interval and **Start schedule**.
3. **Linux logs** — pick common log files and upload a zip (Telegram size limit applies).

### Run a shell command (admin)

Only if `EXEC_ENABLED=true` on the target (bot host and/or agent).

1. **Servers** → pick a server → **Run command**
2. Send commands **one by one** (each chat message is one command). Output streams live.
3. `cd` persists for the session (shown as 📂 path) until you leave.
4. **Stop** sends Ctrl+C to a running command (or closes the session if idle). **Back** leaves the session.
5. Default timeout is **300s** (`EXEC_TIMEOUT_SECONDS`).

Interactive editors (`nano`, `vim`, `top`, …) are blocked — edit files with `cat`, `sed`, or a heredoc in one message instead.

## Upgrade

### From the bot (recommended)

Admins: **Settings** → **Update**. The bot checks GitHub for a newer commit than what's installed:

- **Update available** — shows the new commit titles, then **Yes, update** / **No**.
- **Up to date** — offers **Check again** (or **Force update anyway** if you want to reinstall/restart regardless).

Confirming runs the same update script on the bot host first, then automatically triggers it on every registered agent that has `EXEC_ENABLED=true` (agents without it need a manual update — see below). Both the bot and each agent restart themselves once their update finishes.

Version tracking needs a `VERSION` file written by `install.sh` — existing installs will show "unknown" until they upgrade once (from the bot or the shell).

### From the shell

Quick update (no local clone needed):

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aliseraj00/onhandserver/main/update.sh)
```

Or from a local clone:

```bash
sudo ./update.sh
```

Both refresh the code and run `install.sh --upgrade`, keeping `.env` and data files (`allowed_users.json`, `servers.json`, `monitor_config.json`).

To change settings from scratch: `sudo bash install.sh --reconfigure`.

## Useful commands

```bash
# Bot service
sudo systemctl status onhandserver
sudo journalctl -u onhandserver -f

# Agent service
sudo systemctl status onhandserver-agent
sudo journalctl -u onhandserver-agent -f
```

Config lives under `/opt/onhandserver/` (`.env`, JSON data files).
