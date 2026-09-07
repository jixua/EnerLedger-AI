import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  analyzeDocument as analyzeDocumentRequest,
  ApiError,
  createDataset as createDatasetRequest,
  createModelConfig,
  deleteDataset as deleteDatasetRequest,
  deleteDocument as deleteDocumentRequest,
  deleteModelConfig,
  downloadDocumentAnalysisDocx as downloadDocumentAnalysisDocxRequest,
  getDocument as getDocumentRequest,
  getDocumentAnalysis as getDocumentAnalysisRequest,
  getDocumentAnalysisStatus as getDocumentAnalysisStatusRequest,
  getDocumentPreviewContent,
  getDocumentPreviewMap,
  getHealth,
  getSystemStatus,
  listAllDocuments,
  listDatasets,
  listDocuments,
  listDocumentChunks as listDocumentChunksRequest,
  listModelConfigs,
  reparseDocument as reparseDocumentRequest,
  recallDocuments,
  retryDocument as retryDocumentRequest,
  updateDataset as updateDatasetRequest,
  updateDocument as updateDocumentRequest,
  updateModelConfig,
  uploadDocument,
} from "../lib/api";
import { useAuth } from "./AuthContext";
import { streamAgent as streamAgentRequest } from "../lib/sse";
import {
  PREVIEW_DATA_NOTICE,
  getMockDocumentAnalysis,
  mockDatasets,
  getMockDocumentChunks,
  getMockDocumentPreview,
  mockDocumentsByDataset,
  mockModelConfigs,
  mockRecallResponse,
} from "../lib/mock-data";

const AppContext = createContext(null);

function clone(value) {
  return typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value));
}

function wait(duration, signal) {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, duration);
    signal?.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("The operation was aborted", "AbortError"));
    }, { once: true });
  });
}

function normalizeMessage(error) {
  return error?.message || "请求未完成，请稍后重试";
}

function groupDocuments(items) {
  return (items || []).reduce((result, document) => {
    const datasetId = Number(document.dataset_id ?? document.datasetId);
    if (!Number.isFinite(datasetId)) return result;
    if (!result[datasetId]) result[datasetId] = [];
    result[datasetId].push(document);
    return result;
  }, {});
}

