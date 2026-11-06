# THSL Guru Hockey Card Bot

A `discord.py` 2.x slash-command bot for collecting hockey player cards. It
stores player records, balances, claim cooldowns, and card ownership in a
persistent SQLite database.

## Included commands

| Command | Purpose |
| --- | --- |
| `/card player` | Choose a catalog card using autocomplete and view its full image |
| `/cards` | Browse the full player card catalog |
| `/claim` | Claim a weighted GOLD player card once every 24 hours |
| `/minigame` | Guess a player's nationality, rating, position, and card type for coins |
| `/balance` | Check your balance |
| `/leaderboard` | Show the ten largest balances |
| `/inventory` | Show cards you own |
| `/collection` | Browse all cards and ownership |
| `/pack pack_type` | Choose a GOLD or TOTW pack, then confirm purchase |
| `/packs` | View weighted card odds, rating bands, prices, and purchase buttons |
| `/sell player quantity` | Sell owned copies at the card's listed value |
| `/team` | View your three starters, two substitutes, team value, and rink image |
| `/set position player` | Choose an owned card version for a compatible slot |
| `/formation formation` | Choose Balanced, Offensive, or Defensive |
| `/match opponent` | Challenge a player to a live 45-second match for 5,000,000 coins |
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

The seeded catalog includes 22 GOLD cards, including Megan, Jaycob, Karuwrld,
Ryuko, Rj, Criss, Ebs, Cam, and Gold Licha, plus the existing Licha and Hala
TOTW cards. Card names display as
`<overall> <player> (<card type>)` throughout player-facing lists and details.
If a catalog has no cards of a requested pack rarity, the pack command explains
that none are available and does not charge.

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

The public `/minigame` command starts an individual 60-second game with an
owner-only **Answer** button. Guess submissions are private; a correct answer
awards exactly 500,000 coins through SQLite and publicly reveals the player's
card. Message Content Intent is not needed because guesses arrive through the
interaction modal rather than channel messages.

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
and legacy lineup rows are not reset. The new starter and formation tables are
populated from compatible legacy assignments while preserving original records.

## Lineups and formations

Every team displays exactly three starting positions and two substitute
positions. The starting positions depend on the selected formation:

- **Balanced** — one Wing, one CM, and one GK.
- **Offensive** — two Wings and one GK.
- **Defensive** — two CMs and one GK.

Use `/set` to choose from cards currently in your inventory. Each autocomplete
choice displays the card's name, OVR, and rarity, and selects that exact card
record. Only GK cards can fill GK slots, only CM cards can fill CM slots, and
only Wing cards can fill Wing slots. Cards cannot occupy multiple slots.
`SUB1` and `SUB2` accept any owned card; use `player:CLEAR` to empty a slot.
Changing formation keeps cards in compatible slots, may move a card only to an
empty slot of the same role, and explains any cards that no longer fit.

Use `/match opponent:@player` in a server channel to send a public challenge.
The opponent has 60 seconds to accept or decline. Accepted matches run for 45
seconds, show live events and score updates, and use only the three valid
starting cards for each player's current formation; substitutes are not used.
The final result lists both usernames, starting lineups, goal scorers, the
winner, and the 5,000,000-coin reward. Each player can have only one pending or
active match at a time. A missing lineup, a player leaving the server, or a bot
restart cancels the match without a reward.

`/team` renders the three starting cards inside the white rink, with the two
substitute cards in the section below it. It uses original card artwork and
outlined circles for empty positions while keeping the center circle,
face-off circles, and rink lines visible. The embed shows the starters' OVR
and total team value, calculated from the listed values of assigned starters
and substitutes.

`/pack pack_type` opens a confirmation menu for the selected GOLD or TOTW pack.
`/packs` lists both pack types, eligible cards, rating-based weight bands, and
each card's recalculated probability. Per-card weights are 4 for ratings up to
84, 2 for 85–89, 1 for 90–94, and 0.25 for 95+. Each pack filters to its own
rarity before weighted selection. Clicking a buy button makes one purchase;
each menu is single-use.

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

The catalog uses `cards/dan.png`, `cards/megan_gold.png`,
`cards/jaycob_gold.png`, `cards/karuwrld_gold.png`, `cards/licha.webp`, `cards/hala.webp`,
`cards/hala.png`, `cards/xaviercapone9.png`, `cards/gamer.png`,
`cards/dryboomrun.png`, `cards/needy.png`, `cards/fadi.png`, `cards/mason.png`,
`cards/demox.png`, and `cards/cheddar.png`. These are the supplied card images
and are checked by the offline test suite.

## Tests

Run the offline suite with:

```sh
python -m unittest discover -s tests -v
```

All tests use temporary SQLite files and test-only user IDs. They do not log in
to Discord or use live balances.
