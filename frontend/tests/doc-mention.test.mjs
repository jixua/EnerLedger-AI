import assert from "node:assert/strict";
import test from "node:test";

import {
  MENTION_CANDIDATE_LIMIT,
  applyMention,
  documentIdOf,
  filterMentionCandidates,
  mentionQueryAt,
  mentionRuns,
  referencesDocument,
  removeMention,
  resolveMentionedDocIds,
} from "../src/lib/doc-mention.js";

const 报告 = { document_id: 7, filename: "伊顿碳足迹报告.docx", dataset_name: "碳知识库" };
const 清单 = { document_id: 9, filename: "产品清单 2023.xlsx", dataset_name: "碳知识库" };
const 其他 = { document_id: 11, filename: "差旅报销单.docx", dataset_name: "行政" };

test("@ 只在行首或空白之后触发，邮箱里的 @ 不算", () => {
  assert.deepEqual(mentionQueryAt("请解释 @伊顿", 8), { start: 4, query: "伊顿" });
  assert.deepEqual(mentionQueryAt("@清", 2), { start: 0, query: "清" });
  assert.equal(mentionQueryAt("联系 a@b.com", 11), null);
  // @ 之后打了空格说明这句话已经写完，不再是「正在选文档」
  assert.equal(mentionQueryAt("@伊顿 报告", 7), null);
  assert.equal(mentionQueryAt("没有任何引用", 6), null);
  // 光标在 @ 之前时，不应该把后面的 @ 也算进来
  assert.equal(mentionQueryAt("请解释 @伊顿", 2), null);
});

test("候选：文件名前缀优先，其次包含，最后才按数据集名兜底", () => {
  const documents = [其他, 清单, 报告];

  assert.deepEqual(
    filterMentionCandidates(documents, "伊顿").map(documentIdOf),
    [7]
  );
  assert.deepEqual(
    filterMentionCandidates(documents, "清单").map(documentIdOf),
    [9]
  );
  // 两个碳知识库的文档都能被数据集名搜到，但不该把「行政」那份也带出来
  assert.deepEqual(
    filterMentionCandidates(documents, "碳知识库").map(documentIdOf),
    [9, 7]
  );
  assert.deepEqual(filterMentionCandidates(documents, "报销").map(documentIdOf), [11]);
});

test("候选：打开面板就该给出整个库，不该只给前几条", () => {
  const many = Array.from({ length: 48 }, (_, index) => ({
    document_id: index + 1,
    filename: `文档-${index + 1}.pdf`,
  }));

  // 这里曾经只有 8 条：用户翻不到自己的文件，只能靠打字去撞
  assert.equal(filterMentionCandidates(many, "").length, 48);
  // 上限仍然存在，只是用来兜住超大知识库，可以按需收窄
  assert.deepEqual(filterMentionCandidates(many, "", 3).map(documentIdOf), [1, 2, 3]);
  assert.ok(MENTION_CANDIDATE_LIMIT >= 50, "上限太小会让用户翻不到库里的文件");
  // 没有 id 或没有文件名的条目进不了候选：@ 了也没法上报
  assert.deepEqual(filterMentionCandidates([{ filename: "无 id.pdf" }, { document_id: 3 }], ""), []);
});

test("选中文件后用 @文件名 替换正在输入的查询串", () => {
  const result = applyMention("请解释 @伊顿 的内容", { start: 4, query: "伊顿" }, 报告.filename);

  // 留一个尾随空格，否则用户接着打字会把文件名和正文粘成一个词
  assert.equal(result.text, "请解释 @伊顿碳足迹报告.docx  的内容");
  assert.equal(result.caret, "请解释 @伊顿碳足迹报告.docx ".length);

  const atEnd = applyMention("@清", { start: 0, query: "清" }, 报告.filename);
  assert.equal(atEnd.text, "@伊顿碳足迹报告.docx ");
  assert.equal(atEnd.caret, atEnd.text.length);
});

test("用户把 @文件名 删掉就不再算引用", () => {
  const kept = "用 @伊顿碳足迹报告.docx 生成报告";
  const dropped = "用刚才那份文档生成报告";

  assert.equal(referencesDocument(kept, 报告), true);
  assert.equal(referencesDocument(dropped, 报告), false);
  assert.deepEqual(resolveMentionedDocIds(kept, 报告), [7]);
  assert.deepEqual(resolveMentionedDocIds(dropped, 报告), []);
  assert.deepEqual(resolveMentionedDocIds(kept, null), []);
});

test("移除引用时连尾随空格一起收掉，不留双空格", () => {
  assert.equal(removeMention("用 @伊顿碳足迹报告.docx 生成报告", 报告), "用 生成报告");
  assert.equal(removeMention("@伊顿碳足迹报告.docx 讲了什么", 报告), "讲了什么");
});

test("着色片段：把引用切出来，拼接后与原文一字不差", () => {
  const text = "用 @伊顿碳足迹报告.docx 生成报告";
  const runs = mentionRuns(text, 报告);

  assert.deepEqual(runs, [
    { type: "text", text: "用 " },
    { type: "mention", text: "@伊顿碳足迹报告.docx" },
    { type: "text", text: " 生成报告" },
  ]);
  // 覆盖层是重画一遍同样的文字，拼接结果必须与输入框里的值完全一致，否则会错行
  assert.equal(runs.map((run) => run.text).join(""), text);
});

test("着色片段：没有引用、或引用被删掉时，整段都是普通文字", () => {
  assert.deepEqual(mentionRuns("用刚才那份文档生成报告", 报告), [
    { type: "text", text: "用刚才那份文档生成报告" },
  ]);
  assert.deepEqual(mentionRuns("", 报告), []);
  assert.deepEqual(mentionRuns("随便写点什么", null), [{ type: "text", text: "随便写点什么" }]);
});

test("着色片段：多行与重复引用都不改变原文", () => {
  const text = "@伊顿碳足迹报告.docx 这份\n和 @伊顿碳足迹报告.docx 那份一样吗？";
  const runs = mentionRuns(text, 报告);

  assert.equal(runs.filter((run) => run.type === "mention").length, 2);
  assert.equal(runs.map((run) => run.text).join(""), text);
});
