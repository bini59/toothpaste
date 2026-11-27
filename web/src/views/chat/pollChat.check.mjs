import assert from "node:assert/strict";
import { test } from "node:test";
import { pollChat } from "./pollChat.ts";

const nextDelay = (value) => value?.busy ? 2_500 : 10_000;

test("polls faster while busy and returns to the idle delay after a reply", async ({ mock }) => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const read = mock.fn(async () => ({ busy: read.mock.callCount() === 0 }));
  const onValue = mock.fn();
  const onError = mock.fn();
  const poll = pollChat(read, onValue, onError, nextDelay);

  await poll.reload();
  mock.timers.tick(2_499);
  assert.equal(read.mock.callCount(), 1);
  mock.timers.tick(1);
  await Promise.resolve();
  assert.deepEqual(onValue.mock.calls.map(({ arguments: [value] }) => value.busy), [true, false]);
  mock.timers.tick(9_999);
  assert.equal(read.mock.callCount(), 2);
  mock.timers.tick(1);
  await Promise.resolve();
  assert.equal(read.mock.callCount(), 3);
  assert.equal(onError.mock.callCount(), 0);

  poll.dispose();
  mock.timers.tick(10_000);
  assert.equal(read.mock.callCount(), 3);
});

test("refresh supersedes an older response and disposal ignores a late error", async ({ mock }) => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const requests = [];
  const read = (signal) => new Promise((resolve, reject) => requests.push({ signal, resolve, reject }));
  const onValue = mock.fn();
  const onError = mock.fn();
  const poll = pollChat(read, onValue, onError, nextDelay);

  const old = poll.reload();
  mock.timers.tick(20_000);
  assert.equal(requests.length, 1);
  const refreshed = poll.reload();
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve({ busy: false });
  await refreshed;
  requests[0].resolve({ busy: true });
  await old;
  assert.deepEqual(onValue.mock.calls.map(({ arguments: [value] }) => value.busy), [false]);

  mock.timers.tick(10_000);
  poll.dispose();
  assert.equal(requests[2].signal.aborted, true);
  requests[2].reject(new Error("late network failure"));
  await Promise.resolve();
  await poll.reload();
  mock.timers.tick(20_000);
  assert.equal(onError.mock.callCount(), 0);
  assert.equal(requests.length, 3);
});

test("a network error keeps polling at the last known delay", async ({ mock }) => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const failure = new Error("temporary");
  const read = mock.fn(async () => {
    if (read.mock.callCount() === 1) throw failure;
    return { busy: true };
  });
  const onValue = mock.fn();
  const onError = mock.fn();
  const poll = pollChat(read, onValue, onError, nextDelay);

  await poll.reload();
  mock.timers.tick(2_500);
  await Promise.resolve();
  assert.deepEqual(onError.mock.calls[0].arguments, [failure]);
  mock.timers.tick(2_500);
  await Promise.resolve();
  assert.equal(onValue.mock.callCount(), 2);
  poll.dispose();
});

test("recovers when a request never settles", async ({ mock }) => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const hung = Promise.withResolvers();
  const onValue = mock.fn();
  const requests = [];
  const poll = pollChat((signal) => {
    requests.push(signal);
    return requests.length === 1 ? hung.promise : Promise.resolve({ busy: false });
  }, onValue, assert.fail, nextDelay);

  const original = poll.reload();
  mock.timers.tick(29_999);
  assert.equal(requests.length, 1);
  mock.timers.tick(1);
  await Promise.resolve();
  assert.equal(requests[0].aborted, true);
  assert.equal(requests.length, 2);
  hung.resolve({ busy: true });
  await original;
  assert.deepEqual(onValue.mock.calls.map(({ arguments: [value] }) => value.busy), [false]);

  poll.dispose();
});
