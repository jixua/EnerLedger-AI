function finiteInteger(value) {
  const number = Number(value);
  return Number.isInteger(number) ? number : null;
}

function boundaryOrder(left, right) {
  const leftLine = finiteInteger(left.boundary?.start_line);
  const rightLine = finiteInteger(right.boundary?.start_line);
  const normalizedLeftLine = leftLine !== null && leftLine >= 0 ? leftLine : Number.MAX_SAFE_INTEGER;
  const normalizedRightLine = rightLine !== null && rightLine >= 0 ? rightLine : Number.MAX_SAFE_INTEGER;
  if (normalizedLeftLine !== normalizedRightLine) return normalizedLeftLine - normalizedRightLine;

  const leftBoundaryIndex = finiteInteger(left.boundary?.boundary_index);
  const rightBoundaryIndex = finiteInteger(right.boundary?.boundary_index);
  const normalizedLeftBoundaryIndex = leftBoundaryIndex ?? Number.MAX_SAFE_INTEGER;
  const normalizedRightBoundaryIndex = rightBoundaryIndex ?? Number.MAX_SAFE_INTEGER;
  if (normalizedLeftBoundaryIndex !== normalizedRightBoundaryIndex) {
    return normalizedLeftBoundaryIndex - normalizedRightBoundaryIndex;
  }

  const leftChunkIndex = finiteInteger(left.boundary?.chunk_index);
  const rightChunkIndex = finiteInteger(right.boundary?.chunk_index);
  const normalizedLeftChunkIndex = leftChunkIndex ?? Number.MAX_SAFE_INTEGER;
  const normalizedRightChunkIndex = rightChunkIndex ?? Number.MAX_SAFE_INTEGER;
  return normalizedLeftChunkIndex - normalizedRightChunkIndex || left.inputIndex - right.inputIndex;
}

/**
 * 归一化正文分片边界，给阅读器生成稳定、连续的展示序号。
 *
 * 后端历史数据中的 boundary_index/chunk_index 可能有空洞，也可能因为同一行
 * 存在多个语义边界而重复。阅读器始终按原文位置排序，并使用 readerIndex
 * 生成 1..N 的连续序号；任何边界都不会仅因缺少行号而被丢弃。
 */
export function normalizeDocumentBoundaries(boundaries = []) {
  return (Array.isArray(boundaries) ? boundaries : [])
    .filter((boundary) => boundary && typeof boundary === "object")
    .map((boundary, inputIndex) => ({ boundary, inputIndex }))
    .sort(boundaryOrder)
    .map(({ boundary, inputIndex }, readerIndex) => ({
      boundary,
      inputIndex,
      readerIndex,
      anchorId: `document-chunk-${readerIndex + 1}`,
      targetLine: (() => {
        const startLine = finiteInteger(boundary.start_line);
        return startLine !== null && startLine >= 0 ? startLine + 1 : null;
      })(),
    }));
}

function positionedChildren(tree) {
  return (Array.isArray(tree?.children) ? tree.children : [])
    .map((node, childIndex) => ({
      childIndex,
      startLine: finiteInteger(node?.position?.start?.line),
      endLine: finiteInteger(node?.position?.end?.line),
    }))
    .filter(({ startLine, endLine }) => startLine !== null && endLine !== null);
}

/**
 * 把一个源文件行号映射到 Markdown 顶层节点之间的安全插入槽位。
 *
 * 不在 paragraph/list/table 等节点内部切开 AST。若目标行处于某个顶层节点
 * 内部，就选择距离最近的节点边缘，并把该边界标记为 approximate。
 */
function locateSafeSlot(tree, targetLine) {
  const children = Array.isArray(tree?.children) ? tree.children : [];
  const positioned = positionedChildren(tree);
  if (!positioned.length) {
    return { slot: 0, approximate: targetLine === null };
  }

  if (targetLine === null) {
    return { slot: children.length, approximate: true };
  }

  for (const item of positioned) {
    if (targetLine <= item.startLine) {
      return { slot: item.childIndex, approximate: false };
    }

    if (targetLine <= item.endLine) {
      const distanceToStart = targetLine - item.startLine;
      const distanceToEnd = item.endLine + 1 - targetLine;
      return {
        slot: distanceToStart <= distanceToEnd ? item.childIndex : item.childIndex + 1,
        approximate: targetLine !== item.startLine,
      };
    }
  }

  return { slot: children.length, approximate: false };
}

function boundaryNode(entries, approximate) {
  return {
    type: "documentChunkBoundary",
    data: {
      hName: "document-chunk-boundary",
      hProperties: {
        boundaryIndexes: entries.map((entry) => entry.readerIndex).join(","),
        placementApproximate: approximate ? "true" : "false",
      },
      hChildren: [],
    },
  };
}

function isParserPageMarker(node) {
  return node?.type === "html"
    && /^\s*<!--\s*ODL_PAGE\s*:\s*\d+\s*-->\s*$/i.test(String(node.value ?? ""));
}

/**
 * 依据 remark 解析得到的顶层 source position 插入自定义分片边界节点。
 * 同一个安全位置上的多个边界会合并为一个节点，但会完整保留各自序号。
 */
export function insertDocumentBoundaryNodes(tree, normalizedBoundaries, boundaryPrecision = "line") {
  if (!tree || !Array.isArray(tree.children)) return tree;

  const groupedBySlot = new Map();
  (Array.isArray(normalizedBoundaries) ? normalizedBoundaries : []).forEach((entry) => {
    const placement = locateSafeSlot(tree, entry.targetLine);
    const current = groupedBySlot.get(placement.slot) || { entries: [], approximate: false };
    current.entries.push(entry);
    current.approximate = current.approximate
      || placement.approximate
      || boundaryPrecision === "approximate_line";
    groupedBySlot.set(placement.slot, current);
  });

  if (!groupedBySlot.size) return tree;

  const originalChildren = tree.children.slice();
  const nextChildren = [];
  for (let slot = 0; slot <= originalChildren.length; slot += 1) {
    const group = groupedBySlot.get(slot);
    if (group) nextChildren.push(boundaryNode(group.entries, group.approximate));
    if (slot < originalChildren.length && !isParserPageMarker(originalChildren[slot])) {
      nextChildren.push(originalChildren[slot]);
    }
  }
  tree.children = nextChildren;
  return tree;
}

/**
 * ReactMarkdown/remark 插件工厂。完整 Markdown 只解析一次，边界通过 AST
 * source position 在顶层节点之间插入，不再拆成多个 Markdown 字符串渲染。
 */
export function createDocumentBoundaryPlugin(normalizedBoundaries, boundaryPrecision = "line") {
  return function documentBoundaryPlugin() {
    return (tree) => insertDocumentBoundaryNodes(tree, normalizedBoundaries, boundaryPrecision);
  };
}
