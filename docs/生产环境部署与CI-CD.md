# 生产环境部署与 CI/CD 手册

## 1. 目标与边界

本方案使用 **GitHub Actions + GHCR + SSH + Docker Compose**：

1. GitHub Actions 对候选分支进行 Python、Web、Pi Agent 和部署契约检查。
2. 只有 `master` 分支的完整 40 位 Git SHA 可以生成发布镜像。
3. API、Pi Agent 和 Web 镜像推送到 GitHub Container Registry（GHCR）。
4. 发布 Job 通过 SSH 登录生产服务器，拉取指定 SHA 的镜像，再由 Docker Compose 切换并验证。

本方案不在生产服务器编译源码，不使用浮动的 `latest` 标签，不将生产 `.env` 提交到 Git。

## 2. 分支与发布规则

当前仓库规则是：

1. 所有需求分支从最新 `origin/master` 创建。
2. 候选分支先提交到 `dev` 验收。
3. Dev 验收后，使用同一候选分支和同一 HEAD SHA 提交到 `master`。
4. 禁止使用 `dev -> master` 代替候选分支发布 PR。
5. 生产镜像和自动部署只由 `master` 的 push 或在 `master` 上的手动运行触发。

`dev` 合并、测试成功、GHCR 镜像存在、生产部署成功是四个独立事实，不得互相代替。

## 3. 流水线文件

### `.github/workflows/quality.yml`

在指向 `dev` 或 `master` 的 PR 上执行，也可手动运行：

- Python 3.11：锁定依赖、Ruff、Pytest；
- Node 22.19：前端测试与生产构建；
- Pi Agent：离线模型数据检查、构建、语法检查和测试；
- 生产 Compose 解析与 Shell 语法检查。

### `.github/workflows/release-master.yml`

只接受 `master` push 或 `master` 上的手动运行：

1. 重用完整质量门禁。
2. 构建三个镜像：
   - `ghcr.io/<owner>/enerledger-api:<40位SHA>`
   - `ghcr.io/<owner>/enerledger-pi:<40位SHA>`
   - `ghcr.io/<owner>/enerledger-web:<40位SHA>`
3. 镜像全部成功后才进入发布 Job。
4. 发布 Job 必须同时满足：
   - 当前 ref 为 `refs/heads/master`；
   - GitHub 仓库变量 `PRODUCTION_DEPLOY_ENABLED` 等于 `true`；
   - 所有生产 SSH Secrets 已配置；
   - `production` Environment 的保护规则已通过。

`PRODUCTION_DEPLOY_ENABLED` 默认不存在，因此合入流水线文件本身不会部署服务器。

## 4. GHCR 权限

Workflow 使用 GitHub 自带的短期 `GITHUB_TOKEN` 推送和拉取镜像，需要：

```yaml
permissions:
  contents: read
  packages: write
```

三个 GHCR Package 应与当前仓库关联并继承仓库 Actions 权限。发布时，短期 Token 通过 SSH 标准输入传给 `docker login --password-stdin`；镜像拉取后执行 `docker logout ghcr.io`，不在服务器长期保存 PAT。

## 5. GitHub 配置

在仓库 `Settings -> Environments` 创建 `production`，建议设置必须人工批准且禁止发起人自审。

在 `production` Environment 中配置：

| Secret | 用途 |
| --- | --- |
| `PRODUCTION_SSH_HOST` | 生产服务器地址，不在仓库写死 |
| `PRODUCTION_SSH_PORT` | SSH 端口 |
| `PRODUCTION_SSH_USER` | 专用发布用户 |
| `PRODUCTION_SSH_PRIVATE_KEY` | 发布专用 Ed25519 私钥 |
| `PRODUCTION_SSH_KNOWN_HOSTS` | 经独立核对后的 `known_hosts` 完整行 |

不得将 SSH 密码、宝塔密码、生产 `.env` 或长期 GHCR PAT 存入 Workflow 文件。

最后在 `Settings -> Secrets and variables -> Actions -> Variables` 新建：

```text
PRODUCTION_DEPLOY_ENABLED=false
```

服务器、Secrets、域名和回滚验证都就绪后，再改为 `true`。

## 6. 服务器一次性准备

服务器必须安装 Docker Engine、Docker Compose Plugin、`curl`、`flock` 和 OpenSSH。建议创建专用 `deploy` 用户，而不是在 CI 中使用 root 或日常管理账号。