export function AppProvider({ children }) {
  const forcedDemo = import.meta.env.VITE_DEMO_MODE === "true";
  const { admin } = useAuth();
  const isReviewer = admin?.role === "reviewer";
  const userId = 1;
  const [datasets, setDatasets] = useState([]);
  const [models, setModels] = useState([]);
  const [documents, setDocuments] = useState({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [apiReachable, setApiReachable] = useState(false);
  const [isDemo, setIsDemo] = useState(forcedDemo);
  const [health, setHealth] = useState({ status: "unknown" });
  const [healthLoading, setHealthLoading] = useState(false);
  const [lastError, setLastError] = useState(null);
  const mounted = useRef(true);
  const documentsRef = useRef(documents);
  const documentPollInFlight = useRef(false);
  const pollIntervalMs = Math.max(1000, Number(import.meta.env.VITE_DOCUMENT_POLL_INTERVAL_MS || 3000));
  const allDocuments = useMemo(() => Object.values(documents).flat(), [documents]);

  useEffect(() => {
    documentsRef.current = documents;
  }, [documents]);

  const activatePreview = useCallback((error = null) => {
    if (!mounted.current) return;
    setApiReachable(false);
    setIsDemo(true);
    setHealth({ status: "preview", message: error ? normalizeMessage(error) : PREVIEW_DATA_NOTICE });
    setDatasets(clone(mockDatasets));
    setModels(clone(mockModelConfigs));
    setDocuments(clone(mockDocumentsByDataset));
    setLastError(error ? normalizeMessage(error) : null);
  }, []);

  const markBackendUnavailable = useCallback((error) => {
    if (!mounted.current) return;
    const message = normalizeMessage(error);
    setApiReachable(false);
    setIsDemo(false);
    setHealth({ status: "failed", message });
    setDatasets([]);
    setModels([]);
    setDocuments({});
    setLastError(message);
  }, []);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    setLastError(null);
    if (forcedDemo) {
      activatePreview();
      setRefreshing(false);
      setLoading(false);
      return;
    }

    try {
      await getHealth();
    } catch (error) {
      markBackendUnavailable(error);
      if (mounted.current) {
        setRefreshing(false);
        setLoading(false);
      }
      return;
    }

    if (mounted.current) {
      setApiReachable(true);
      setIsDemo(false);
      setHealth({ status: "ok" });
      // 一旦进入真实 API 模式，先移除预览数据，避免后续操作误以为已经持久化。
      setDatasets((current) => current.some((item) => item.preview) ? [] : current);
      setModels((current) => current.some((item) => item.preview) ? [] : current);
      setDocuments((current) => Object.values(current).flat().some((item) => item.preview) ? {} : current);
    }

    try {
      const [nextDatasets, nextModels, nextDocuments, nextHealth] = await Promise.all([
        listDatasets(),
        listModelConfigs({ includeInactive: !isReviewer }),
        isReviewer ? Promise.resolve([]) : listAllDocuments(),
        isReviewer ? Promise.resolve({ status: "restricted" }) : getSystemStatus(),
      ]);
      if (!mounted.current) return;
      setDatasets(nextDatasets);
      setModels(nextModels);
      setDocuments(groupDocuments(nextDocuments));
      setHealth(nextHealth);
    } catch (error) {
      if (mounted.current) setLastError(normalizeMessage(error));
    } finally {
      if (mounted.current) {
        setRefreshing(false);
        setLoading(false);
      }
    }
  }, [activatePreview, forcedDemo, isReviewer, markBackendUnavailable]);

  useEffect(() => {
    mounted.current = true;
    void refreshAll();
    return () => { mounted.current = false; };
  }, [refreshAll]);

  const refreshHealth = useCallback(async () => {
    setHealthLoading(true);
    if (isDemo && forcedDemo) {
      const preview = { status: "preview", message: PREVIEW_DATA_NOTICE };
      setHealth(preview);
      setHealthLoading(false);
      return preview;
    }
    try {
      const live = await getSystemStatus();
      setHealth(live);
      setApiReachable(true);
      setLastError(null);
      return live;
    } catch (error) {
      setApiReachable(false);
      setLastError(normalizeMessage(error));
      const failed = { status: "failed", message: normalizeMessage(error) };
      setHealth(failed);
      return failed;
    } finally {
      setHealthLoading(false);
    }
  }, [forcedDemo, isDemo]);

  const loadAllDocuments = useCallback(async ({ silent = false } = {}) => {
    if (isDemo) return Object.values(documentsRef.current).flat();
    try {
      const result = await listAllDocuments();
      setDocuments(groupDocuments(result));
      return result;
    } catch (error) {
      if (!silent) setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const loadDocuments = useCallback(async (datasetId) => {
    const id = Number(datasetId);
    if (isDemo) return documentsRef.current[id] ?? [];
    try {
      const result = await listDocuments(id);
      setDocuments((current) => ({ ...current, [id]: result }));
      return result;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  useEffect(() => {
    const hasActiveDocuments = allDocuments.some((document) => ["QUEUED", "PROCESSING"].includes(String(document.status || "").toUpperCase()));
    if (forcedDemo || isDemo || !apiReachable || !hasActiveDocuments) return undefined;
    const timer = window.setInterval(() => {
      if (documentPollInFlight.current) return;
      documentPollInFlight.current = true;
      void loadAllDocuments({ silent: true })
        .catch(() => {})
        .finally(() => { documentPollInFlight.current = false; });
    }, pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [allDocuments, apiReachable, forcedDemo, isDemo, loadAllDocuments, pollIntervalMs]);

  const createModel = useCallback(async (payload) => {
    setLastError(null);
    if (isDemo) {
      await wait(320);
      const next = {
        ...payload,
        id: Math.max(1100, ...models.map((model) => Number(model.id) || 0)) + 1,
        scope: "USER",
        owner_user_id: userId,
        provider_id: 1,
        provider_type: String(payload.provider_type || "custom").toLowerCase(),
        capability: String(payload.capability || "CHAT").toUpperCase(),
        protocol: String(payload.protocol || "openai").toLowerCase(),
        api_key_masked: "••••••••（预览）",
        is_active: payload.is_active !== false,
        snapshot_version: 1,
        preview: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      delete next.api_key;
      setModels((current) => [next, ...current]);
      return next;
    }
    try {
      const next = await createModelConfig(payload);
      setModels((current) => [next, ...current]);
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo, models, userId]);

  const updateModel = useCallback(async (configId, payload) => {
    const id = Number(configId);
    setLastError(null);
    try {
      const next = isDemo
        ? {
            ...models.find((model) => Number(model.id) === id),
            ...payload,
            id,
            api_key_masked: payload.api_key ? "••••••••（预览）" : models.find((model) => Number(model.id) === id)?.api_key_masked,
            snapshot_version: Number(models.find((model) => Number(model.id) === id)?.snapshot_version || 1) + 1,
            updated_at: new Date().toISOString(),
          }
        : await updateModelConfig(id, payload);
      delete next.api_key;
      setModels((current) => current.map((model) => Number(model.id) === id ? next : model));
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo, models]);

  const removeModel = useCallback(async (configId) => {
    const id = Number(configId);
    setLastError(null);
    try {
      if (isDemo) await wait(180);
      else await deleteModelConfig(id);
      setModels((current) => current.filter((model) => Number(model.id) !== id));
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const createDataset = useCallback(async (payload) => {
    setLastError(null);
    if (isDemo) {
      await wait(320);
      const next = {
        ...payload,
        id: Math.max(2100, ...datasets.map((dataset) => Number(dataset.id) || 0)) + 1,
        status: "ACTIVE",
        preview: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      setDatasets((current) => [next, ...current]);
      setDocuments((current) => ({ ...current, [next.id]: [] }));
      return next;
    }
    try {
      const next = await createDatasetRequest(payload);
      setDatasets((current) => [next, ...current]);
      setDocuments((current) => ({ ...current, [next.id]: [] }));
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [datasets, isDemo]);

  const updateDataset = useCallback(async (datasetId, payload) => {
    const id = Number(datasetId);
    setLastError(null);
    try {
      const current = datasets.find((dataset) => Number(dataset.id) === id);
      const next = isDemo
        ? { ...current, ...payload, id, preview: true, updated_at: new Date().toISOString() }
        : await updateDatasetRequest(id, payload);
      setDatasets((items) => items.map((dataset) => Number(dataset.id) === id ? next : dataset));
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [datasets, isDemo]);

  const removeDataset = useCallback(async (datasetId) => {
    const id = Number(datasetId);
    setLastError(null);
    try {
      if (isDemo) {
        if ((documentsRef.current[id] ?? []).length) {
          throw new Error("请先删除数据集中的文档");
        }
        await wait(180);
      } else {
        await deleteDatasetRequest(id);
      }
      setDatasets((items) => items.filter((dataset) => Number(dataset.id) !== id));
      setDocuments((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const uploadDocuments = useCallback(async (datasetId, fileList, options = {}) => {
    const id = Number(datasetId);
    const files = Array.from(fileList || []);
    const results = [];
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      options.onFileStart?.(file, index);
      try {
        if (isDemo) {
          const pending = {
            preview: true,
            document_id: Date.now() + index,
            dataset_id: id,
            folder_id: options.folderId ?? null,
            filename: file.name,
            file_type: file.name.split(".").pop()?.toLowerCase() || "file",
            file_size: file.size,
            parser_backend: file.name.toLowerCase().endsWith(".pdf") ? "opendataloader" : "builtin",
            status: "QUEUED",
            error_message: null,
            page_count: null,
            chunk_count: 0,
            parse_time_ms: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            queued_at: new Date().toISOString(),
            attempt_count: 0,
          };
          setDocuments((current) => ({ ...current, [id]: [pending, ...(current[id] ?? [])] }));
          results.push(pending);
          options.onFileComplete?.(file, pending, index);

          // 预览模式仅模拟队列状态变化；上传请求本身会立即返回。
          window.setTimeout(() => {
            setDocuments((current) => ({
              ...current,
              [id]: (current[id] ?? []).map((item) => documentIdentity(item) === pending.document_id
                ? { ...item, status: "PROCESSING", attempt_count: 1, processing_started_at: new Date().toISOString(), updated_at: new Date().toISOString() }
                : item),
            }));
          }, 900 + index * 120);
          window.setTimeout(() => {
            setDocuments((current) => ({
              ...current,
              [id]: (current[id] ?? []).map((item) => documentIdentity(item) === pending.document_id
                ? {
                    ...item,
                    status: "READY",
                    page_count: pending.file_type === "pdf" ? 12 : null,
                    chunk_count: 48,
                    parse_time_ms: 1680,
                    parse_quality_status: pending.file_type === "pdf" ? "PASSED" : "NOT_APPLICABLE",
                    parse_quality: pending.file_type === "pdf"
                      ? { schema_version: 2, status: "PASSED", pdf_page_count: 12, markdown_page_count: 12, text_coverage_ratio: 1, ocr_page_count: 0, ocr_required_pages: [], low_confidence_pages: [], warnings: [] }
                      : { schema_version: 2, status: "NOT_APPLICABLE", warnings: [] },
                    finished_at: new Date().toISOString(),
                    updated_at: new Date().toISOString(),
                  }
                : item),
            }));
          }, 2600 + index * 180);
        } else {
          // 当前后端一次接收一个文件，成功后立即返回 QUEUED。
          const result = await uploadDocument(id, file, {
            signal: options.signal,
            folderId: options.folderId,
          });
          results.push(result);
          options.onFileComplete?.(file, result, index);
          await loadDocuments(id);
        }
      } catch (error) {
        options.onFileError?.(file, error, index);
        setLastError(normalizeMessage(error));
        if (options.stopOnError) throw error;
        results.push({ file, error });
      }
    }
    return results;
  }, [isDemo, loadDocuments]);

  function documentIdentity(document) {
    return document?.document_id ?? document?.id;
  }

  const updateDocumentState = useCallback((datasetId, documentId, next) => {
    setDocuments((current) => {
      const items = current[datasetId] ?? [];
      const exists = items.some((document) => Number(documentIdentity(document)) === Number(documentId));
      return {
        ...current,
        [datasetId]: exists
          ? items.map((document) => Number(documentIdentity(document)) === Number(documentId) ? { ...document, ...next } : document)
          : [next, ...items],
      };
    });
  }, []);

  const loadDocument = useCallback(async (documentId) => {
    const id = Number(documentId);
    try {
      const next = isDemo
        ? Object.values(documentsRef.current).flat().find((item) => Number(documentIdentity(item)) === id)
        : await getDocumentRequest(id);
      if (!next) throw new Error("未找到文档");
      const datasetId = Number(next.dataset_id ?? next.datasetId);
      updateDocumentState(datasetId, id, next);
      setLastError(null);
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo, updateDocumentState]);

  const loadDocumentChunks = useCallback(async (documentId, params = {}, options = {}) => {
    const id = Number(documentId);
    try {
      if (!isDemo) {
        const next = await listDocumentChunksRequest(id, params, options);
        setLastError(null);
        return next;
      }

      const query = String(params.query || "").trim().toLocaleLowerCase("zh-CN");
      const chunkType = params.chunkType && params.chunkType !== "ALL"
        ? String(params.chunkType).toLowerCase()
        : null;
      const offset = Math.max(0, Number(params.offset) || 0);
      const limit = Math.min(100, Math.max(1, Number(params.limit) || 20));
      const matches = getMockDocumentChunks(id).filter((chunk) => {
        const matchesType = !chunkType || String(chunk.chunk_type).toLowerCase() === chunkType;
        const haystack = `${chunk.chunk_id} ${chunk.content}`.toLocaleLowerCase("zh-CN");
        return matchesType && (!query || haystack.includes(query));
      });
      const document = Object.values(documentsRef.current).flat().find((item) => Number(documentIdentity(item)) === id);
      const next = {
        document_id: id,
        dataset_id: Number(document?.dataset_id ?? document?.datasetId),
        document_version: Number(document?.version || 1),
        items: clone(matches.slice(offset, offset + limit)),
        total: matches.length,
        offset,
        limit,
      };
      setLastError(null);
      return next;
    } catch (error) {
      if (options.signal?.aborted || error?.name === "AbortError") throw error;
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const loadDocumentPreview = useCallback(async (documentId, options = {}) => {
    const id = Number(documentId);
    try {
      let contentResult;
      let previewMap;
      if (isDemo) {
        if (options.signal?.aborted) {
          throw new DOMException("The operation was aborted", "AbortError");
        }
        await wait(120, options.signal);
        const preview = clone(getMockDocumentPreview(id));
        if (!preview) throw new Error("未找到文档预览");
        previewMap = preview.map;
        contentResult = {
          content: preview.content,
          documentVersion: Number(preview.map.document_version),
        };
      } else {
        [contentResult, previewMap] = await Promise.all([
          getDocumentPreviewContent(id, options),
          getDocumentPreviewMap(id, options),
        ]);
      }

      const contentVersion = Number(contentResult.documentVersion);
      const mapVersion = Number(previewMap?.document_version);
      if (
        !Number.isSafeInteger(contentVersion)
        || contentVersion < 1
        || !Number.isSafeInteger(mapVersion)
        || mapVersion < 1
        || contentVersion !== mapVersion
      ) {
        throw new ApiError("文档内容与分片映射版本不一致，请刷新后重试", {
          status: 409,
          code: "DOCUMENT_PREVIEW_VERSION_MISMATCH",
          detail: { content_version: contentVersion, map_version: mapVersion },
        });
      }

      setLastError(null);
      return { ...previewMap, content: contentResult.content };
    } catch (error) {
      if (options.signal?.aborted || error?.name === "AbortError") throw error;
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const analyzeDocument = useCallback(async (documentId, payload = {}, options = {}) => {
    const id = Number(documentId);
    try {
      if (isDemo) {
        await wait(520);
        const preview = clone(getMockDocumentAnalysis(id));
        if (!preview) throw new Error("未找到文档分析预览");
        setLastError(null);
        return {
          run_id: `preview-${Date.now()}`,
          document_id: id,
          dataset_id: Number(preview.dataset_id || 0),
          document_version: Number(preview.document_version || 1),
          state: "RUNNING",
          stage: "ANALYZING",
          started_at: new Date().toISOString(),
          finished_at: null,
          error_code: null,
          error_message: null,
        };
      }
      const result = await analyzeDocumentRequest(id, payload, options);
      setLastError(null);
      return result;
    } catch (error) {
      if (options.signal?.aborted || error?.name === "AbortError") throw error;
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const loadDocumentAnalysis = useCallback(async (documentId, options = {}) => {
    const id = Number(documentId);
    try {
      if (isDemo) {
        await wait(120, options.signal);
        return clone(getMockDocumentAnalysis(id));
      }
      const result = await getDocumentAnalysisRequest(id, options);
      setLastError(null);
      return result;
    } catch (error) {
      if (options.signal?.aborted || error?.name === "AbortError") throw error;
      if (error instanceof ApiError && error.code === "DOCUMENT_ANALYSIS_NOT_FOUND") {
        return null;
      }
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const loadDocumentAnalysisStatus = useCallback(async (documentId, options = {}) => {
    const id = Number(documentId);
    try {
      if (isDemo) {
        await wait(80, options.signal);
        const preview = getMockDocumentAnalysis(id);
        return {
          run_id: null,
          document_id: id,
          dataset_id: Number(preview?.dataset_id || 0),
          document_version: Number(preview?.document_version || 1),
          state: preview ? "SUCCEEDED" : "IDLE",
          stage: preview ? "COMPLETED" : "IDLE",
          started_at: null,
          finished_at: preview?.generated_at || null,
          error_code: null,
          error_message: null,
        };
      }
      const result = await getDocumentAnalysisStatusRequest(id, options);
      setLastError(null);
      return result;
    } catch (error) {
      if (options.signal?.aborted || error?.name === "AbortError") throw error;
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const downloadDocumentAnalysisDocx = useCallback(async (documentId, options = {}) => {
    const id = Number(documentId);
    try {
      if (isDemo) {
        throw new Error("预览模式不生成 Word 文件，请连接后端后下载");
      }
      const result = await downloadDocumentAnalysisDocxRequest(id, options);
      setLastError(null);
      return result;
    } catch (error) {
      if (options.signal?.aborted || error?.name === "AbortError") throw error;
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const updateDocument = useCallback(async (datasetId, documentId, payload) => {
    const dsId = Number(datasetId);
    const id = Number(documentId);
    try {
      const current = (documentsRef.current[dsId] ?? []).find((item) => Number(documentIdentity(item)) === id);
      const next = isDemo
        ? { ...current, ...payload, updated_at: new Date().toISOString() }
        : await updateDocumentRequest(id, payload);
      updateDocumentState(dsId, id, next);
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo, updateDocumentState]);

  const retryDocument = useCallback(async (datasetId, documentId) => {
    const dsId = Number(datasetId);
    const id = Number(documentId);
    try {
      const next = isDemo
        ? { ...(documentsRef.current[dsId] ?? []).find((item) => Number(documentIdentity(item)) === id), status: "QUEUED", error_message: null, available_at: new Date().toISOString(), updated_at: new Date().toISOString() }
        : await retryDocumentRequest(id);
      updateDocumentState(dsId, id, next);
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo, updateDocumentState]);

  const reparseDocument = useCallback(async (datasetId, documentId) => {
    const dsId = Number(datasetId);
    const id = Number(documentId);
    try {
      const next = isDemo
        ? { ...(documentsRef.current[dsId] ?? []).find((item) => Number(documentIdentity(item)) === id), status: "QUEUED", reparse_requested: true, error_message: null, available_at: new Date().toISOString(), updated_at: new Date().toISOString() }
        : await reparseDocumentRequest(id);
      updateDocumentState(dsId, id, next);
      return next;
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo, updateDocumentState]);

  const removeDocument = useCallback(async (datasetId, documentId) => {
    const dsId = Number(datasetId);
    const id = Number(documentId);
    try {
      if (isDemo) await wait(160);
      else await deleteDocumentRequest(id);
      setDocuments((current) => ({
        ...current,
        [dsId]: (current[dsId] ?? []).filter((document) => Number(documentIdentity(document)) !== id),
      }));
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const recall = useCallback(async (payload) => {
    if (isDemo) {
      await wait(420, payload.signal);
      return { ...clone(mockRecallResponse), query: payload.query || mockRecallResponse.query };
    }
    try {
      return await recallDocuments({
        query: payload.query,
        datasetIds: payload.datasetIds ?? payload.dataset_ids,
        docIds: payload.docIds ?? payload.doc_ids,
        includeContent: payload.includeContent ?? payload.include_content ?? true,
      }, { signal: payload.signal });
    } catch (error) {
      setLastError(normalizeMessage(error));
      throw error;
    }
  }, [isDemo]);

  const streamAgent = useCallback(async ({ query, datasetIds, llmConfigId, docIds, history, signal, onEvent }) => {
    if (!isDemo) {
      try {
        return await streamAgentRequest({ query, datasetIds, llmConfigId, docIds, history }, {
          signal,
          onEvent: ({ event, data }) => onEvent?.(event, data),
        });
      } catch (error) {
        setLastError(normalizeMessage(error));
        throw error;
      }
    }

    const requestId = `preview-${Date.now()}`;
    const hits = clone(mockRecallResponse.hits);
    const answer = `根据当前预览资料，企业天然气燃烧排放可按“活动数据 × 排放因子”进行核算。活动数据应优先采用经过校验的计量数据，并统一热值、体积和时间边界。[片段1]\n\n在形成核算结果前，还需要确认组织边界、排放因子来源及其适用年份，并保留原始凭证供追溯。[片段2]`;
    onEvent?.("stream_started", { request_id: requestId });
    await wait(240, signal);
    onEvent?.("recall_done", { request_id: requestId, hits, failed_sources: [] });
    const chunks = answer.match(/.{1,12}/gs) || [answer];
    let partial = "";
    for (const text of chunks) {
      await wait(30, signal);
      partial += text;
      onEvent?.("answer_delta", { text });
    }
    const done = { request_id: requestId, answer: partial, hits, failed_sources: [], usage: { prompt_tokens: 428, completion_tokens: 92, total_tokens: 520 }, elapsed_ms: 1260 };
    onEvent?.("answer_done", done);
    return { requestId, answer: partial, hits, failedSources: [], usage: done.usage, elapsedMs: done.elapsed_ms, emptyRecall: false, terminalEvent: "answer_done" };
  }, [isDemo]);

  const connectionMode = isDemo ? "PREVIEW" : apiReachable ? "LIVE API" : "OFFLINE";
  const actions = useMemo(() => ({
    createDataset,
    updateDataset,
    deleteDataset: removeDataset,
    loadAllDocuments,
    loadDocuments,
    loadDocument,
    loadDocumentChunks,
    loadDocumentPreview,
    loadDocumentAnalysis,
    loadDocumentAnalysisStatus,
    downloadDocumentAnalysisDocx,
    analyzeDocument,
    uploadDocuments,
    updateDocument,
    retryDocument,
    reparseDocument,
    deleteDocument: removeDocument,
    recall,
  }), [analyzeDocument, createDataset, downloadDocumentAnalysisDocx, loadAllDocuments, loadDocument, loadDocumentAnalysis, loadDocumentAnalysisStatus, loadDocumentChunks, loadDocumentPreview, loadDocuments, recall, removeDataset, removeDocument, reparseDocument, retryDocument, updateDataset, updateDocument, uploadDocuments]);
  const value = useMemo(() => ({
    datasets,
    models,
    documents,
    allDocuments,
    loading,
    refreshing,
    apiReachable,
    apiLive: apiReachable,
    health,
    healthLoading,
    isDemo,
    connectionMode,
    previewNotice: isDemo ? PREVIEW_DATA_NOTICE : null,
    lastError,
    userId,
    refreshAll,
    refreshHealth,
    createModel,
    updateModel,
    deleteModel: removeModel,
    createDataset,
    updateDataset,
    deleteDataset: removeDataset,
    loadAllDocuments,
    loadDocuments,
    loadDocument,
    loadDocumentChunks,
    loadDocumentPreview,
    loadDocumentAnalysis,
    loadDocumentAnalysisStatus,
    downloadDocumentAnalysisDocx,
    analyzeDocument,
    uploadDocuments,
    updateDocument,
    retryDocument,
    reparseDocument,
    deleteDocument: removeDocument,
    recall,
    streamAgent,
    actions,
  }), [actions, allDocuments, analyzeDocument, apiReachable, connectionMode, createDataset, createModel, datasets, documents, downloadDocumentAnalysisDocx, health, healthLoading, isDemo, lastError, loadAllDocuments, loadDocument, loadDocumentAnalysis, loadDocumentAnalysisStatus, loadDocumentChunks, loadDocumentPreview, loadDocuments, loading, models, recall, refreshAll, refreshHealth, refreshing, removeDataset, removeDocument, removeModel, reparseDocument, retryDocument, streamAgent, updateDataset, updateDocument, updateModel, uploadDocuments, userId]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp 必须在 AppProvider 内使用");
  return value;
}
