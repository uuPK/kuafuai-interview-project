# 步骤 1.1：项目骨架与配置

交付日期：2026-10-10。范围对应《实施计划-v1.2.md》第 8.1 节的 1.1；用户本次同时授权细化五阶段步骤，并将简短 review 要求写入 AGENTS.md。

## 简短 review

启动入口是 `app/main.py` 的 `create_app()`：读取并校验 Settings，再创建 FastAPI 应用并挂载健康接口。Settings 将 `.env` 和环境变量集中转换成有类型、有范围限制的配置；缺少真实服务参数时保留缺失状态，后续真实适配器调用前再检查。`/health` 仅返回进程状态。建议先看 `create_app()`，再看 Settings 的默认预算、敏感字段、校验器和服务配置检查，最后对照测试确认边界。

## 完成内容与逐文件说明

| 文件 | 职责与本步改动 |
| --- | --- |
| `AGENTS.md` | 新增第 8、9 条：每步测试/提交/同步/review 后停下；详细说明之外增加通俗的简短 review |
| `实施计划-v1.2.md` | 新增 8.1 节，共 25 个小步骤；保留原有阶段范围和验收目标，每阶段最后一步核对阶段验收 |
| `.gitignore` | 补充数据目录、SQLite/WAL 文件、uv 缓存、Python 包元数据和覆盖率输出忽略规则 |
| `.env.example` | 提供占位服务参数、会话配置、超时、重试和预算；真实凭据留空 |
| `pyproject.toml` | 定义 Python 包、运行/开发依赖、pytest 临时目录、分支覆盖率门槛和 Ruff 规则 |
| `uv.lock` | 锁定应用与测试依赖，供 `uv sync --frozen --extra dev` 重建环境 |
| `app/__init__.py` | 建立应用包，导入时不读取凭据或执行外部请求 |
| `app/config.py` | 集中定义 Settings、配置校验、存储路径、敏感值排除、服务参数缺失检查 |
| `app/main.py` | 提供应用工厂，可注入测试配置；保存进程内配置并注册路由 |
| `app/api/__init__.py` | 建立 API 包，后续产品接口按计划引入 |
| `app/api/health.py` | 只读健康接口与响应 schema，返回 `{"status":"ok"}` |
| `tests/conftest.py` | 自动移除测试进程中的已知配置环境变量，隔离开发机参数 |
| `tests/test_config.py` | 配置默认值、文件/环境/参数优先级、空占位、非法值、URL、密钥隐藏、服务完整性等 50 项测试 |
| `tests/test_app.py` | 健康响应、应用实例配置隔离、启动工厂读取 `.env` 的 3 项测试 |
| `docs/development.md` | 环境安装、启动、配置优先级与边界、测试及检查命令 |
| `docs/acceptance.md` | 按步骤记录交付入口，将真实服务和人工核对列为待验收 |
| `docs/reviews/step-1.1.md` | 本次详细说明与简短 review，记录测试和下一步边界 |

## 关键逻辑与边界

1. 配置在应用工厂启动时读取，可显式注入用于测试。读取顺序是构造参数、环境变量、`.env`、默认值；加载后不可修改，重启应用才加载新的配置。
2. key、SMTP 认证与会话密钥使用 SecretStr，同时从 repr 和序列化排除；端点、邮件地址也不展示。校验错误文字不附原始输入，缺失参数错误只列名称。公共健康响应不读取 Settings 内容。后续日志须对结构化错误单独脱敏。
3. 默认预算与原计划一致，本步只建立配置与校验；计数持久化在 1.2，引入执行限制与重试在 1.5。输入、工具结果和下载大小配置同样在对应执行步骤落实。
4. 服务参数可缺失，应用可启动；真实适配器后续调用 `require_service_config()` 后才联网。配置完整不代表服务可连接。SMTP 允许本地无认证测试服务，配置认证时检查用户名/密码成对提供。
5. 配置解析不创建数据目录或数据库，`database_path` 为受配置控制的数据目录中的 `news.sqlite3`。任务文件归属、越界检查和网页连接权限分别在后续步骤实现。
6. 本步没有新增模型工具，也未实现 Agent 决策循环。五个指定基础工具在 2.4 独立交付与测试。

## 验证结果

环境：Windows、Python 3.13.14；主要锁定版本为 FastAPI 0.143.0、Pydantic 2.14.0、pydantic-settings 2.15.0、pytest 9.1.1、Uvicorn 0.54.0。完整依赖见 `uv.lock`。

执行命令：

```text
.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing --tb=short
.venv\Scripts\ruff.exe check app tests
.venv\Scripts\ruff.exe format --check app tests
git diff --check
```

- pytest：53 passed，测试执行耗时 1.19 秒；应用代码 93 条语句、12 个分支，行与分支联合覆盖率 100%，达到 90% 门槛。
- Ruff：代码检查通过，8 个 Python 文件格式检查通过。
- Uvicorn CLI 冒烟：使用 `python -m uvicorn app.main:create_app --factory` 启动，临时绑定 127.0.0.1，请求 `/health` 得到 HTTP 200 和预期 JSON，检查后停止进程。
- 文件权限：pytest 临时文件放在项目内 `.pytest_cache/tmp/`。Windows 沙箱限制 TestClient 事件循环，本步完整离线测试经执行权限审核后在项目虚拟环境运行；没有请求真实模型、搜索或 SMTP 服务。

Git 差异检查及同步结果由最终交付说明附上，提交对应步骤 1.1。

## 未完成项与下一步

DeepSeek、博查、SMTP 联通，真实模型至少 3 次运行及至少 5 份简报人工核对均待验收，详见 `docs/acceptance.md`。当前应用只提供健康接口，数据库、工具和 Agent 尚待后续步骤实现；健康状态不代表这些能力已就绪。

下一步为 1.2「SQLite 与运行状态」：数据库初始化、运行和事件记录、订阅快照、固定新闻窗口、状态流转及预算持久化。步骤 1.1 提交同步并交付后停下，等用户确认再执行。
