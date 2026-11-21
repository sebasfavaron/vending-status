# Sales-feed pinger

A Cloudflare Worker whose cron (`*/5 * * * *`) starts `.github/workflows/sales-feed.yml` in
`recent` mode through `workflow_dispatch`. GitHub's own `schedule` is best-effort (once in 6 h on
2026-11-03); a dispatch starts within seconds. Goal: the live sales feed stops depending on the
Pi's `ballbox-sales-feed.timer`.

Cost: 288 runs a day of about one minute each, free on a public repository. The Worker free plan
covers the cron. Overlapping runs are safe: the `sales-feed` concurrency group keeps one running
and one pending, and Ballbox replays an identical import.

## Setup (Sebas: token and deploy)

1. GitHub → Settings → Developer settings → Fine-grained tokens: repository
   `sebasfavaron/vending-status` only, permission **Actions: read and write**, nothing else.
   An expiry of one year (the pinger fails with HTTP 401 after it).
2. `cd pinger && npx wrangler login` (the Cloudflare account that already has R2).
3. `npx wrangler secret put GITHUB_TOKEN` and paste the token.
4. `npx wrangler deploy`.

## Proof before the Pi switch

- `gh run list -R sebasfavaron/vending-status -w sales-feed.yml -e workflow_dispatch -L 30`
  shows a run about every 5 minutes, all green.
- Cloudflare → Workers → `sales-feed-pinger` → Logs: cron invocations without errors
  (`workflow_dispatch failed: HTTP <status>`: 401 token, 403 scope, 404 workflow name).
- After 48 h without a gap above 20 minutes in "Ventas en vivo", propose turning off
  `ballbox-sales-feed.timer` on the Pi. Sebas decides; nobody turns it off from here.

Rollback: `npx wrangler delete` (or remove the cron in the dashboard). The `schedule` backup at
:07 and :37 keeps running either way.

Test: `node --test pinger/worker.test.mjs`.
