import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const modelsPageSource = await readFile(new URL("../src/pages/ModelsPage.jsx", import.meta.url), "utf8");

test("model registry uses the selected flat directory layout", () => {
  assert.match(modelsPageSource, /className="model-controls"[\s\S]*className="model-tabs"[\s\S]*className="search-control"/);
  assert.match(modelsPageSource, /className="model-list__header"/);
  assert.match(modelsPageSource, /<span>模型<\/span>[\s\S]*<span>能力<\/span>[\s\S]*<span>厂商 \/ 协议<\/span>[\s\S]*<span>状态<\/span>[\s\S]*<span>操作<\/span>/);
  assert.doesNotMatch(modelsPageSource, /paper-card model-registry/);
  assert.doesNotMatch(modelsPageSource, /model-row__endpoint/);
});

test("model registry keeps all existing management actions", () => {
  assert.match(modelsPageSource, /onClick=\{openCreateForm\}/);
  assert.match(modelsPageSource, /onClick=\{\(\) => openEditForm\(model\)\}/);
  assert.match(modelsPageSource, /onClick=\{\(\) => toggleModel\(model\)\}/);
  assert.match(modelsPageSource, /onClick=\{\(\) => removeModel\(model\)\}/);
});

test("editing uses a centered dialog while creation keeps the side sheet", () => {
  assert.match(modelsPageSource, /sheet-backdrop\$\{editingModel \? " sheet-backdrop--dialog" : ""\}/);
  assert.match(modelsPageSource, /form-sheet\$\{editingModel \? " form-sheet--dialog" : ""\}/);
  assert.match(modelsPageSource, /editingModel \? "关闭编辑模型弹窗" : "关闭新增模型表单"/);
});
