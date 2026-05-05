# Lichess Ultrabullet BOT Project

Production-oriented Python bot runner for Lichess **BOT accounts only**, with local `1/2+0` ultrabullet-style and `1/4+0` hyperbullet-style dry-run testing. It uses `python-chess`, Stockfish through UCI, strict time budgets, a premove-like prepared-reply cache, and a fast tactical blunder filter.

Never run this with a normal Lichess user account. The live runner verifies the account has the `BOT` title before playing.

## Compliance Guardrails

- The runner calls `/api/account` before live play and refuses to run unless the token belongs to a Lichess account with title `BOT`.
- Incoming challenges can be BOT-only or allow humans depending on `ALLOW_HUMAN_CHALLENGES`.
- Set `ALLOW_HUMAN_CHALLENGES=true` to accept standard `<=30s +0` human challenges for testing; set it to `false` for BOT-only challenges.
- Accepted live games must be standard chess, bullet, clock-based, `30+0`, and increment `0`.
- The project does not implement human-play assistance, browser automation, lobby seeks, pools, tournaments, or simuls.
- Challenge loops use direct bot-vs-bot challenges only, with a configurable cooldown to avoid spam.
- Lichess API rate-limit guidance is respected: request starts are serialized across threads and client instances, with a full-minute pause after `429`.

## Files

- `run_bot.py` - live BOT runner, game stream handler, local dashboard, dry-run mode.
- `run_two_bots.py` - runs bot 1 and bot 2 processes in one Python program.
- `challenge_loop.py` - repeatedly challenges `BOT_2_USERNAME` from bot 1.
- `config.py` - `.env` loading and validation.
- `engine_controller.py` - Stockfish UCI wrapper with hard millisecond limits.
- `move_selector.py` - time management, opening book, prepared replies, candidate ordering.
- `blunder_check.py` - fast mate, queen, and major-piece sanity checks.
- `lichess_client.py` - Lichess BOT API client and challenge filter.
- `logs/` - JSONL game logs, one file per game.

## Setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/Scripts/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Install Stockfish and set its path. On Windows Git Bash, a typical `.env` entry looks like:

   ```env
   STOCKFISH_PATH=C:\tools\stockfish\stockfish.exe
   ```

4. Copy the example env file:

   ```bash
   cp .env.example .env
   ```

5. Fill in:

   ```env
   LICHESS_TOKEN_BOT_1=...
   LICHESS_TOKEN_BOT_2=...
   BOT_1_USERNAME=YourBotOne
   BOT_2_USERNAME=YourBotTwo
   ENABLE_PREPARED_REPLIES=false
   PREPARE_REPLY_BUDGET_MS=10
   ```

## Creating A Lichess BOT Account

Create a normal Lichess account dedicated to the bot, then upgrade it to a BOT account using Lichess' official BOT workflow/API. Generate a personal access token for that BOT account with bot-play permissions, then place it in `.env` as `LICHESS_TOKEN_BOT_1`. Use a second BOT account and token for `LICHESS_TOKEN_BOT_2` if you want two local bots playing each other.

Important: BOT conversion is effectively permanent for that account. Do not use your personal account.

## Run

Dry-run a local self-contained game without Lichess:

```bash
python run_bot.py --dry-run --plies 10
```

Local ultrabullet dry-run at `1/2+0`:

```bash
python run_bot.py --dry-run --clock-ms 500 --increment-ms 0 --plies 200
```

Save a local ultrabullet dry-run PGN:

```bash
python run_bot.py --dry-run --clock-ms 500 --increment-ms 0 --plies 200 --pgn-path logs/ultrabullet-sample.pgn
```

Generate a higher-quality sample PGN with local timing relaxed while preserving the `0.5+0` PGN time control:

```bash
python run_bot.py --dry-run --clock-ms 500 --increment-ms 0 --plies 200 --quality-mode sample --pgn-path logs/ultrabullet-sample.pgn
```

Analyze batches of local dry-run PGNs:

```bash
python scripts/analyze_pgns.py --dir logs
```

Local hyperbullet dry-run at `1/4+0`:

```bash
python run_bot.py --dry-run --clock-ms 250 --increment-ms 0 --plies 200
```

Local dry-run supports `1/4+0` and `1/2+0` testing. Live Lichess BOT play appears to support `30+0`, but Lichess rejects `15+0` ultrabullet challenges for BOT accounts with `"Game incompatible with a BOT account"`.

Run tests:

```bash
pytest
```

The pytest suite is offline-only: it does not require real Lichess tokens, does not call the network, and does not require Stockfish to be installed.

## Before Live Launch

Run preflight checks before starting any live BOT process:

```bash
python scripts/preflight.py
python scripts/preflight.py --live
```

Use `--live --bot2` to verify both BOT account tokens. Preflight does not start games, accept challenges, or create challenges; it only checks Stockfish, cooldown settings, and BOT account title via `/api/account` when requested.

Run one live bot:

```bash
python run_bot.py --bot 1
```

Run two local live bots:

```bash
python run_two_bots.py
```

In `run_two_bots.py`, bot 2's dashboard startup is intentionally disabled so both bot threads do not try to bind `localhost:3000`.

Start repeated challenges from bot 1 to bot 2:

```bash
python challenge_loop.py --seconds 0.5
python challenge_loop.py --seconds 0.25
python challenge_loop.py --seconds 15
python challenge_loop.py --seconds 30
python challenge_loop.py --accept-only
```

Add `--rated` only when both BOT accounts are allowed and configured for rated bot-vs-bot challenges.

## Dashboard

The local dashboard runs at:

```text
http://localhost:3000
```

It shows current game links, clocks, last move, eval, selected move, think time, blunder-check result, and result. Logs are written to `logs/<game-id>.jsonl`.

## Time Management

The selector never asks Stockfish for more than one second. Defaults are intentionally tiny:

- Hyperbullet-ish `<= 0.25s`: `20ms-80ms` when safe.
- Ultrabullet-ish `<= 0.5s`: `50ms-150ms` when safe.
- Under `5s`: max `25ms`.
- Under `2s`: max `10ms`.
- Under `1s`: use cached prepared replies or an immediate legal fallback.

The bot favors legal, safe, forcing moves over deeper calculation because flagging and avoiding one-move disasters matter more than beautiful engine depth at these clocks.

## Premove Simulation

True premoves are not available through the Lichess BOT API. This project only simulates premove-like instant replies by playing normal legal BOT moves as soon as matching game states arrive.

Prepared replies are disabled by default. Set `ENABLE_PREPARED_REPLIES=true` and keep `PREPARE_REPLY_BUDGET_MS` small if you want the bot to predict likely opponent checks, captures, promotions, and other forcing replies. Preparation is skipped under two seconds remaining and capped by the configured budget so hyperbullet move selection does not accidentally spend extra time on future lines.

## Blunder Check

Before submitting a candidate, the bot checks:

- illegal moves and moving into check,
- immediate mate in one for the opponent,
- undefended queen hangs,
- undefended rook/queen hangs,
- shallow recapture-aware exchange checks for queen/rook danger,
- optional shallow Stockfish verification from `5ms-35ms`.

If every candidate fails and the clock is critical, it plays the least-bad legal move rather than losing on time.

## Notes

Lichess may reject fractional challenge clocks if they are not currently supported by the public challenge endpoint. The code accepts and handles games with clock limits up to 30 seconds, so manually issued supported ultrabullet/bullet challenges still work.
