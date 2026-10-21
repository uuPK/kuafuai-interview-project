# 核心验收与可复现演示

## 一次运行

在已安装开发依赖的项目根目录运行，Node.js 需要在 PATH 中或显式传入：

```powershell
.venv\Scripts\python.exe scripts/build_images.py
.venv\Scripts\python.exe scripts/verify.py --docker --node D:\Programs\node-v24.18.0-win-x64\node.exe
```

其他电脑将 --node 改为实际路径；Linux 可用 `python scripts/verify.py --docker`。不加 --docker 时跳过真实容器。入口顺序执行 Ruff、完整 pytest 与分支覆盖率、Node 前端状态测试；任一步失败立即返回非零状态。覆盖率门槛 90%。真实付费模型/搜索/邮箱验收开关会清除，不读取历史重放结果充当当前通过。Docker 用临时空客户端配置，不改变全局配置。

完整结果在 data/acceptance/final-regression/tests.xml、coverage.json；演示补查摘要在 data/acceptance/demos。质量与恢复轨迹在 data/acceptance/phase-three-step-3.5/<场景>/<UUID>/report.json，完整协议单独受控保存。数据目录全部由 Git 忽略，演示数据为固定虚构新闻和模型替身。当前实测数量与日期见 docs/acceptance.md。

## 八项验收对应

| 计划验收 | 主要用例 | 判断依据 |
| --- | --- | --- |
| 1：中文、最多五条、信息说明与固定引用 | test_briefs.py、test_review.py、test_fetch.py、test_verification_display.py | 已知超窗不入主新闻；时间缺项强制标注；事实存疑单列原文链接；字符位置/摘录/正文哈希一致；数字、比较条件与中文检查 |
| 2：结果/零结果、抓取成败、定位未命中调整 | test_phase_two.py、test_research.py、test_feeds.py、test_fetch.py | 同一候选结构；空清单后搜索，403 后换来源，中文未命中后英文命中并读回 |
| 3：话题缺口与补查 | test_research.py、test_phase_two.py | collected/missing/issues 持久化；后续查询、选择与缺口一致 |
| 4：摘要修订与事件去重 | test_review.py、test_phase_three.py、test_research.py | 数字/条件/范围问题反馈，一次修订；同ID/明确版本/完整事实集合一致先拒绝，其余由模型核对；同事件合并、新版本检查 |
| 5：响应、工具、入队、SMTP 接受后中断 | test_recovery.py、test_phase_three.py、test_recovery_docker_live.py、test_mail.py | 同 run/窗口/快照/预算恢复，简报和入队唯一；不确定邮件停止自动重发 |
| 6：凭据与权限/诱导/预算 | test_tool_execution.py、test_files.py、test_public_http.py、test_harness.py、test_auth.py | 目录、SSRF、用户隔离、网页诱导、限流、上下文与预算边界；真实 Bash 无网络/凭据/socket |
| 7：登录至预览/历史/调度邮件 | test_product_api.py、test_schedules.py、test_mail.py、test_compose_live.py | API 闭环、真实本地 TCP SMTP、定时日唯一、页面已审计；真实模型与真实邮箱另见 5.4 |
| 8：偏好与定位统计 | test_phase_two.py、test_research.py、test_files.py、test_review.py | 相同材料不同偏好产生不同查询/理由；排除词和未覆盖说明；定位后读取条件与固定正文一致 |

替身回归验证状态与执行规则。真实模型自主行为、真实来源摘要质量、指定邮箱实际收件及至少五份人工核对，依照实施计划另记，不由这个入口宣告通过。

RSS 为默认关闭的可选补充：tests/test_app.py、test_feeds.py 验证关闭时只开放搜索/正文等工具，启用时按可信来源清单开放 read_feed；test_briefs.py 验证有效搜索零候选时可选 RSS 失败不阻断空简报。test_review.py 验证来源时间状态进入独立核对输入，以及原预算内预留首次提交、核对、修订和再次核对。

2026-10-18 按用户要求调整显示规则：正文支持事实但发布时间缺失、时区/精度不明或时间冲突的条目可以显示，服务端生成不可省略的信息说明。涉及真实性的缺口先补查，仍存疑的候选在页面和邮件末尾单列“待核实信息”，保留原因和原文链接，不占主新闻条数，也不写入已交付事实历史。只有待核实候选时仍可保存，不能表示没有候选。tests/test_verification_display.py 覆盖说明、候选归属与重复、服务端链接、恢复快照、邮件和旧简报兼容；前端状态测试覆盖对应展示与链接安全。

