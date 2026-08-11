const CHINESE_RECALL_NUMBER_MAP = {
  一: 1,
  二: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  七: 7,
  八: 8,
  九: 9,
};

export function parseRecallMentionNumber(value) {
  const normalized = String(value ?? "").trim();
  if (/^\d+$/.test(normalized)) return Number(normalized);
  if (CHINESE_RECALL_NUMBER_MAP[normalized]) return CHINESE_RECALL_NUMBER_MAP[normalized];
  if (normalized === "十") return 10;
  if (!/^[一二三四五六七八九十]+$/.test(normalized)) return null;

  const [tenPart, unitPart] = normalized.split("十");
  const tens = tenPart ? CHINESE_RECALL_NUMBER_MAP[tenPart] : 1;
  const units = unitPart ? CHINESE_RECALL_NUMBER_MAP[unitPart] : 0;
  if (!Number.isFinite(tens) || !Number.isFinite(units)) return null;
  return tens * 10 + units;
}

export function findHitByCitationIndex(hits, citationIndex) {
  if (!Number.isFinite(citationIndex) || citationIndex < 1) return null;
  return (hits ?? []).find((hit) => Number(hit.citation_index ?? hit.citationIndex) === citationIndex) ?? null;
}

export function recallChunkNumberFromHref(href) {
  const match = /^#recall-chunk-(\d+)$/.exec(String(href ?? ""));
  return match ? Number(match[1]) : null;
}

export function linkifyRecallChunkMentions(content, hits) {
  if (!content || !Array.isArray(hits) || hits.length === 0) return content;

  const replaceMentions = (text) => text.replace(
    /(?:[\[［【]\s*片段\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*[\]］】]|片段\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3}))/g,
    (match, bracketedNumber, plainNumber) => {
      const index = parseRecallMentionNumber(bracketedNumber ?? plainNumber);
      if (!findHitByCitationIndex(hits, index)) return match;
      return `[片段${index}](#recall-chunk-${index})`;
    },
  );

  let inCodeFence = false;
  return content
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        inCodeFence = !inCodeFence;
        return line;
      }
      if (inCodeFence) return line;

      const protectedPattern = /(`[^`]*`|\[[^\]]+\]\([^)]+\))/g;
      let nextLine = "";
      let lastIndex = 0;
      let match;
      while ((match = protectedPattern.exec(line))) {
        nextLine += replaceMentions(line.slice(lastIndex, match.index));
        nextLine += match[0];
        lastIndex = match.index + match[0].length;
      }
      nextLine += replaceMentions(line.slice(lastIndex));
      return nextLine;
    })
    .join("\n");
}
