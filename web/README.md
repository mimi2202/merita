# Merita — Verdict Ledger (explorer)

A live, read-only explorer for Merita's verifications. Polls the public `/feed` endpoint
and renders every verdict — the sealed commitment, the pass/fail ruling, and the on-chain
tx that settled it. No secrets are reachable: `/feed` exposes only the public record.

## Run locally
    npm install
    npm run dev            # http://localhost:5173

Point it at a different backend with `VITE_API`:
    VITE_API=https://merita-asp.onrender.com npm run dev

## Build
    npm run build          # -> dist/

## Deploy (free, static)
The build is pure static files. Any of these work with zero config:
  - Cloudflare Pages: connect the repo, build `npm run build`, output `dist`
  - Vercel / Netlify: same — framework "Vite", output `dist`
  - GitHub Pages: push `dist/`

It talks to the ASP over the public `/feed` endpoint (CORS-open), so the explorer can be
hosted anywhere — it does not need to live on the same origin as the API.
