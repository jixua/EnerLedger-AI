function sortedChildren(folders) {
  const knownIds = new Set(folders.map((folder) => Number(folder.id)));
  const children = new Map();
  folders.forEach((folder) => {
    const parentId = folder.parent_id == null || !knownIds.has(Number(folder.parent_id))
      ? null
      : Number(folder.parent_id);
    children.set(parentId, [...(children.get(parentId) || []), folder]);
  });
  children.forEach((items) => items.sort((left, right) => String(left.name || "").localeCompare(
    String(right.name || ""),
    "zh-CN",
    { numeric: true },
  )));
  return children;
}

export function flattenFolderTree(folders, { expandedIds } = {}) {
  const children = sortedChildren(folders);
  const rows = [];
  const walk = (parentId, depth, ancestors) => {
    (children.get(parentId) || []).forEach((folder) => {
      const id = Number(folder.id);
      if (ancestors.has(id)) return;
      const hasChildren = (children.get(id) || []).length > 0;
      rows.push({ ...folder, depth, hasChildren });
      if (expandedIds === undefined || expandedIds.has(id)) {
        walk(id, depth + 1, new Set([...ancestors, id]));
      }
    });
  };
  walk(null, 0, new Set());
  return rows;
}

export function folderDescendantIds(folders, folderId) {
  const children = sortedChildren(folders);
  const result = new Set();
  const pending = [Number(folderId)];
  while (pending.length) {
    const current = pending.pop();
    if (!Number.isFinite(current) || result.has(current)) continue;
    result.add(current);
    pending.push(...(children.get(current) || []).map((folder) => Number(folder.id)));
  }
  return result;
}

export function folderPath(folder, folders) {
  if (!folder) return "未分类";
  const byId = new Map(folders.map((item) => [Number(item.id), item]));
  const names = [];
  const visited = new Set();
  let current = folder;
  while (current && !visited.has(Number(current.id))) {
    visited.add(Number(current.id));
    names.unshift(current.name);
    current = current.parent_id == null ? null : byId.get(Number(current.parent_id));
  }
  return names.join(" / ");
}

export function expandedParentIds(folders) {
  const knownIds = new Set(folders.map((folder) => Number(folder.id)));
  return new Set(folders
    .filter((folder) => folder.parent_id != null && knownIds.has(Number(folder.parent_id)))
    .map((folder) => Number(folder.parent_id)));
}
