# 开发与配置

当前完成步骤 2.6：执行骨架、持久记录、DeepSeek、博查与 RSS 候选、正文快照和字符位置索引、五个基础工具，以及历史查询、任务进度和偏好研究提示词。正式简报提交、质量核对与 Worker 按阶段 3 实施。

## 本地环境

Python 要求 3.12 或更高，本步在 Windows、Python 3.13.14 上验证。使用 uv 管理项目虚拟环境；`uv.lock` 固定应用及测试依赖，`.venv/` 与 `.uv-cache/` 不进入 Git。

在项目根目录执行：

```text
uv sync --frozen --extra dev --cache-dir .uv-cache
uv run --frozen --extra dev --cache-dir .uv-cache python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

启动后请求 `GET http://127.0.0.1:8000/health`，成功响应为 `{"status":"ok"}`。启动过程初始化 SQLite；DeepSeek、博查分别在配置齐全时建立异步客户端，并建立公开网页客户端，关闭应用时释放连接。启动与健康接口均不发送真实服务请求，健康响应只代表进程可响应。研究任务的公开 API 与 Worker 在对应步骤加入。

## 配置读取与边界

本地将 `.env.example` 复制为 `.env`，填写自己的配置。未填写服务参数时仍可启动本步应用。测试使用临时配置与固定假凭据，测试配置不读取项目真实 `.env`。

`app/config.py` 的 Settings 读取优先级为：显式传入参数、进程环境变量、项目工作目录中的 `.env`、默认值。空环境变量沿用默认值，空白服务参数作为缺失处理。`.env` 中未知字段会报错，帮助发现拼写错误。配置在应用工厂启动时读取，加载后不可修改；修改配置后重启生效。这些行为基于 [Pydantic Settings 官方文档](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)。

| 配置组 | 主要字段与约束 |
| --- | --- |
| 存储 | `DATA_DIR` 默认为 `./data`，解析成绝对目录；`database_path` 为其下 `news.sqlite3`。启动初始化数据库并保留既有运行记录 |
| DeepSeek | endpoint、key、model 从 `.env` 读取；默认关闭思考、temperature=0.2、max_tokens=4096；思考开启时读取 `DEEPSEEK_REASONING_EFFORT`（low/high/max，默认 high） |
| 博查 | `BOCHA_SEARCH_URL` 为完整 REST 端点，`BOCHA_API_KEY` 只进入 Authorization 请求头；未配置时工具明确反馈配置缺失 |
| SMTP | host、port、用户名/密码、发件人、TLS 模式、指定测试收件人；本地测试 SMTP 可无需认证，配置认证时须提供完整用户名/密码 |
| 会话 | 密钥待填；填写时至少 32 字符；Secure Cookie 与有效期可配置 |
| 网络限制 | 单次超时默认 30 秒；含首请求最多尝试 3 次；重定向和下载大小限制可配置 |
| 任务预算 | 默认模型 15、搜索 8、抓取 12、工具 50 次；10 分钟截止；模型预算至少 2 次，以容纳后续决策预留 |
| 内容大小 | 工具结果默认 12,000 字符、模型输入默认 120,000 字符、`MAX_MODEL_RESPONSE_BYTES` 默认 2,000,000 字节；执行器、循环与 HTTP 读取分别检查 |

API key、SMTP 认证信息、会话密钥使用 `SecretStr`，相关字段排除 repr 与配置序列化；端点和邮件地址也排除展示。校验错误的文字隐藏输入值，服务配置缺失错误只列字段名。Settings 属于服务端内部对象，不能整体放入模型输入、工具结果或公共响应。Pydantic 的结构化 `ValidationError.errors()` 仍可能包含原始输入，后续错误记录须通过统一脱敏处理，不直接记录原始对象。

服务端点只接受 http(s)，拒绝 URL 内嵌账号密码、查询参数和 fragment。`missing_service_fields()` 仅判断参数是否齐全；`require_service_config()` 为后续真实适配器提供调用前检查，不能代表联通验收。

