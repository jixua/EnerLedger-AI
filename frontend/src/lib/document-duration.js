export function resolveDocumentDurationMs(document) {
  const startedAt = Date.parse(document?.processing_started_at ?? "");
  const finishedAt = Date.parse(document?.finished_at ?? "");
  if (Number.isFinite(startedAt) && Number.isFinite(finishedAt) && finishedAt >= startedAt) {
    return finishedAt - startedAt;
  }

  const rawFallback = document?.parse_time_ms;
  if (rawFallback === null || rawFallback === undefined || rawFallback === "") return null;
  const fallback = Number(rawFallback);
  return Number.isFinite(fallback) && fallback >= 0 ? fallback : null;
}

export function formatDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined || milliseconds === "") return "—";
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;

  const totalSeconds = value / 1000;
  if (totalSeconds < 60) return `${totalSeconds.toFixed(1)} s`;

  const totalWholeSeconds = Math.round(totalSeconds);
  const seconds = totalWholeSeconds % 60;
  const totalMinutes = Math.floor(totalWholeSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes} 分 ${seconds} 秒`;

  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours} 小时 ${minutes} 分`;
}
