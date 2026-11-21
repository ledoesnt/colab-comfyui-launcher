# CLI、任务与素材接口

`colabctl.py` 默认向 stdout 输出一个 JSON 对象；`--json` 可显式声明同一模式。参数错误和命令错误也输出结构化错误，诊断及升级提醒放 stderr。`--help` 仍输出普通帮助文字。提供方的原始授权输出不会透传到 JSON。

命名账号使用全局 `--account account-b`，例如 `python3 scripts/colabctl.py --account account-b -s "$SESSION" status`。账号需先在 TUI 中添加并登录，命令始终操作明确指定的账号；省略时使用最初的账号（旧版 `default` 或其迁移后的名称），不会跟随界面最近选择。实例的所有后续操作须保留同一账号。自动候补只在 dashboard 新建前执行，低层 CLI 不跨账号重试未知分配。见[多账号说明](accounts.md)。

## 持久请求

写操作在本地和远端保存 `request_id`/`job_id`。CLI 自动生成 ID，也接受调用者提供的稳定 `--request-id`；同一 ID 不得用于不同参数。分配预算另有独立的分配请求与预算 job 记录。

`new` 在预检（列出账号会话、核对认证范围）阶段失败时返回 `status: not_submitted`、`allocation_submitted: false`、`create_requests: 0`，此时没有创建实例也没有写入预算记录；`resubmit_allowed: true` 表示可用同一 request ID 重试（同名会话已存在时为 `false`）。预检列表本身可能刷新 OAuth token、改变凭据文件元数据，范围变化时会自动重查一次。`new` 的 `allocation_pending` / `allocation_unknown` 即使退出 1 也仍为非终态，分配可能继续。保存原分配的 `request_id` 和预算 `job_id`，使用 `colabctl.py -s "$SESSION" budget` 只读轮询并核对 ID，不依据退出码重复 `new`。预算记录不属于下面的 `job ID` 接口；`budget --refresh` 会恢复守护且可能触发到期释放，不能当作只读轮询。详见[分配恢复说明](instance-budget.md#创建前设置)。

```bash
python3 scripts/colabctl.py -s "$SESSION" --request-id prepare-001 prepare \
  --ephemeral --mode download_missing
python3 scripts/colabctl.py -s "$SESSION" job prepare-001
python3 scripts/colabctl.py -s "$SESSION" jobs
python3 scripts/colabctl.py -s "$SESSION" status
```

本地记录只保存参数摘要、身份和结果，不保存凭证或完整操作参数。远端通用 job 记录位于 `/content/colab-comfyui-runtime/jobs`，渲染提交 journal 位于 `render-jobs`。全局 `render-result.json` 等兼容状态仍可用，新任务不会覆盖旧 request 的独立记录。

`started`/`starting` 只表示已接受；需查询最终状态。进程退出、断网或收到无法解释的响应时使用 `submission_unknown` 等状态。**相同 ID 的重试只查询原请求，不重新执行写操作。** 即使查询暂时查不到，也不能把它当成 POST 未执行。重新发一个 ID 是明确的新任务，应先核清原任务。第一次响应丢失且调用者未保存自动 ID 时，用 `jobs` 找回本地记录。

提交时先取得请求回执锁，再进入提交与释放共用的短互斥区。等待回执不会阻塞释放；释放意图已提交后，不再发送新的工作。锁等待、归属核验、部署上传和 CLI 传输共用同一个单调时钟截止，最多为全局 `--timeout` 与提交窗口中较小值（`deploy`、`start` 为 120 秒，其余写操作 90 秒，见下文冷启动说明），并受预算保护点裁剪。远端工作的 `not_after` 在等待前固定，发送时重新计算剩余量，等待不会延长任务期限。长期推理不持有提交互斥区。

`receipt_wait_expired` 表示本调用没有发送，但其他调用可能已持有该回执并提交；`query_unknown` 表示原请求仍无法查清。这两种状态均不是终态，返回 `this_call_dispatched: false`、`resubmit_allowed: false`，应保留原 ID 查询。`not_submitted` 表示本调用在发送前被拒绝。不能仅凭退出 1、等待超时或本调用未发送，推断原请求从未执行。

渲染 POST 前会先持久化图摘要、预生成 prompt ID 和提交意图，附带请求元数据。恢复会查询 ComfyUI queue/history；客户端预设 prompt ID 本身并不被假定具有服务端去重能力。`job` 对已失去 runner 的渲染可主动执行只读 reconciliation，始终不 POST。历史被清除、服务重启导致无法确认时仍保留未知状态。已完成且本地保存的结果可在实例释放后查询；尚未取回的 VM 文件在释放后不能靠记录恢复。

任务提交失败保留结构化 `server_error`/`node_errors` 信息，诊断中的常见凭证形式会脱敏。服务端节点 schema 是最终权威；泛型 `COMFY_MATCHTYPE_V3`/Switch 不会被本地不完整的类型知识改写成固定分支。当前主线原本已有泛型放行行为，本轮增加了针对真实官方 schema 的回归覆盖。

ComfyUI 可能接受部分有效输出，同时返回节点诊断。有有效 `prompt_id` 的响应仍按已入队任务跟踪，脱敏诊断保存为 `submission_warnings`；最终结果须由 history 与输出核验确认。后续查询断线不丢弃 ID，也不把任务标为终态或重新 POST。无有效 ID 且只有 `node_errors` 时保持 `submission_unknown`；无有效 ID 加明确 `error` 或 HTTP 400 验证拒绝仍按终态拒绝处理。

## Provider 失败分类

命名账号（当前安装的标准形态）通过 `scripts/account_cli.py` 在官方 CLI 自己的解释器里运行固定的 `google-colab-cli==0.7.4`。它不改官方凭据流程，只在提供方边界增加三件事：每个 Colab REST 与运行时文件请求都有超时；没有终端时，缺少登录直接返回 `auth_required`，不再打印 OAuth 网址并等待永远不会到来的 code；失败时在 stderr 末尾写一行 `LAUNCHER_PROVIDER_ERROR=` 分类结果，不再输出可能带局部变量的原始 traceback。说明文字全部由 `scripts/provider_errors.py` 按分类生成，不转发提供方响应、URL、token 或 body。

失败的 JSON 结果包含 `provider_error`：

| 字段 | 含义 |
| --- | --- |
| `code` | 稳定分类，见下表。 |
| `outcome` | `not_created`：确认没有创建分配；`not_dispatched`：启动器代码没有送达内核；`unknown`：可能已经执行；`failed`：已执行并失败；`none`：只读操作，无副作用。 |
| `dispatched` | 仅 `exec`：代码是否已真正发送给内核。只有 vendored exec 明确记录的 `false` 才算未发送；CLI 进程被杀、崩溃或旧 `default` 账号没有 shim 时为 `null`，按 `unknown` 处理。 |
| `phase`、`endpoint`、`http_status` | 失败阶段、端点类别（`assign`、`assignments`、`unassign`、`ccu_info`、`kernel`、`contents`、`oauth`）与 HTTP 状态。 |
| `retryable`、`message`、`next` | 是否值得重试、完整说明、建议的下一步。 |

| `code` | 典型场景 | `outcome` | 下一步 |
| --- | --- | --- | --- |
| `capacity_unavailable` | 分配 POST 返回 503，所选硬件暂时无容量 | `not_created` | 稍后重试或换硬件 |
| `allocation_refused` | 412：活动会话过多或临时用量/容量限制 | `not_created` | 释放闲置实例、等待或换硬件 |
| `accelerator_not_entitled` | 400：账号无该 GPU 配额或资格 | `not_created` | 换 GPU 或账号，同参数重试仍会失败 |
| `auth_required` | 登录缺失、过期或被撤销 | 无副作用 | 交互登录后重试 |
| `auth_refresh_failed` | 刷新 token 时网络失败 | 无副作用 | 检查网络；登录未核实，不等于已过期 |
| `session_not_found` / `runtime_gone` | 本机 CLI 不认识该 session / 服务器已回收该 VM | `not_dispatched` | 视旧 VM 及本地文件为不可用 |
| `kernel_connect_failed` / `kernel_busy` / `predispatch_timeout` | 内核连接失败 / 内核被其他执行占用（如 Drive 授权、之前超时的调用）/ 发送前期限已用尽 | `not_dispatched` | 可安全重试 |
| `execution_timeout` / `connection_lost` | 已发送，但截止前未完成 / 连接断开 | `unknown` | 只查询原请求，不重发 |
| `remote_exception` | 启动器代码在 VM 上抛出异常（字段 `remote_exception` 为异常类名） | `failed` | 读 stderr 尾部，修复后再提交 |
| `local_timeout` | 本机 CLI 进程在调用者期限内未报告结果 | `unknown` | 先查询状态 |
| `command_failed` | CLI 退出但没有分类证据（例如发送后进程被杀） | `exec`/`new` 为 `unknown` | 先查询状态，不重发 |
| `allocation_lookup_empty` | 按原分配键查找时 Colab 还没有该分配（只读查找，不会创建） | `none` | 保持待确认，继续轮询 `budget` |

`colabctl` 把 `not_dispatched` / `not_created` 映射为 `status: not_submitted`、`terminal: true`、`resubmit_allowed: true`，用新 request ID 重新提交是安全的；`unknown` 仍为 `submission_unknown`，同 ID 只查询；`failed` 为终态失败。重试只在 shim 证明未发送且原因可能是暂时的（`kernel_connect_failed`、`kernel_busy`、`network_error`、`http_timeout`、`provider_server_error`、`auth_refresh_failed`）时自动进行，最多 3 次并受同一截止限制；结果带 `attempts` 和每次的 `attempt_codes`。已发送的请求从不自动重发。

分配失败时，若 HTTP 拒绝（503、412、400 等）证明没有创建，且随后平台列表核实没有新的同名或未命名（`?`）分配，预算记录进入终态 `allocation_failed`，带 `error`、`next`、`retryable` 和 `allocation_diagnostic`；可以用新 request ID 再次 `new`。只要出现无法解释的新未命名分配、拒绝无法证明、或列表查询失败，仍保持非终态 `allocation_unknown`，不重复 `new`。

### 远端已接受、回复丢失

两类写操作都可能在远端已经接受后丢失回复。处理原则相同：同一身份只查询，不重发；只有能证明请求没有发出时才允许用新 ID 重试。

**分配。** 官方 `new` 每次用随机 notebook hash 请求 `/tun/m/assign`，回复丢失后本机 CLI 没有保存会话名，平台列表只显示无名 `?` 分配，预算记录永远停在 `allocation_unknown`，无法绑定、保护或释放该实例。现在预算记录在发送前落盘 `create_requests: 1` 和由预算 `job_id` 派生的分配键，`new` 用它作为 notebook hash。创建回复丢失（`outcome: unknown`）且创建进程已返回（或超过 330 秒）后，守护以只读模式查找：shim 运行官方 `new` 但拒绝发送 `POST /tun/m/assign`，`GET` 对同一键返回已有分配时由官方代码保存会话名，守护再按平台列表核对该 `assignment_id` 后绑定，结果带 `allocation_recovered_by: allocation_key_lookup`，`request_id`、`job_id` 不变。查不到时 `allocation_lookup.result: absent`，记录保持非终态并给出 `next`，不会转为失败或重新创建；之后原分配出现仍会绑定。同一 request ID 的 `new` 只回放并触发查找。仅命名账号支持；旧 `default` 账号仍按列表核对。

**任务。** `exec` 发出后连接中断、超时或本机 CLI 被杀时，结果为 `submission_unknown`、`this_call_dispatched: true` 或 `null`、`resubmit_allowed: false`。用相同 `--request-id` 重跑原命令或 `job <request-id>` 只发送查询；远端请求记录在执行前落盘，同一 ID 不会再次分派。渲染查询返回原 `prompt_id`、状态和成品，并附 `execution_evidence`：ComfyUI 队列与保留历史中带该 request ID 的任务数（`comfyui_jobs`）、`prompt_ids` 和 `single_execution`（恰好一个且等于记录的 `prompt_id`）。历史可能被清理或在 ComfyUI 重启后丢失，所以 0 不证明未执行；多于 1 会直接报告。成品用 `collect --job <request-id>` 取回。远端还没有请求记录时返回 `not_found`，同样是 `terminal: false`、`resubmit_allowed: false`。

### vendored exec

官方 0.7.4 `exec` 的 `--timeout` 只限制最后的 reply 等待：内核忙时 iopub 循环以零超时空转，命令既不结束也不报错，只能被外层进程期限杀死，留下无法判断的 "Local Colab command timed out"。shim 对启动器自己的固定参数形状（`exec -s S -f FILE --timeout N`）使用复制自官方 `commands/execution.py` 的执行路径，并做了以下改动：

- 调用者期限通过 `--launcher-deadline` 传入；连接、状态查询和执行都受它限制，并在外层终止前约 3 秒写出分类结果。
- 连接内核后先确认 websocket 就绪。
- 发送前查询内核 `execution_state`，忙碌或启动中最多等待 30 秒，仍忙返回 `kernel_busy`（未发送）。一旦观察到 busy，后续状态查询失败也不会被当成空闲。
- 只有真正发送 `execute_request` 后才标记 `dispatched: true`；未发送前的失败都可安全重试。
- 前置 `chdir` 与程序在同一次执行中；历史记录只保存程序 SHA256，不保存程序内容。

其他参数形状与交互命令（登录、`drivemount`、`ssh`）仍走官方路径。未迁移的旧 `default` 账号直接运行官方可执行文件，没有 shim，只能按官方固定提示做有限分类，其余为 `command_failed`。

### 冷启动与重复启动

新分配 VM 的第一次运行时请求或 websocket 连接实测可达约 40 秒（经本机代理时更明显）。`deploy` 是新 VM 的第一次接触（上传加 exec），与 `start` 一样使用 120 秒提交窗口。其他写操作也要新建内核连接：经本机代理实测，身份列表约 9 秒、websocket 5–20 秒，程序输出在发送后 10–21 秒才到达本机，45 秒窗口曾让一次已被远端接受的 `install` 变成 `submission_unknown`，因此改为 90 秒。`new` 在分配成功后最多等待 30 秒让守护正在进行的协调结束，再返回最新状态，减少成功分配却返回 `allocation_pending`/退出 1 的情况；仍返回 pending 时按上文只读轮询 `budget`。

同一 VM 上已有同类任务（install、start、download、prepare、render）在运行时，新请求返回 `ok: false`、`status: not_started`、`running_request_id`、`terminal: true`、`resubmit_allowed: true`。旧行为返回 `ok: true / running`，会把新 request ID 绑定到别人的任务，之后 `job` 一直显示 running。等原任务结束后用新 ID 提交。

### status 摘要

`status` 增加 `summary`：`state`（`not_deployed`、`not_installed`、`installing`、`install_failed`、`preparing_models`、`models_missing`、`starting`、`start_failed`、`ready`、`installed`、`budget_blocked`）、`problems`（失败组件及其阶段、错误、脱敏日志尾部）和单一建议动作 `next`。安装失败时 `installation` 保留失败阶段、退出码、行号和 `install.log` 尾部；启动失败保留失败阶段（`comfyui` / `cloudflare`）、错误和对应日志尾部；后台任务未返回 JSON 时保留脱敏 stderr 尾部。`summary` 只由已返回字段推导，不代替任何浏览器或推理验收。

## 准备模型

`prepare --mode verify_only` 只使用已有、可校验的模型；在 Drive 模式下仍会复制并验证到 VM 磁盘，不下载缺失权重。`prepare --mode download_missing` 额外下载清单中缺失的已固定文件。`--download-missing` 作为兼容别名保留，但不能和显式 `verify_only` 并用。

TUI 完整启动和 Prepare / refresh 原本就传入 `download_missing=True`；现在把模式显式传给 CLI，语义一致。receipt 复用、新 SHA 校验和强制 `--verify-cache` 的已有区别保持。模型始终从 VM 磁盘加载。

`prepare --only FILE`（可重复，文件名或清单路径）只准备清单中已启用的指定文件，例如某个工作流实际加载的模型；不修改 `models/*.json`。每个名字必须恰好匹配一个文件，否则在任何下载或复制前失败。成功后在 VM 模型目录记录选择，`start` 的模型检查和 `status.models_ready` 只要求这些文件，`status.models_selection` 显示所选路径（`null` 表示完整清单）；之后不带 `--only` 的完整 prepare 会清除该选择。此前跳过不需要的模型只能临时改动受 Git 跟踪的 `models/h3.json` 再还原，忘记还原会让仓库变脏并阻止启动前更新。

## 输入上传与输出收集

先部署、准备并启动服务，使存储配置已被验证，然后上传输入：

```bash
python3 scripts/colabctl.py -s "$SESSION" upload ./input.png
```

返回 `filename`/`relative_path`/`comfyui_name`、`sha256`、`bytes`、`session` 和 `assignment_id`。输入根目录中的名称包含完整实例摘要和内容 SHA，同名不同内容不会互相覆盖，原生 LoadImage 下拉菜单也能看到上传结果。CLI 传输先使用本地稳定快照，远端校验完成后才发布为可用输入。

一次给多个文件（`upload a.png b.png ref.mp4`）时按一个包上传：一次暂存、一次提交，各文件字节首尾相接后切成不超过 8 MiB 的分片，每片是一次完整文件写入，失败的分片单独重发（最多 3 次，重发是覆盖而非追加）。提交时按清单长度切回各文件，**全部** SHA 与字节数一致才发布，任一不符或分片缺失、多出字节都不发布任何文件；不解析归档格式，没有路径逃逸面。结果为 `assets` 列表（每项字段同单文件上传）、`parts` 和每片的 `part_attempts`。单文件上传每次要两次远端程序和多次实例身份查询，经本机代理实测每个小文件约 1.5 分钟（6 个文件 616 秒）；打包后这些固定开销只付一次。

全局 `--timeout` 放在子命令之前，默认 120 秒。对 `upload` / `collect`，它是素材操作的**共享总期限**，覆盖快照/摘要、暂存或描述、传输、校验与提交；`collect` 的全部文件共用同一期限，每个步骤或文件不会获得新的完整窗口。上传还受 CU `work_deadline` 裁剪，收集受 `release_deadline` 裁剪，两者还受已有 `hold_deadline` 限制；增加 `--timeout` 不会延长这些保护点。应按文件大小、调用开销和校验时间设置足够的窗口，短窗口超时不等同于传输实现失败。

初始平台身份与输出 manifest 查询在素材总期限建立之前，因此 `--timeout` 不是整个 CLI 进程严格的墙钟退出上限。其他远端控制调用通常按单次本地调用使用该值；预算分配使用提供方独立期限，后台安装/渲染另由 `--install-timeout` / `--max-seconds` 控制。

生成完成后按 job 或保存的输出 manifest 收集：

```bash
python3 scripts/colabctl.py -s "$SESSION" collect --job render-001 \
  --destination ./local-results
# 或 --manifest ./saved-render-result.json
```

下载目标按实例和 job 分目录，每个文件核对 SHA 和字节数后原子发布，并保存 manifest。传输前后复核实例身份；路径逃逸、符号链接、错误实例、损坏校验值或不完整下载会失败，不报告完整成功。预算收集使用 release 保护点前的剩余时间，上传使用 work 保护点；查询或传输不会重置预算。

统一收集支持通过受管渲染得到的 manifest；原生网页输出也可用明确的同实例输出 manifest 收集，但不会扫描或建立复杂素材数据库。官方 CLI 0.7.4 的 upload/download 内部整文件读入内存并做 base64 传输；此处复用它，尚未验证大型视频的内存占用、Drive FUSE 与真实 Colab 传输性能。

## 自定义 API JSON 与可选参数映射

完整 API JSON 是直接入口，不需要命名参数映射：

```bash
python3 scripts/colabctl.py -s "$SESSION" --request-id render-001 render \
  --workflow-file ./custom-api.json --max-seconds 900 --export-frames
```

可选 `--mapping ./mapping.json` 和可重复的 `--param NAME=JSON` 只供 CLI/agent 使用。映射固定 API 图的规范化 JSON SHA、节点 class/input、参数类型、模板版本和来源版本；图内容改变会拒绝旧映射。原生画布无需创建映射，完整自定义 API JSON 入口也不会因没有映射而受限。

仓库的 `workflows/template-capabilities.json` 列出模式边界：I2V 和独立首尾帧示例对应固定官方节点实际支持的输入；首尾帧示例尚无本轮云端推理验收。R2V 是独立节点/模型能力，当前组合未验证，不提供伪装成可用模板的示例。示例只使用公共模板来源和通用占位输入，不包含私人媒体、Drive 目录、账号或剧情提示词。

视频结果单列 `requested_geometry` 与实际解码的分辨率、FPS、`frame_count`、时长，不把请求帧数当成输出帧数。没有音轨的视频也允许成功。`--export-frames` 额外输出带 SHA 的首尾 PNG；SaveImage 等非视频输出保留文件身份和校验值，不强行套用视频元数据。完整解码和首尾帧导出占用任务校验时间，超时会明确报告未完成核验。
