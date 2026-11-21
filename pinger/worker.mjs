// Sales-feed pinger: a Cloudflare Worker whose cron starts .github/workflows/sales-feed.yml
// (recent mode) every 5 minutes through workflow_dispatch.
//
// Why: GitHub runs `schedule` best-effort (the 5-minute cron fired once in 6 hours on
// 2026-11-03), while a workflow_dispatch starts within seconds. Ballbox marks the sales feed stale
// after 20 minutes, so the Pi's ballbox-sales-feed.timer stayed the primary feed. With this pinger
// the feed no longer needs the Pi. Overlapping runs are safe: the workflow's concurrency group
// keeps one running and one pending, and Ballbox replays an identical import.
//
// Secret: GITHUB_TOKEN, a fine-grained token for this repository only, "Actions: read and write".
// Deploy and token: pinger/README.md.

export const REPO = "sebasfavaron/vending-status";
export const WORKFLOW = "sales-feed.yml";

export async function dispatch(env, fetchImpl = fetch) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN is not set");
  const response = await fetchImpl(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json",
        "User-Agent": "sales-feed-pinger",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: env.REF || "main", inputs: { mode: "recent" } }),
    },
  );
  // 204 is the only success. Throwing marks the cron invocation as failed in Cloudflare's log;
  // the status is enough to diagnose (401 token, 403 scope, 404 workflow name, 422 inputs).
  if (response.status !== 204) {
    throw new Error(`workflow_dispatch failed: HTTP ${response.status}`);
  }
  return response.status;
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  // No HTTP surface: the worker only acts on its cron.
  async fetch() {
    return new Response("not found", { status: 404 });
  },
};
