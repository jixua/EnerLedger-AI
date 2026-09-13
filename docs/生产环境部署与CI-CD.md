# 生产环境部署与 CI/CD

## 1. 方案概览

生产发布使用 **Jenkins + SSH 镜像流式传输 + Docker Compose**。生产服务器不再从 GHCR 拉取大镜像。

1. GitHub Pull Request 仍由 `.github/workflows/quality.yml` 执行质量门禁。
2. 候选分支先合并到 `dev` 并完成验收，再以同一 SHA 合并到 `master`。
3. `master` push webhook 只触发 Jenkins `enerledger-production` job。
4. Jenkins 在 `x86_64` 主机上构建 API、Pi Agent 和 Web 镜像。
5. Jenkins 从 Docker Hub/Quay 按官方 manifest 摘要拉取中间件镜像。
6. Jenkins 通过 `docker save | gzip | ssh | docker load` 将全部镜像直接传入生产服务器。
7. 生产服务器以 `SKIP_PULL=true` 运行发布脚本，先确认全部镜像存在，再启动 Compose 并做健康检查。

GitHub PR 检查、Dev 验收、Master 合并、Jenkins 成功和生产健康是五个独立事实，不得相互代替。

## 2. 分支与发布约束

- `dev` 是集成环境，由现有 `enerledger-dev` job 发布。
- `master` 是生产发布源，由独立 `enerledger-production` job 发布。
- 生产 job 必须精确 checkout `refs/heads/master`。
- 生产 job 必须禁止并发发布，不得取消正在运行的前一个生产发布。
- 镜像标签使用完整 40 位 Git SHA，并写入 `org.opencontainers.image.revision`。

## 3. 仓库内发布文件

- `deploy/jenkins/Jenkinsfile.production`：Master 生产构建、镜像传输和部署流水线。
- `deploy/production/docker-compose.yml`：生产容器、网络、健康检查和数据卷契约。
- `deploy/production/scripts/deploy.sh`：环境校验、预加载镜像校验、启动与验收。
- `deploy/production/scripts/verify.sh`：运行容器、SHA、HTTP 健康和 Alembic 检查。
- `deploy/production/scripts/rollback.sh`：仅在上一个 SHA 镜像仍已预加载时回滚应用容器。

`.github/workflows/release-master.yml` 已移除。GitHub Actions 不再构建 GHCR 镜像或 SSH 发布生产。

## 4. Jenkins 主机要求

Jenkins agent 需要 Linux `x86_64`、Docker Engine/Buildx/Compose、`git`、`ssh`、`scp`、`gzip`，并能访问 GitHub 仓库和生产 SSH 端口。

Jenkins 容器中使用的生产 SSH 文件为：

```text
/opt/tolink/jenkins-secrets/enerledger-production/id_ed25519
/opt/tolink/jenkins-secrets/enerledger-production/known_hosts
```

私钥和 `known_hosts` 权限必须为 `600`。这些文件不得提交到 Git、写入 Jenkins 日志或打包到镜像中。

## 5. Jenkins job 配置

创建 Pipeline job `enerledger-production`：

- SCM URL：`ssh://git@ssh.github.com:443/jixua/EnerLedger-AI.git`；
- Credential：复用仓库只读 credential `git-cred`；
- Branch：`refs/heads/master`；
- Script path：`deploy/jenkins/Jenkinsfile.production`；
- Lightweight checkout：开启；
- Generic Webhook Trigger filter：`^refs/heads/master$`；
- Disable concurrent builds：开启，保留前一个构建运行。

可克隆 `enerledger-dev` job 以复用 webhook token，但必须同时替换分支过滤、SCM branch、Script path和 job 名称，不得让生产 job 指向 Dev 部署目录。

## 6. 生产服务器初始配置

生产目录为 `/opt/enerledger-ai`，其中 `.env` 必须为 `600`。发布用户只需要写入该目录、访问 Docker socket，并通过专用 ed25519 密钥 SSH 登录。

