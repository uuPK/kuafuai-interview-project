# 每日 AI 新闻助手

根据订阅话题自主搜索、读取原文并整理每日中文 AI 简报。支持预览与重新生成、历史阅读、待核实信息说明、每日定时邮件和手动发送。

技术栈：Python + FastAPI + 原生 HTML/CSS/JavaScript + SQLite，DeepSeek 负责研究决策，博查提供搜索，独立 Worker 执行研究、调度和邮件发送。

## 5 分钟快速启动

以下命令在项目根目录执行，适用于 Windows PowerShell、Linux 和 macOS。先安装好 Git、Python 3.12+、Docker，并启动 Docker 的 **Linux 容器引擎**。无需先安装 Python 第三方包、Node.js 或独立数据库。

Docker Engine 需要 26.0+，Compose 需要 2.24+；分别用于工具容器的 [volume-subpath](https://docs.docker.com/engine/release-notes/26.0/) 和配置文件的 [env_file.required](https://docs.docker.com/reference/compose-file/services/#required)。执行 `docker info`、`docker compose version` 确认可用。需要能够访问 Docker Hub、ghcr.io 和 PyPI；首次下载镜像的耗时取决于网络。

### 1. 获取项目并生成配置

当前仓库为私有仓库，其他人需先获得读取权限，并在 Git 客户端完成 GitHub 登录后克隆；`Repository not found` 或要求登录时，先确认访问权限。仓库链接不会附带部署者的密钥，运行时填写自己的服务参数。

```sh
git clone https://github.com/uuPK/kuafuai-interview-project.git
cd kuafuai-interview-project
python scripts/prepare_env.py
```

该命令从 [.env.example](.env.example) 创建本地 `.env`，自动生成随机 `SESSION_SECRET`；已有 `.env` 会保留。如果系统使用 `python3` 命令，替换本文的 `python` 即可。

### 2. 填写模型和搜索密钥

用文本编辑器打开 `.env`，替换下列五行，保留自动生成的 `SESSION_SECRET` 和其他默认值：

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_API_KEY=填写你的DeepSeek密钥
BOCHA_SEARCH_URL=https://api.bochaai.com/v1/web-search
BOCHA_API_KEY=填写你的博查密钥
```

密钥申请：[DeepSeek 控制台](https://platform.deepseek.com/)、[博查开放平台](https://open.bochaai.com/)。模型名和接口示例依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/quick_start/agent_integrations/) 与 [博查官方示例](https://open.bochaai.com/)，可按账户实际可用模型调整；模型需支持工具调用。真实研究会消耗对应服务额度。

还没有密钥也可以先跳过本步，启动网页、注册和保存订阅；生成真实新闻需要填写完整配置。SMTP 和 RSS 均默认关闭，不影响网页启动。

### 3. 构建并启动

Linux 使用本机 Docker socket 时，先设置 Worker 的 socket 组权限；Docker Desktop 通常可保持默认值，跳过此命令：

```sh
export BASH_SOCKET_GID="$(stat -c %g /var/run/docker.sock)"
```

然后执行以下命令：

```sh
python scripts/build_images.py
docker compose up -d --no-build --wait --wait-timeout 90 api worker
docker compose ps
```

构建脚本使用 Python 标准库，自动构建应用与工具两种镜像，同时兼容 Windows 中文目录。第三方依赖在镜像内按 `uv.lock` 安装，不需要先创建 `.venv`。

### 4. 打开页面

- 页面：[http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- 健康检查：[http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- `docker compose ps` 中 `api`、`worker` 均显示 `healthy`，表示服务已启动。

首次使用自行注册账户，密码为 12～128 个字符。进入“订阅设置”保存至少一个话题，例如“开源模型”，保持订阅启用，然后点击“生成预览”。生成期间可以继续阅读旧简报；完成后点击“阅读新简报”。每次结果都保存在“历史简报”中。

**5 分钟指启动服务；真实简报生成另需等待模型、搜索和原文抓取，单次任务默认最多 10 分钟。** 健康状态表示服务进程正常，模型与邮箱是否联通需分别验证。待核实新闻会显示简介、具体疑点和原文链接，并与主新闻分栏。

## 依赖清单与一键安装

| 文件 | 用途 |
| --- | --- |
| [requirements.txt](requirements.txt) | 从锁文件导出的完整运行依赖，固定版本及包哈希，可直接供 pip 安装 |
| [pyproject.toml](pyproject.toml) | 直接依赖、Python 版本和开发依赖声明 |
| [uv.lock](uv.lock) | Docker 与 uv 使用的可复现依赖锁文件 |
| [compose.yaml](compose.yaml) | 一次启动 API、Worker、共享数据卷与健康检查 |

Docker 路径按上面的构建、启动命令即可一次准备环境。原生 Python 开发可执行：

```sh
python -m venv .venv
```

Windows 安装依赖：

```powershell
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
```

Linux / macOS 安装依赖：

```sh
.venv/bin/python -m pip install --require-hashes -r requirements.txt
```

然后在两个终端分别执行以下命令，将命令中的 `python` 替换为 Windows 的 `.venv\Scripts\python.exe` 或 Linux/macOS 的 `.venv/bin/python`；二者在同一项目目录运行，以共享 `.env` 和 `./data`。模型的 Bash 工具仍需要 Docker 和已构建的工具镜像。

```sh
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

```sh
python -m app.worker
```

如使用 uv，可用 `uv sync --frozen --extra dev` 一次安装项目及测试工具。依赖更新后，从锁文件重新导出清单：

```sh
uv export --frozen --no-dev --no-emit-project --output-file requirements.txt
```

## 数据库与 migration

使用 SQLite，无需安装 MySQL、PostgreSQL 或手动创建数据库账号。

- **建表与升级 SQL**：[app/storage/schema.py](app/storage/schema.py)，按版本保存 SQL，当前版本为 16。
- **迁移执行器**：[app/storage/database.py](app/storage/database.py)，在事务内初始化/升级，以 `PRAGMA user_version` 记录版本；重复执行保留已有数据，拒绝向低版本应用降级。
- **独立命令**：[app/migrate.py](app/migrate.py)，与 API/Worker 启动使用同一套迁移。

首次启动 API/Worker 会自动建表。需要提前初始化时，镜像构建后执行：

```sh
docker compose run --rm --no-deps api python -m app.migrate
```

原生环境使用虚拟环境的 Python：

```sh
python -m app.migrate --data-dir ./data
```

成功输出 `Database ready: schema version 16.`。默认数据位置：

| 运行方式 | 数据库与持久化 |
| --- | --- |
| Docker Compose | 卷 `daily-ai-news-data`，容器内 `/data/news.sqlite3`，API/Worker 共享 |
| 原生 Python | `./data/news.sqlite3`；可通过 `DATA_DIR` 修改 |

来源快照、任务状态、历史简报和发送记录也保存在数据目录。`docker compose down` 保留数据卷；需要保留数据时不要执行 `docker compose down -v`。升级前的停写、备份和恢复操作见 [部署说明](docs/deployment.md)。

## 开启邮箱发送（可选）

在 `.env` 填写 SMTP 参数。以 QQ 邮箱的 SSL 配置为例，账号与收件地址均使用你自己的值：

```dotenv
SMTP_ENABLED=true
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_TLS_MODE=ssl
SMTP_USERNAME=你的QQ邮箱地址
SMTP_PASSWORD=邮箱生成的SMTP授权码
SMTP_FROM=你的QQ邮箱地址
SMTP_TEST_RECIPIENT=
```

使用其他服务商时按其 SMTP 配置填写。授权码取自邮箱的 SMTP 服务设置，和网页登录密码分开管理。`SMTP_TEST_RECIPIENT` 留空允许投递到账户的收件邮箱；填写后，只允许该测试地址，页面会提前说明名单不匹配。

重新注入配置：

```sh
docker compose up -d --no-build --force-recreate --wait api worker
```

在页面“个人资料”设置登录与收件邮箱。“订阅设置”可设置每日邮件生成时间（北京时间）：到点开始生成，完成后自动发送。预览不会自动发信，点击简报中的“发送这份简报到邮箱”可手动发送。暂停订阅会同时暂停生成和发信。

## 常用操作与排错

```sh
docker compose logs --tail 100 api worker
docker compose stop
docker compose up -d --no-build --wait api worker
```

| 现象 | 检查与处理 |
| --- | --- |
| Docker 无法连接 | 启动 Docker Desktop/Linux 引擎，先让 `docker info` 成功 |
| 镜像构建下载超时 | 检查 Docker Hub、ghcr.io、PyPI 网络；重跑构建会复用已完成的缓存 |
| 8000 端口被占用 | PowerShell 执行 `$env:NEWS_HTTP_PORT='8001'`；Linux/macOS 执行 `export NEWS_HTTP_PORT=8001`，再启动并访问 8001 |
| 注册返回认证服务未配置 | 确认 `.env` 中 `SESSION_SECRET` 为至少 32 字符的随机值；`prepare_env.py` 保留已有配置，需自行补齐空值，再重建容器 |
| 网页可打开，生成失败 | 核对 DeepSeek/博查五个字段、账户额度及外网访问；缺少模型配置会报告 `model_dependency_configuration` |
| 原文抓取报告 `PUBLIC_ADDRESS_BLOCKED` | 检查代理 Fake-IP；可在 `.env` 设置 `PUBLIC_DNS=google_doh` 后重建容器，公网地址校验仍有效，详见部署说明 |
| 邮件发送按钮禁用 | 检查 SMTP 是否启用、参数是否齐全、账户收件人是否符合测试名单 |
| 修改 `.env` 后没有生效 | 使用上面的 `--force-recreate` 命令；单纯 `restart` 不会重新注入环境变量 |

配置文件只填写 `.env.example` 中的应用字段；`NEWS_HTTP_PORT` 等 Compose 参数设在命令行环境。`.env`、数据库和授权码不上传 GitHub。服务默认仅监听本机，公网 HTTPS 部署见 [docs/deployment.md](docs/deployment.md)。

## 测试与代码入口

安装 uv 开发依赖后，离线回归不调用真实模型或外部邮箱：

```sh
uv sync --frozen --extra dev
uv run --no-sync pytest -q
node --test tests/test_frontend.mjs tests/test_frontend_state.mjs
```

Node.js 仅供前端测试使用，运行网站不需要。包含真实 Docker 的完整验收入口：`uv run --no-sync python scripts/verify.py --docker --node node`；真实模型、搜索和邮箱测试需单独显式开启，见 [验收记录](docs/acceptance.md)。

```text
app/api/        登录、订阅、简报与邮件 HTTP 接口
app/agent/      研究循环、提示词、稿件规则与事实核对
app/tools/      文件、检索、抓取、提交及隔离 Bash 工具
app/providers/  DeepSeek、博查、正文提取、RSS 与 SMTP
app/storage/    SQLite 表结构、迁移、持久化和恢复状态
app/worker/     后台执行、调度与邮件队列
app/static/     页面、样式与浏览器交互
scripts/        配置准备、镜像构建与验收脚本
tests/          自动化测试
```

更多说明：[实施计划](实施计划-v1.2.md) · [开发说明](docs/development.md) · [核心验收](docs/core-acceptance.md) · [部署与备份](docs/deployment.md)。
