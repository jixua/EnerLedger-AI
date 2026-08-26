const env = import.meta.env ?? {};

const runtimeConfig = {
  baseUrl: normalizeBaseUrl(env.VITE_API_BASE_URL ?? ""),
  accessToken: typeof localStorage === "undefined"
    ? ""
    : localStorage.getItem("energy-carbon-access-token") || "",
};

function normalizeBaseUrl(value) {
  const normalized = String(value ?? "").trim();
  return normalized === "/" ? "" : normalized.replace(/\/+$/, "");
}

function appendQuery(path, query) {
  const search = new URLSearchParams();
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    search.set(key, String(value));
  });
  const suffix = search.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function extractErrorMessage(payload, fallback) {
  if (typeof payload === "string" && payload.trim()) return payload.trim();
  if (!payload || typeof payload !== "object") return fallback;

  if (typeof payload.detail === "string" && payload.detail.trim()) {
    return payload.detail.trim();
  }
  if (Array.isArray(payload.detail)) {
    const messages = payload.detail
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const location = Array.isArray(item.loc) ? item.loc.join(".") : "";
        const message = typeof item.msg === "string" ? item.msg : "";
        return [location, message].filter(Boolean).join(": ") || null;
      })
      .filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  if (payload.detail && typeof payload.detail === "object") {
    if (typeof payload.detail.message === "string" && payload.detail.message.trim()) {
      return payload.detail.message.trim();
    }
    if (typeof payload.detail.code === "string" && payload.detail.code.trim()) {
      return payload.detail.code.trim();
    }
  }
  if (typeof payload.message === "string" && payload.message.trim()) {
    return payload.message.trim();
  }
  return fallback;
}

function extractErrorCode(payload, status) {
  if (payload && typeof payload === "object") {
    if (typeof payload.code === "string") return payload.code;
    if (payload.detail && typeof payload.detail === "object") {
      if (typeof payload.detail.code === "string") return payload.detail.code;
    }
  }
  return status ? `HTTP_${status}` : "NETWORK_ERROR";
}

