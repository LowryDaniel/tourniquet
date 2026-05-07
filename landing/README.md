# Tourniquet landing page

Static HTML/CSS/JS — no build step required.

## Deploy to Cloudflare Pages (recommended)

1. Push this repo to GitHub.
2. In the Cloudflare dashboard: **Pages → Create application → Connect to Git** → select the repo.
3. Set **Framework preset** to `None`, **Build command** empty, **Output directory** to `landing`.
4. Add a custom domain: `tourniquet.dev`.
5. Done — `_headers` is picked up automatically and sets CSP + cache headers.

## Other platforms

- **GitHub Pages:** enable Pages on the repo, set source to the `landing/` directory (or copy files to `docs/`).
- **Vercel:** `vercel --prod` from inside `landing/`, or drag-and-drop the folder in the Vercel dashboard.
- **Any static server:** `cd landing && python3 -m http.server 8080`.
