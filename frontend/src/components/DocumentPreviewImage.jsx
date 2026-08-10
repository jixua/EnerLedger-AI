import { ImageOff, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import {
  getDocumentPreviewAsset,
  isDocumentPreviewAssetUrl,
} from "../lib/api";

export function DocumentPreviewImage({ src, alt, node: _node, ...props }) {
  const protectedAsset = isDocumentPreviewAssetUrl(src);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState({ status: protectedAsset ? "loading" : "direct", url: "", error: "" });

  useEffect(() => {
    if (!protectedAsset) {
      setState({ status: "direct", url: "", error: "" });
      return undefined;
    }

    const controller = new AbortController();
    let objectUrl = "";
    setState({ status: "loading", url: "", error: "" });

    void getDocumentPreviewAsset(src, { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setState({ status: "ready", url: objectUrl, error: "" });
      })
      .catch((error) => {
        if (controller.signal.aborted || error?.name === "AbortError") return;
        setState({
          status: "error",
          url: "",
          error: error instanceof Error ? error.message : "文档图片加载失败",
        });
      });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attempt, protectedAsset, src]);

  const imageAlt = alt || "文档图片";
  if (!protectedAsset) {
    return <img {...props} src={src} alt={imageAlt} loading="lazy" />;
  }
  if (state.status === "ready") {
    return <img {...props} src={state.url} alt={imageAlt} loading="lazy" />;
  }
  if (state.status === "error") {
    return (
      <span className="document-reader-image-state document-reader-image-state--error" role="alert">
        <ImageOff size={19} aria-hidden="true" />
        <span><strong>{imageAlt}</strong><small>{state.error}</small></span>
        <button type="button" onClick={() => setAttempt((current) => current + 1)}><RefreshCw size={14} />重试</button>
      </span>
    );
  }
  return (
    <span className="document-reader-image-state" role="status" aria-label={`正在加载图片：${imageAlt}`}>
      <Loader2 className="spin" size={18} aria-hidden="true" />
      <span>正在加载图片</span>
    </span>
  );
}

export default DocumentPreviewImage;