export class ApiError extends Error {
  constructor(message, { status = 0, code, detail = null, payload = null, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "ApiError";
    this.status = status;
    this.code = code ?? (status ? `HTTP_${status}` : "NETWORK_ERROR");
    this.detail = detail;
    this.payload = payload;
  }
}

export function configureApi({ baseUrl, accessToken } = {}) {
  if (baseUrl !== undefined) runtimeConfig.baseUrl = normalizeBaseUrl(baseUrl);
  if (accessToken !== undefined) setApiAccessToken(accessToken);
  return getApiConfig();
}

export function setApiBaseUrl(baseUrl) {
  runtimeConfig.baseUrl = normalizeBaseUrl(baseUrl);
  return runtimeConfig.baseUrl;
}

export function setApiAccessToken(accessToken) {
  runtimeConfig.accessToken = String(accessToken || "").trim();
  if (typeof localStorage !== "undefined") {
    if (runtimeConfig.accessToken) {
      localStorage.setItem("energy-carbon-access-token", runtimeConfig.accessToken);
    } else {
      localStorage.removeItem("energy-carbon-access-token");
    }
  }
  return runtimeConfig.accessToken;
}

export function getApiConfig() {
  return { ...runtimeConfig };
}

export function buildApiUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${runtimeConfig.baseUrl}${normalizedPath}`;
}

export function createApiHeaders(headers = {}, { json = false, auth = true } = {}) {
  const result = new Headers(headers);
  if (!result.has("Accept")) result.set("Accept", "application/json");
  if (json && !result.has("Content-Type")) {
    result.set("Content-Type", "application/json");
  }
  if (auth && runtimeConfig.accessToken && !result.has("Authorization")) {
    result.set("Authorization", `Bearer ${runtimeConfig.accessToken}`);
  }
  return result;
}

export async function readApiError(response) {
  const contentType = response.headers.get("content-type") ?? "";
  let payload = null;
  try {
    payload = contentType.includes("json") ? await response.json() : await response.text();
  } catch {
    payload = null;
  }

  const fallback = response.statusText || `请求失败（${response.status}）`;
  return new ApiError(extractErrorMessage(payload, fallback), {
    status: response.status,
    code: extractErrorCode(payload, response.status),
    detail: payload && typeof payload === "object" ? payload.detail ?? null : payload,
    payload,
  });
}

export async function apiRequest(
  path,
  { method = "GET", body, headers, signal, auth = true } = {},
) {
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const hasJsonBody = body !== undefined && body !== null && !isFormData;

  let response;
  try {
    response = await fetch(buildApiUrl(path), {
      method,
      body: hasJsonBody ? JSON.stringify(body) : body,
      headers: createApiHeaders(headers, { json: hasJsonBody, auth }),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("无法连接后端服务", {
      status: 0,
      code: "NETWORK_ERROR",
      cause: error,
    });
  }

  if (!response.ok) {
    if (auth && response.status === 401) {
      setApiAccessToken("");
      if (typeof window !== "undefined") window.dispatchEvent(new Event("auth:expired"));
    }
    throw await readApiError(response);
  }
  if (response.status === 204) return null;

  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) return response.json();
  const text = await response.text();
  return text || null;
}

export function getHealth({ signal } = {}) {
  return apiRequest("/health/live", { signal, auth: false });
}

export function loginAdmin(username, password, { signal } = {}) {
  return apiRequest("/api/v1/auth/login", {
    method: "POST",
    body: { username, password },
    signal,
    auth: false,
  });
}

export function getCurrentAdmin({ signal } = {}) {
  return apiRequest("/api/v1/auth/me", { signal });
}

export function getSystemStatus({ signal } = {}) {
  return apiRequest("/api/v1/system/status", { signal });
}

export function searchArxivPapers(
  { query, maxResults = 10 },
  { signal } = {},
) {
  const path = appendQuery("/api/v1/crawler/arxiv", {
    query: String(query || "").trim(),
    max_results: maxResults,
  });
  return apiRequest(path, { signal });
}

export function importArxivPapers(
  { datasetId, arxivIds },
  { signal } = {},
) {
  return apiRequest("/api/v1/crawler/arxiv/import", {
    method: "POST",
    body: {
      dataset_id: Number(datasetId),
      arxiv_ids: arxivIds,
    },
    signal,
  });
}

export function listModelConfigs(
  { capability, includeInactive = false } = {},
  { signal } = {},
) {
  const path = appendQuery("/api/v1/llm/configs", {
    capability: capability ? String(capability).toUpperCase() : undefined,
    include_inactive: includeInactive,
  });
  return apiRequest(path, { signal });
}

export function createModelConfig(payload, { signal } = {}) {
  return apiRequest("/api/v1/llm/configs", {
    method: "POST",
    body: payload,
    signal,
  });
}

export function updateModelConfig(configId, payload, { signal } = {}) {
  return apiRequest(`/api/v1/llm/configs/${encodeURIComponent(configId)}`, {
    method: "PATCH",
    body: payload,
    signal,
  });
}

export function deleteModelConfig(configId, { signal } = {}) {
  return apiRequest(`/api/v1/llm/configs/${encodeURIComponent(configId)}`, {
    method: "DELETE",
    signal,
  });
}

export function listDatasets({ signal } = {}) {
  return apiRequest("/api/v1/datasets", { signal });
}

export function getDataset(datasetId, { signal } = {}) {
  return apiRequest(`/api/v1/datasets/${encodeURIComponent(datasetId)}`, { signal });
}

export function createDataset(payload, { signal } = {}) {
  return apiRequest("/api/v1/datasets", {
    method: "POST",
    body: payload,
    signal,
  });
}

export function updateDataset(datasetId, payload, { signal } = {}) {
  return apiRequest(`/api/v1/datasets/${encodeURIComponent(datasetId)}`, {
    method: "PATCH",
    body: payload,
    signal,
  });
}

export function deleteDataset(datasetId, { signal } = {}) {
  return apiRequest(`/api/v1/datasets/${encodeURIComponent(datasetId)}`, {
    method: "DELETE",
    signal,
  });
}

export function listDocumentFolders(datasetId, { signal } = {}) {
  return apiRequest(`/api/v1/datasets/${encodeURIComponent(datasetId)}/folders`, { signal });
}

export function createDocumentFolder(datasetId, payload, { signal } = {}) {
  return apiRequest(`/api/v1/datasets/${encodeURIComponent(datasetId)}/folders`, {
    method: "POST",
    body: payload,
    signal,
  });
}

export function updateDocumentFolder(datasetId, folderId, payload, { signal } = {}) {
  return apiRequest(
    `/api/v1/datasets/${encodeURIComponent(datasetId)}/folders/${encodeURIComponent(folderId)}`,
    { method: "PATCH", body: payload, signal },
  );
}

export function deleteDocumentFolder(datasetId, folderId, { signal } = {}) {
  return apiRequest(
    `/api/v1/datasets/${encodeURIComponent(datasetId)}/folders/${encodeURIComponent(folderId)}`,
    { method: "DELETE", signal },
  );
}

export function listDocuments(datasetId, { signal } = {}) {
  return apiRequest(
    `/api/v1/datasets/${encodeURIComponent(datasetId)}/documents`,
    { signal },
  );
}

export function listAllDocuments(
  { datasetId, status } = {},
  { signal } = {},
) {
  const path = appendQuery("/api/v1/documents", {
    dataset_id: datasetId,
    status: status ? String(status).toUpperCase() : undefined,
  });
  return apiRequest(path, { signal });
}

export function getDocument(documentId, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}`, { signal });
}

