# 实测记录

以下分轮保留真实执行证据；历史轮次中的“未执行”只描述当时状态，最新验收边界见后续记录。

## 2026-10-04 首轮 G4 / CPU（历史）

### Question / Goal

验证本地官方 Colab CLI 是否能分配 G4、经过人工授权挂载 Drive、启动 ComfyUI 和受保护的
Cloudflare 临时入口，并验证队列、WebSocket、持久化输出与资源清理。另调查 H3 的真实部署方式。
通用 PNG 流程不代替 H3 视频、邮箱 OTP 或长期稳定性的验收。

### Environment

- 本机 Linux，`google-colab-cli==0.7.4`，OAuth2；未使用 SSH。
- Colab G4：NVIDIA RTX PRO 6000 Blackwell Server Edition，97887 MiB，compute capability 12.0。
- VM：Python 3.13.15，PyTorch 2.11.0+cu130，CUDA 13.0，初始 `/content` 可用约 193 GiB。
- ComfyUI commit `f1072eb0350638a3390ddb6afbcaa8c6b237c6fd`。
- cloudflared 2026.9.3，Linux amd64 SHA256
  `77e26d8d900e0b8469f416239d14b5f296525fdf79fee6f511ef55609e3fbac2`，真实下载后校验通过。
- Cloudflare 白名单用 reserved `.invalid` 测试邮箱；没有真实邮箱登录，没有对外提供可公开访问的 ComfyUI。
- 测试资产仅放入专用 Drive 目录；没有遍历私人文件，没有提交模型、媒体、日志或认证材料。

### Steps

1. 分配独立 G4 并执行 GPU/版本检查；设 20 分钟测试清理上限。
2. `drivemount` 请求浏览器授权。等待过程中默认 mount 内部超时，授权后再次挂载成功。
3. 首次 Python venv 创建因 ensurepip 失败；改为 `--without-pip --system-site-packages`。
   `pip check` 发现系统镜像的 IPython 缺 `jedi`，安装固定版本后检查通过。
4. 通过 Contents API 上传代码，短 `exec` 启动受期限限制的独立安装进程；安装完成后启动服务。
5. ComfyUI 只监听 127.0.0.1:8188，自定义节点关闭。服务存活及 `/system_stats` 就绪。
6. 在本地服务上连接 WebSocket、提交 PNG workflow、读取 history 和 `/view`，比较 Drive 文件。
7. 从本机通过临时网址执行未登录 HTTP、POST 和 WebSocket upgrade 请求，验证认证门。
8. 停止启动器服务，再显式停止该 G4。随后新建独立 CPU、重新挂载同一 Drive 并读回前次输出。
   新授权再次触发 mount 内部超时；授权后重试成功，读回 hash 相同，再停止该 CPU。

### Commands

以下按本轮实际操作整理，session 名称、邮箱和资产标识已替换为占位。首次安装修复通过本轮脚本反复部署后验证。

```bash
colab --auth=oauth2 new -s "$G4_SESSION" --gpu G4
colab --auth=oauth2 drivemount -s "$G4_SESSION" /content/drive
python3 scripts/colabctl.py -s "$G4_SESSION" deploy
python3 scripts/colabctl.py -s "$G4_SESSION" install
python3 scripts/colabctl.py -s "$G4_SESSION" status
python3 scripts/colabctl.py -s "$G4_SESSION" start \
  --storage-root /content/drive/MyDrive/colab-comfyui-launcher-test \
  --allowed-email smoke@example.invalid
python3 scripts/colabctl.py -s "$G4_SESSION" status
python3 scripts/colabctl.py -s "$G4_SESSION" smoke
python3 scripts/colabctl.py -s "$G4_SESSION" stop
colab --auth=oauth2 stop -s "$G4_SESSION"
colab --auth=oauth2 new -s "$RECOVERY_SESSION"
colab --auth=oauth2 drivemount -s "$RECOVERY_SESSION" /content/drive
# 本轮执行短 Python probe 读取专用已知 PNG、计算 SHA256；未列 Drive 目录。
colab --auth=oauth2 exec -s "$RECOVERY_SESSION" -f recovery_probe.py --timeout 60
colab --auth=oauth2 stop -s "$RECOVERY_SESSION"
colab --auth=oauth2 sessions
```

