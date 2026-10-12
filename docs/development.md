# 开发与配置

当前完成步骤 2.2：执行骨架、持久记录、DeepSeek 适配器和博查搜索候选。后续功能按《实施计划-v1.2.md》第 8.1 节逐步实现。

## 本地环境

Python 要求 3.12 或更高，本步在 Windows、Python 3.13.14 上验证。使用 uv 管理项目虚拟环境；`uv.lock` 固定应用及测试依赖，`.venv/` 与 `.uv-cache/` 不进入 Git。

在项目根目录执行：

```text
uv sync --frozen --extra dev --cache-dir .uv-cache
uv run --frozen --extra dev --cache-dir .uv-cache python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

启动后请求 `GET http://127.0.0.1:8000/health`，成功响应为 `{"status":"ok"}`。启动过程初始化 SQLite；DeepSeek、博查分别在配置齐全时建立异步客户端，关闭应用时释放连接。启动与健康接口均不发送真实服务请求，健康响应只代表进程可响应。研究任务的公开 API 与 Worker 在对应步骤加入。

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
