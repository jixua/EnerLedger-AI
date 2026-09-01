import {
  Activity,
  Boxes,
  Check,
  CircleAlert,
  Database,
  HardDrive,
  Network,
  RefreshCw,
  SearchCode,
  Waypoints,
} from "lucide-react";

import { useApp } from "../state/AppContext";

const COMPONENTS = [
  {
    key: "mysql",
    name: "MySQL",
    role: "业务数据",
    detail: "数据集、文档、片段和模型配置",
    icon: Database,
  },
  {
    key: "minio",
    name: "MinIO",
    role: "原文件与解析产物",
    detail: "原始文件、Markdown 和附件",
    icon: HardDrive,
  },
  {
    key: "qdrant",
    name: "Qdrant",
    role: "稠密与稀疏向量索引",
    detail: "向量数据与文档筛选条件",
    icon: Waypoints,
  },
  {
    key: "manticore",
    name: "Manticore",
    role: "关键词索引",
    detail: "按数据集建立全文索引",
    icon: SearchCode,
  },
  {
    key: "queue",
    name: "解析队列",
    role: "接收并调度文档解析任务",
    detail: "任务状态、超时处理和延迟重试",
    icon: Boxes,
  },
  {
    key: "worker",
    name: "解析服务",
    role: "后台解析、切块与建索",
    detail: "运行状态、并发数和待处理数量",
    icon: Network,
  },
];

function normalizeStatus(value) {
  const raw = value !== null && typeof value === "object" ? value.status : value;
  if (["ok", "ready", "healthy", "up", true].includes(raw)) return "ready";
  if (["error", "failed", "down", "unhealthy", false].includes(raw)) return "failed";
  return "pending";
}

function statusCopy(status) {
  if (status === "ready") return "已验证";
  if (status === "failed") return "异常";
  return "待检查";
}

export function SystemPage() {
  const {
    apiLive,
    apiReachable,
    isDemo,
    health = {},
    healthLoading,
    refreshHealth,
  } = useApp();
  const apiStatus = isDemo
    ? "pending"
    : apiReachable === false
    ? "failed"
    : normalizeStatus(apiLive ?? health.api ?? health);
  const apiDetail = isDemo
    ? "当前使用预览数据，尚未连接接口服务"
    : apiStatus === "ready"
    ? "接口服务已正常响应"
    : apiStatus === "failed"
      ? "接口服务检查失败，请检查服务进程"
      : "正在检查接口服务";

  return (
    <div className="page-shell system-page feature-page">
      <header className="knowledge-hero system-hero">
        <div className="knowledge-hero__copy">
          <h1>系统状态</h1>
          <p className="knowledge-hero__subtitle">服务与依赖</p>
        </div>
        <button
          className="secondary-button knowledge-hero__action"
          type="button"
          onClick={() => refreshHealth?.().catch(() => {})}
          disabled={healthLoading || typeof refreshHealth !== "function"}
        >
          <RefreshCw size={16} className={healthLoading ? "spin" : ""} />
          {healthLoading ? "检查中" : "重新检查"}
        </button>
      </header>

      <section className={`api-health-hero api-health-hero--${apiStatus}`} aria-label="接口服务状态">
        <div className="api-health-hero__icon">
          {apiStatus === "ready" ? <Check size={24} /> : apiStatus === "failed" ? <CircleAlert size={24} /> : <Activity size={24} />}
        </div>
        <div>
          <h2>接口服务</h2>
          <p>{apiDetail}</p>
        </div>
        <div className="api-health-hero__meta">
          <span className={`state-pill state-pill--${apiStatus}`}><i />{statusCopy(apiStatus)}</span>
          <small>/health/live</small>
        </div>
      </section>

      <section className="system-section">
        <div className="section-heading">
          <div><h2>依赖服务</h2></div>
        </div>

        <div className="dependency-list">
          <div className="dependency-list__header" aria-hidden="true">
            <span>服务</span>
            <span>用途</span>
            <span>当前状态</span>
            <span>验证状态</span>
          </div>
          {COMPONENTS.map((component) => {
            const Icon = component.icon;
            const status = normalizeStatus(health.components?.[component.key] ?? health[component.key]);
            const liveDetail = health.components?.[component.key]?.message
              ?? health[component.key]?.message
              ?? component.detail;
            return (
              <article className={`dependency-row dependency-row--${status}`} key={component.key}>
                <div className="dependency-row__main">
                  <span className="dependency-row__icon"><Icon size={21} /></span>
                  <h3>{component.name}</h3>
                </div>
                <span className="dependency-row__role">{component.role}</span>
                <span className="dependency-row__detail">{liveDetail}</span>
                <span className={`state-pill state-pill--${status}`}><i />{statusCopy(status)}</span>
              </article>
            );
          })}
        </div>
      </section>

    </div>
  );
}

export default SystemPage;