远端一般 Python error 有可能仍让官方 CLI exit 0；bridge 要求唯一 `LAUNCHER_RESULT` 且 `ok: true`。
CPU 恢复 probe 首次挂载失败时确实返回 `ok: false`；重挂后返回 `ok: true`，没有忽略失败结果。

### Result

| 检查 | 实测结果 |
| --- | --- |
| G4 与 Torch | Blackwell 约 96 GB，cu130 可见 |
| Drive 挂载 | 首次/新 VM 要求人工授权；授权后重挂成功 |
| 安装 | 固定源码、cloudflared hash、pip check 通过；新增包见 `tested-overlay-requirements.txt` |
| 服务 | ComfyUI 与 cloudflared 存活，本地 HTTP 就绪 |
| ComfyUI workflow | 64×64 EmptyImage → SaveImage，history completed |
| 本地 WebSocket | 收到 status、executing、executed、execution_success 等事件 |
| 输出 | 可解析 64×64 PNG，421 bytes，API bytes 与 Drive 文件 hash 一致 |
| 未登录 GET `/` | 302，登录目标 host 为 `login.trycloudflare.com` |
| 未登录 POST `/prompt` | 401 |
| 未登录 GET `/ws` upgrade | 302，没有升级为 WebSocket |
| 新 VM 读取旧输出 | G4 已停止后，独立 CPU 从 Drive 读回，hash 一致 |
| 清理 | 服务停止、独立 G4 和 CPU 显式释放；不批量停止其他会话 |
| 真实邮箱 OTP + 浏览器任务 | 未执行，等待用于登录的邮箱 |
| H3 下载及推理 | 未执行；只有官方资料、metadata 与静态工作流核对 |

PNG SHA256 为 `8bcc96141f0cc79731ed0586c621533c0846d84391576afc50b8005c3a15976c`。
另外本地 safety tests 和独立 skill forward-test 使用 fixture，通过的是控制逻辑而非 GPU/外部服务。

### Known Limitations

没有 H3 MP4、音频或模型缓存下载性能结果。没有邮箱登录后的页面和公网 WebSocket 验收。
没有长时间运行、Colab 回收恢复、断网、并发用户或不同系统镜像的验证。
Drive 的实际挂载证明不等于所有大小的模型读取性能保证。首次安装经历修复和重试，不能声称
已在另一个全新 G4 一次安装通过；代码和二进制固定，Colab 基础依赖仍会变化。
Quick Tunnel 为临时入口，有服务限制且邮箱认证需要浏览器；不作为无人值守 API 的身份方案。

### Architectural Decision

固定源码及校验二进制，代码/venv/日志放 VM 本地，资产放专用 Drive 目录；将人工认证步骤保留为
明确可恢复的状态。服务与计算资源分两层关闭，操作仅限自己拥有的进程和会话。
H3 与真实浏览器验收完成前保持草稿，不将部分通过降级为完整通过，也不提前公开发布。
该工具保持独立，不改变业务 Application / Domain、RenderWorker 或 StorageBackend 契约。

## 2026-10-05 新 G4 的 H3 验收与浏览器失败记录（UTC 2026-10-04）

### Question / Goal

在全新 G4 上验证修正后的 bootstrap、完整 H3 模型校验、本地 bridge 的 download/render 后台入口，
以及真实音视频输出和 VM 释放后的 Drive 取回。浏览器邮箱认证、显式 public UI 另行验收，
不把 GPU 推理或文件下载成功当成浏览器访问通过。

### Environment

