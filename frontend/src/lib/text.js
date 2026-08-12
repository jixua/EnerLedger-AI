const WINDOWS_1252_BYTES = new Map([
  [0x20ac, 0x80], [0x201a, 0x82], [0x0192, 0x83], [0x201e, 0x84],
  [0x2026, 0x85], [0x2020, 0x86], [0x2021, 0x87], [0x02c6, 0x88],
  [0x2030, 0x89], [0x0160, 0x8a], [0x2039, 0x8b], [0x0152, 0x8c],
  [0x017d, 0x8e], [0x2018, 0x91], [0x2019, 0x92], [0x201c, 0x93],
  [0x201d, 0x94], [0x2022, 0x95], [0x2013, 0x96], [0x2014, 0x97],
  [0x02dc, 0x98], [0x2122, 0x99], [0x0161, 0x9a], [0x203a, 0x9b],
  [0x0153, 0x9c], [0x017e, 0x9e], [0x0178, 0x9f],
]);

const MOJIBAKE_HINT = /(?:Ã|Â|â€|æ|è|å|ç|é|ä|ð|ï¿½|[\u0080-\u009f])/g;
const CJK_TEXT = /[\u3400-\u9fff]/;

function mojibakeScore(value) {
  return value.match(MOJIBAKE_HINT)?.length ?? 0;
}

export function repairLegacyMojibake(value) {
  if (typeof value !== "string" || mojibakeScore(value) < 2) return value;
  const bytes = [];
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    const byte = codePoint <= 0xff ? codePoint : WINDOWS_1252_BYTES.get(codePoint);
    if (byte === undefined) return value;
    bytes.push(byte);
  }
  try {
    const candidate = new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(bytes));
    if (!CJK_TEXT.test(candidate) || mojibakeScore(candidate) >= mojibakeScore(value)) return value;
    return candidate;
  } catch {
    return value;
  }
}

export function documentErrorMessage(document, fallback = "解析或索引失败") {
  const value = repairLegacyMojibake(document?.error_message);
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}
