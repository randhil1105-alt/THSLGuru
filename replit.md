# THSL Guru — Discord Hockey Card Bot

THSL Guru is a Discord slash-command hockey-card game backed by a persistent
SQLite economy and a Python `discord.py` bot. This workspace also retains its
existing pnpm API and component-preview artifacts.

## Run & Operate

- `python main.py` — run the Discord bot; the **THSL Guru Bot** workflow uses
  this command.
- `python -m unittest discover -s tests -v` — run the offline bot test suite.
- `pnpm run typecheck` — typecheck the existing pnpm workspace.
- Required secret: `DISCORD_BOT_TOKEN`; never print it or put it in source.
- Optional environment variables: `DISCORD_GUILD_ID` for development-guild
  command sync and `BOT_DATABASE_PATH` for a custom SQLite file path.
- Rotate any token included in the supplied archive before connecting to
  Discord. Do not run the live bot until a replacement is stored in Secrets.

## Stack

- Bot: Python 3.10+ and `discord.py` 2.x
- Data: SQLite with additive schema creation and transactional economy updates
- Lineup images: SVG rendered to PNG with ImageMagick
- Existing workspace: pnpm, Node.js 24, TypeScript, Express, and React

## Where Things Live

- `main.py`, `config.py`, `commands/` — bot startup and slash commands
- `database.py`, `data/guru.sqlite3` — persistent economy and lineup records
- `data/players.json` — catalog seed; inserts missing records without replacing
  existing player data
- `cards/` — original card artwork
- `tests/` — offline command and database tests using temporary databases
- `artifacts/`, `lib/` — existing API and component-preview workspace

## Architecture Decisions

- Keep the Discord bot and existing pnpm artifacts in the same project.
- Preserve existing SQLite player, balance, inventory, and lineup records;
  schema changes must be additive.
- Use `BEGIN IMMEDIATE` for economy mutations and separate owned-card checks
  across all five team positions.
- Load command extensions once, then sync slash commands during startup.

## Product

Players collect and trade hockey cards, manage balances, open packs, earn
3,000,000 coins per level, claim a daily TOTW pack and coin reward, and complete
personal daily quests. They set a three-player starting lineup plus two
substitutes. Administrators manage the catalog and player economies. `/team`
renders assigned card artwork on a hockey rink image.

## Gotchas

- Image rendering requires the `magick` executable.
- A Discord OAuth account connection does not replace the bot token needed for
  the long-running gateway connection.
- The bot workflow must not be started until `DISCORD_BOT_TOKEN` has been
  rotated and stored in Replit Secrets.
- Tests and command-sync tests are offline; they do not prove live Discord
  access or permissions in a server.

## Pointers

- `README.md` documents commands, setup, data behavior, and tests.
- Use the `pnpm-workspace` skill for the existing TypeScript workspace.