- 本机官方 `google-colab-cli==0.7.4`，OAuth2；使用新建独立 G4，不复用首轮 runtime。
- G4 为 Blackwell；沿用固定 ComfyUI commit 和 cloudflared 版本、校验值，以及修正后的 bootstrap。
- 模型使用 `models/h3.json` 固定的 4 个文件，共 40,073,842,159 字节，存放专用 Drive 模型目录。
- 实际执行的 `h3-api.json` SHA256 为
  `07a30c993ba61426f6e2943652ebac0c90784d0ac779bad86dee158a64cd28a9`；864×480、124 帧、24 fps。
- 浏览器测试包括 IAB 与 Chrome，使用实际允许的邮箱；记录不保存邮箱、session 名称、公网 URL、
  OAuth/login 链接、cookie、Drive 文件 ID 或私人 probe。

### Steps

1. 新 G4 部署修正后的 bootstrap，一次安装到 `installation.status: ready`。
2. Drive 由用户完成人工授权，挂载重试后成功；使用本轮专用目录。
3. 下载全部 4 个 H3 文件，逐一核验完整大小和 SHA256；没有用文件存在代替校验。
4. 实际调用 download bridge 再次校验上述文件，4 个文件均返回 `skipped`，后台状态 `succeeded`。
5. 通过 render bridge 提交已部署的固定 H3 API 工作流，等待后台结果；校验 MP4 轨道、首帧解码、
   尺寸、帧率、时长，并核对 `/view`、Drive 文件与本地下载的字节一致性。
6. 尝试邮箱 OTP 浏览器访问：IAB 的 cloudflared 回调日志显示缺少 state cookie；Chrome 显示
   `ERR_BLOCKED_BY_CLIENT`。没有将这两次失败描述为成功登录。
7. 显式停止 G4 上的启动器服务；watchdog 于 2026-10-04 16:05:56 UTC 停止该 VM，16:06 UTC 查询
   无活动 session。此后经独立 Drive connector 直接下载已知资产，SHA256 仍一致。
8. 用户最终选择显式 public 临时 URL 测试；另创建独立 CPU、采用 ephemeral 存储。记录本轮 G4 结果时
   该 CPU 仍在安装，后续完成与失败边界见下面独立 CPU 记录。

### Commands

以下为本轮调用接口的脱敏复现示例，不是原始终端日志；`SESSION`、`ALLOWED_EMAIL` 和目录均为占位，
后台任务仍须检查 `status` 中的最终结果。完整模型下载先完成，download bridge 本轮实际验证的是校验后跳过。

```bash
colab --auth=oauth2 new -s "$SESSION" --gpu G4
colab --auth=oauth2 drivemount -s "$SESSION" /content/drive
# 用户完成本次 Drive 授权后，重试挂载。
colab --auth=oauth2 drivemount -s "$SESSION" /content/drive
python3 scripts/colabctl.py -s "$SESSION" deploy
python3 scripts/colabctl.py -s "$SESSION" install
python3 scripts/colabctl.py -s "$SESSION" status
python3 scripts/colabctl.py -s "$SESSION" start \
  --storage-root /content/drive/MyDrive/EXPERIMENT_DIRECTORY \
  --allowed-email "$ALLOWED_EMAIL"
python3 scripts/colabctl.py -s "$SESSION" download \
  --models-root /content/drive/MyDrive/EXPERIMENT_DIRECTORY/models
python3 scripts/colabctl.py -s "$SESSION" status
python3 scripts/colabctl.py -s "$SESSION" render
python3 scripts/colabctl.py -s "$SESSION" status
python3 scripts/colabctl.py -s "$SESSION" stop
# 本轮 VM 停止由限定该 owned session 的 watchdog 执行。
colab --auth=oauth2 stop -s "$SESSION"
colab --auth=oauth2 sessions
```

释放后通过独立 Drive connector 读取已知资产并校验大小与 SHA256；没有虚构 CLI 下载命令，
也不提交真实资产 ID、下载地址或媒体。后续 CPU public UI 的执行记录单独列在下文。

