import assert from "node:assert/strict";
import test from "node:test";

import { copyText } from "../src/lib/clipboard.js";

function fakeDocument(copyResult = true) {
  const calls = [];
  const textarea = {
    style: {},
    setAttribute: (...args) => calls.push(["setAttribute", ...args]),
    focus: () => calls.push(["focus"]),
    select: () => calls.push(["select"]),
    setSelectionRange: (...args) => calls.push(["setSelectionRange", ...args]),
    remove: () => calls.push(["remove"]),
  };

  return {
    calls,
    body: { appendChild: (node) => calls.push(["appendChild", node]) },
    createElement: (tag) => {
      calls.push(["createElement", tag]);
      return textarea;
    },
    execCommand: (command) => {
      calls.push(["execCommand", command]);
      return copyResult;
    },
  };
}

test("uses the Clipboard API when available", async () => {
  const writes = [];
  const copied = await copyText("回答内容", {
    navigatorObject: { clipboard: { writeText: async (value) => writes.push(value) } },
    documentObject: null,
  });

  assert.equal(copied, true);
  assert.deepEqual(writes, ["回答内容"]);
});

test("falls back when the Clipboard API is unavailable", async () => {
  const documentObject = fakeDocument();
  const copied = await copyText("回答内容", { navigatorObject: {}, documentObject });

  assert.equal(copied, true);
  assert.ok(documentObject.calls.some(([name, value]) => name === "execCommand" && value === "copy"));
  assert.equal(documentObject.calls.at(-1)[0], "remove");
});

test("falls back when the Clipboard API rejects permission", async () => {
  const documentObject = fakeDocument();
  const copied = await copyText("回答内容", {
    navigatorObject: { clipboard: { writeText: async () => { throw new Error("NotAllowedError"); } } },
    documentObject,
  });

  assert.equal(copied, true);
  assert.ok(documentObject.calls.some(([name]) => name === "execCommand"));
});

test("reports failure when neither copy path succeeds", async () => {
  const copied = await copyText("回答内容", {
    navigatorObject: {},
    documentObject: fakeDocument(false),
  });

  assert.equal(copied, false);
});