用户审核后增加最后一次事实核对的待核实交付：test_unverified_fallback.py 覆盖两次事实拒绝、先修引用再被事实拒绝、确定性历史重复、只交付原文线索、固定邮件、提交后中断恢复唯一性，以及链接/核对响应/来源损坏后停止。review_status=unverified 的文档必须无主新闻且有待核实来源；原始模型草稿仍与调用绑定，恢复校验相同来源哈希、拒绝结果和固定链接。test_phase_three.py 的修订上限停止场景使用损坏引文，继续验证硬错误不能转成待核实交付。真实模型固定材料的两次拒绝用例另行显式执行，不表示真实新闻质量已通过人工审阅。

## 三条演示轨迹

2026-10-20 前端改版额外验收37项Node测试与133项相关Python页面/身份/产品/交付回归。浏览器通过登录退出、资料与偏好、历史分页/直达/刷新/返回、生成提示与阅读保护、折叠引用和内容转义；320、375、1280像素布局含长标题与展开长引文检查。独立替身验证成功生成与分页保持，真实服务本轮返回model_incomplete_response，前端正确保留旧内容并允许重新生成；该后端失败不记为真实新闻生成通过。详细证据见acceptance.md本轮记录。

### 反馈驱动补查

test_phase_two.py 中的开源模型场景，从近期历史与空 RSS 开始，记录话题缺口后执行搜索。第一次抓取返回 FETCH_FORBIDDEN，再选另一个已发现候选抓取。关键词定位先未命中，再换英文词并用 read_file 读取上下文，write_file 保存草稿、更新进度后提交。实测预算：9 次模型请求、2 次发现、2 次抓取、12 次工具调用；芯片偏好使用同组材料，选择芯片互联条目，8/2/1/11 次。这里的最终提交为测试替身，只用于展示研究反馈与偏好；正式质量提交由下一条轨迹验证。

### 摘要修订

test_phase_three.py 的 revision-number/conditions/range 分别加入无依据数字、遗漏测试条件和错误引用位置。第一次提交得到 BRIEF_REVIEW_REJECTED 或 BRIEF_RULES_REJECTED；Agent 读取原位置附近的正文，再提交包含原条件的修订稿。正式提交检查通过后写入简报、交付历史和邮件待发送记录，三者数量均为一。提交后的同批次额外工具收到 RUN_FINISHED，调用消息配对完整。

### 中断恢复

同一修订用例在第二次模型响应保存后中断，调用保持 pending。重开数据库后直接恢复该响应，保持原 run、窗口、截止时间、来源哈希与引用位置，结果只保存一份。committed-queue-process-exit 场景在事务提交后令真实子进程以 37 退出，重启后简报/历史/队列仍各一条，未额外请求模型。邮件接受后丢失确认的用例转 unknown，需要页面明确确认后才允许新发送周期。

运行限制同样可复现：修订次数耗尽、鉴权失败、核对服务耗尽、任务截止、模型预算及材料变化均保存具体停止原因，交付数量为零。对应 report.json 保留 before/after 与时间线，可通过 run_id 对照数据库中的持久记录。

## 2026-10-20 待核实简介与邮箱操作

test_verification_display.py 验证待核实简介/话题保存、邮件显式标注、排除词/敏感内容/订阅话题限制，以及旧记录读取补充简介且不改写原文。test_unverified_fallback.py 验证最终拒绝保留简介、旧版提交恢复仍唯一且不更改旧邮件。test_manual_mail.py 通过本地 TCP SMTP 验证预览显式发送、当前账户收件人、并发唯一入队、收件人固定、重复请求不重复投递、CSRF/归属/配置/暂停检查，以及 unknown 确认不可绕过。前端状态测试验证发送目标、空请求体、状态轮询、不可用提示和切换账户后的迟到响应。每日时间继续使用既有调度验收。

追加用例验证收件名单限制在 GET 可用性和 POST 入队前反馈，浏览器禁用按钮并说明原因，不泄露服务端允许地址。

## 2026-10-21 逐条核对回归

test_review_policy.py 覆盖逐条决定完整性、阻断问题一致性、厂商归因说明、混合交付、仅保留主新闻的事实历史、共享候选、全局问题、既有待核实原因、确定历史重复不可被模型覆盖、完整引文唯一定位，以及恢复不重复请求/交付。篡改逐条决定、说明、简介、来源时停止恢复；旧协议1保存的批准/拒绝响应继续恢复，不增加模型请求。test_briefs.py 验证原调用不变、引用纠正留痕、伪造引文与来源损坏仍拒绝；test_phase_three.py 的修订场景使用内容损坏的引文，保留重读、修订与恢复演示。

参数反馈补测嵌套对象定位且不回显未知键或敏感值；重定向后的待核实正文节选仍校验候选和任务归属。前端状态测试检查 partial 栏目、可见说明、待核实简介与计数；邮件测试检查同一混合文档和固定链接。真实容器、恢复和全量结果见 acceptance.md。