export function analyzeDocument(documentId, { llmConfigId } = {}, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}/analysis`, {
    method: "POST",
    body: {
      ...(llmConfigId ? { llm_config_id: Number(llmConfigId) } : {}),
    },
    signal,
  });
}

export function getDocumentAnalysis(documentId, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}/analysis`, { signal });
}

export function getDocumentAnalysisStatus(documentId, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}/analysis/status`, { signal });
}

export async function downloadDocumentAnalysisDocx(documentId, { signal } = {}) {
  let response;
  try {
    response = await fetch(
      buildApiUrl(`/api/v1/documents/${encodeURIComponent(documentId)}/analysis/docx`),
      {
        method: "GET",
        headers: createApiHeaders({
          Accept: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }),
        signal,
      },
    );
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("无法连接后端服务", {
      status: 0,
      code: "NETWORK_ERROR",
      cause: error,
    });
  }
  if (!response.ok) {
    if (response.status === 401) {
      setApiAccessToken("");
      if (typeof window !== "undefined") window.dispatchEvent(new Event("auth:expired"));
    }
    throw await readApiError(response);
  }

  const disposition = response.headers.get("content-disposition") || "";
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  let filename = "企业文档-分析报告.docx";
  if (encodedName) {
    try {
      filename = decodeURIComponent(encodedName);
    } catch {
      // 保留安全的默认文件名。
    }
  }
  return { blob: await response.blob(), filename };
}

export async function getDocumentPreviewContent(documentId, { signal } = {}) {
  let response;
  try {
    response = await fetch(
      buildApiUrl(`/api/v1/documents/${encodeURIComponent(documentId)}/preview/content`),
      {
        method: "GET",
        headers: createApiHeaders({ Accept: "text/markdown" }),
        signal,
      },
    );
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("无法连接后端服务", {
      status: 0,
      code: "NETWORK_ERROR",
      cause: error,
    });
  }

  if (!response.ok) throw await readApiError(response);
  const rawVersion = response.headers.get("X-Document-Version");
  const documentVersion = Number(rawVersion);
  if (!Number.isSafeInteger(documentVersion) || documentVersion < 1) {
    throw new ApiError("文档预览缺少有效的版本信息", {
      status: response.status,
      code: "INVALID_DOCUMENT_PREVIEW_VERSION",
      detail: rawVersion,
    });
  }
  return {
    content: await response.text(),
    documentVersion,
  };
}

export function getDocumentPreviewMap(documentId, { signal } = {}) {
  return apiRequest(
    `/api/v1/documents/${encodeURIComponent(documentId)}/preview/map`,
    { signal },
  );
}

export function isDocumentPreviewAssetUrl(value) {
  if (typeof value !== "string" || !value.trim()) return false;
  try {
    const browserOrigin = globalThis.location?.origin || "http://document-preview.local";
    const apiOrigin = new URL(runtimeConfig.baseUrl || browserOrigin, browserOrigin).origin;
    const url = new URL(value, browserOrigin);
    const hasExplicitAuthority = /^(?:https?:)?\/\//i.test(value.trim());
    if (hasExplicitAuthority && url.origin !== apiOrigin) return false;
    return /\/api\/v1\/documents\/[^/]+\/preview\/versions\/[^/]+\/assets(?:\/|$)/.test(url.pathname);
  } catch {
    return false;
  }
}

export async function getDocumentPreviewAsset(source, { signal } = {}) {
  let response;
  try {
    response = await fetch(buildApiUrl(source), {
      method: "GET",
      headers: createApiHeaders({ Accept: "image/*" }),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("无法加载文档图片", {
      status: 0,
      code: "NETWORK_ERROR",
      cause: error,
    });
  }

  if (!response.ok) throw await readApiError(response);
  return response.blob();
}

export function listDocumentChunks(
  documentId,
  { offset = 0, limit = 20, query, chunkType } = {},
  { signal } = {},
) {
  const path = appendQuery(
    `/api/v1/documents/${encodeURIComponent(documentId)}/chunks`,
    {
      offset,
      limit,
      q: query?.trim() || undefined,
      chunk_type: chunkType && chunkType !== "ALL" ? String(chunkType).toLowerCase() : undefined,
    },
  );
  return apiRequest(path, { signal });
}

export function updateDocument(documentId, payload, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}`, {
    method: "PATCH",
    body: payload,
    signal,
  });
}