```bash
sudo useradd --create-home --shell /bin/bash deploy
sudo usermod -aG docker deploy
sudo install -d -m 750 -o deploy -g deploy /opt/enerledger-ai/bin
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
sudo install -m 600 -o deploy -g deploy /dev/null /home/deploy/.ssh/authorized_keys
```

将发布专用公钥追加到 `/home/deploy/.ssh/authorized_keys`。先验证新密钥登录，再考虑关闭 SSH 密码登录。
发布密钥应单独生成，不复用个人日常 SSH 密钥：

```bash
ssh-keygen -t ed25519 -N '' -f ./enerledger-production-deploy -C enerledger-production-deploy
```

`enerledger-production-deploy.pub` 追加到服务器，无后缀的私钥内容保存为 `PRODUCTION_SSH_PRIVATE_KEY`。密钥不得提交到仓库，配置完 GitHub Secret 后应安全删除本地临时副本。

生成 `known_hosts` 候选行：

```bash
ssh-keyscan -p <ssh-port> <server> > /tmp/enerledger-known-hosts
ssh-keygen -lf /tmp/enerledger-known-hosts
```

必须在服务器控制台或另一条已信任通道核对指纹，核对后再将文件的完整内容保存为 `PRODUCTION_SSH_KNOWN_HOSTS`。不能仅因为 `ssh-keyscan` 能连通就信任结果。

用户加入 `docker` 组后需要重新登录，然后执行 `docker info` 确认不需要 `sudo`。

Docker 组等价于很高的宿主机权限，因此 `deploy` 用户只用于此发布链路，不接收互联网上不受信的任务。

## 7. 生产 `.env`

在本地从模板复制，通过安全通道上传，不经过 Git：

```bash
cp deploy/production/.env.example /tmp/enerledger-production.env
```

至少替换：

- MySQL root 和业务密码；
- MinIO Access Key 和 Secret Key；
- RabbitMQ 密码；
- `API_KEY_ENCRYPTION_SECRET` 和 `JWT_SECRET`；
- 管理员用户名和 `ADMIN_PASSWORD_HASH`；
- Pi Agent 双向 Token；
- 真实域名对应的 `CORS_ALLOW_ORIGINS`；
- 如需外部 MinerU，配置 Token 与受控的 `MINIO_PUBLIC_ENDPOINT`。

`MINIO_PUBLIC_ENDPOINT` 不能使用 `127.0.0.1`：这个 URL 可能被浏览器或外部 MinerU 访问，回环地址会指向访问者自身。应在正式发布前确定独立文件域名、HTTPS、桶访问策略或鉴权代理。当前实现生成普通对象 URL，不生成预签名 URL；在访问模型未定稿前，不应把私有原文件桶直接改成公开读取。

随机值可用：

```bash
openssl rand -hex 32
```

管理员密码哈希必须使用项目实现生成。以下命令从终端隐藏输入密码，不把明文写入 Shell 历史：

```bash
uv run python -c 'from getpass import getpass; from app.domain.auth import hash_admin_password; print(hash_admin_password(getpass("Admin password: ")))'
```

安装生产文件：

```bash
scp -P <ssh-port> /tmp/enerledger-production.env deploy@<server>:/tmp/enerledger.env
ssh -p <ssh-port> deploy@<server> \
  'install -m 600 /tmp/enerledger.env /opt/enerledger-ai/.env && rm /tmp/enerledger.env'
```

`.env` 必须属于发布用户且权限为 `600`。密钥变量一旦启用后不能随意重新生成；丢失 `API_KEY_ENCRYPTION_SECRET` 会导致历史模型 API Key 无法解密。

## 8. 端口和反向代理

生产 Compose 默认只在回环地址暴露：

| 端口 | 用途 |
| --- | --- |
| `127.0.0.1:18080` | Web 与 `/api` 入口 |
| `127.0.0.1:18000` | API 诊断入口 |
| `127.0.0.1:19000` | MinIO API，仅用于本机运维/受控反代 |
| `127.0.0.1:19001` | MinIO Console，不直接公开 |

MySQL、Qdrant、Manticore、RabbitMQ 不发布宿主机端口。宿为宝塔 Nginx 时，为业务域名配置 HTTPS，再将请求反向代理到 `http://127.0.0.1:18080`。没有确定域名和 TLS 证书之前，不应把 Compose 改为 `0.0.0.0` 公开暴露。

