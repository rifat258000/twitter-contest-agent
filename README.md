# twitter-contest-agent

An AI agent that scans Twitter/X for **meme contests** and **AI events / hackathons**,
extracts prize-pool and deadline info using a local LLM (Ollama), ranks posts by
engagement, and pushes the top new ones to a **Telegram** channel.

> **Reality check.** Free Twitter scraping is fragile:
> - `snscrape` no longer works.
> - `twscrape` works but requires real X accounts that you register with it.
> - Public Nitter instances are often offline or rate-limited.
>
> This agent tries `twscrape` first and falls back to Nitter. If you want
> reliable data, plug in your own X accounts for `twscrape` or switch to a
> paid API.

---

## Architecture

```
┌──────────┐   ┌───────────┐   ┌──────────┐   ┌─────────┐   ┌──────────┐
│ queries  │ → │  scraper  │ → │ extractor│ → │ ranker  │ → │ telegram │
│          │   │ twscrape  │   │  ollama  │   │ engage- │   │ poster   │
│          │   │  Nitter   │   │   +      │   │ ment    │   │          │
│          │   │           │   │  regex   │   │ score   │   │          │
└──────────┘   └───────────┘   └──────────┘   └─────────┘   └──────────┘
                                                  │
                                             ┌────┴─────┐
                                             │  dedup   │
                                             │  sqlite  │
                                             └──────────┘
```

- **`src/contest_agent/scraper.py`** — searches Twitter via `twscrape` then Nitter.
- **`src/contest_agent/extractor.py`** — asks Ollama to return a strict JSON blob with
  `is_contest_or_event`, `contest_type`, `prize_pool`, `deadline`, `tags`, `summary`.
  Falls back to regex/keyword matching if Ollama is unreachable.
- **`src/contest_agent/ranking.py`** — weighted engagement score
  (`likes*w_l + retweets*w_r + replies*w_rep + quotes*w_q + views*w_v`).
- **`src/contest_agent/dedup.py`** — SQLite store of already-posted tweet IDs.
- **`src/contest_agent/telegram.py`** — sends formatted HTML messages via the Bot API.
- **`src/contest_agent/agent.py`** — orchestrates one run.
- **`src/contest_agent/cli.py`** — `contest-agent run`, `contest-agent schedule`,
  `contest-agent test-telegram`.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/rifat258000/twitter-contest-agent.git
cd twitter-contest-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` with your `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (see below).

### 1. Telegram bot

1. Open Telegram, chat with [@BotFather](https://t.me/BotFather), run `/newbot`,
   follow the prompts, copy the token into `.env` → `TELEGRAM_BOT_TOKEN`.
2. Create a channel or group, add your bot as an **admin** with permission to post.
3. Send any message in the channel, then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the `chat.id`
   (negative for groups/channels) into `.env` → `TELEGRAM_CHAT_ID`.
4. Verify:
   ```bash
   contest-agent test-telegram
   ```

### 2. Ollama (optional but recommended)

Install from <https://ollama.com>. Then pull a small instruct model:

```bash
ollama pull llama3.1:8b          # or phi3, qwen2.5:7b, mistral
ollama serve &                    # runs on http://localhost:11434
```

The agent will auto-detect Ollama; if it's unreachable, extraction falls back
to regex/keyword matching (lower quality but still works).

### 3. Twitter scraping (choose one)

**Option A — RapidAPI (recommended).** Sign up free at
<https://rapidapi.com/alexanderxbx/api/twitter-api45>, subscribe to the Basic
(Free) plan, and copy your `X-RapidAPI-Key` into `.env → RAPIDAPI_KEY`.
Free tier allows ~500 requests/month. Works from any IP, including cloud.

**Option B — `twscrape`.** You need at least one real X account. Accounts get
logged in and their session tokens are saved to SQLite.

```bash
mkdir -p data
# Create accounts.txt in this format (no header, colon-separated):
#   username:password:email:email_password
twscrape --db ./data/twscrape.db add_accounts accounts.txt username:password:email:email_password
twscrape --db ./data/twscrape.db login_accounts
```

**X usually blocks logins from datacenter / cloud IPs with a Cloudflare 403**;
if you hit that, use Option A instead or run the agent from a residential IP.

**Option C — Nitter fallback.** Nothing to configure; the agent rotates through
the instances in `NITTER_INSTANCES`. Expect frequent failures.

## Run

One-shot, dry-run (no Telegram posts):

```bash
contest-agent run --dry-run -v
```

One-shot, live:

```bash
contest-agent run
```

Continuous polling (every `POLL_INTERVAL_MINUTES`):

```bash
contest-agent schedule
```

## Deploy (free, via GitHub Actions)

This repo ships a `.github/workflows/scheduled-run.yml` workflow that runs the
agent every 30 minutes on GitHub's free runners. No server required.

1. Fork / push this repo to GitHub (done if you're reading this).
2. In the repo go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `RAPIDAPI_KEY`
3. Go to **Actions** tab → enable workflows if prompted.
4. The workflow triggers on cron, or manually via **Run workflow** (dry-run supported).

Dedup state is persisted between runs using Actions cache (`data/posted.db`).
Free GitHub Actions minutes are unlimited on public repos and 2000/month on
private repos — a 30-minute cron uses ~90 runs × ~1 min = 90 min/month, well
under the cap.

## Configuration

All settings live in `.env`. See `.env.example` for the full list. Highlights:

| Key | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Where to post. |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local LLM endpoint + model tag. |
| `NITTER_INSTANCES` | Comma-separated fallback Nitter hosts. |
| `WEIGHT_LIKES`, `WEIGHT_RETWEETS`, ... | Engagement weights. |
| `POLL_INTERVAL_MINUTES` | How often `schedule` fires a run. |
| `MIN_ENGAGEMENT_SCORE` | Floor below which a tweet is dropped. |
| `TOP_N_PER_RUN` | Max posts per run (after dedup). |
| `DEDUP_DB_PATH` | Where the "already posted" SQLite lives. |

Default search queries live in `src/contest_agent/queries.py`. Edit
`DEFAULT_QUERIES` to tune what the agent looks for.

## Tests / lint

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## Caveats

- Nitter output sometimes lacks reliable view counts; the ranking weights are
  set so the agent still works without them.
- The regex fallback is conservative — it requires both a category keyword
  (meme / hackathon / ...) AND a prize keyword before classifying a tweet as
  a contest.
- Dedup is by tweet ID only. If the same contest is announced in multiple
  tweets, each unique tweet may post once.
- This project does **not** run Twitter auth flows on your behalf. For
  `twscrape` you supply the accounts yourself.

## License

MIT.
