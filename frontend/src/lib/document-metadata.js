export function hasDocumentPageCount(document) {
  const fileType = String(document?.file_type || "").trim().toLowerCase();
  return !["html", "htm"].includes(fileType) && document?.page_count != null;
}
