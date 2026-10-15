# 本地容器启动与部署

## 5.1：镜像、Compose 与持久化

需要 Docker Desktop 的 Linux 引擎，或支持 volume-subpath 的 Linux Docker Engine 与现代 Compose。本轮在 Docker Engine 29.6.1、Compose 5.2.0 验证。API 和 Worker 使用 daily-ai-news-app:local；Bash 工具使用 daily-ai-news-tools:local。

项目根目录的 `.env` 保存应用配置。首次使用且文件不存在时，从 `.env.example` 复制；已有配置直接保留。SESSION_SECRET 至少 32 字符，模型/搜索/SMTP 的实际参数按各自账户填写。缺少模型时，预览任务明确失败为 model_dependency_configuration；SMTP 默认关闭并保留队列。凭据以运行环境注入，不进入镜像构建上下文。

Windows PowerShell 在项目根目录执行：

```powershell
.venv\Scripts\python.exe scripts/build_images.py
docker compose up -d --no-build
docker compose ps
```

构建脚本只复制 pyproject.toml、uv.lock、应用源文件及 Dockerfile 到临时路径，使用空的 Docker 客户端配置并在结束后清理。当前 Windows 中文路径下 Compose/Bake 曾产生非法会话请求头，该脚本已实际构建通过。它不修改全局 Docker 设置，不复制 .env、Git、运行数据、测试资料或缓存。可选 --python-image/--docker-cli-image/--uv-image 用于明确指定受信任的基础镜像来源；默认分别为 Python 官方镜像、Docker 官方 CLI 和固定版本 uv。

Linux 的 ASCII 路径可直接构建：

```sh
docker compose --profile build build tools api
docker compose up -d --no-build
docker compose ps
```

浏览器访问 http://127.0.0.1:8000/，接口存活检查为 /health。默认只发布回环端口。NEWS_HTTP_PORT 可通过运行 Compose 的 shell 环境临时改变宿主端口；NEWS_ENV_FILE 可指向另一份专用应用配置文件。它们是 Compose 操作参数，不加入应用 .env；测试会用它们选择假凭据与独立端口。使用 `docker compose -p <项目名>` 时，命名数据卷和 Worker 的卷映射同步使用 `<项目名>-data`，隔离不同部署。

### 数据与配置

默认持久卷 daily-ai-news-data 同时挂到 API/Worker 的 /data，保存 SQLite、来源快照、笔记和发送状态。原生 Python 运行的 ./data 与这个卷分别保留；数据导入和备份恢复按 5.2 的停写流程进行。普通 `docker compose down` 保留卷；停止/重建容器不会清空历史。不要在需要保留数据时使用 down -v 或删除该卷。

config 目录只读挂载到 /config，默认加载 rss-sources.json，其中 NVIDIA 官方来源沿用已验证的入口。配置变更后用 `docker compose up -d --force-recreate api worker` 重新创建，使更新后的环境生效；镜像代码修改后先重新构建。仅 restart 不重新注入已更改的环境。

### 权限与资源

API/Worker 以 UID/GID 10001 运行，根文件系统只读，只有 /data 和大小受限的 /tmp 可写；移除 Linux capabilities、启用 no-new-privileges，并限制进程、内存和 CPU。API 不挂 Docker socket。可信 Worker 使用本机 Unix socket 管理工具容器；Linux 如该 socket 属于其他 GID，在 .env 中填写 BASH_SOCKET_GID（可用 `stat -c %g /var/run/docker.sock` 查看），重建服务后生效。

