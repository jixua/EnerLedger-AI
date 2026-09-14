import assert from "node:assert/strict";
import test from "node:test";

import { hasDocumentPageCount } from "../src/lib/document-metadata.js";

test("HTML never exposes a synthetic page count", () => {
  assert.equal(hasDocumentPageCount({ file_type: "html", page_count: 8 }), false);
  assert.equal(hasDocumentPageCount({ file_type: "HTM", page_count: 3 }), false);
});

test("paginated documents keep a recorded page count", () => {
  assert.equal(hasDocumentPageCount({ file_type: "pdf", page_count: 8 }), true);
  assert.equal(hasDocumentPageCount({ file_type: "pdf", page_count: null }), false);
});