不需要 GHCR PAT，服务器 `~/.docker/config.json` 不应长期保存 `ghcr.io` 登录信息。

## 7. 镜像输送与启动

Jenkins 对九个镜像执行一次流式传输：

```bash
docker save <images...> | gzip -1 | \
  ssh <production> 'gzip -dc | docker load'
```

镜像导入完成后才执行：

```bash
SKIP_PULL=true \
GHCR_NAMESPACE=ghcr.io/jixua \
RELEASE_SHA=<full-master-sha> \
/opt/enerledger-ai/bin/deploy.sh /opt/enerledger-ai
```

`GHCR_NAMESPACE` 在这个流程中仅作为稳定的本地镜像名称空间，不会发起 GHCR 网络请求。`deploy.sh` 会对 Compose 要求的每个镜像执行 `docker image inspect`，任何镜像缺失都会在启动前失败。

## 8. 发布验收

Jenkins 成功后仍必须独立确认：

```bash
cd /opt/enerledger-ai
docker compose --env-file .env -f docker-compose.yml ps -a
cat .deployment/current_sha
cat .deployment/deployed_at
curl -fsS http://127.0.0.1:18000/health/live
curl -fsS http://127.0.0.1:18080/health/live
```

验收标准：

- `.deployment/current_sha` 等于 Jenkins checkout 的完整 Master SHA；
- MySQL、MinIO、Qdrant、Manticore、RabbitMQ、Pi Agent、API、Parse Worker 和 Web 都达到预期状态；
- `minio-init` 以 0 退出；
- API 和 Web 健康检查成功；
- Alembic 已到当前 head；
- 无关容器不受影响。

Master 合并或 Jenkins 开始运行都不等于已部署；只有上述验收完成才能宣布发布成功。

## 9. 回滚

回滚依赖上一个 SHA 镜像仍存在于生产 Docker。默认使用 `SKIP_PULL=true`，不会回退到 GHCR 拉取。

```bash
/opt/enerledger-ai/bin/rollback.sh /opt/enerledger-ai
```

回滚只切换应用镜像，不会自动降级数据库。执行前必须确认迁移兼容性。

## 10. 故障排查

### Jenkins 中间件拉取慢

确认 Jenkins 主机 Docker daemon 的 Docker Hub 镜像源，并确认 Quay 可访问。中间件始终按官方摘要校验，不得用未锁定的 `latest`。

### API 镜像的 Debian 软件安装慢

根目录 `Dockerfile` 默认通过阿里云 Debian 镜像安装 LibreOffice、字体等系统依赖，避免 Jenkins 直接访问缓慢的 `deb.debian.org`。如构建节点不在中国大陆，可通过 `DEBIAN_MIRROR` 和 `DEBIAN_SECURITY_MIRROR` build args 覆盖，不能在 Jenkins 工作区临时改写 Dockerfile。

NLTK 构建资产使用官方静态站点上四个固定路径的 `punkt`、`punkt_tab`、`stopwords` 和 `wordnet` 压缩包，单文件超时为 900 秒。不再调用会访问 GitHub Raw 的交互式 downloader，也不使用可能重定向到 GitHub Raw 的 jsDelivr；当前链路不需要的 `omw-1.4` 不进入镜像。

### SSH 输送中断

Jenkins 使用 `ServerAliveInterval=30` 和 `ServerAliveCountMax=10`。重新运行生产 job 会重新执行传输，不会覆盖 `.env` 或删除数据卷。

### 预加载镜像缺失

`deploy.sh` 会输出缺失的完整镜像名。先修复 Jenkins 的构建/传输阶段，不得将 `SKIP_PULL` 改回 `false` 绕过门禁。

### 生产页面不可公网访问

当前 Compose 默认仅绑定 `127.0.0.1:18080` 和 `127.0.0.1:18000`。域名、TLS 和反向代理属于独立的公网入口配置。
