import assert from "node:assert/strict";
import test from "node:test";

import {
  expandedParentIds,
  flattenFolderTree,
  folderDescendantIds,
  folderPath,
} from "../src/lib/folder-tree.js";

const folders = [
  { id: 4, parent_id: 2, name: "量化方法" },
  { id: 1, parent_id: null, name: "标准文件" },
  { id: 3, parent_id: 1, name: "温室气体量化" },
  { id: 2, parent_id: null, name: "组织碳足迹" },
  { id: 5, parent_id: 999, name: "失联目录" },
];

test("folder tree keeps hierarchy, numeric sorting and orphan safety", () => {
  const rows = flattenFolderTree(folders);
  assert.deepEqual(rows.map(({ id, depth }) => [id, depth]), [
    [1, 0],
    [3, 1],
    [5, 0],
    [2, 0],
    [4, 1],
  ]);
  assert.equal(rows.find((row) => row.id === 1).hasChildren, true);
});

test("collapsed tree only hides descendants of collapsed folders", () => {
  const rows = flattenFolderTree(folders, { expandedIds: new Set([2]) });
  assert.deepEqual(rows.map((row) => row.id), [1, 5, 2, 4]);
});

test("folder path and descendants stop at the selected branch", () => {
  assert.equal(folderPath(folders[0], folders), "组织碳足迹 / 量化方法");
  assert.deepEqual([...folderDescendantIds(folders, 2)].sort(), [2, 4]);
  assert.deepEqual([...expandedParentIds(folders)].sort(), [1, 2]);
});
