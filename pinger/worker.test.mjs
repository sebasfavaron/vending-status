// node --test pinger/worker.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { dispatch } from "./worker.mjs";

function fakeFetch(status) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status });
  };
  return { calls, impl };
}

test("dispatches sales-feed.yml in recent mode on main", async () => {
  const { calls, impl } = fakeFetch(204);
  assert.equal(await dispatch({ GITHUB_TOKEN: "t" }, impl), 204);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "https://api.github.com/repos/sebasfavaron/vending-status/actions/workflows/sales-feed.yml/dispatches",
  );
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer t");
  assert.deepEqual(JSON.parse(calls[0].init.body), { ref: "main", inputs: { mode: "recent" } });
});

test("REF overrides the branch", async () => {
  const { calls, impl } = fakeFetch(204);
  await dispatch({ GITHUB_TOKEN: "t", REF: "test-branch" }, impl);
  assert.equal(JSON.parse(calls[0].init.body).ref, "test-branch");
});

test("fails on anything but 204, without leaking the token", async () => {
  for (const status of [401, 403, 404, 422, 500]) {
    const { impl } = fakeFetch(status);
    await assert.rejects(dispatch({ GITHUB_TOKEN: "secret-token" }, impl), (error) => {
      assert.match(error.message, new RegExp(`HTTP ${status}`));
      assert.doesNotMatch(error.message, /secret-token/);
      return true;
    });
  }
});

test("refuses without a token and does not call GitHub", async () => {
  const { calls, impl } = fakeFetch(204);
  await assert.rejects(dispatch({}, impl), /GITHUB_TOKEN is not set/);
  assert.equal(calls.length, 0);
});

test("fetch has no HTTP surface", async () => {
  assert.equal((await worker.fetch()).status, 404);
});

test("scheduled hands the dispatch to waitUntil", async () => {
  const pending = [];
  const original = globalThis.fetch;
  globalThis.fetch = fakeFetch(204).impl;
  try {
    await worker.scheduled({}, { GITHUB_TOKEN: "t" }, { waitUntil: (p) => pending.push(p) });
    assert.equal(pending.length, 1);
    assert.equal(await pending[0], 204);
  } finally {
    globalThis.fetch = original;
  }
});
