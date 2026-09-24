function legacyCopy(text, documentObject) {
  if (!documentObject?.body || typeof documentObject.execCommand !== "function") return false;

  const textarea = documentObject.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.inset = "0 auto auto 0";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  documentObject.body.appendChild(textarea);

  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    return documentObject.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

/**
 * Clipboard API 只在安全上下文和获得权限时可用。部署在普通 HTTP、内嵌浏览器或
 * 权限被拒绝时，回退到浏览器仍普遍支持的同步复制命令。
 */
export async function copyText(text, options = {}) {
  const value = String(text ?? "");
  if (!value) return false;

  const navigatorObject = options.navigatorObject ?? globalThis.navigator;
  const documentObject = options.documentObject ?? globalThis.document;

  if (typeof navigatorObject?.clipboard?.writeText === "function") {
    try {
      await navigatorObject.clipboard.writeText(value);
      return true;
    } catch {
      // Clipboard API 可能存在但因当前来源或权限失败，继续尝试兼容路径。
    }
  }

  try {
    return legacyCopy(value, documentObject);
  } catch {
    return false;
  }
}
