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
| 1：中文、最多五条、时间与固定引用 | test_briefs.py、test_review.py、test_fetch.py | 条数与时间拒绝；字符位置/摘录/正文哈希一致；数字、比较条件与中文检查 |
| 2：结果/零结果、抓取成败、定位未命中调整 | test_phase_two.py、test_research.py、test_feeds.py、test_fetch.py | 同一候选结构；空清单后搜索，403 后换来源，中文未命中后英文命中并读回 |
| 3：话题缺口与补查 | test_research.py、test_phase_two.py | collected/missing/issues 持久化；后续查询、选择与缺口一致 |
| 4：摘要修订与事件去重 | test_review.py、test_phase_three.py、test_research.py | 数字/条件/范围问题反馈，一次修订；同ID/明确版本/完整事实集合一致先拒绝，其余由模型核对；同事件合并、新版本检查 |
| 5：响应、工具、入队、SMTP 接受后中断 | test_recovery.py、test_phase_three.py、test_recovery_docker_live.py、test_mail.py | 同 run/窗口/快照/预算恢复，简报和入队唯一；不确定邮件停止自动重发 |
| 6：凭据与权限/诱导/预算 | test_tool_execution.py、test_files.py、test_public_http.py、test_harness.py、test_auth.py | 目录、SSRF、用户隔离、网页诱导、限流、上下文与预算边界；真实 Bash 无网络/凭据/socket |
| 7：登录至预览/历史/调度邮件 | test_product_api.py、test_schedules.py、test_mail.py、test_compose_live.py | API 闭环、真实本地 TCP SMTP、定时日唯一、页面已审计；真实模型与真实邮箱另见 5.4 |
| 8：偏好与定位统计 | test_phase_two.py、test_research.py、test_files.py、test_review.py | 相同材料不同偏好产生不同查询/理由；排除词和未覆盖说明；定位后读取条件与固定正文一致 |

替身回归验证状态与执行规则。真实模型自主行为、真实来源摘要质量、指定邮箱实际收件及至少五份人工核对，依照实施计划另记，不由这个入口宣告通过。

RSS 为默认关闭的可选补充：tests/test_app.py、test_feeds.py 验证关闭时只开放搜索/正文等工具，启用时按可信来源清单开放 read_feed；test_briefs.py 验证有效搜索零候选时可选 RSS 失败不阻断空简报。test_review.py 验证来源时间状态进入独立核对输入，以及原预算内预留首次提交、核对、修订和再次核对。

## 三条演示轨迹

### 反馈驱动补查

test_phase_two.py 中的开源模型场景，从近期历史与空 RSS 开始，记录话题缺口后执行搜索。第一次抓取返回 FETCH_FORBIDDEN，再选另一个已发现候选抓取。关键词定位先未命中，再换英文词并用 read_file 读取上下文，write_file 保存草稿、更新进度后提交。实测预算：9 次模型请求、2 次发现、2 次抓取、12 次工具调用；芯片偏好使用同组材料，选择芯片互联条目，8/2/1/11 次。这里的最终提交为测试替身，只用于展示研究反馈与偏好；正式质量提交由下一条轨迹验证。

### 摘要修订

test_phase_three.py 的 revision-number/conditions/range 分别加入无依据数字、遗漏测试条件和错误引用位置。第一次提交得到 BRIEF_REVIEW_REJECTED 或 BRIEF_RULES_REJECTED；Agent 读取原位置附近的正文，再提交包含原条件的修订稿。正式提交检查通过后写入简报、交付历史和邮件待发送记录，三者数量均为一。提交后的同批次额外工具收到 RUN_FINISHED，调用消息配对完整。

### 中断恢复

同一修订用例在第二次模型响应保存后中断，调用保持 pending。重开数据库后直接恢复该响应，保持原 run、窗口、截止时间、来源哈希与引用位置，结果只保存一份。committed-queue-process-exit 场景在事务提交后令真实子进程以 37 退出，重启后简报/历史/队列仍各一条，未额外请求模型。邮件接受后丢失确认的用例转 unknown，需要页面明确确认后才允许新发送周期。

运行限制同样可复现：修订次数耗尽、鉴权失败、核对服务耗尽、任务截止、模型预算及材料变化均保存具体停止原因，交付数量为零。对应 report.json 保留 before/after 与时间线，可通过 run_id 对照数据库中的持久记录。