DeepSeek 的 `DEEPSEEK_BASE_URL` 是 API 根地址，可使用官方 `https://api.deepseek.com` 或其 `/v1` 路径；适配器追加 `/chat/completions`，模型 ID 来自账号可用目录。普通模式明确发送 disabled 思考开关；思考模式明确发送 enabled 与 effort，并完整回传历史 reasoning_content。参数与历史均经校验，完整响应保存于受控数据库，公开界面不展示思考内容。协议依据见 [DeepSeek 官方工具调用](https://api-docs.deepseek.com/guides/tool_calls/) 与 [思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

适配器不跟随重定向，凭据只附加到配置端点的 Authorization 请求头；不从环境代理读取连接设置。单次调用对应一次 HTTP 尝试，预算、退避、Retry-After 与 waiting_retry 沿用 harness。HTTP 与 schema 错误保存有长度限制的脱敏摘要、状态码及可取得的请求 ID，具体结果见 `docs/acceptance.md`。

## 测试与代码检查

```text
uv run --frozen --extra dev --cache-dir .uv-cache python -m pytest --cov=app --cov-report=term-missing
uv run --frozen --extra dev --cache-dir .uv-cache ruff check app tests
uv run --frozen --extra dev --cache-dir .uv-cache ruff format --check app tests
git diff --check
```

pytest 临时文件固定在 Git 忽略的 `.pytest_cache/tmp/`，每次运行会清理该临时目录，请勿在其中保存手工材料。这样测试文件留在项目权限范围内。测试自动清除已知配置环境变量，避免依赖开发机凭据；临时 `.env` 只包含测试数据。

HTTP 测试使用 FastAPI TestClient，开发依赖包含其当前使用的 `httpx2`，见 [Starlette TestClient 文档](https://starlette.dev/testclient/)。DeepSeek、博查适配器使用运行依赖 `httpx`；离线契约测试使用 MockTransport，不读取真实凭据或访问服务。测试将警告视为错误，并检查分支覆盖率；测试与真实联通结果见 `docs/acceptance.md`。

## 搜索候选与恢复

真实 `web_search` 在 `app/tools/search.py` 注册，模型只提供 `query / language / domains / limit`。query 使用实际中文或英文词，language 是查询意图，未映射为供应商语言过滤参数，也不替代返回材料的实际语言。domains 接受主机名、最多 10 个，统一为小写 IDNA 并去重，拒绝 URL、端口、IP 和查询操作符；服务端加入 site 查询条件与已实测的 `include`（多域名用 `|` 连接），仍核对返回链接的主机名及子域边界。limit 默认 5、最大 10；固定发送 `summary=true / freshness=oneDay`。协议和端点依据 [博查开放平台](https://open.bochaai.com/)，实际能力与样本见验收记录。

候选统一保留标题、规范 URL、摘要/短摘录、站点、供应商语言和原始日期线索。`datePublished`、`dateLastCrawled` 缺失或为空时保留该状态，不推断统一发布时间；oneDay 是供应商粗筛条件，正文时间核实在 2.3。候选 URL 的规范化只检查语法、账号密码和字面内网地址，搜索阶段不访问候选网页；公网 DNS、实际连接与跳转检查由 2.3 抓取执行。

工具根据运行中的订阅快照检查排除词，过滤越域链接及重复规范 URL，反馈各类过滤数量。有效无结果返回成功的空候选和补查提示；无效响应、鉴权/额度不足、下载超限等返回明确错误，避免把依赖失败解释为没有新闻。429、5xx、超时和网络故障沿用 harness 的最多三次尝试、Retry-After、截止时间与 waiting_retry；一次逻辑搜索只计一次搜索与工具预算。

SQLite v5 增加 discoveries、candidates，升级保持既有消息、调用与预算。候选生成 `cand-` ID 并绑定当前 run 和工具调用，查询同时核对当前用户及任务。HTTP 请求在数据库事务之外完成；脱敏供应商响应、候选、完整工具返回结果与搜索事件一起在短事务保存。已保存发现快照不可覆盖，同一调用再次执行直接返回原结果和候选 ID；工具最终完成与反馈消息仍沿用执行器的事务。保存后、工具完成前中断时可复用发现快照；执行状态不确定的 Worker 接管仍在 3.3 实施。

模型收到每条标题最多 300、摘要最多 800 字符，并看到截断标记；候选原始文本保存在受控数据库。整个工具结果继续受 `MAX_TOOL_RESULT_CHARS` 限制，必要时减少可见条目并标明省略数量，无法容纳任何条目时反馈大小错误。供应商响应读取受 `MAX_DOWNLOAD_BYTES` 限制；真实契约样本在 `tests/fixtures/bocha-success.json`，运行数据库与真实请求报告在 Git 忽略的 `data/acceptance/`。

## 正文抓取、时间依据与快照

`app/tools/fetch.py` 注册真实 `fetch_url(candidate_id)`，candidate_id 为 1～100 字符，必须来自当前用户、当前 run 的发现记录。工具参数拒绝额外字段；URL、任务目录、用户和调用 ID 由服务端取得。应用启动注册 search/fetch，研究任务入口按后续步骤接入。一次逻辑抓取计一次 fetches 和 tool_calls，临时故障沿用 harness 的有限重试、Retry-After、截止时间和 waiting_retry。

`app/providers/public_http.py` 使用 [HTTP Core 的网络后端接口](https://www.encode.io/httpcore/network-backends/)：先解析域名，检查整组 DNS 地址，直接连接通过校验的 IP；发送 HTTP 前和 TLS 前后核对实际 peer，HTTPS 使用原域名的 Host/SNI 和默认 CA 证书校验。拒绝内网、回环、链路本地、保留、多播及 IPv6 过渡地址；每次重定向重新校验 URL、DNS 和连接。请求不携带 API 密钥、Cookie 或环境代理配置。最多 `MAX_REDIRECTS` 次跳转，整个 GET 尝试受 `REQUEST_TIMEOUT_SECONDS` 约束，正文受 `MAX_DOWNLOAD_BYTES` 限制。接收 HTML/XHTML，使用 identity 编码，其他内容类型或压缩编码返回明确错误。

`app/providers/article.py` 根据 [Trafilatura 提取接口](https://trafilatura.readthedocs.io/en/latest/corefunctions.html) 提取正文与元数据，先识别网页编码再统一成 UTF-8。HTML 解析禁用额外网络请求。正文保留标题、段落、列表与表格行；标题优先取页面声明的首行，缺失时使用候选标题。正文脱敏后统一 LF 换行，再一次生成 Unicode 字符偏移、SHA-256 和章节/段落索引。偏移从零开始，区间为 `[start_offset, end_offset)`，按解码后字符串计算，包含中文和 emoji。

发布时间依据单独保存：HTML 明确发布日期、文章 JSON-LD、标记 datePublished 的 time 元素，以及搜索和提取器的原始日期线索。只有原文明示、含时区的精确发布时间一致且处于 run 固定窗口，才返回 `usable_in_window=true`。日期缺失、仅精确到天、无时区、冲突或窗口外时，正文仍可保存，但工具返回 false 和具体 issues，供模型补查或换来源；抓取时间和更新时间不补成发布日期。正式提交检查在 3.1 接入。

模型得到 source_id、最终 URL、标题、相对正文路径、字符数、哈希、最多 800 字符预览、章节位置、时间判定和同 URL/同正文的来源线索。返回值受 `MAX_TOOL_RESULT_CHARS` 限制，缩减章节时附 omitted_sections，完整索引留在受控材料中；无法容纳最小结果时明确失败。相同 URL 的新逻辑调用创建新快照，保留旧来源；相同调用的完整登记材料复用原 ID、正文与位置，不重新访问网页。事件级合并在 2.6 实施。

`app/storage/snapshots.py` 把正文和 index.json 保存到 `data/runs/<run_id>/sources/<source_id>/`。临时文件在同目录写入并 fsync，通过硬链接原子发布且禁止覆盖已有不同内容；固定快照不属于模型 scratch 写权限。可信登记材料保存在模型工作目录之外的 `data/source_jobs/<run_id>/`，记录调用、来源、索引和完整工具结果，先保存意图再发布正文/索引。

SQLite v6 增加 sources 与 source_calls，升级保留原候选和调用。网络、提取和文件发布在数据库事务之外；执行器的服务端同步完成回调把来源元数据、索引、关联、调用结果与模型反馈放进同一短事务。任一写入失败全部回滚，完整文件可继续复用。文件、索引、登记材料或数据库内容缺失/不一致时返回 `SOURCE_STATE_PROBLEM`；路径和归属越界返回权限错误。自动领取和接管尚未完成登记的执行调用在 3.3 实施，本步测试已验证重开数据库后的材料复用和一致登记。

## 五个基础工具与材料权限

应用启动在 `app/main.py` 注册五个基础工具，沿用 `ToolExecutor` 的严格 schema、当前用户/run 绑定、统一 `ToolResult`、调用记录、结果大小、截止时间和反馈完成事务。五个工具只计 `tool_calls`；材料关键词检索不计外部搜索预算。启动不执行 Docker 请求。模型参数不能指定身份、任务目录、凭据或收件人。

| 工具与参数 | 行为与边界 |
| --- | --- |
| `list_dir(path=".")` | 列当前任务材料目录；返回相对路径、文件字符数、哈希。只读 sources/scratch，结果容量不足时返回 omitted_count；隐藏条目不展示 |
| `read_file(path, offset=0, limit=3000)` | 按 Unicode 字符、零起点、半开区间读取 UTF-8；limit 最大 6000。返回实际 start/end、next_offset、eof/truncated、完整字符数和哈希。正文附 source_id，索引和笔记不提供正文引用身份 |
| `search_content(keyword, dir=".", cursor=null, limit=10)` | 非空字面、区分大小写检索；limit 最大 20。返回匹配和邻近摘录的字符范围、路径、版本、正文 source_id；索引 JSON 不参与全文搜索 |
| `write_file(path, content)` | 只写 scratch，脱敏后统一 UTF-8/LF。记录原哈希、目标哈希和完整结果后同目录临时写入/fsync/原子替换。返回哈希和字符数；写草稿不改变交付状态 |
| `bash(command)` | 只接受单条 ls/wc/head/tail/sort/uniq/rg 命令及明确允许的参数；模型命令分成 argv，经固定 Bash 脚本 `exec "$@"` 启动。rg 强制字面匹配，Bash 输出不作为引用字符索引 |

材料路径只允许当前任务的 sources/scratch，拒绝绝对路径、盘符、越界、符号链接和 junction、隐藏文件、Windows 设备名/备用数据流及敏感凭据路径。来源正文/索引读取先经过 SourceStore 归属和双文件哈希核对，再核对实际读入字节；来源文件不可写。普通笔记须有效 UTF-8，含未经脱敏凭据的文件阻止读取，避免正文删改后位置漂移。文件、扫描总量和目录条目有独立上限，工具完整 JSON 仍受结果字符限制。

SQLite v7 新增 material_reads 和 material_cursors，保持已有候选、来源、消息和预算。成功读取的版本与调用结果/反馈在同一短事务保存；笔记变化后旧分页明确返回 `FILE_VERSION_CHANGED`，从 offset=0 重新读取会建立新版本。首次按章节位置读取可直接使用非零 offset。搜索游标是随机令牌，服务端保存 run、目录、关键词哈希、完整文件版本清单和位置；伪造或跨查询返回 `FILE_CURSOR_INVALID`，增删文件或内容变化返回版本变化。结果容量缩小时，游标从最后实际返回的命中继续，避免丢失条目。

写入意图保存在模型目录之外的 `data/file_jobs/<run_id>/<call_hash>.json`，核对调用、路径、内容、哈希和结果一致性。完成前中断可复用同一意图：当前哈希等于目标则复用原结果；等于原哈希则继续写入；其他变化返回 `FILE_WRITE_CONFLICT`。文件写入在数据库事务外完成，material_written 事件、最终调用结果与模型反馈一起提交；反馈事务失败时文件和意图保留供恢复。执行状态不确定的调用仍交由 3.3 Worker 接管，本步没有自动重复覆盖。

## 隔离 Bash 运行环境

工具镜像源码为 `docker/tool/Dockerfile`，提供 Bash、GNU coreutils 和 ripgrep，默认非 root。启用本地 Linux Docker 引擎后，在项目根目录执行：

```text
docker build -t daily-ai-news-tools:local docker/tool
```

构建上下文仅为 docker/tool，项目 `.env` 与运行数据不会进入上下文。`BASH_IMAGE` 默认上述本地标签，工具使用 `--pull=never`，镜像需事先构建。`BASH_DOCKER_HOST` 为空时选择本地 Unix socket，Windows 选择 Docker Desktop Linux named pipe；配置只接受本地 unix/npipe 地址。Docker 管理端点仅由可信服务端使用，容器只有当前任务目录的只读 /work 挂载。

Docker Hub 下载不可达时，可从 [Docker 官方的 ECR 发布仓库](https://gallery.ecr.aws/docker/library/debian) 下载相同 Debian 基础镜像，增加本地标签后构建；无须修改系统 Docker 镜像源。本机 2026-10-13 已用此路径构建成功，基础镜像摘要记录于验收报告。

```text
docker pull public.ecr.aws/docker/library/debian:bookworm-slim
docker tag public.ecr.aws/docker/library/debian:bookworm-slim debian:bookworm-slim
docker build -t daily-ai-news-tools:local docker/tool
```

真实容器测试位于 `tests/test_docker_live.py`，默认跳过；设置 `NEWS_DOCKER_LIVE=1` 后执行 `python -m pytest tests/test_docker_live.py tests/test_bash.py`。测试要求可用本地 Linux 引擎及已构建镜像，直接核对容器权限、命令执行与清理。测试中的可信权限探测脚本不属于模型可调用命令。

运行参数固定禁用网络、只读根文件系统、非 root 用户 10001、移除全部 capability、no-new-privileges，并限制 PID=32、内存/交换总量=64 MB、CPU=0.5。启动禁止 profile/rc，清空 BASH_ENV/ENV；客户端进程只保留 PATH/Windows 系统和临时目录环境变量。每次建立空 auths/proxies 的临时 Docker 配置，防止默认客户端配置自动注入代理凭据。挂载前验证全部文件，容器不带模型/搜索/SMTP 密钥或 Docker socket。参数依据 [Docker 运行文档](https://docs.docker.com/engine/containers/run/) 与 [container run CLI](https://docs.docker.com/reference/cli/docker/container/run/)。

只读命令允许 ls 的 -l/-a/-la/-al/-1/-d、wc 的 -c/-m/-l/-w/-L、sort 的 -u/-n/-r、uniq 的 -c/-d/-u、rg 的 -n/-i/-l/-c/-F；head/tail 的 -n 取 1～200、-c 取 1～6000。文件参数最多 10 个，uniq 只允许一个输入文件；仅 ls/rg 可指定目录。拒绝管道、重定向、命令替换、控制字符、任意程序及其他选项。所有文件路径转换成 /work 内路径，参数通过独立 argv 传递。

stdout/stderr 合计读取受 `BASH_OUTPUT_BYTES` 限制，超过立即停止；执行受 Bash 超时和外层任务截止时间共同约束。超时/取消会终止 Docker CLI，并尝试 `docker rm -f` 清理当前调用的确定性容器名，临时配置最后清理。bash_started 事件在启动前保存 run、调用、命令名和容器名；Docker 失联时不能确认清理完成，后续 3.3 Worker 根据任务标签/容器名接管。Docker 不可用明确反馈 `BASH_UNAVAILABLE`，请求无响应反馈 `BASH_TIMEOUT`；真实隔离验收状态见 acceptance.md。

新增配置默认：`MAX_MATERIAL_FILE_BYTES=16100000`、`MAX_MATERIAL_SCAN_BYTES=32200000`、`MAX_MATERIAL_FILES=200`、`MAX_NOTE_CHARS=50000`、`BASH_TIMEOUT_SECONDS=10`、`BASH_OUTPUT_BYTES=24000`。上限与类型在 Settings 校验，`.env.example` 提供说明，配置修改后重启生效。

## 官方 RSS/Atom 候选

`app/tools/feed.py` 注册 `read_feed(feed_id, limit)`，模型只能选择服务端已启用的来源 ID；limit 默认 5，范围 1～10，拒绝额外参数和任意订阅地址。初始任务输入中的 available_feeds 仅包含 ID、名称。模型自行决定是否读取、选择哪个来源，以及读取后继续搜索或抓取；应用启动不访问 RSS。

在本地 `.env` 中设置 `RSS_SOURCES_FILE=data/rss-sources.json`，按 `config/rss-sources.example.json` 创建来源文件；示例只有禁用的占位来源。配置结构为：

```json
{
  "feeds": [
    {
      "feed_id": "example-official",
      "name": "官方来源示例",
      "url": "https://example.com/feed.xml",
      "enabled": false
    }
  ]
}
```

feed_id 为最多 64 字符的小写字母、数字、下划线或连字符，名称最多 100 字符；ID 不得重复，最多 20 个来源，文件最多 100 KB。URL 必须是无账号密码的公开 HTTP(S) 地址，配置不得含凭据。未设置路径时可启动但没有可用来源；明确指定的文件缺失、无效或含敏感内容时启动失败，错误只展示通用说明。来源配置加载一次，修改后重启生效；实际地址与可用性见 acceptance.md，本地运行配置与 `.env` 均受 Git 忽略。

`app/providers/feeds.py` 用共享 PublicHttpClient 请求清单，沿用整组 DNS、实际 peer、每次跳转、identity 编码、整体超时和下载大小校验；请求不带 API 密钥或 Cookie。lxml 解析 RSS 2.0 和 Atom，禁用实体替换、DTD 加载、网络与额外文件解析，拒绝 DTD、实体声明和 XInclude。解析上限由 `MAX_RSS_ENTRIES` 控制，默认 500、最大 2000；RSS 链接/永久 GUID、Atom alternate HTML 链接及 xml:base 相对地址在解析后再次规范化并校验，拒绝内网、凭据或无标题的条目。解析规则参考 [RSS 2.0](https://www.rssboard.org/rss-specification)、[Atom RFC 4287](https://www.rfc-editor.org/rfc/rfc4287) 和 [lxml 解析接口](https://lxml.de/parsing.html)。

候选保存 discovery_method/feed_id、标题、URL、摘要、语言及原始日期线索。RSS pubDate/dc:date 与 Atom published 只用于发现时粗筛；含时区且相互一致的精确日期用于排除明确的旧条目与未来条目。缺失、天精度、无时区和冲突日期保留原值，交给正文抓取核实；Atom updated 和清单更新时间单独记录。摘要移除 HTML 标记并脱敏，解析过程中不访问图片或文章。

搜索和 RSS 通过 CandidateStore 按当前用户/run 的规范 URL 双向复用候选 ID。同一 URL 首次登记的材料、日期和来源保持固定；本次工具结果顶层和脱敏发现快照记录当前发现方式、RSS 来源或博查响应，条目中的 discovery_method/feed_id 表示候选首次来源。原始响应保留本次线索，复用引用须属于当前 run。去重只处理页面链接，事件合并按 2.6 实施。整个发现结果、候选及复用关系在同一短事务登记，同一调用复用完整结果，反馈提交失败后可读回快照恢复；自动 Worker 接管在 3.3 实施。

返回标题最多 300、摘要最多 800 字符，附 text_truncated、过滤原因、reused_count、omitted_count；完整 JSON 受 `MAX_TOOL_RESULT_CHARS` 限制。有效空清单/全部旧条目返回成功的空候选，未知或禁用 ID、解析失败、全部条目无效、结果无法容纳等返回统一错误，HTTP 故障沿用公网请求错误码。429、5xx、超时由 harness 有限重试或保存 waiting_retry；每次逻辑读取计一次 searches 和 tool_calls，与 web_search 共用发现预算，重试保持同一调用，下载清单不扣 fetches。候选经 fetch_url 进入已有正文、索引、哈希、定位和分页读取链路，正式引用提交检查在 3.1 实施。

RSS 测试入口为 `tests/test_feeds.py`，覆盖配置/参数、解析与 XML 安全、日期、链接和候选复用、大小限制、重试/恢复及模型按反馈换来源后读取正文。真实请求报告保存到 Git 忽略的 `data/acceptance/`；本机已启用 NVIDIA，DeepMind 编码兼容和 Hugging Face 联通保持待复验。

## 偏好、历史与研究进度

`app/tools/research.py` 注册两个工具。`query_history(days, limit)` 默认最近 7 天/10 条，days 为 1～30，limit 为 1～20；基准时间固定为当前 run 的 window_end。`update_progress(collected, missing, issues)` 用整组替换保存三个短列表，collected/missing 最多 20 项、每项 120 字符，issues 最多 10 项、每项 300 字符。列表拒绝空白、控制字符、重复项，已取得与待补充话题不可重叠，话题必须来自订阅快照；大小写匹配后返回订阅原名。身份、任务、调用 ID 都由服务端绑定，两个工具各消耗一次 tool_calls，不扣搜索或抓取预算。

SQLite v8 增加 run_progress 和 delivered_history，升级保留既有窗口、预算、候选、来源、工具反馈和材料位置。`app/storage/research.py` 的进度完成回调与调用结果、模型反馈在同一短事务保存，失败回滚，重复完成同一调用不重复产生事件。进度保存原模型报告及 model_reported 标记，只辅助决策，不能改变 run 完成状态。重开数据库可读回最后一份进度，自动接管执行状态不确定的调用在 3.3 接入。

历史保存已接受简报中的事件标识、标题、话题、机构、产品版本、事件日期、关键事实、引用和交付时间；标题相似或 event_id 相同不会自动删掉不同事实。只读查询通过 runs.user_id 校验归属，仅读取 completed 运行的历史，排除当前任务及固定基准时间之后的交付，按时间排序并报告省略数量。来源 URL 和正文哈希从服务端来源记录取得，模型不能提供覆盖值；历史引用用于比较，不属于当前任务的新新闻证据。结果受完整 JSON 大小限制，无法容纳有效条目时返回明确错误；存储或来源关系损坏返回 HISTORY_STATE_PROBLEM，避免解释为有效空历史。

可信发布入口 `ResearchStore.record_delivery(connection, run_id, user_id, brief_id, events)` 要求已有写事务，校验完成状态、当前用户、全部来源归属与范围后才写入事件，固定交付记录不可覆盖。正式提交工具在 3.1 完成原文引用及新闻规则检查，并在同一交付事务调用此入口；本步生产应用尚无提交工具，也不会把普通文本、笔记或替身提交自动写成已交付历史。测试通过显式历史替身验证读写契约，正式历史入库联通保留给 3.1。

`app/agent/prompts.py` 明确订阅相关性、新进展、实际价值与证据充分程度四项选题依据；要求每条有简短 selection_reason、每个未覆盖话题有原因，合并同事件多报道、比较不同版本，并在缺口/失败/定位未命中后调整行动。JSON 草稿可写到 scratch/brief.json，items 每项至少带 title/topics/selection_reason；`app/agent/research.py` 只投影这三个有界字段，由文件完成回调记录 research_selections。摘要和其他草稿字段仍保存在文件与调用记录中。事件标记 model_reported，供时间线展示模型选题说明；草稿写入及理由事件与反馈协调提交，文件落盘后反馈失败仍可复用写入意图恢复。普通笔记不产生选题说明，写草稿不代表正式提交。

离线入口为 tests/test_research.py、tests/test_phase_two.py 和 tests/research_support.py；后者是标记 fixture 的固定新闻世界，生产解析、来源保存、文件读取和工具执行器仍实际运行。真实模型测试 tests/test_research_live.py 默认跳过，显式设置 NEWS_RESEARCH_LIVE=1 才用本地 DeepSeek 配置运行三个场景，每场最多 15 次模型请求、单次最多 2048 输出 tokens、外部请求只尝试一次；搜索/RSS/正文和历史均为替身，不请求真实搜索或邮件。

可再设置 NEWS_RESEARCH_REPLAY=1 审计每个场景已保存的最近轨迹；审计读取原数据库、草稿和固定正文，不新增模型请求，也不改模型草稿。报告的 verification_scope 明确限于阶段 2 决策和材料读取，citation_checks 单独记录原文切片是否等于 quote。本次三份真实模型草稿均暴露引用位置与摘录不一致，原始失败 JUnit 保留，阶段 3.1 继续严格拒绝并反馈修订；提示词已补充空白/换行与位置一致的要求，该新增提示尚未重新进行真实模型引用验收。