宿为 Nginx 的核心反代配置为：

```nginx
location / {
    proxy_pass http://127.0.0.1:18080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 360s;
}
```

在宝塔中应先新建独立站点和证书，不要直接改写其他站点的 `server` 块。

## 9. 首次发布

1. 确认生产 `.env` 已安装且权限为 `600`。
2. 配置 GitHub `production` Environment 和 SSH Secrets。
3. 在 GitHub 中合并流水线候选分支到 `dev`，完成 Dev 验收。
4. 用同一候选分支提交到 `master`。
5. 第一次保持 `PRODUCTION_DEPLOY_ENABLED=false`，观察三个 GHCR 镜像是否成功生成。
6. 在服务器完成域名、密钥和 Compose 预检。
7. 将 `PRODUCTION_DEPLOY_ENABLED=true`，在 `master` 上手动运行 `Master 镜像与生产发布`，选择 `deploy=true`。
8. 审批 `production` Environment 发布。

## 10. 发布过程与成功标准

`deploy.sh` 会：

1. 校验完整 SHA、Compose、`.env` 与文件权限；
2. 拒绝缺失值、开发占位口令、非法加密密钥和相同的 Pi 双向 Token；
3. 使用 `flock` 防止两次发布同时运行；
4. 先拉取全部应用镜像；
5. 使用 `docker compose up -d --no-build --wait` 切换容器；
6. 验证 API、Web、Alembic 和三个应用镜像 SHA；
7. 只有全部验证通过后才更新 `.deployment/current_sha`。

生产成功需要同时有：

- GitHub Actions 最终状态成功；
- 三个镜像均存在相同完整 SHA 标签；
- Compose 服务已启动且健康；
- API 和 Web `/health/live` 成功；
- Alembic 版本与当前代码一致；
- `.deployment/current_sha` 与实际镜像一致；
- 最终业务域名通过 HTTPS 可访问。

## 11. 回滚

每次成功发布会记录 `current_sha` 和 `previous_sha`。手动回滚：

```bash
/opt/enerledger-ai/bin/rollback.sh /opt/enerledger-ai
```

脚本要求输入完整的上一版 SHA，避免误操作。回滚只切换应用镜像，**不自动执行 Alembic downgrade**。如新版已应用不向后兼容的数据库迁移，必须先判断数据兼容性，不能盲目回滚容器。

## 12. 备份与恢复

自动部署不等于数据备份。上线前必须另行建立：

- MySQL 定时逻辑备份与定期恢复演练；
- MinIO 对象数据备份；
- Qdrant 快照或可重建性方案；
- Manticore 索引的重建流程；
- RabbitMQ 持久消息和未处理任务的业务补偿方案。

未进行真实恢复演练时，不能将“已备份”描述为“可恢复”。

## 13. 故障排查

### 镜像可构建但不发布

检查 `PRODUCTION_DEPLOY_ENABLED` 是否为 `true`，Workflow 是否运行在 `master`，手动运行时 `deploy` 是否选择了 `true`。

### SSH 拒绝连接

核对 Host、Port、User、私钥和 `known_hosts` 整行。不得为了绕过错误加入 `StrictHostKeyChecking=no`。

### GHCR 返回 denied

确认 Workflow 有 `packages: write`，Package 与仓库已关联，且未禁止当前仓库 Actions 访问。

### Compose 卡在不健康

```bash
cd /opt/enerledger-ai
docker compose --env-file .env -f docker-compose.yml ps
docker compose --env-file .env -f docker-compose.yml logs --tail=200 api
docker compose --env-file .env -f docker-compose.yml logs --tail=200 mysql rabbitmq minio
```

先根据实际不健康服务定位，不要先删数据卷。

## 14. 当前仍需人工确认的事项

正式启用发布前，还需确认：

1. 生产业务域名和 TLS 证书；
2. 是否需要 MinerU 从公网下载 MinIO 原文件，以及对应的受控公开方案；
3. Pi Agent 和 Report Worker 是否在首次上线时启用；
4. 管理员初始密码的安全交付方式；
5. MySQL、MinIO 和 Qdrant 的备份保留周期；
6. GitHub `production` Environment 的审批人。

上述事项不影响流水线文件的静态准备，但会阻止正式生产发布的完整验收。
