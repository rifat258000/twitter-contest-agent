# Deploying X Comment AI

This is a **Python (FastAPI) backend** + a server-rendered Jinja UI. It will **not** run on static-only hosts (Netlify free, Vercel free static, GitHub Pages, shared cPanel without Python). You need a host that runs Docker or Python.

The app is also stateful: it stores X session cookies in a SQLite DB (`accounts.db`). You need a **persistent disk / volume** so cookies survive restarts, otherwise login will be required after every redeploy.

---

## What you need

1. A **Groq API key** — https://console.groq.com/keys
2. A logged-in **X (Twitter) account**:
   - **Either** username/password/email/email-app-password (less reliable from cloud IPs because Cloudflare blocks login from datacenter IPs)
   - **Or** the `auth_token` and `ct0` cookies from your browser (recommended — works from any host)

To grab cookies: open https://x.com in Chrome → F12 → Application tab → Cookies → `https://x.com` → copy `auth_token` and `ct0` values.

3. A host that supports Docker or Python:

| Host | Difficulty | Free tier | Notes |
|---|---|---|---|
| **Render** | Easy | Yes (sleeps after 15min idle) | Native Docker support, persistent disk |
| **Railway** | Easy | Trial credit only | Native Docker, easy env vars |
| **Fly.io** | Easy | Yes | What this app currently runs on; great free tier |
| **DigitalOcean App Platform** | Easy | No | $5/mo minimum |
| **AWS App Runner** | Medium | No | $5–10/mo |
| **Any VPS** (Hetzner, Vultr, DO Droplet) | Medium | $4–6/mo | Most reliable for X scraping (residential-ish IPs) |

---

## Required environment variables

Set these in your host's "Environment" / "Secrets" panel — **never commit them to git**:

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | From https://console.groq.com/keys |
| `GROQ_MODEL` | No | Defaults to `llama-3.3-70b-versatile` |
| `X_AUTH_TOKEN` | Yes (cookie path) | The `auth_token` cookie from x.com |
| `X_CT0` | Yes (cookie path) | The `ct0` cookie from x.com |
| `X_TWSCRAPE_USERNAME` | Yes | The handle of the X account whose cookies you copied (no `@`) |
| `X_TWSCRAPE_PASSWORD` | No | Only needed if not using cookies |
| `X_TWSCRAPE_EMAIL` | No | Only needed if not using cookies |
| `X_TWSCRAPE_EMAIL_PASSWORD` | No | Gmail app password if not using cookies |
| `TWSCRAPE_DB_PATH` | No | Defaults to `/data/accounts.db` in the Docker image |

---

## Option A — Render (easiest, free tier available)

1. Create a new Git repo on GitHub and push the files from this zip.
2. In Render: **New → Web Service → Connect repo**.
3. Settings:
   - **Environment**: Docker
   - **Health check path**: `/health`
   - **Disk**: add a 1 GB persistent disk mounted at `/data`
4. Add the environment variables from the table above.
5. Deploy. Public URL appears in the Render dashboard.
6. Point your domain at the URL via Render's Custom Domain feature.

## Option B — Railway

1. Create a new Railway project from your GitHub repo.
2. Railway auto-detects the Dockerfile.
3. Add the env vars listed above.
4. Add a Volume mounted at `/data`.
5. Generate a public domain or attach yours under Settings → Networking.

## Option C — Fly.io

```bash
# install flyctl: https://fly.io/docs/flyctl/install/
fly launch --no-deploy        # answer the prompts; choose a region near you
fly volumes create app_data --size 1 --region <your-region>
fly secrets set GROQ_API_KEY=... X_AUTH_TOKEN=... X_CT0=... X_TWSCRAPE_USERNAME=...
fly deploy
fly certs create yourdomain.com   # CNAME the domain to <appname>.fly.dev
```

## Option D — Any VPS (Ubuntu)

```bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sh

# 2. Copy the zip onto the VPS, unzip
scp x-comment-ai.zip user@vps:~/
ssh user@vps
unzip x-comment-ai.zip && cd x-comment-ai

# 3. Create a .env file from .env.example, fill in real values
cp .env.example .env
nano .env

# 4. Build and run with persistent volume + env file
docker build -t x-comment-ai .
docker run -d --name x-comment-ai \
  -p 80:8000 \
  -v $(pwd)/data:/data \
  --env-file .env \
  --restart unless-stopped \
  x-comment-ai

# 5. Point your domain's A record at the VPS IP.
# 6. (Optional) Put Caddy or Nginx in front for HTTPS.
```

For HTTPS the easiest path is Caddy in front:

```caddyfile
yourdomain.com {
  reverse_proxy localhost:8000
}
```

`sudo apt install caddy && sudo systemctl restart caddy` and HTTPS auto-provisioning happens via Let's Encrypt.

---

## After deployment — sanity check

```
curl https://yourdomain.com/health
```

You should see:

```json
{
  "status": "ok",
  "accounts_total": 1,
  "accounts_active": 1,
  "groq_configured": true,
  "model": "llama-3.3-70b-versatile"
}
```

If `accounts_active` is `0`, the X cookies didn't take — re-grab them from a fresh browser session and update the env vars.
If `groq_configured` is `false`, your `GROQ_API_KEY` env var isn't set or is malformed.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `accounts_active: 0` | Cookies expired / wrong account | Re-grab `auth_token` + `ct0` from browser, update env, restart |
| Generate returns "Cloudflare blocked" | Datacenter IP banned by X | Use cookie auth (don't try password login from cloud), or move to a VPS with cleaner IP |
| All requests time out | App hasn't started — check logs | Most hosts show container logs in their UI |
| 503 "Groq is rate-limiting us" | Free-tier RPM exceeded | Upgrade Groq plan or wait a minute |
| 503 "no active twscrape accounts" | Cookies didn't refresh on startup | Re-deploy with correct cookie env vars |
| HTTPS errors / mixed content | Your domain isn't on HTTPS yet | Use Render/Railway/Fly built-in SSL, or Caddy on a VPS |

---

## Connecting your domain

- **Render / Railway / Fly**: Each has a "Custom domains" section. Add your domain there, then create the DNS record (CNAME or A) they tell you to. SSL certificate is auto-provisioned.
- **VPS**: Point your domain's A record to the VPS IP. Use Caddy for auto-HTTPS (snippet above).

Allow up to 15 minutes for DNS to propagate after pointing the domain.
