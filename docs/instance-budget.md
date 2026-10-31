# 单实例 CU 预算与守护

预算属于**一次 Colab 分配**，由 `session` 和平台 `assignment_id`（官方 CLI 的 endpoint）绑定。新分配需要重新设置预算；重连、TUI 重启、任务重试不会重新开始计时。它不是跨视频项目台账，也不是 Colab 原生硬配额。

## 创建前设置

向导的新建摘要提供 **Instance CU budget** 设置页。填写本次 CU 上限和保守的 CU/hour 费率上界；未填写预算的旧流程明确显示未受预算保护。预算开启但费率未知时不会创建实例。费率需要按实际账号和硬件核实，启动器不内置未经验证的 GPU 价格表。

CLI 对应操作（变量需由调用者填写；不会因为执行文档而自动分配）：

```bash
python3 scripts/colabctl.py -s "$SESSION" --request-id "$ALLOCATION_REQUEST" new \
  --gpu T4 --cu-limit "$CU_LIMIT" --rate-ceiling "$CU_RATE_CEILING"
python3 scripts/colabctl.py -s "$SESSION" budget
```

CPU 使用 `--cpu` 替代 `--gpu`，仍需明确预算和费率上界。不要把账号当前空闲时的零费率当成下一台 GPU 的费率。

预算守护在分配请求前作为独立本机进程启动，确认 PID 启动标识、独占锁和心跳后才允许分配。分配请求意图先落盘；请求超时后只核查原会话，绝不自动再次 `new`。不能确认属于本工具的 orphan 分配不会被猜测接管或释放。

`new` 返回 `allocation_pending` 或 `allocation_unknown`、`ok: false` 和退出码 1 时，**仍是非终态，分配可能继续进行**，不能据此断言失败或重复 `new`。例如守护正在核查并持有协调锁时，客户端可能先返回 pending，平台稍后才显示实例。保存原响应的 `request_id` 和预算 `job_id`，在同一 session 上只读轮询：

```bash
python3 scripts/colabctl.py -s "$SESSION" budget
```

每次先解析 JSON，并核对两项 ID 与原请求一致；该查询在 pending/unknown 时也可能退出 1，测试 driver 不能仅按非零退出码终止恢复。普通 `budget` 读取当前 session 的本地持久记录，由独立守护继续查询平台。它没有按预算 ID 选择记录的参数；`job ID` 查询的是另一套远端任务，不能用预算 `job_id` 替代。若 ID 已变化，先核查原请求，不把新记录当成恢复成功。只有原请求进入允许工作的状态并确认实例身份后才继续部署；在原预算保护点内等待，不重置或延长预算。

若守护失活，`budget --refresh` 是显式恢复与核查操作，**不是只读轮询**；它可能继续已到期的释放流程。pending/unknown 持续到预算保护点或测试观察窗口结束时，应核查官方实例列表并按已有授权处理清理；没有实例 ID 时不得猜测停止对象。客户端观察超时既不证明分配失败，也不证明远端已经释放。

失败记录保留固定安全分类 `allocation_error_code` 和可用的 CLI 进程退出码 `allocation_error_returncode`（未知为 `null`）；`allocation_error` 仍为通用安全说明。不保存提供方原始 stdout/stderr。`command_failed` 或进程退出码不是 HTTP 状态，不能据此推断上游返回了某个具体错误。

官方 CLI 0.7.4 提供服务器分配列表与**账号总余额、账号总每小时消耗率**，没有每实例累计 CU 或可靠的原始分配时间。创建流程从分配请求发出前的时间作保守起点，包含可能的分配等待；成功分配后安装、下载、生成和空闲全部连续累计。明确接管已有实例时，调用者必须提供原始分配时间或更早的保守时间，不能填当前重连时间：

```bash
python3 scripts/colabctl.py -s "$SESSION" adopt \
  --assignment-id "$ASSIGNMENT_ID" --allocated-at "$ORIGINAL_UNIX_TIME" \
  --cu-limit "$CU_LIMIT" --rate-ceiling "$CU_RATE_CEILING" --confirm
```

`adopt` 只在平台确认准确身份后建立预算，已有预算不会被接管操作重置。

## 实测与估算

状态里的 `observed_account` 是实际账号查询快照，标有 `scope: account`、查询时间和 stale 状态。它包含其他实例，不能把余额变化算作当前实例的实际消费。`estimate` 是本实例经过时间乘以保守费率的估算，`actual_instance_cu` 为 `null`。账号总费率高于当前保守值时只会缩短保护时间；低费率、充值、重连均不会延长预算。

只需设置 CU，不必再指定总运行时长。默认保留 `max(预算的10%, 保守费率对应的4分钟CU)`，其中一半是停止接收工作后的输出收集窗口，另一半用于提前请求释放。可通过 `--reserve-cu` 调整，预留必须小于预算；大输出应自行增大预留。

