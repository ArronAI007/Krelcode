import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { existsSync } from "node:fs";

import {
  KrelcodeClient,
  type KrelcodeOptions,
  KrelcodeResultError,
  PROTOCOL_NAME,
  PROTOCOL_VERSION,
} from "../src/index.ts";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");

function options(overrides: KrelcodeOptions = {}): KrelcodeOptions {
  return {
    workspace: root,
    executable: process.env.PYTHON ?? (existsSync("/opt/anaconda3/bin/python")
      ? "/opt/anaconda3/bin/python"
      : "python3"),
    env: {PYTHONPATH: `${root}/src`},
    mock: true,
    noSession: true,
    noContext: true,
    noMcp: true,
    noSkills: true,
    noCheckpoint: true,
    noWeb: true,
    startupTimeoutMs: 30_000,
    closeTimeoutMs: 5_000,
    ...overrides,
  };
}

test("command keeps API keys out of argv", () => {
  const client = new KrelcodeClient(options({env: {DEEPSEEK_API_KEY: "sk-test-secret"}}));
  const environment = (client as unknown as {options: {env: Record<string, string>}}).options.env;
  assert.equal(environment.DEEPSEEK_API_KEY, "sk-test-secret");
  assert.equal(client.command().includes("sk-test-secret"), false);
  assert.equal(client.stderrTail(), "");
});

test("streams query and preserves request id", async () => {
  const client = await KrelcodeClient.start(options());
  try {
    assert.equal(client.init?.get("protocol"), PROTOCOL_NAME);
    assert.equal(client.init?.get("protocol_version"), PROTOCOL_VERSION);
    const messages = [];
    for await (const message of client.query("hello", {requestId: "ts-1"})) {
      messages.push(message);
    }
    assert.ok(messages.some((message) => message.type === "assistant"));
    assert.equal(messages.at(-1)?.type, "result");
    assert.equal(messages.at(-1)?.requestId, "ts-1");
    assert.equal(messages.at(-1)?.isError, false);
  } finally {
    await client.close();
  }
});

test("routes concurrent queries by request id", async () => {
  const client = await KrelcodeClient.start(options());
  try {
    const results = await Promise.all([
      client.run("one", {requestId: "one"}),
      client.run("two", {requestId: "two"}),
    ]);
    assert.equal(results.length, 2);
    assert.ok(results[0].includes("mock") || results[0].length > 0);
    assert.ok(results[1].length > 0);
  } finally {
    await client.close();
  }
});

test("interrupt is a control message and close is idempotent", async () => {
  const client = await KrelcodeClient.start(options());
  assert.equal(await client.interrupt(), true);
  await client.close();
  await client.close();
  assert.equal(await client.interrupt(), false);
});

test("result errors throw a typed error", async () => {
  const client = await KrelcodeClient.start(options());
  try {
    const result = await client.run("hello");
    assert.equal(typeof result, "string");
  } finally {
    await client.close();
  }
});
