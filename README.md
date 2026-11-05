# THSL Guru Hockey Card Bot

A `discord.py` 2.x slash-command bot for collecting hockey player cards. It
stores player records, balances, claim cooldowns, and card ownership in a
persistent SQLite database.

## Included commands

| Command | Purpose |
| --- | --- |
| `/player name` | Look up a player's card |
| `/card name` | Show the full card and image when available |
| `/players` | Browse the full player catalog |
| `/cards` | Browse the full player card catalog |
| `/claim` | Claim 5,000 coins once every 24 hours |
| `/balance` | Check your balance |
| `/leaderboard` | Show the ten largest balances |
| `/inventory` | Show cards you own |
| `/collection` | Browse all cards and ownership |
| `/pack pack_type` | Choose a GOLD or TOTW pack, then confirm purchase |
| `/packs` | View pack contents, equal odds, prices, and purchase buttons |
| `/sell player quantity` | Sell owned copies at the card's listed value |
| `/team` | View your rink lineup image, substitutes, formation, and rating |
| `/set position player` | Assign an owned card to a starter or substitute slot |
| `/formation formation` | Choose Balanced, Attack, or Defensive |
| `/help` | Show the command guide |
| `/botinfo` | Show bot and catalog information |

`/packs`, `/pack`, `/inventory`, and `/balance` post their responses publicly
in the channel.

Administrator-only commands:

- `/addplayer` adds a card. Stats use comma-separated `KEY=value` pairs, such
  as `SHO=83,DRI=88`. Allowed stats are `SHO`, `DRI`, `SAV`, `PAS`, `DEF`,
  `ACT`, `DIV`, `CLE`, and `SWE`. Image paths must be relative paths inside
  `cards/`.
- `/setbalance` sets a member's balance to a non-negative amount.
- `/grantcoins` grants coins.
- `/grantcard` grants one or more card copies.

The initial catalog contains Dan, XavierCapone9, Gamer, Hala (Gold 90),
Dryboomrun, and Needy as GOLD cards, plus Licha and the original Hala card as TOTW. Card
names display as `<overall> <player> (<card type>)` throughout player-facing
lists and details. If a future catalog
has no cards of a requested pack rarity, the pack command explains that none are
available and does not charge.

## Run locally or on Replit

1. Install Python 3.10 or newer.
2. Install dependencies with `pip install -r requirements.txt`.
   The `/team` image renderer also requires the ImageMagick command-line tool.
3. Create a Discord application and bot in the Discord Developer Portal.
4. Add the bot token as the `DISCORD_BOT_TOKEN` secret/environment variable.
   Do not put it in source code, a text file, or chat. The token previously
   included with the supplied brief should be revoked and replaced before use.
5. Invite the bot using the `bot` and `applications.commands` OAuth2 scopes.
   Allow it to view channels, send messages, embed links, and attach files.
6. Start it with `python main.py`.

On Replit, add `DISCORD_BOT_TOKEN` under **Tools → Secrets**, then start the
**THSL Guru Bot** workflow or run `python main.py`. The bot syncs global slash
commands on startup. To sync immediately to a development server, set the
non-secret `DISCORD_GUILD_ID` environment variable to that server's ID. Remove
it to use global command sync.

## SQLite persistence

By default the database is stored at `data/guru.sqlite3`. To use a mounted
persistent disk on another host, set `BOT_DATABASE_PATH` to a file path on that
disk. Keep the database file on durable storage and back it up before replacing
or resetting it.

The economy operations use SQLite write transactions to prevent duplicate
claims, negative balances, double-spending, and charging for unavailable pack
rarities. Pack selection queries only the requested rarity and awards one card.
Purchase interactions are recorded idempotently so a retried confirmation
cannot charge twice. Lineups, formations, and pack receipts use additive
`CREATE TABLE IF NOT EXISTS` migrations; existing player, balance, inventory,
and starting-lineup rows are not reset. Substitute assignments are stored in an
additional table so the existing starter table does not need to be rebuilt.

## Lineups and formations

Each team has three starting slots—`LW` (Left Wing), `RW` (Right Wing), and
`GK` (Goalie)—plus two substitute slots, `SUB1` and `SUB2`. For example,
`/set position:LW player:Dan` assigns a starter and
`/set position:SUB1 player:Dan` assigns a substitute. Use `player:CLEAR` to
empty a slot. A card must be in the user's inventory and cannot be assigned to
more than one of the five slots.

`/formation formation:Attack` saves the user's tactical style independently
from the three player assignments:

- **Balanced** — equal emphasis on offense and defense.
- **Attack** — offense-first style, emphasizing the wing positions.
- **Defensive** — defense-first style, emphasizing protection of the goal.

`/team` generates a dark-background hockey-rink lineup image with light ice,
red center and goal markings, blue lines, face-off circles and dots, rounded
boards, the original card images for assigned players, and outlined circles for
empty positions. It sends that PNG as a Discord attachment and also includes
the formation and starting-lineup rating in the embed. The rating uses only
the three starters; substitutes do not affect it. The formation style is stored
and displayed, but this bot does not currently simulate matches or modify
player ratings based on it. Missing or unreadable card images display a
placeholder instead of preventing the lineup image from being sent.

`/pack pack_type` opens a confirmation menu for the selected GOLD or TOTW pack.
`/packs` lists both pack types, their eligible cards, equal per-card odds, and
purchase buttons. Clicking a buy button makes one purchase; each menu is single-use.

## GameUnit Plus deployment

This project uses standard Python, `discord.py`, and SQLite; it does not depend
on Replit-only services. In a GameUnit Plus Python service:

1. Upload or connect this project.
2. Install dependencies with `pip install -r requirements.txt`.
3. Add `DISCORD_BOT_TOKEN` in the host's private environment/secrets settings.
4. Set the process start command to `python main.py`.
5. Attach durable storage and set `BOT_DATABASE_PATH` to a location on it.
6. Keep the service running as a worker/background process. A Discord gateway
   bot is not an HTTP web server.

Panel labels and storage configuration can vary by GameUnit Plus plan. Confirm
that the selected service supports persistent volumes and long-running Python
workers before relying on it for the live economy.

## Card images

The catalog uses `cards/dan.png`, `cards/licha.webp`, `cards/hala.webp`,
`cards/hala.png`, `cards/xaviercapone9.png`, `cards/gamer.png`, and
`cards/dryboomrun.png`, and `cards/needy.png`. These are
the supplied card images and are checked by the offline test suite.

## Tests

Run the offline suite with:

```sh
python -m unittest discover -s tests -v
```

All tests use temporary SQLite files and test-only user IDs. They do not log in
to Discord or use live balances.
