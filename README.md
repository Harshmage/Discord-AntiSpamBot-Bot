# Anti-Spam Signals Bot

This bot acts on two of Discord's member **Signals**, the flags shown on the server's Members page:

| Signal | Members page wording |
| --- | --- |
| Unusual DM Activity | Excessive DMs to non-friend server members |
| Unusual Account Activity | Engaged in suspected spam activity |

What it does:

- **A member carrying either signal is kicked.** The bot checks a few seconds after the member joins, then re-scans the whole server every few minutes, because Discord often adds a signal after the join.
- **A member it kicked before who is flagged again is banned.** Discord keeps no record of kicks, so the bot keeps its own in SQLite.
- **Every action is posted to the private moderator channel.** The post includes the user, their ID, account age, join date, how they joined (invite code or inviter), and which signal matched.
- **Moderators can exempt users** with `/whitelist add | remove | list`.
- **`/signals check <member>`** shows a member's current signals without taking any action.
- **`/signals probe [member] [limit]`** tests the Signals endpoint and posts the results to the mod channel. The post is a summary plus a `signals-probe.txt` file with the full requests and responses. It takes no action.

## ⚠️ How Signals are read (important)

Discord does **not** include Signals in the member data it sends to bots.

The only way a bot can read them is `POST /guilds/{id}/members-search`. This is the search endpoint the Members page uses. Bot tokens can call it, but Discord has **not documented** it, so Discord could change or remove it without notice.

To stay safe, the bot:

- **never acts when a lookup fails.** A failed check is treated as "unknown", never as "clean" or "flagged".
- **posts a warning to the mod channel** after 3 failures in a row, and posts again when lookups recover.
- **starts in `DRY_RUN=true` mode.** In dry-run it only posts "would kick/ban" reports and takes no action.

## Setup

1. **Create the bot** at https://discord.com/developers/applications.
   - Under **Bot**, enable the **Server Members Intent**.
2. **Invite the bot** with these permissions:
   - **Kick Members**, **Ban Members**
   - **Manage Server**. Signals are moderator-only data, and the search may be refused without this permission.
   - **View Channel**, **Send Messages**, **Embed Links** and **Attach Files** in the mod channel
   - Scopes: `bot` and `applications.commands`
3. **Move the bot's role above ordinary member roles.** The bot can't kick anyone whose highest role is equal to or above its own. For those members it posts a "no action possible" notice instead.
4. `cp .env.example .env` and fill in `DISCORD_TOKEN`, `GUILD_ID` and `MOD_CHANNEL_ID`. `MOD_ROLE_ID` is optional.

### Check that the endpoint works for your server

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/probe_signals.py            # who is flagged right now?
.venv/bin/python scripts/probe_signals.py --user ID  # a single member
```

You should get `HTTP 200` for each query. Compare the flagged list with the Signals column on **Server Settings → Members**. The probe never takes any action.

Once the bot is running, moderators can run the same probe from Discord with `/signals probe`. The results are posted to the mod channel.

### Run

```bash
docker compose up -d --build
docker compose logs -f
```

The SQLite database is stored in the Docker volume `antispam-data`, which Docker creates with the right permissions for the bot's non-root user. It survives rebuilds and `docker compose down`, but `docker compose down -v` deletes it, including the whitelist and kick history.

To back it up:

```bash
docker compose cp antispam:/data/bot.db ./bot-backup.db
```

1. Leave `DRY_RUN=true` at first.
2. Check the "🧪 DRY RUN" reports in the mod channel.
3. Set `DRY_RUN=false` in `.env`.
4. Run `docker compose up -d` again.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | required | Bot token |
| `GUILD_ID` | required | Server to protect |
| `MOD_CHANNEL_ID` | required | Channel where reports are posted |
| `MOD_ROLE_ID` | — | Extra role allowed to use the commands. Kick Members or Administrator always works. |
| `DRY_RUN` | `true` | Report only, never kick or ban |
| `SWEEP_MINUTES` | `5` | How often to re-scan the whole server |
| `JOIN_CHECK_DELAY` | `5` | Seconds to wait after a join before checking (tried up to 3 times) |
| `BAN_DELETE_SECONDS` | `86400` | Message history deleted when banning a member who rejoined (max 7 days) |
| `DB_PATH` | `/data/bot.db` | SQLite file |

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