### Result

| 检查 | 实测结果 |
| --- | --- |
| 全新 G4 安装 | 修正后的 bootstrap 一次安装到 ready |
| Drive 挂载 | 人工授权后重试成功 |
| H3 模型 | 4 文件，共 40,073,842,159 字节，完整大小与 SHA256 均通过 |
| download bridge | 4 个已校验文件均 skipped，status 为 succeeded |
| render bridge | succeeded，elapsed 99.251 秒，包含渲染和输出校验 |
| MP4 | 575,001 字节，864×480，124 帧，24 fps，视频时长 5.166667 秒 |
| 视频轨道 | H.264，首帧解码通过 |
| 音频轨道 | AAC、stereo、32,000 Hz，时长 5.166688 秒，首帧解码通过 |
| 资产一致性 | `/view`、Drive 文件、本地下载大小和 SHA256 一致 |
| VM 释放后取回 | G4 已停止后，独立 Drive connector 取回同一 MP4，SHA256 一致 |
| G4 清理 | 服务显式停止，VM 由 watchdog 停止，16:06 UTC 查询无活动 session |
| 邮箱 OTP 浏览器访问 | IAB 回调缺 state cookie；Chrome ERR_BLOCKED_BY_CLIENT，未通过 |
| 后续 CPU public UI | 此轮 G4 记录时仍在安装；后续结果见独立 CPU 记录 |

MP4 SHA256 为 `df92b9467069520c7bb50df39b0e7f2350b6c90a9174cdc3cbd21eb49eaa2cf3`。
99.251 秒不是单独 GPU 推理耗时。渲染器当时检查各轨首帧；随后对同一已取回 MP4 完整解码，
读出 124 个视频帧和 162 个音频帧且没有报错。完整可解码不等于画面或音质的主观验收，
该次检查也不是媒体质量或解码性能 benchmark。

### Known Limitations

邮箱 OTP 正向访问未通过；Chrome 的具体阻断扩展或其他客户端规则尚未诊断，不能确定归因。
用户显式选择 public 模式不等于该 UI 已测试完成，后续 CPU 的浏览器检查结果单独记录。
H3 本轮只验证该工作流、分辨率、帧数及这套 G4/依赖环境；未验证其他 GPU、原生 768p、2K 流程、
长期稳定性、并发或完整媒体质量。download bridge 本轮通过的是完整文件校验与跳过，
不代表其后台网络下载路径已在这次真实 VM 中从零完成。Drive 取回通过不推断所有模型缓存的可靠性或读取性能。

### Architectural Decision

H3 实际推理与音视频资产链路已有真实证据，全新 G4 安装与 VM 释放后的 Drive 取回也已验证。
邮箱保护浏览器访问仍未通过；public 模式必须由用户显式选择，后续 CPU 单独保留其验收状态。
完整浏览器流程通过前不宣称全部启动器验收成功，不以 H3 成功掩盖认证失败。
公开仓库只保留脱敏代码、配置和记录，排除 session、个人邮箱、URL、凭据、私人 probe、日志、权重和媒体。
本工具保持独立，不改变 `video-render-lab` 的 Application / Domain 或 worker/storage 契约。

## 2026-10-05 独立 CPU public / ephemeral 测试（UTC 2026-10-04）

### Question / Goal

按用户明确选择，验证无需邮箱登录的临时公开 ComfyUI 入口，并检查全新 CPU 安装、临时 PNG 流程
和浏览器 Queue 操作。CPU ephemeral 测试不用于验证 Drive 持久化或 H3 推理。

### Environment

新建独立 CPU runtime；官方 CLI 0.7.4、OAuth2，修正后的固定 bootstrap。
启动参数显式选择 `--public --ephemeral --cpu`，没有挂载 Drive；设 8 分钟 watchdog 清理上限。
浏览器使用 Codex IAB 与独立 Chrome。记录不含真实 session、临时公网 URL、邮箱、日志或私有标识。

