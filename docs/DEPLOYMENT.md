# StockVest Deployment Guide

## Local Development

```bash
# 1. Clone / unzip project
cd stockvest_full

# 2. Backend
cd backend
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 3. Frontend — open in browser
open ../frontend/index.html
# Or serve with Python: python -m http.server 3000 --directory ../frontend
```

## Production (Linux / Ubuntu)

```bash
# Install dependencies
sudo apt update && sudo apt install python3.11 python3-pip nginx redis-server -y

# Backend service
pip install -r requirements.txt
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

# Nginx config (serve frontend + proxy API)
server {
    listen 80;
    root /var/www/stockvest/frontend;
    index index.html;
    location /api/ { proxy_pass http://127.0.0.1:8000; }
}

# Environment variables (.env file)
SECRET_KEY=your-production-secret-key
REDIS_URL=redis://localhost:6379
```

## Docker

```bash
# Build and run
docker compose up --build

# docker-compose.yml included in root
```

## Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| SECRET_KEY | dev-secret | JWT signing key |
| REDIS_URL | redis://localhost:6379 | Redis URL |
| PORT | 8000 | API server port |
| DEBUG | false | Enable debug mode |
