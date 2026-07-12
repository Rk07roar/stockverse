# Deploying StockVest to Render (free)

Render's free web service tier runs the FastAPI backend (WebSockets, background
jobs) and serves the frontend from the same app — no separate frontend host
needed. It sleeps after 15 min of no traffic and takes ~30-50s to wake back up
on the next visit; that's the trade-off for $0.

`render.yaml` and `.gitignore` have already been added to your project folder.

## 1. Clean up the repo (run locally, in your project folder)

Your repo currently has `__pycache__/` files and the SQLite database
(`backend/stockvest.db`) committed to git. The database holds your real
portfolio/transaction data — if `stockverse` is a **public** GitHub repo,
that data is currently exposed. Untrack them:

**PowerShell** (what you're using — run each line separately, ignore any
"did not match any files" errors, those just mean that path wasn't tracked):

```powershell
git rm -r --cached backend/__pycache__
git rm -r --cached backend/api/__pycache__
git rm -r --cached backend/ml/__pycache__
git rm -r --cached backend/utils/__pycache__
git rm -r --cached backend/tools/__pycache__
git rm -r --cached backend/tests/__pycache__
git rm --cached backend/stockvest.db
git rm --cached backend/stockvest_backup_before_reset.db
git add .gitignore render.yaml
git commit -m "Add Render deploy config, stop tracking pycache/db"
git push
```

## 2. Create a Render account

1. Go to https://render.com and sign up (GitHub sign-in is fastest — it
   auto-grants repo access).
2. No credit card required for the free tier.

## 3. Deploy via Blueprint

1. In the Render dashboard, click **New** → **Blueprint**.
2. Select your `stockverse` repo (Rk07roar/stockverse). Render will detect
   `render.yaml` automatically and pre-fill the service config.
3. Click **Apply**. Render will:
   - Install `backend/requirements.txt`
   - Start the app with `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Generate a random `SECRET_KEY` for JWT auth
4. First build takes ~5-10 min (scipy/scikit-learn are slow to compile). Watch
   the **Logs** tab.
5. Once live, Render gives you a URL like `https://stockvest.onrender.com` —
   that's your permanent live link. Open it to confirm the app loads.

## Known limitations of the free tier

- **Cold starts**: after 15 min idle, the service sleeps. The next visitor
  waits ~30-50s for it to wake up.
- **No persistent disk**: `backend/stockvest.db` resets to empty on every
  redeploy or when the service restarts after sleeping. Portfolio/watchlist
  data won't survive across deploys. If you need real persistence, the next
  free-tier step would be swapping SQLite for Render's free PostgreSQL
  (90-day free instance, then $7/mo) — happy to wire that up if you want it.
- **512 MB RAM**: pandas/scikit-learn/scipy are loaded at startup. If the app
  crashes on boot with an out-of-memory error, let me know and I'll trim what
  loads eagerly.
- **Background jobs pause while asleep**: the price-refresh and alert-check
  loops only run while the instance is awake.

## After it's live

Send me the Render URL and I can verify the deployed app loads correctly and
check the logs if anything's off.