### Steps

1. 部署到新 CPU，修正后的 bootstrap 一次安装 ready。
2. 以 public、ephemeral、CPU 模式启动；状态确认本地 HTTP 就绪、服务存活和启动完成。
3. 实际执行 smoke：观察本地 WebSocket 事件、提交 `EmptyImage → SaveImage`，校验 HTTP 与临时存储 PNG。
4. 从公网访问首页 HTML 与入口 JavaScript，均得到 HTTP 200；继续用 IAB 和 Chrome 尝试浏览器页面。
5. IAB 停在 Loading/splash，Chrome 显示 `ERR_BLOCKED_BY_CLIENT`；没有成功执行浏览器 Queue 任务。
6. watchdog 到期停止本轮 CPU，后续 `sessions` 查询无活动会话；不将临时资产描述为持久化输出。

### Commands

以下按本轮接口整理为脱敏复现示例；实际命令中的 session 与公网 URL 不存入记录。

```bash
colab --auth=oauth2 new -s "$CPU_SESSION"
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" install
python3 scripts/colabctl.py -s "$CPU_SESSION" status
python3 scripts/colabctl.py -s "$CPU_SESSION" start --public --ephemeral --cpu
python3 scripts/colabctl.py -s "$CPU_SESSION" status
python3 scripts/colabctl.py -s "$CPU_SESSION" smoke
# 本轮 owned CPU 的 watchdog 到期停止计算资源。
colab --auth=oauth2 stop -s "$CPU_SESSION"
colab --auth=oauth2 sessions
```

公网 HTML/入口 JS 的 HTTP 检查与真实浏览器页面分别验收，不通过保存登录凭据、移除浏览器限制
或自动切换访问模式来替代结果。

### Result

| 检查 | 实测结果 |
| --- | --- |
| 新 CPU 安装 | 修正后的 bootstrap 一次 ready |
| 服务 | public / ephemeral / cpu 启动 ready，本地 HTTP 就绪 |
| 本地 PNG / WebSocket | smoke 通过，421 字节 PNG，收到 `execution_success` 等事件 |
| 存储 | HTTP 与 VM 临时文件一致；没有 Drive 持久化证据 |
| 公网 HTML / 入口 JS | HTTP 200 |
| IAB 浏览器 | 停在 Loading/splash，未完成页面和 Queue 验收 |
| Chrome 浏览器 | ERR_BLOCKED_BY_CLIENT，未完成页面和 Queue 验收 |
| 浏览器 Queue / 输出预览 | 未通过，没有成功任务证据 |
| 清理 | 8 分钟 watchdog 停止本轮 CPU，sessions 查询无活动会话 |

本轮 PNG SHA256 为 `c0b9c6949d6c66f0ebfcf17636e7e69802a8f0aba9fdc3576d746462b5a85073`。
它与前轮 PNG 的哈希不同不代表不一致，应比较同次 HTTP 与存储文件。

### Known Limitations

HTTP 200 只证明对应资源可取回，未证明完整前端、外部 WebSocket、Queue 或输出预览能正常工作。
IAB 停在 splash、Chrome 被客户端阻断的具体原因尚未确诊；不能把扩展、cookie 或网络原因当成已定位根因。
本轮没有 H3 渲染、持久化资产或跨 runtime 恢复测试；临时文件随 runtime 释放失去保留保证。

### Architectural Decision

修正后的 CPU 安装和本地临时 PNG 执行已有证据，临时公开 HTML/JS 也能取回。
public 浏览器任务仍未通过，邮箱保护模式此前也未通过；不宣称完整浏览器启动流程验收成功。
继续保留显式访问模式与 owned runtime 清理，不因客户端失败自动扩大访问范围。

## 2026-10-05 第二轮 CPU 诊断（历史，UTC 2026-10-04）

