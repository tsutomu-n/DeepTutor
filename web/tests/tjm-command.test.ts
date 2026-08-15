import test from "node:test";
import assert from "node:assert/strict";

import { TjmCommandLedger } from "../lib/tjm-command";

test("a logical retry reuses the exact idempotency key and request body", () => {
  let sequence = 0;
  const ledger = new TjmCommandLedger(() => `key-${++sequence}`);
  const first = ledger.begin("answer:0", () => ({ elapsed_ms: 100, client_created_at: "first" }));
  const replay = ledger.begin("answer:0", () => ({ elapsed_ms: 999, client_created_at: "changed" }));

  assert.equal(replay, first);
  assert.deepEqual(replay, {
    key: "key-1",
    payload: { elapsed_ms: 100, client_created_at: "first" },
  });
});

test("completion or explicit intent change creates a new logical command", () => {
  let sequence = 0;
  const ledger = new TjmCommandLedger(() => `key-${++sequence}`);
  const first = ledger.begin("answer:0", () => ({ selected: "A" }));
  ledger.complete("answer:0", first.key);
  assert.equal(ledger.begin("answer:0", () => ({ selected: "A" })).key, "key-2");
  ledger.abandon("answer:0");
  assert.equal(ledger.begin("answer:0", () => ({ selected: "B" })).key, "key-3");
});

test('pending commands survive a same-tab reload and clear after completion', () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  }
  const firstLedger = new TjmCommandLedger(() => 'durable-key', storage, 'test-ledger')
  const first = firstLedger.begin('start:exam:exam', () => ({
    examId: 'exam',
    mode: 'exam',
  }))

  const reloadedLedger = new TjmCommandLedger(() => 'wrong-new-key', storage, 'test-ledger')
  const replay = reloadedLedger.begin('start:exam:exam', () => ({
    examId: 'changed',
    mode: 'practice',
  }))

  assert.deepEqual(replay, first)
  reloadedLedger.complete('start:exam:exam', replay.key)
  assert.equal(values.has('test-ledger'), false)
})
