import assert from "node:assert/strict";
import test from "node:test";

import { bearerToken, tokensEqual } from "../src/auth.js";

test("service auth requires a non-empty exact bearer token", () => {
  assert.equal(bearerToken({ authorization: "Bearer token-value" }), "token-value");
  assert.equal(tokensEqual("token-value", "token-value"), true);
  assert.equal(tokensEqual("token-value", "token-other"), false);
  assert.equal(tokensEqual("", ""), false);
});
