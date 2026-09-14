import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const playgroundSource = await readFile(
  new URL("../src/pages/PlaygroundPage.jsx", import.meta.url),
  "utf8",
);
const pageStyles = await readFile(
  new URL("../src/pages.css", import.meta.url),
  "utf8",
);

test("empty conversation centers the composer without the former hero title", () => {
  assert.doesNotMatch(playgroundSource, /把碳知识库/);
  assert.doesNotMatch(playgroundSource, /conversation-empty__intro/);
  assert.match(pageStyles, /\.conversation-empty \{[\s\S]*?grid-template-rows: minmax\(0, 1fr\) auto minmax\(0, 1fr\);/);
  assert.match(pageStyles, /\.conversation-empty__composer \{ width: 100%; grid-row: 2; \}/);
  assert.match(pageStyles, /\.conversation-empty > \.chat-suggestions \{ grid-row: 3; align-self: start; \}/);
  assert.doesNotMatch(pageStyles, /\.conversation-empty \{ width: calc\(100% - 28px\); justify-content: flex-start;/);
});
