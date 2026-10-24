# CLI、任务与素材接口

`colabctl.py` 默认向 stdout 输出一个 JSON 对象；`--json` 可显式声明同一模式。参数错误和命令错误也输出结构化错误，诊断及升级提醒放 stderr。`--help` 仍输出普通帮助文字。提供方的原始授权输出不会透传到 JSON。

命名账号使用全局 `--account account-b`，例如 `python3 scripts/colabctl.py --account account-b -s "$SESSION" status`。账号需先在 TUI 中添加并登录，命令始终操作明确指定的账号；省略时使用旧的 `default`，不会跟随界面最近选择。实例的所有后续操作须保留同一账号。自动候补只在 dashboard 新建前执行，低层 CLI 不跨账号重试未知分配。见[多账号说明](accounts.md)。

## 持久请求

写操作在本地和远端保存 `request_id`/`job_id`。CLI 自动生成 ID，也接受调用者提供的稳定 `--request-id`；同一 ID 不得用于不同参数。分配预算另有独立的分配请求与预算 job 记录。

`new` 的 `allocation_pending` / `allocation_unknown` 即使退出 1 也仍为非终态，分配可能继续。保存原分配的 `request_id` 和预算 `job_id`，使用 `colabctl.py -s "$SESSION" budget` 只读轮询并核对 ID，不依据退出码重复 `new`。预算记录不属于下面的 `job ID` 接口；`budget --refresh` 会恢复守护且可能触发到期释放，不能当作只读轮询。详见[分配恢复说明](instance-budget.md#创建前设置)。

```bash
python3 scripts/colabctl.py -s "$SESSION" --request-id prepare-001 prepare \
  --ephemeral --mode download_missing
python3 scripts/colabctl.py -s "$SESSION" job prepare-001
python3 scripts/colabctl.py -s "$SESSION" jobs
python3 scripts/colabctl.py -s "$SESSION" status
```

本地记录只保存参数摘要、身份和结果，不保存凭证或完整操作参数。远端通用 job 记录位于 `/content/colab-comfyui-runtime/jobs`，渲染提交 journal 位于 `render-jobs`。全局 `render-result.json` 等兼容状态仍可用，新任务不会覆盖旧 request 的独立记录。

`started`/`starting` 只表示已接受；需查询最终状态。进程退出、断网或收到无法解释的响应时使用 `submission_unknown` 等状态。**相同 ID 的重试只查询原请求，不重新执行写操作。** 即使查询暂时查不到，也不能把它当成 POST 未执行。重新发一个 ID 是明确的新任务，应先核清原任务。第一次响应丢失且调用者未保存自动 ID 时，用 `jobs` 找回本地记录。

渲染 POST 前会先持久化图摘要、预生成 prompt ID 和提交意图，附带请求元数据。恢复会查询 ComfyUI queue/history；客户端预设 prompt ID 本身并不被假定具有服务端去重能力。`job` 对已失去 runner 的渲染可主动执行只读 reconciliation，始终不 POST。历史被清除、服务重启导致无法确认时仍保留未知状态。已完成且本地保存的结果可在实例释放后查询；尚未取回的 VM 文件在释放后不能靠记录恢复。

任务提交失败保留结构化 `server_error`/`node_errors` 信息，诊断中的常见凭证形式会脱敏。服务端节点 schema 是最终权威；泛型 `COMFY_MATCHTYPE_V3`/Switch 不会被本地不完整的类型知识改写成固定分支。当前主线原本已有泛型放行行为，本轮增加了针对真实官方 schema 的回归覆盖。

ComfyUI 可能接受部分有效输出，同时返回节点诊断。有有效 `prompt_id` 的响应仍按已入队任务跟踪，脱敏诊断保存为 `submission_warnings`；最终结果须由 history 与输出核验确认。后续查询断线不丢弃 ID，也不把任务标为终态或重新 POST。无有效 ID 且只有 `node_errors` 时保持 `submission_unknown`；无有效 ID 加明确 `error` 或 HTTP 400 验证拒绝仍按终态拒绝处理。

## 准备模型

`prepare --mode verify_only` 只使用已有、可校验的模型；在 Drive 模式下仍会复制并验证到 VM 磁盘，不下载缺失权重。`prepare --mode download_missing` 额外下载清单中缺失的已固定文件。`--download-missing` 作为兼容别名保留，但不能和显式 `verify_only` 并用。

TUI 完整启动和 Prepare / refresh 原本就传入 `download_missing=True`；现在把模式显式传给 CLI，语义一致。receipt 复用、新 SHA 校验和强制 `--verify-cache` 的已有区别保持。模型始终从 VM 磁盘加载。

## 输入上传与输出收集

先部署、准备并启动服务，使存储配置已被验证，然后上传输入：

```bash
python3 scripts/colabctl.py -s "$SESSION" upload ./input.png
```

返回 `filename`/`relative_path`/`comfyui_name`、`sha256`、`bytes`、`session` 和 `assignment_id`。输入根目录中的名称包含完整实例摘要和内容 SHA，同名不同内容不会互相覆盖，原生 LoadImage 下拉菜单也能看到上传结果。CLI 传输先使用本地稳定快照，远端校验完成后才发布为可用输入。

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
