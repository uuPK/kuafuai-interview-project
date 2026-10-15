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

日志轮转为每文件 10 MB、保留 3 份。Worker 中断后的任务租约、来源快照和邮件 unknown 规则继续生效。Linux HTTPS、Secure Cookie、一致性备份/恢复及服务器操作在 5.2 继续交付。

### 本步验收

先构建两种镜像，再设置 NEWS_COMPOSE_LIVE=1 运行 tests/test_compose_live.py。用例创建带唯一名称的临时 Compose 项目、假凭据与独立卷，验证 API/Worker 健康、注册/订阅/预览、缺少模型的明确失败、实际进程退出自动重启、数据持久化，以及 Worker 内调用生产 Bash 和隔离探测。测试结束只删除本次带项目标签的测试资源，不删除默认数据卷。详细结果在 docs/acceptance.md；报告保存在 Git 忽略的 data/acceptance 中。