### Question / Goal

区分公网 API/WebSocket、真实编辑器加载和浏览器 Queue 的验收边界，调查前轮 splash 等待。
通过无模型的 PNG 任务检查外部入口，同时保留严格的计算资源清理期限；不把后端任务完成
当成浏览器操作成功，也不为诊断重新分配 GPU。

### Environment

新建独立 CPU runtime，官方 CLI 0.7.4、OAuth2，固定 ComfyUI 和 frontend package 1.53.10。
显式 `--public --ephemeral --cpu`，没有 Drive 挂载；该轮 watchdog 上限 900 秒。
公网诊断器在本机显式使用 `websocket-client==1.8.0`，整体诊断目标上限 60 秒。
真实页面使用 Codex IAB，用户也观察了同一页面。记录不保存真实 URL、session、邮箱或资产标识。

### Steps

1. 在独立 CPU 上部署和安装，启动公开临时入口，保持计算资源 watchdog。
2. 从本机执行 `check_public.py`；第一尝试在 `/api/object_info` 的响应读取阶段遇到单请求
   5 秒超时。将单请求等待改为最多 30 秒，整体诊断上限仍为 60 秒。
3. 第二尝试的七个初始化/状态 JSON 路由返回 HTTP 200，公网 WebSocket 已连接，
   `/api/prompt` 和 history 请求返回 HTTP 200；诊断器仍因整体期限返回失败。
4. 独立的远端只读 completion probe 查询此前已经提交的那项任务，确认 history success
   及真实 PNG 文件；没有再次提交任务，没有将 probe 结果改写成诊断器成功。
5. IAB 最终实际加载编辑器，用户也确认看见侧边栏。计算资源期限过后，在已经加载的页面
   用 Ctrl+O 导入 `workflows/smoke-ui.json` 成功；此时后端已停止，没有执行 Queue。
6. 修正诊断器：优先消费 WebSocket 缓冲事件；收到本任务的 `execution_success` 后立即
   查询 history，其余最多每五秒查询一次，避免每收一帧都被 HTTP 阻塞。成功标准仍要求
   自有 WS success、history completed/success 和真实 64×64 PNG；八项离线测试通过。
7. watchdog 约于 16:37:31 UTC 停止本轮 CPU，16:40 UTC 查询未发现活动会话。
   后续最终 CPU 复验记录另列，不将其尚在安装的状态算作本轮通过。

### Commands

以下是按本轮调用接口整理的脱敏示例，实际 session 和临时 URL 均使用变量占位。
只读 completion probe 是独立的短 `exec` 检查，其私有任务标识和原始输出不保存到仓库。

```bash
colab --auth=oauth2 new -s "$CPU_SESSION"
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" install
python3 scripts/colabctl.py -s "$CPU_SESSION" status
python3 scripts/colabctl.py -s "$CPU_SESSION" start --public --ephemeral --cpu
uv run --with websocket-client==1.8.0 python scripts/check_public.py \
  --url "$TASK_PUBLIC_URL" --max-seconds 60
colab --auth=oauth2 exec -s "$CPU_SESSION" -f "$READ_ONLY_COMPLETION_PROBE" --timeout 60
python3 -m unittest discover -s tests -p test_check_public.py -v
# 本轮 owned CPU 由900秒watchdog停止，随后查询计算资源状态。
colab --auth=oauth2 sessions
```

Ctrl+O 导入是浏览器中的真实人工界面操作，没有用 HTTP 提交替代 Queue 点击。

### Result

