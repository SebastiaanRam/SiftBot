# SiftBot 🤖📄

A serverless Telegram bot that delivers a daily digest of new scientific papers filtered by your research interests, learns from your feedback, and costs essentially nothing to run.

## What it does

1. Every morning, fetches new papers from arXiv, Semantic Scholar, and PubMed
2. Passes each abstract through an LLM to score relevance against your keyword profiles
3. Sends you a ranked Telegram digest with inline 👍 / 👎 / ❤️ rating buttons
4. A weekly job retrains a lightweight preference model on your ratings, improving future rankings

## Why this exists

Google Scholar alerts are email-only and not filterable. arXiv_recbot is the closest prior art but requires a permanently running Python process, is arXiv-only, and has no LLM filtering. This project is fully serverless, multi-source, LLM-filtered, and deployable in under 30 minutes.

## Architecture in one paragraph

A **GitHub Actions cron job** runs daily, fetches papers, calls an LLM API to score abstracts, stores results in **Supabase** (Postgres), and sends a digest via the Telegram Bot API. When users tap rating buttons, Telegram pushes the callback to a **Cloudflare Worker** webhook endpoint, which writes the rating to Supabase. A second weekly GitHub Actions job reads all ratings, retrains the preference model, and commits the weights back to the repo. No servers. No idle processes.

## Running cost estimate

| Component | Free tier limit | Expected usage |
|---|---|---|
| GitHub Actions | 2,000 min/month | ~10 min/day = 300 min/month |
| Cloudflare Workers | 100k req/day | <100 req/day |
| Supabase | 500 MB, 50k rows | ~1 MB/month |
| LLM API (Haiku) | — | ~$0.003/day = ~$0.10/month |

**Total: ~$0.10/month** (just the LLM API calls).

---

## Quick start

### Prerequisites

- A Telegram account + a bot token from [@BotFather](https://t.me/botfather)
- An [Anthropic API key](https://console.anthropic.com) (or OpenAI key if preferred)
- A [Supabase](https://supabase.com) project (free tier)
- A [Cloudflare](https://cloudflare.com) account (free tier)
- A GitHub account
- *(Optional)* A [Semantic Scholar API key](https://www.semanticscholar.org/product/api) for higher rate limits

### 1. Fork and clone

```bash
git clone https://github.com/your-username/SiftBot
cd SiftBot
```

### 2. Set up Supabase

1. Create a new Supabase project
2. Open the SQL editor and run `db/schema.sql`
3. Copy your project URL and `anon` key

### 3. Configure environment variables

```bash
cp .env.example .env
# Fill in all values in .env
```

For GitHub Actions, add the same variables as repository secrets:
`Settings → Secrets and variables → Actions → New repository secret`

Required secrets:
- `TELEGRAM_BOT_TOKEN`
- `ANTHROPIC_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_KEY`

### 4. Deploy the Cloudflare Worker

```bash
cd cloudflare
npm install
npx wrangler login
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_KEY
npx wrangler deploy
```

Copy the resulting `*.workers.dev` URL.

### 5. Register the Telegram webhook

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://your-worker.workers.dev/webhook"
```

### 6. Register yourself as the first user

Start a conversation with your bot and send `/start`. This registers your `chat_id` in the database.

### 7. Enable the GitHub Actions workflows

The workflows in `.github/workflows/` are set to run on schedule. Push to `main` to activate them, or trigger them manually from the Actions tab to test.

---

## Adding yourself as a user

Send `/start` to the bot. It will prompt you for keyword profiles — comma-separated topic descriptions, e.g.:

```
computational pathology, whole slide image analysis, vision language models in pathology
```

You can update your profile anytime with `/topics`.

## Self-hosting with Docker (alternative to serverless)

If you prefer running everything locally or on a VPS/homeserver:

```bash
touch SiftBot.db   # must exist as a file before first run; Docker would otherwise create it as a directory
docker compose up -d
```

This runs the scheduler and bot polling mode in a single container, using SQLite instead of Supabase.

---

## Project structure

```
SiftBot/
├── .github/
│   └── workflows/
│       ├── daily_digest.yml       # Runs every morning
│       └── weekly_retrain.yml     # Retrains preference model
├── worker/
│   ├── fetch.py                   # arXiv, Semantic Scholar, PubMed clients
│   ├── filter.py                  # LLM relevance scoring
│   ├── digest.py                  # Ranking + digest assembly
│   ├── retrain.py                 # Preference model training
│   └── main.py                    # Daily digest entrypoint
├── db/
│   ├── client.py                  # Supabase client wrapper
│   └── schema.sql                 # Database schema
├── cloudflare/
│   ├── webhook.js                 # Cloudflare Worker — commands & rating callbacks
│   └── wrangler.toml              # Cloudflare config
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## Attribution

Paper data from [Semantic Scholar](https://www.semanticscholar.org/):

> Kinney et al., *The Semantic Scholar Open Data Platform*, 2023. [arXiv:2301.10140](https://arxiv.org/abs/2301.10140)

---

## Contributing / Donating

<!-- If you find this useful and want to support it, a coffee is always appreciated: [ko-fi.com/yourname](https://ko-fi.com) -->

PRs are welcome.
