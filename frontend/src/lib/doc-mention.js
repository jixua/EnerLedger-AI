/**
 * 输入框里的 @ 文档引用。
 *
 * 引用的身份分两半：
 *
 * - **文本**里写 `@文件名`：模型看得到「用户指的是哪一份」，意图识别（生成报告还是
 *   解释）才有依据，用户自己回头读这句话也读得懂。
 * - **结构化身份**走 `doc_ids`：document_id 是冻结来源，靠文件名去猜是不可靠的，
 *   @ 选中的文档必须带 id 上报。
 *
 * 文本是「还引用着吗」的唯一依据：用户把 @文件名 删掉，就不再算引用——否则会出现
 * 输入框里一个字都没提，报告却按那份文档生成的怪事。所以这里只做纯文本判断，
 * 不额外维护一份引用状态。
 */

export const MENTION_TRIGGER = "@";

/**
 * 一次最多渲染多少个候选。面板本身可滚动，所以这个数只是防止极端大的库把 DOM 撑爆，
 * 不是「让用户去打字筛」——翻不到自己的文件比列表长更让人恼火。
 */
export const MENTION_CANDIDATE_LIMIT = 50;

/** 文档身份兼容：列表接口历史上用过 id 与 document_id 两种字段名。 */
export function documentIdOf(document) {
  const id = document?.document_id ?? document?.id;
  return id === null || id === undefined ? null : Number(id);
}

export function documentNameOf(document) {
  const name = document?.filename ?? document?.title ?? "";
  return String(name).trim();
}

/**
 * 光标是否落在一次 @ 查询里。
 *
 * 只在「@ 落在行首或紧跟空白」时才算——邮箱、`a@b`、代码里的装饰符都不该弹出面板。
 * 返回 {start, query}：start 是 @ 的位置，query 是 @ 之后到光标处的文字。
 */
export function mentionQueryAt(text, caret) {
  const source = String(text ?? "");
  const position = Number.isInteger(caret) ? Math.min(Math.max(caret, 0), source.length) : source.length;
  const upto = source.slice(0, position);
  const at = upto.lastIndexOf(MENTION_TRIGGER);
  if (at === -1) return null;
  const previous = at > 0 ? upto[at - 1] : "";
  if (previous && !/\s/.test(previous)) return null;
  const query = upto.slice(at + 1);
  // 查询串里出现空白说明这次 @ 已经写完了，不再是「正在选」
  if (/\s/.test(query)) return null;
  return { start: at, query };
}

/** 候选按文件名匹配优先，数据集名次之；没有查询串时给最近用过的文档。 */
export function filterMentionCandidates(documents, query, limit = MENTION_CANDIDATE_LIMIT) {
  const keyword = String(query ?? "").trim().toLowerCase();
  const items = (Array.isArray(documents) ? documents : []).filter(
    (item) => documentIdOf(item) !== null && documentNameOf(item)
  );
  if (!keyword) return items.slice(0, limit);
  const scored = [];
  for (const item of items) {
    const name = documentNameOf(item).toLowerCase();
    const dataset = String(item?.dataset_name ?? "").toLowerCase();
    if (name.includes(keyword)) {
      scored.push({ item, rank: name.startsWith(keyword) ? 0 : 1 });
    } else if (dataset.includes(keyword)) {
      scored.push({ item, rank: 2 });
    }
  }
  return scored
    .sort((left, right) => left.rank - right.rank)
    .slice(0, limit)
    .map((entry) => entry.item);
}

/**
 * 把正在输入的 `@查询` 换成 `@文件名`，并给出新的光标位置。
 * 文件名后的空格是刻意留的：不留一个空格，用户接着打字会变成 `@文件名讲了什么`，
 * 那串文字既不好读，也没法再识别出引用的边界。
 */
export function applyMention(text, span, filename) {
  const source = String(text ?? "");
  const start = span?.start ?? source.length;
  const caret = start + 1 + String(span?.query ?? "").length;
  const name = String(filename ?? "").trim();
  const inserted = `${MENTION_TRIGGER}${name} `;
  return {
    text: `${source.slice(0, start)}${inserted}${source.slice(caret)}`,
    caret: start + inserted.length,
  };
}

/** 文本里是否还写着这份文档的 @ 引用——用户删掉了就不再引用。 */
export function referencesDocument(text, document) {
  const name = documentNameOf(document);
  if (!name) return false;
  return String(text ?? "").includes(`${MENTION_TRIGGER}${name}`);
}

/** 从文本里摘掉这份文档的 @ 引用，返回清理后的文本。 */
export function removeMention(text, document) {
  const name = documentNameOf(document);
  if (!name) return String(text ?? "");
  return String(text ?? "").replace(`${MENTION_TRIGGER}${name} `, "").replace(`${MENTION_TRIGGER}${name}`, "");
}

/**
 * 发送前收敛：文本里还引用着就把 document_id 带上，删掉了就不带。
 * 返回 doc_ids 的数组形态，直接进请求体。
 */
export function resolveMentionedDocIds(text, document) {
  if (!document) return [];
  if (!referencesDocument(text, document)) return [];
  const id = documentIdOf(document);
  return id === null ? [] : [id];
}

/**
 * 把正文切成「普通文字」与「@ 引用」两种片段，供输入框上那层着色用。
 *
 * 输入框是 textarea，没法给其中一段文字单独上色，所以着色靠一层与它逐字对齐的
 * 覆盖层来画。切片的判据必须与 ``referencesDocument`` 一致——颜色是「这一处还算
 * 引用」的唯一提示，两者走岔就会骗人（颜色还在，其实已经不算引用）。
 */
export function mentionRuns(text, document) {
  const source = String(text ?? "");
  if (!source) return [];
  const name = documentNameOf(document);
  if (!name) return [{ type: "text", text: source }];

  const token = `${MENTION_TRIGGER}${name}`;
  const runs = [];
  let cursor = 0;
  for (;;) {
    const at = source.indexOf(token, cursor);
    if (at === -1) break;
    if (at > cursor) runs.push({ type: "text", text: source.slice(cursor, at) });
    runs.push({ type: "mention", text: token });
    cursor = at + token.length;
  }
  if (cursor < source.length) runs.push({ type: "text", text: source.slice(cursor) });
  return runs;
}
