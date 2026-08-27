import { timingSafeEqual } from "node:crypto";

export function bearerToken(headers) {
  const value = headers.authorization ?? "";
  return value.startsWith("Bearer ") ? value.slice(7).trim() : "";
}

export function tokensEqual(left, right) {
  const a = Buffer.from(left ?? "", "utf8");
  const b = Buffer.from(right ?? "", "utf8");
  return a.length > 0 && a.length === b.length && timingSafeEqual(a, b);
}