| 检查 | 实测结果 |
| --- | --- |
| 七个公开 JSON 路由 | `/api/features`、`users`、`settings`、`i18n`、`object_info`、`queue`、`system_stats` 均 HTTP 200；不记录响应内容 |
| 公网 WebSocket | 已连接，诊断器仅记录到 `status`、`execution_cached`；没有诊断器 WS success 通过证据 |
| 诊断器第二尝试 | 60 秒目标上限触发，62.869 秒输出 `ok: false`、stage `execution`；不当成成功 |
| 独立只读 completion probe | 此前提交的任务实际完成，history status 为 success；PNG 419 字节 |
| PNG SHA256 | `a9c67e9fbd1f2493f054e03eb6a6033d5f88b578d9758969daa9259b4f3e7a59` |
| IAB 编辑器 | 最终真实加载，用户也确认侧边栏可见 |
| 浏览器工作流导入 | 在已加载页面 Ctrl+O 导入 `smoke-ui.json` 成功，但当时后端已停止 |
| 浏览器 Queue / 输出预览 | 未测，不能列为通过 |
| 诊断器修正 | WebSocket 优先、减少 history 轮询的八项离线测试通过；修正版本待后续真实复验 |
| 清理 | 900 秒 watchdog 约 16:37:31 UTC 停止 owned CPU，16:40 UTC 查询无活动会话 |

### Known Limitations

后端 PNG 完成是独立只读检查的证据，不表示超时的公网诊断器拿到了 execution_success，
也不表示浏览器提交和输出预览通过。页面最终加载及导入成功说明编辑器可以运行，
但后端在导入时已经停止，不能据此补记 Queue 成功。

诊断器的 60 秒期限失败实际在 62.869 秒输出，按实测保留这个差异；单请求超时、
WebSocket 接收与慢 HTTP 轮询的记录不足以单独确定前轮 splash 的全部根因。
本轮没有 Drive 持久化、H3 或邮箱认证测试。临时 PNG 不提供 VM 停止后的保留保证。

### Architectural Decision

真实浏览器加载、导入、Queue 和输出预览继续分别验收。保留独立公网诊断器作为
显式 public 模式的 API/WS 检查；其 JSON 明确 `browser_ui_verified: false`，不替代 UI 测试。
修正后继续在另一个独立 CPU 上复验，不降低任务成功标准、不改变 H3 后端，也不延长
已经到期的资源期限。主项目 Application / Domain 与 worker/storage 契约不受影响。

## 2026-10-05 最终 CPU 公网与页面验收（UTC 2026-10-04）

### Question / Goal

在不再分配 GPU、不再下载模型的条件下，验证用户选择的临时公开模式：公网初始化 API、
WebSocket、图片任务，以及真实浏览器导入、Run 和输出预览。发布前保留实际失败和修复证据。

### Environment

独立 CPU runtime，固定 ComfyUI commit、frontend 1.53.10 和 cloudflared 2026.9.3；
显式 public / ephemeral / cpu，不挂载 Drive。设置仅针对本会话的 20 分钟 watchdog。
浏览器使用 Codex IAB。代码审查、离线测试不分配计算资源。

### Steps

1. 新 CPU 一次安装 ready，服务启动成功；本地 PNG smoke 通过。
2. 公网诊断在 `/api/object_info` 收到 HTTP 200 后，读取响应体遇到 TimeoutError，
   39.607 秒返回失败；页面当时仍在 Logo。不能将 HTTP 200 当作完整 JSON 已读完。
3. 从该 VM 安装的固定官方源码确认 `--enable-compress-response-body` 参数，停止自有服务，
   部署带该参数的启动器，再启动公开入口。没有修改第三方源码或安装自定义节点。
4. 诊断器请求 gzip，按块有界解压，解压正文最多 16 MiB；保留总期限、无重定向及严格成功标准。
   修正版本真实公网检查通过，本地 smoke 也再次通过。
5. IAB 约三分钟后显示模板和编辑器。关闭模板列表，Ctrl+O 导入 `smoke-ui.json`，点击 Run，
   在 Job Queue 的 Completed 中看到该任务，打开 Gallery；图片 complete 为 true，
   naturalWidth/naturalHeight 均为 64。后续只读检查确认该任务 history success 及 API/文件一致。
