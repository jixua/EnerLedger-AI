import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";

import {
  createDocumentBoundaryPlugin,
  insertDocumentBoundaryNodes,
  normalizeDocumentBoundaries,
  replaceDocumentPageMarkers,
} from "../src/lib/document-reader.js";

function documentTree() {
  return {
    type: "root",
    children: [
      { type: "heading", depth: 1, children: [], position: { start: { line: 1 }, end: { line: 1 } } },
      { type: "paragraph", children: [], position: { start: { line: 3 }, end: { line: 6 } } },
      { type: "table", children: [], position: { start: { line: 8 }, end: { line: 11 } } },
    ],
  };
}

test("normalizeDocumentBoundaries keeps every same-line boundary and assigns continuous reader indexes", () => {
  const normalized = normalizeDocumentBoundaries([
    { chunk_id: "late", boundary_index: 9, chunk_index: 9, start_line: 8 },
    { chunk_id: "same-b", boundary_index: 7, chunk_index: 7, start_line: 2 },
    { chunk_id: "same-a", boundary_index: 3, chunk_index: 3, start_line: 2 },
    { chunk_id: "without-line", boundary_index: 99 },
  ]);

  assert.deepEqual(normalized.map((entry) => entry.boundary.chunk_id), [
    "same-a",
    "same-b",
    "late",
    "without-line",
  ]);
  assert.deepEqual(normalized.map((entry) => entry.readerIndex), [0, 1, 2, 3]);
  assert.deepEqual(normalized.map((entry) => entry.anchorId), [
    "document-chunk-1",
    "document-chunk-2",
    "document-chunk-3",
    "document-chunk-4",
  ]);
});

test("insertDocumentBoundaryNodes uses top-level source positions and merges unsafe same-slot boundaries", () => {
  const tree = documentTree();
  const normalized = normalizeDocumentBoundaries([
    { chunk_id: "start", start_line: 0 },
    { chunk_id: "inside-a", boundary_index: 1, start_line: 3 },
    { chunk_id: "inside-b", boundary_index: 2, start_line: 3 },
    { chunk_id: "table", start_line: 7 },
  ]);

  insertDocumentBoundaryNodes(tree, normalized, "line");

  const boundaryNodes = tree.children.filter((node) => node.type === "documentChunkBoundary");
  assert.equal(boundaryNodes.length, 3);
  assert.equal(boundaryNodes[0].data.hProperties.boundaryIndexes, "0");
  assert.equal(boundaryNodes[0].data.hProperties.placementApproximate, "false");
  assert.equal(boundaryNodes[1].data.hProperties.boundaryIndexes, "1,2");
  assert.equal(boundaryNodes[1].data.hProperties.placementApproximate, "true");
  assert.equal(boundaryNodes[2].data.hProperties.boundaryIndexes, "3");
  assert.equal(boundaryNodes[2].data.hProperties.placementApproximate, "false");
  assert.deepEqual(
    tree.children.filter((node) => node.type !== "documentChunkBoundary").map((node) => node.type),
    ["heading", "paragraph", "table"],
  );
});

test("approximate semantic boundaries are never dropped and remain visibly approximate", () => {
  const tree = documentTree();
  const normalized = normalizeDocumentBoundaries([
    { chunk_id: "semantic-a", start_line: 2 },
    { chunk_id: "semantic-b", start_line: 2 },
    { chunk_id: "semantic-without-line" },
  ]);

  insertDocumentBoundaryNodes(tree, normalized, "approximate_line");

  const boundaryNodes = tree.children.filter((node) => node.type === "documentChunkBoundary");
  assert.equal(boundaryNodes.length, 2);
  assert.equal(boundaryNodes[0].data.hProperties.boundaryIndexes, "0,1");
  assert.equal(boundaryNodes[0].data.hProperties.placementApproximate, "true");
  assert.equal(boundaryNodes[1].data.hProperties.boundaryIndexes, "2");
  assert.equal(boundaryNodes[1].data.hProperties.placementApproximate, "true");
});