export function uploadDocument(datasetId, file, { signal, folderId } = {}) {
  const isFile = typeof File !== "undefined" && file instanceof File;
  const isBlob = typeof Blob !== "undefined" && file instanceof Blob;
  if (!isFile && !isBlob) {
    throw new TypeError("file 必须是 File 或 Blob");
  }
  const form = new FormData();
  const filename = file.name || "document";
  form.append("file", file, filename);
  if (folderId !== undefined && folderId !== null && folderId !== "") {
    form.append("folder_id", String(folderId));
  }
  return apiRequest(
    `/api/v1/datasets/${encodeURIComponent(datasetId)}/documents`,
    { method: "POST", body: form, signal },
  );
}

export function retryDocument(documentId, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}/retry`, {
    method: "POST",
    signal,
  });
}

export function reparseDocument(documentId, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}/reparse`, {
    method: "POST",
    signal,
  });
}

export function deleteDocument(documentId, { signal } = {}) {
  return apiRequest(`/api/v1/documents/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
    signal,
  });
}

export function recallDocuments(
  { query, datasetIds, docIds, includeContent = true },
  { signal } = {},
) {
  const body = {
    query,
    dataset_ids: datasetIds,
    include_content: includeContent,
  };
  if (docIds?.length) body.doc_ids = docIds;
  return apiRequest("/api/v1/recall", {
    method: "POST",
    body,
    signal,
  });
}