6. 浏览器验收结束后显式停止自有服务，再停止该 CPU runtime；16:57:20 UTC 查询无活动会话。
   本轮没有等待 watchdog 到期，也没有保留 GPU、CPU 或公开服务。

### Commands

以下为脱敏复现示例；重启服务会生成新的临时网址，需从最新 status 读取。

```bash
colab --auth=oauth2 new -s "$CPU_SESSION"
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" install
python3 scripts/colabctl.py -s "$CPU_SESSION" status
python3 scripts/colabctl.py -s "$CPU_SESSION" start --public --ephemeral --cpu
python3 scripts/colabctl.py -s "$CPU_SESSION" smoke
# 诊断超时后，部署修正版本并重启本启动器服务。
python3 scripts/colabctl.py -s "$CPU_SESSION" stop
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" start --public --ephemeral --cpu
python3 scripts/colabctl.py -s "$CPU_SESSION" status
uv run --with websocket-client==1.8.0 python scripts/check_public.py \
  --url "$PUBLIC_URL" --max-seconds 60
python3 scripts/colabctl.py -s "$CPU_SESSION" smoke
python3 scripts/colabctl.py -s "$CPU_SESSION" stop
colab --auth=oauth2 stop -s "$CPU_SESSION"
colab --auth=oauth2 sessions
```

### Result

| 检查 | 实测结果 |
| --- | --- |
| 新 CPU 安装 | 一次 ready |
| 修正前公网诊断 | object_info HTTP 200 后正文读取超时，39.607 秒，失败 |
| 修正后公网诊断 | 43.646 秒，`ok: true`，stage complete |
| 七个初始化/状态 API | 均 HTTP 200，JSON 已完整读取 |
| 公网 WebSocket | 收到本任务 execution_success，另有 executing、executed、status 等事件 |
| 公网任务和取回 | prompt/history/view 均 HTTP 200，history completed/success，64×64 PNG 419 字节 |
| 公网 PNG SHA256 | `24bcb9f0ea2b944595256f9a45b66b193f93723230fd73ddcc1aa3f039e76594` |
| 修正后本地 smoke | 421 字节 PNG，WS success，API/临时文件一致 |
| 本地 PNG SHA256 | `0bd99f03088ec375a448ef48d2ca842cededa2471ed5c139bdf10b9f63bd3500` |
| IAB 导入 / Run / 输出预览 | 真实 UI 操作通过，Completed 任务、Gallery 与 64×64 图片解码均已确认 |
| 浏览器任务输出 | PNG 1,625 字节，history completed/success，API 与临时文件一致 |
| 浏览器 PNG SHA256 | `9c0163bc70a650cd76d97debf4c27a525cb7cd8f5b93716136d7f860f4a70ef4` |
| 最终清理 | 服务和 owned CPU 显式停止，16:57:20 UTC 查询无活动 session |
| 离线验证 | 94 tests、Ruff 检查与格式、bash syntax、skill validation 全通过 |

### Known Limitations

公网 API/WS 与 IAB 的浏览器 Run 分别通过，后者为实际界面操作和预览。压缩和诊断器修正后的组合通过，
不据此断言压缩是此前所有 splash 等待的唯一原因，也没有做受控网络性能对比。
邮箱模式的真实登录仍未通过，Chrome 的 ERR_BLOCKED_BY_CLIENT 具体原因未确诊。
本轮为无模型的 CPU 临时图片测试，不增加 H3 推理、Drive 持久化或长期稳定性结论。

### Architectural Decision

默认启用已核对的官方响应压缩，以减少大节点信息和其他可压缩响应的传输开销；
公开模式的额外诊断器只在用户明确选择该入口后执行。Drive 资产目录必须为 MyDrive
内的专用子目录，拒绝整个 MyDrive 根目录，回归测试通过。用户最终选择的公开模式已满足
浏览器验收条件，发布范围保留真实通过与邮箱失败的边界，并在结束时释放本轮 owned CPU。