- `work_deadline`：停止接收新的受管安装、模型下载、准备、渲染、上传等工作。相应后台任务期限会被剩余时间裁剪。
- `release_deadline`：收集窗口结束，守护请求释放整台实例，不等待生成结束。
- `estimated_exhaustion`：按保守费率计算的预算耗尽时间，仅作估算，释放提前于它进行。

原生 ComfyUI 画布仍然可用。它不会经过 CLI 的任务准入检查，已有生成也可能在收集窗口继续运行；整实例计时和释放截止仍生效。收集窗口不保证生成已经完成，也不保证任意大小的视频能下载完。需要持久保存时使用已有 Drive 模式。

## 保留、重连与恢复

```bash
# 最多再保留十分钟；实际截止自动钳制到预算工作保护点。
python3 scripts/colabctl.py -s "$SESSION" hold --seconds 600
python3 scripts/colabctl.py -s "$SESSION" status
# 显式恢复已有守护并核查；已到期时会继续释放流程，不会重置预算。
python3 scripts/colabctl.py -s "$SESSION" budget --refresh
python3 scripts/colabctl.py -s "$SESSION" release
```

`hold` 是带截止的保留，不会暂停 CU 累计。TUI 的 Exit 只退出界面，守护继续执行既定保护；选择保留也不能超过预算保护点。守护意外退出时状态显示 `guard.running: false`，下一次受管任务准入会先恢复守护并核查旧账，恢复失败则拒绝启动工作。`status`/`budget` 本身用于检查，不假称失活守护仍在保护。

本机状态默认位于 `~/.local/state/colab-comfyui-launcher/budgets`，支持 `XDG_STATE_HOME`；测试或隔离部署可指定 `COLAB_LAUNCHER_STATE_DIR`。目录和记录为私有权限，不在 Git 中。计时保存墙钟、启动标识与包含休眠时间的本机时钟；同机回拨不会把已累计时间减掉。重启后无法可靠解释的回拨会进入保守释放策略。

查询失败时显式记录 `query_failed`/`release_unknown`，估算继续累计，禁止新的受管工作。账号用量暂不可查询时保留旧快照并标 stale，已有明确保守费率继续有效。守护保留释放意图并重复核查；恢复网络后先确认原分配，再决定是否重试释放。

短暂的实例列表查询失败允许最多 10 秒恢复，且不超过原工作或释放截止；连续失败不会刷新这段窗口。期间禁止提交新工作，正常守护可提前重查同一实例。失败记录只保留错误类别、退出码等脱敏字段，不保存提供方原始输出。窗口耗尽后进入释放流程，释放仍须核验原实例身份。

## 释放证明与边界

`Session terminated.` 和命令退出码为零都不足以证明释放。成功必须由**同一认证上下文中的完整平台分配列表**确认原 `assignment_id` 已消失，结果为 `status: released, verified: true`。列表中的 `?` orphan 也参与核验；按原分配 ID 核验，同名新实例不会被停止，原 ID 确已消失则可确认原实例释放。失败、超时或无法核验时保存待恢复状态，而不是报告成功。显式 `release` 的未完成身份记录也会保留，重试先核原分配，不因当前名字指向另一台实例而改释放对象。

工具仅释放它创建并绑定的实例，或用户明确接管/明确选择释放的实例。官方 CLI 只能按会话名停止，外部工具在身份复核与 `stop` 之间并发改写同名映射仍无法做到平台原子操作；请勿同时用另一个 CLI 重建/重绑该会话名。

CLI 不提供账号 ID，因此启动器只对 OAuth 缓存做文件元数据变化检测，不读取内容。当前账号存在未释放受管实例时，TUI 阻止替换其登录或退出账号；选择另一个已保存账号可以进行，原守护仍固定使用原账号及其独立缓存。命名账号的预算、请求和 SSH 状态隔离，详见[多账号说明](accounts.md)。外部替换某配置的登录、删除缓存或刷新导致上下文不明时，列表缺失不会轻率当成释放证明；需要恢复原账号并核验。元数据变化检测是保守保护，不能替代提供方账号身份接口。

正常刷新也可能改变文件元数据。只有在原 profile 下重新获得稳定、完整的平台列表，并确认原 `assignment_id` 与 session 仍匹配，才更新该元数据指纹；不会仅因文件变化判定账号更换。同名替代实例、session 改绑或变化后的空列表均不能作为原归属的重新验证，也不会重置预算。

**控制端断网、关机、休眠、被平台回收、守护被杀死或认证失效时，无法保证按时远端释放。** 本进程不是远端租约；机器重启不会自动安装系统服务。应在可靠在线的控制端运行，检查守护心跳，并保留通过官方 Colab 界面核查/结束原实例的能力。速率变化、提供方延迟、输出传输与释放失败都可能造成实际消费超出估算上限；启动器只提供尽力保护。
