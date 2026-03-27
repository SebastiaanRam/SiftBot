# SiftBot

A Telegram bot that delivers a daily digest of new scientific papers filtered to your research interests. Papers are pulled from arXiv, Semantic Scholar, and PubMed, scored for relevance by an LLM, and ranked. Rate what you like — after 30 ratings the bot starts personalising your digest using a lightweight preference model.

---

## Using the bot

**[t.me/literature_sift_bot](https://t.me/literature_sift_bot)**

1. Open the link above and send `/start`
2. Describe your research interests as a comma-separated list, for example:
   ```
   computational pathology, whole slide image analysis, vision language models
   ```
3. Every morning you'll receive a ranked digest with inline rating buttons (👎 👍 ❤️)
4. Use `/topics` to update your interests at any time, `/pause` and `/resume` to control delivery

That's it!

---

## Self-hosting

If you want to run your own instance — for privacy, custom paper sources, or just to tinker — there are two deployment options.

### Option A — Serverless (GitHub Actions + Supabase + Cloudflare)

Zero infrastructure to manage. The daily digest runs as a GitHub Actions cron job; rating callbacks are handled by a Cloudflare Worker.

**Prerequisites**
- A Telegram bot token from [@BotFather](https://t.me/botfather)
- An [Anthropic API key](https://console.anthropic.com)
- A [Supabase](https://supabase.com) project (free tier is sufficient)
- A [Cloudflare](https://cloudflare.com) account (free tier)
- *(Optional)* A [Semantic Scholar API key](https://www.semanticscholar.org/product/api) for higher rate limits

**1. Fork and clone**

```bash
git clone https://github.com/your-username/SiftBot
cd SiftBot
```

**2. Set up Supabase**

Create a new project, open the SQL editor, and run `db/schema.sql`. Note your project URL and `anon` key.

**3. Configure secrets**

```bash
cp env.example .env
# Fill in all values
```

Add the same variables as repository secrets in GitHub:
`Settings → Secrets and variables → Actions → New repository secret`

Required: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`

**4. Deploy the Cloudflare Worker**

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

**5. Register the Telegram webhook**

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://your-worker.workers.dev"
```

**6. Activate workflows**

Push to `main` to activate the scheduled workflows, or trigger them manually from the Actions tab to test.

---

### Option B — Docker Compose (local server or VPS)

Runs the digest scheduler and bot in a single container using SQLite instead of Supabase. Good for a homeserver or VPS.

```bash
cp env.example .env
# Fill in TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY (Supabase vars not needed)
touch SiftBot.db
docker compose up -d
```

The container runs the daily digest on schedule and handles Telegram polling directly.

---

## Architecture

A **GitHub Actions cron job** runs daily, fetches papers from arXiv, Semantic Scholar, and PubMed, calls an LLM API to score abstracts against each user's keyword profile, stores results in **Supabase** (Postgres), and sends a digest via the Telegram Bot API. When users tap rating buttons, Telegram pushes the callback to a **Cloudflare Worker** webhook, which writes the rating to Supabase. A second weekly Actions job reads all ratings, retrains a per-user TF-IDF + logistic regression preference model, and commits the weights back to the repo.

## Project structure

```
SiftBot/
├── .github/workflows/
│   ├── daily_digest.yml       # Runs every morning at 07:00 UTC
│   └── weekly_retrain.yml     # Retrains preference models every Monday
├── worker/
│   ├── fetch.py               # arXiv, Semantic Scholar, PubMed clients
│   ├── filter.py              # LLM relevance scoring
│   ├── digest.py              # Ranking + Telegram sender
│   ├── retrain.py             # Preference model training
│   └── main.py                # Daily digest entrypoint
├── db/
│   ├── client.py              # Supabase client wrapper
│   └── schema.sql             # Database schema
├── cloudflare/
│   ├── webhook.js             # Cloudflare Worker — commands & rating callbacks
│   └── wrangler.toml
├── docker-compose.yml
├── requirements.txt
└── env.example
```

---

## Attribution

Paper data from [Semantic Scholar](https://www.semanticscholar.org/):

> Kinney et al., *The Semantic Scholar Open Data Platform*, 2023. [arXiv:2301.10140](https://arxiv.org/abs/2301.10140)