test("createDocumentBoundaryPlugin exposes a remark transformer", () => {
  const tree = documentTree();
  const normalized = normalizeDocumentBoundaries([{ chunk_id: "start", start_line: 0 }]);
  const transformer = createDocumentBoundaryPlugin(normalized, "line")();

  assert.equal(typeof transformer, "function");
  transformer(tree);
  assert.equal(tree.children[0].type, "documentChunkBoundary");
});

test("parser-only page markers stay out of the continuous reading document", () => {
  const tree = {
    type: "root",
    children: [
      { type: "html", value: "<!-- ODL_PAGE:1 -->", position: { start: { line: 1 }, end: { line: 1 } } },
      { type: "html", value: "<!-- WORD_PAGE:2 -->", position: { start: { line: 2 }, end: { line: 2 } } },
      { type: "html", value: "<!-- PAGE_FALLBACK:VISION -->", position: { start: { line: 2 }, end: { line: 2 } } },
      { type: "html", value: "<!-- PAGE_FALLBACK:OCR -->", position: { start: { line: 3 }, end: { line: 3 } } },
      { type: "heading", depth: 1, children: [], position: { start: { line: 4 }, end: { line: 4 } } },
      { type: "html", value: "<!-- keep this author comment -->", position: { start: { line: 5 }, end: { line: 5 } } },
    ],
  };
  const normalized = normalizeDocumentBoundaries([{ chunk_id: "start", start_line: 2 }]);

  insertDocumentBoundaryNodes(tree, normalized, "line");

  assert.equal(tree.children.some((node) => node.value === "<!-- ODL_PAGE:1 -->"), false);
  assert.equal(tree.children.some((node) => node.value === "<!-- WORD_PAGE:2 -->"), false);
  assert.equal(tree.children.some((node) => node.value === "<!-- PAGE_FALLBACK:VISION -->"), false);
  assert.equal(tree.children.some((node) => node.value === "<!-- PAGE_FALLBACK:OCR -->"), false);
  assert.equal(tree.children.some((node) => node.value === "<!-- keep this author comment -->"), true);
  assert.equal(tree.children[0].type, "documentChunkBoundary");
  assert.equal(tree.children[1].type, "heading");
});

test("Word and PDF page comments become visible reader page markers", () => {
  const tree = {
    type: "root",
    children: [
      { type: "html", value: "<!-- WORD_PAGE:3 -->" },
      { type: "paragraph", children: [] },
      { type: "html", value: "<!-- ODL_PAGE:4 -->" },
    ],
  };

  replaceDocumentPageMarkers(tree);

  assert.equal(tree.children[0].type, "documentPageMarker");
  assert.equal(tree.children[0].data.hProperties.pageNumber, "3");
  assert.equal(tree.children[0].data.hProperties.markerType, "WORD_PAGE");
  assert.equal(tree.children[2].data.hProperties.pageNumber, "4");
  assert.equal(tree.children[2].data.hProperties.markerType, "ODL_PAGE");
});

test("ReactMarkdown renders the custom top-level boundary node in a single parse", () => {
  const normalized = normalizeDocumentBoundaries([
    { chunk_id: "same-a", boundary_index: 0, start_line: 0 },
    { chunk_id: "same-b", boundary_index: 1, start_line: 0 },
  ]);
  const plugin = createDocumentBoundaryPlugin(normalized, "approximate_line");
  const html = renderToStaticMarkup(createElement(
    ReactMarkdown,
    {
      remarkPlugins: [plugin],
      components: {
        "document-chunk-boundary": ({ node }) => createElement(
          "span",
          { "data-testid": "boundary" },
          `${node.properties.boundaryIndexes}:${node.properties.placementApproximate}`,
        ),
      },
    },
    "# 标题\n\n正文",
  ));

  assert.match(html, /data-testid="boundary">0,1:true<\/span>\s*<h1>标题<\/h1>/);
  assert.equal(html.match(/data-testid="boundary"/g)?.length, 1);
});
