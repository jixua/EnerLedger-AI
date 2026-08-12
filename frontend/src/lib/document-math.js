/** Normalize Word's single-line $$...$$ output into remark-math display syntax. */
export function normalizeDocumentMath(markdown) {
  return String(markdown || "").replace(
    /(^|\n)[\t ]*\$\$([^\n]+?)\$\$[\t ]*(?=\n|$)/g,
    (_match, prefix, formula) => `${prefix}$$\n${formula.trim()}\n$$`,
  );
}
