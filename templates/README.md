# CODEXGOJO Dashboard — Vercel

Static site. No build step.

## Deploy
- CLI: `npm i -g vercel` then `vercel --prod` in this folder
- Or push to GitHub → Import in Vercel (Framework: Other, no build command, output dir blank)

## Backend
Vercel can't run your long-running Python bot. Without a backend the dashboard runs in DEMO mode (simulated data).
To connect your real backend (must be HTTPS), add this to vercel.json:

    "rewrites": [
      { "source": "/api/:path*", "destination": "https://YOUR-BACKEND-DOMAIN/api/:path*" }
    ]
