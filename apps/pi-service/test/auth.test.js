import assert from "node:assert/strict";
import test from "node:test";

import { bearerToken, tokensEqual } from "../src/auth.js";

test("bearer auth is exact and timing-safe", () => {
  assert.equal(bearerToken({ authorization: "Bearer secret-value" }), "secret-value");
  assert.equal(tokensEqual("secret-value", "secret-value"), true);
  assert.equal(tokensEqual("secret-value", "other-value"), false);
});