Worker 的 BASH_DATA_VOLUME 自动绑定当前 Compose 数据卷。DockerRunner 校验当前任务目录与 DATA_DIR/runs/run_id 一致，只读挂载 `volume-subpath=runs/<run_id>` 到工具容器 /work；模型不能指定卷名、挂载点或收件人。工具仍无网络、无 socket/凭据、非 root、只读根，限制 64 MB/0.5 CPU/32 PID。原生运行未设置 BASH_DATA_VOLUME 时沿用当前任务目录 bind mount。依据 [Docker volumes](https://docs.docker.com/engine/storage/volumes/) 的卷子目录挂载能力。

### 健康与重启

API 探针执行 `python -m app.healthcheck api`，只请求容器回环 /health；Worker 探针只读共享 SQLite 中的活动 Worker 心跳。当前单 Worker 部署默认允许最近 120 秒，实际阈值会随请求超时/心跳/轮询配置增大。它检查进程与执行循环，不表示模型、搜索或邮箱已联通。探针输出仅 healthy/unhealthy，不回显配置或异常原文。

restart:unless-stopped 在进程退出或引擎重启时恢复容器；healthcheck 失败只标记 unhealthy。排障时检查状态和日志，再决定重启：

```sh
docker compose ps
docker compose logs --tail 100 api worker
docker compose exec -T api python -m app.healthcheck api
docker compose exec -T worker python -m app.healthcheck worker
docker compose restart worker
```

日志轮转为每文件 10 MB、保留 3 份。Worker 中断后的任务租约、来源快照和邮件 unknown 规则继续生效。

### 本步验收

先构建两种镜像，再设置 NEWS_COMPOSE_LIVE=1 运行 tests/test_compose_live.py。用例创建带唯一名称的临时 Compose 项目、假凭据与独立卷，验证 API/Worker 健康、注册/订阅/预览、缺少模型的明确失败、实际进程退出自动重启、数据持久化，以及 Worker 内调用生产 Bash 和隔离探测。测试结束只删除本次带项目标签的测试资源，不删除默认数据卷。详细结果在 docs/acceptance.md；报告保存在 Git 忽略的 data/acceptance 中。

## 5.2：Linux HTTPS 与运维

在单台 Linux 服务器的本地磁盘运行 Docker/Compose，挂载 SQLite 卷；不要使用跨主机共享文件系统承载 SQLite。系统时间需同步。将项目、应用 .env 和 config 放在部署账户可访问的目录，`.env` 权限设为 600，备份媒体限制访问。Worker 可控制本机 Docker，因此部署账户及其代码应视为服务器的可信管理入口。服务器防火墙仅按实际需要开放 SSH、80、443，8000 继续只监听回环。

### HTTPS

使用已有域名，DNS A/AAAA 指向服务器；安装主机版 Caddy，并将 config/Caddyfile.example 的 news.example.com 改为真实域名后放到 /etc/caddy/Caddyfile。保持公网 80/443 可达，使 Caddy 自动申请和续期证书。操作前使用 `caddy validate --config /etc/caddy/Caddyfile`，通过后 `sudo systemctl reload caddy`。代理保留原始 Host 并发送 X-Forwarded-Proto；应用按同源和 CSRF 规则验证写请求。依据 [Caddy HTTPS](https://caddyserver.com/docs/automatic-https) 与 [反向代理文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

应用 .env 设置 `SESSION_COOKIE_SECURE=true`。本地 HTTP 演示保持 false；Secure Cookie 仅经 HTTPS 发送。首次先创建默认网络，再读取主机代理访问容器时使用的网关 IP：

```sh
docker compose create api worker
docker network inspect daily-ai-news_default --format '{{(index .IPAM.Config 0).Gateway}}'
export NEWS_TRUSTED_PROXY_IP=上一步输出的精确IP
docker compose -f compose.yaml -f compose.https.yaml up -d --no-build --force-recreate api worker
```

自定义项目名时，网络名对应 `<项目名>_default`，每条 compose 命令使用同一个 `-p`。NEWS_TRUSTED_PROXY_IP 是 shell 操作参数，不写入应用 .env。compose.https.yaml 仅使 API 信任这个精确地址的转发头；不要设置通配符。网络重建后重新确认地址。以后启动/重建 HTTPS 部署继续同时传入两份 Compose 文件；仅 stop/run 运维命令可使用基础文件。

上线验收：HTTP 跳转 HTTPS、证书域名/有效期正确、注册登录与偏好保存成功、Cookie 含 Secure/HttpOnly/SameSite、跨来源写请求拒绝、外部不可直接访问 8000；随后检查 API/Worker healthy。没有实际服务器与域名时，这些公网结果记录为待部署验收。本轮以代理 ASGI 测试覆盖可信/伪造转发头、同源/跨来源和 Secure Cookie，并展开检查 HTTPS Compose 配置。

### 一致性备份

`python -m app.ops` 是管理员离线命令，不向模型或网页开放。停写是前提：停止全部 API/Worker 实例，确认没有原生进程仍指向同一数据目录，再执行。`--writers-stopped` 是操作确认，命令本身不会停止服务。停写期间不会自动发送邮件。备份包含 news.sqlite3、runs、source_jobs、file_jobs；正文、索引、笔记、待恢复意图和数据库保存在同一包内。SQLite 使用 [backup API](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)，包括已提交的 WAL 内容；不直接复制 WAL/SHM。tool_runtime、日志、测试资料和 .env 不打包。

Linux 主机创建可供容器 UID 10001 写入的备份目录，并采用唯一文件名：

```sh
sudo install -d -m 700 -o 10001 -g 10001 /srv/daily-ai-news-backups
docker compose stop api worker
docker compose run --rm --no-deps -T -v /srv/daily-ai-news-backups:/backups api \
  python -m app.ops backup --data-dir /data --archive /backups/2026-10-15.zip --writers-stopped
# 只有返回 ok:true 后才将包复制到另一块磁盘或受控备份存储。
docker compose start api worker
```

HTTPS 服务日常重启可用 start 保留既有参数；重建按上节的双文件命令。Windows Docker Desktop 可把 `-v` 前的主机路径换成实际绝对目录。原生目录备份先停止其服务，再运行 `.venv\Scripts\python.exe -m app.ops backup --data-dir data --archive E:\备份目录\唯一名称.zip --writers-stopped`。

备份失败时返回固定错误码，不回显正文、路径内容或凭据；未发布的临时文件自动清理，已有备份不覆盖。包内 manifest 保存每个文件的 SHA-256 和长度。当前限制为 10 GiB、10 万文件、16 MiB manifest；遇到超限先保留原数据并评估容量。包含用户资料、研究记录、受控模型协议和邮件内容，按私有数据保管；哈希校验检测损坏，不提供来源签名或加密。.env 与 RSS 配置分别安全备份，恢复时保留原 SESSION_SECRET 才能继续使用已有会话。

### 恢复与升级

只恢复到新的空数据目录或空卷；命令拒绝覆盖现有数据。保留旧卷，使用新项目名 `daily-ai-news-restored` 进行演练，确保原项目已停止以释放端口：

```sh
docker compose stop api worker
docker compose -p daily-ai-news-restored run --rm --no-deps -T \
  -v /srv/daily-ai-news-backups:/backups:ro api \
  python -m app.ops restore --data-dir /data --archive /backups/2026-10-15.zip --writers-stopped
docker compose -p daily-ai-news-restored up -d --no-build --wait api worker
```

上面的启动命令适用于本地 HTTP 演练；服务器仍按 HTTPS 小节读取新网络网关，并使用双文件命令启动。恢复先在目标卷内临时目录完整校验路径、重复/特殊文件、长度、哈希、SQLite integrity_check、foreign_key_check 与版本，验证后才发布材料目录，数据库最后发布。正常异常会回滚本次发布；进程硬中断可能留下临时目录，此时保留错误现场并换新的空卷重试。不要清空含用户数据的目录来绕过检查。

恢复后核对用户/订阅/历史、来源引用可读、任务窗口/预算、邮件记录和 Worker 心跳。备份后的外部副作用无法回滚：旧包里 pending 的邮件可能已被服务器接受。生产灾难恢复先在 .env 设置 `SMTP_ENABLED=false`，核对发送流水与实际邮箱后再启用；unknown 沿用显式确认规则。未完成任务保持原截止时间，超时后按原规则失败，恢复不会刷新新闻窗口。

原生数据迁入 Compose：先备份原生 data，再按新空项目卷的恢复步骤导入。同版本恢复优先使用备份时对应的 Git commit/镜像；升级前备份并保存镜像标识，启动会执行有版本检查的数据库迁移，回滚时恢复旧包及旧镜像，不对新数据库直接降级。每次备份后检查退出码并定期在独立项目演练恢复；本轮已经通过两个隔离 Compose 卷之间的实际恢复验收。

### 排障顺序

先看 compose ps/health 和最近 100 条日志；按页面 run_id 查询任务事件，区分模型、搜索、正文、提交核对或邮件错误。检查宿主磁盘空间、Docker 引擎与网络，再确认配置字段是否齐备。不要将 .env、compose config 的完整输出、Cookie、授权码或数据库公开贴到问题记录。心跳健康不代表外部供应商可用；供应商验收结果单独记录在 docs/acceptance.md。
