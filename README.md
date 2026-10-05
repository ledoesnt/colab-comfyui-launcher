# Colab ComfyUI Launcher

通过官方 Colab CLI 启动 ComfyUI：**Drive 长期保存模型缓存 → 每个新 VM 复制并校验到本地磁盘 → ComfyUI 从本地加载模型 → 输出保存到 Drive**。在本机仓库根目录运行终端界面即可管理这个流程，无需手动进入 `colab console`：

```bash
python3 scripts/dashboard.py
```

默认通过 `local-only` 服务与 SSH 转发，在本机 `http://127.0.0.1:8188` 访问。也可以显式选择 `--public` Cloudflare 临时公开网址，或 `--allowed-email` 邮箱登录；不会自动公开服务。SSH 模式需要专用密钥，创建密钥必须由你明确选择。

这是独立的基础设施工具，不依赖 `video-render-lab` 的 Render API。2026-10-05 已确认四个模型均从 VM 本地磁盘加载，H3 音视频生成、输出保存及释放 G4 后从 Drive 取回通过。最新整套 40.07 GB 计时：先复制、落盘再本地 SHA 共 439.911 秒，其中本地 SHA 26.875 秒；同机随后同步复制校验共 367.357 秒。固定执行顺序和暖缓存影响比较，不能承诺固定提速。前轮同 VM receipt 元数据复用为 0.055 秒。

最新版上下选择向导已实际验证新建 CPU、两台 G4 的 Drive / 临时存储分支、四模型逐文件进度、SSH、退出保留与接回；G4 两分支均完成 H3 API 音视频生成，Drive 输出落盘通过。临时分支直接下载同步 SHA 用时 305.849 秒，Drive 分支复制同步 SHA 为 726.705 秒；来自不同 VM 和传输路径，不能作为固定速度比较。操作截图和逐步 Markdown 保存于 Git 外。

本轮真实 Chrome 成功打开 G4 的 SSH 编辑器；IAB 实际导入、Run 并预览 PNG，也显示 H3 产物。Chrome 的测试文件上传需要扩展文件访问权限，本轮没有修改该权限。此前 Cloudflare 的客户端拦截原因尚未排清，邮箱登录未通过。H3 模型推理与媒体校验通过 API 工作流完成，不能据此宣称所有浏览器模板通过。详见 [实测记录](docs/test-report.md) 与 [H3 调查](docs/colab-h3-research.md)。

## 准备

- 本机 Linux。启动器脚本与离线测试支持 Python 3.10+；官方 `google-colab-cli==0.7.4` 的独立工具环境需要 Python 3.12+。向导会检查 CLI 登录，必要时引导完成 Google OAuth。
- Colab 账号有可用资源。GPU 类型和计费随账号及供应变化；先查看 `colab --auth=oauth2 usage`。Colab 对主要通过 Web UI / SSH 使用 runtime 的限制见[官方 FAQ](https://research.google.com/colaboratory/faq.html)；使用符合要求的付费资源。
- 使用邮箱模式时，准备一个能收取 Cloudflare 登录验证码的邮箱；公开模式不需要邮箱。Google Drive 按提供方实际提示完成人工授权；不保存或重放授权链接。
- 默认仅启用官方 ComfyUI 节点，不安装第三方 Manager 或未知自定义节点。
- 本机使用 SSH 转发时需要 `ssh` 和 `ssh-keygen`。Cloudflare 模式不需要 SSH 密钥。

```bash
uv tool install --python 3.13 google-colab-cli==0.7.4
colab --auth=oauth2 sessions
```

首次 CLI 登录会引导授权。不要把 OAuth code、token、cookie 或 SSH 私钥发到聊天、Git 或 issue。

## 终端界面

默认是逐步启动向导，采用 ComfyUI `#F2FF59`、Colab `#E77012` / `#F9AA00` 配色。**上下选择，Enter 确认**；移动选中项不会执行操作。Esc 返回或取消输入；PgUp / PgDn 翻看右侧详情。输入字段为空时显示默认值，开始输入后提示消失，普通输入使用高对比文字。

首次使用：

1. 选择 **Create a new runtime**，选择 GPU 或 CPU；GPU 再选择型号。G4 是本项目已测型号，其余型号的 H3 兼容性未验证。
2. 选择 **Google Drive** 或 **VM disk**。前者长期缓存模型和保存输出；后者跳过挂载，直接下载到 VM，释放后不保留模型与输出。
3. 检查配置摘要，再确认创建。首次缺少专用 SSH key 时，明确选择创建或填写已有 key 路径；不会覆盖已有 key。
4. 若官方 CLI 登录或 Drive 挂载要求授权，右侧显示本轮链接和操作提示，选择 **Open authorization in browser**。按提供方要求完成后，选择 **I have authorized · continue**；若官方 CLI 要求 code，在屏内遮罩输入。子终端保持在后台，不切走 TUI；实际挂载和 MyDrive 检查通过才继续。
5. 自动部署、安装、准备 GPU 模型并启动 ComfyUI。右侧显示每个文件的下载、复制或校验进度；CPU 跳过 H3。下载和复制同步计算 SHA256，已有同 VM 验证记录复用会明确标注没有新计算 SHA。
6. 自动建立 SSH 转发并验证本机 HTTP；出现 **ComfyUI is ready** 后选择 **Open ComfyUI in browser**。

**View existing runtimes** 会先核对真实状态、恢复已有配置，再显示概览或继续缺少的步骤。不会因为 CLI 超时自动重复新建。配置未知时重新选择存储；已有运行服务的存储和访问模式不能直接覆盖。更改已运行 SSH 的端口或 key 前，先到 Advanced actions 停止旧转发。

同一实例的唯一有效 owned SSH 转发会恢复实际端口和 key 路径，再检查本机 HTTP；不会静默回到 8188。多个转发或旧版本缺少配置的活跃记录需明确处理：用已知端口停止旧转发，再继续向导。只读状态查询遇到连接关闭或本机命令超时最多尝试三次；创建、安装、准备等写操作不因此重复提交。

Ready 页面可以打开浏览器、**Exit and keep resources**、**End this VM** 或查看其他实例。Exit 保留 VM、已运行服务、转发和远端任务；End this VM 有独立确认，默认选中 Cancel，且只清理选中的实例。启动期间请求释放会取消后续协调并串行清理原实例。挂载、启动失败或状态未知时停在错误页，先 Inspect 再处理，不自动重试未知结果。

离线预览不创建 Colab、不访问凭据、不打开浏览器：

```bash
python3 scripts/dashboard.py --demo
python3 scripts/dashboard.py --demo --demo-state ready
python3 scripts/dashboard.py --demo --demo-state error
```

Demo 是只读 fixture，可预览导航、输入与进度，不作为云端成功证据。`--no-color` 或 `NO_COLOR` 关闭配色。`TERM=dumb` 或 curses 不可用时退回数字选项 + Enter 的文字界面；需输入敏感授权 code 时使用全屏终端。非交互 Demo 只输出一次快照。支持 Python 3.10+，只使用标准库，不需额外 TUI 框架。

需要旧操作码界面时显式运行 `python3 scripts/dashboard.py --classic`；`f` 是完整部署/安装/准备/启动流程，`m` 单独挂载，`H` 停止 SSH，`s` 停服务保留 VM，`X` 释放 VM，`q` 保留资源退出。这些操作码都需 Enter。新向导用 **Advanced actions** 管理单独步骤，正常启动无需记操作码。

### Advanced actions

这些操作针对当前选中的实例，上下选择后按 Enter 执行。正常启动优先使用完整向导；需要检查、测试或恢复单独步骤时再进入高级操作。

| 菜单项 | 作用 |
| --- | --- |
| Inspect current runtime | 查询真实状态，恢复已保存的配置与本机 SSH 转发信息。 |
| Authorize / check Google Drive | 检查 Drive；需要时在屏内引导授权，已挂载则跳过。VM disk 模式直接跳过，不切换存储模式。 |
| Run a PNG smoke test | 运行无模型的 64×64 PNG 工作流，检查执行、输出获取与存储一致性；CPU 也可使用。 |
| Render the H3 API test | 执行项目固定的 H3 视频测试并校验媒体和存储输出；需要 GPU、已准备的模型和运行中的 ComfyUI。 |
| Stop services · retain VM | 先停止本机 owned SSH 转发，再停止启动器服务；保留 VM。 |
| Start local SSH forwarding | 为当前实例建立本机 SSH 转发并检查 ComfyUI HTTP；不创建新 VM。 |
| Stop local SSH forwarding | 只停止当前配置的本机 owned SSH 转发，保留远端服务与 VM。 |
| Continue missing startup steps | 检查已有状态，跳过已就绪步骤、等待已有任务，并完成缺少的启动步骤；状态不明时停下。 |

**Return to runtime overview** 返回概览。**End this VM** 在概览页单独操作并再次确认；停止服务或转发不等于释放 VM。

完整导航和两条存储分支见 [启动流程图](docs/startup-wizard.md)。

### H3 模板提示缺模型

当前 40.07 GB 清单含基础 FL2VA、Qwen 文本编码器、视频 VAE 和音频 VAE，项目的 `workflows/h3-api.json` 用这四个文件运行基础文本生成视频测试。它没有囊括所有 H3 模板的可选权重。

官方 I2V 模板可能提示缺少 `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`（约 1.82 GB），这是用于加速的 Turbo LoRA。可按[官方指南](https://docs.comfy.org/tutorials/video/minimax/minimax-h3)选择基础模式或补充对应 LoRA；本启动器尚未验证 Turbo 工作流，不自动下载这份附加权重。`Missing Inputs` 则表示 I2V 还没有选择输入图片。R2V 等其他模式也可能需要各自的生成模型，不能把四模型测试通过理解为所有模板均可直接运行。

## 手动启动

以下命令在本机仓库根目录执行。`SESSION` 选择一个新名称；已有会话请明确使用其名称，不要创建重名资源。

```bash
SESSION=comfyui-g4-demo
STORAGE_ROOT=/content/drive/MyDrive/colab-comfyui-launcher-test
colab --auth=oauth2 new -s "$SESSION" --gpu G4
colab --auth=oauth2 status -s "$SESSION"
colab --auth=oauth2 drivemount -s "$SESSION" /content/drive

python3 scripts/colabctl.py -s "$SESSION" deploy
python3 scripts/colabctl.py -s "$SESSION" install
python3 scripts/colabctl.py -s "$SESSION" status
```

Drive 可能在首次使用或新的 runtime 中要求浏览器授权后回车。若等待授权期间出现 `mount failed`，完成授权后再执行一次挂载，并检查实际挂载结果；已有授权被接受时不必重复人工操作。不要根据 CLI exit 0 判断成功。

`install` 返回的是已开始安装。它有默认 900 秒上限，后台记录状态和日志；重复检查 `status`，待 `installation.status` 为 `ready`。安装失败时检查 VM 上 `/content/colab-comfyui-runtime/logs/install.log`。本机命令结束后，安装和服务仍在 VM 上运行。

GPU 服务启动前必须准备本地 H3 模型。下面一个后台任务会下载 Drive 中缺失的清单文件，并把全部文件复制、同步校验到 `/content/colab-comfyui-runtime/models`；已有 Drive 文件直接在复制时校验，不先完整读取一遍：

```bash
python3 scripts/colabctl.py -s "$SESSION" prepare \
  --download-missing --cache-root "$STORAGE_ROOT/models" --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

若已明确选择临时 VM 存储，跳过 Drive，改用下面命令；权重直接下载到 VM 模型目录并同步 SHA，不另外保存一份 40 GB 临时缓存。启动时同样加 `--ephemeral`。

```bash
python3 scripts/colabctl.py -s "$SESSION" prepare \
  --ephemeral --download-missing --max-seconds 1800
```

等待 `model_prepare.running: false`、`model_prepare.status: succeeded`、`model_prepare.result.ok: true` 和 `models_ready: true`，再启动 GPU 服务。逐文件进度位于 `model_prepare.progress`。只有 `started` 表示后台任务已接受，不能当成模型已经就绪。

从以下访问方式中选择一种。使用 SSH 时，ComfyUI 只监听 VM 的本地端口，不创建 Cloudflare 入口：

```bash
python3 scripts/colabctl.py -s "$SESSION" start \
  --local-only --storage-root "$STORAGE_ROOT"
python3 scripts/colabctl.py -s "$SESSION" status
# 等 startup.ok: true、comfyui_alive/http_ready: true，再启动转发。
# 首次明确创建专用密钥；已有该密钥时可省略 --create-key。
python3 scripts/ssh_forward.py -s "$SESSION" start --local-port 8188 \
  --identity "$HOME/.ssh/colab_comfyui_launcher" --create-key
python3 scripts/ssh_forward.py -s "$SESSION" status --local-port 8188
```

local-only 服务状态应有 `access_mode: local`；转发返回 `running: true`、`http_ready: true` 后打开 `http://127.0.0.1:8188`。转发只绑定本机 127.0.0.1，使用已有 Colab 会话；不会分配或释放 VM。

以下示例显式选择临时公开网址：**知道网址的人可以操作 ComfyUI、提交任务并访问其输入和输出**。这是用户选择的访问方式，不是邮箱模式失败后自动采用的降级。用完后关闭服务和 runtime。

```bash
python3 scripts/colabctl.py -s "$SESSION" start \
  --public --storage-root "$STORAGE_ROOT"
python3 scripts/colabctl.py -s "$SESSION" status
# 等 startup.ok/http_ready: true、两个服务存活后再运行 smoke。
python3 scripts/colabctl.py -s "$SESSION" smoke
```

`start` 返回 `starting`；公开和邮箱模式随后应有 `http_ready: true`、两个服务存活、`startup.ok: true` 和期望的 `access_mode`（`public` 或 `email`）。打开返回的网址。已运行服务需先 `stop` 再切换访问方式，不要依次执行多个 `start`。

`smoke` 是无模型的真实 `EmptyImage → SaveImage` 测试，会检查本地 WebSocket、history、64×64 PNG 和存储文件校验值；它不是视频模型测试。浏览器按 `Ctrl+O` 导入 [workflows/smoke-ui.json](workflows/smoke-ui.json)，点击 **Run**，检查任务完成与图片预览。首次页面加载可能需要等待；停在 Logo 时不能仅凭后台 ready 判断页面正常。

公开模式还可以在本机检查初始化 API、公网 WebSocket、任务提交及 PNG 下载。它会提交一个临时图片任务，默认总上限 60 秒；返回 `ok: true` 也不能代替浏览器页面验收。把 `PUBLIC_URL` 设为本次 `status` 返回的网址，不要把它提交到 Git：

```bash
uv run --with websocket-client==1.8.0 python scripts/check_public.py \
  --url "$PUBLIC_URL" --max-seconds 60
```

Drive 的专用目录保存 `{models,input,output,user}`，其中 `models` 是持久缓存。**ComfyUI 的模型目录始终在 VM 的 `/content/colab-comfyui-runtime/models`**，输入、输出与用户设置留在所选存储目录。自定义 `--storage-root` 时，`prepare --cache-root` 使用同一个目录的 `models` 子目录。必须真的挂载 Drive，脚本不会创建一个普通目录来假装挂载。

只测试页面、网络和 PNG 时可用独立 CPU，无需下载 H3：

```bash
CPU_SESSION=comfyui-cpu-demo
colab --auth=oauth2 new -s "$CPU_SESSION"
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" install
python3 scripts/colabctl.py -s "$CPU_SESSION" status
# 等 installation.status: ready；CPU 跳过 H3 准备，临时存储无需 Drive。
python3 scripts/colabctl.py -s "$CPU_SESSION" start --cpu --ephemeral --public
python3 scripts/colabctl.py -s "$CPU_SESSION" status
# 等服务就绪，再执行 smoke 和浏览器验收。
python3 scripts/colabctl.py -s "$CPU_SESSION" smoke
```

CPU `--ephemeral` 的输出会随 VM 回收失去保留保证；测试后仍需关闭服务并显式停止该 CPU 会话。

需要邮箱保护时，停止已有服务，再以另一种显式方式启动；三种访问方式只能选一个：

```bash
read -r -p '允许访问 ComfyUI 的邮箱: ' ALLOWED_EMAIL
python3 scripts/colabctl.py -s "$SESSION" start \
  --allowed-email "$ALLOWED_EMAIL" --storage-root "$STORAGE_ROOT"
```

邮箱模式传递 Cloudflare `--allowed-mail`。未认证页面应重定向到登录页，POST `/prompt` 应被拒绝，未认证 `/ws` 不能建立连接；这些拦截已实测。真实登录尝试在 Codex 内置浏览器发生回调缺少 authentication-state cookie 的错误；独立 Chrome 尝试显示 `ERR_BLOCKED_BY_CLIENT`。具体浏览器原因未确诊，邮箱登录后的成功访问尚未验收。公开模式由用户明确选择，不需要该登录步骤。两种方式都要另行验证页面、WebSocket 和浏览器任务；只有网址出现不代表端到端访问通过。[Cloudflare 官方说明](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

## MiniMax H3

[官方 FAQ](https://design.minimax.io/h3)明确支持 Colab GPU runtime；本项目已核对 G4 实际为约 96 GB 的 RTX PRO 6000 Blackwell。模型文件清单见 [models/h3.json](models/h3.json)，固定 4 个文件的 revision、大小和 SHA256，约 40.07 GB，已真实下载并通过全部校验。[workflows/h3-api.json](workflows/h3-api.json) 已完成真实推理：864×480、124 帧、24 fps、20 步、seed 20261004，生成约 5.17 秒、带立体声音频的 MP4；历史轮次生成与校验用时 99.251 秒。

H3 使用有地域和商业条件的社区许可证；本项目的 MIT 许可证只覆盖自己的脚本。下载、部署前阅读 [H3 说明](docs/h3.md)。不能单凭云节点名字自动判断使用主体是否获授权；也不能以文件可下载代替许可证说明。H3 基础权重与托管的 2K 流程有不同边界。

完整启动使用前面的 `prepare --download-missing` 即可。若只想维护 Drive 缓存而暂不复制到 VM，可独立运行：

```bash
python3 scripts/colabctl.py -s "$SESSION" download \
  --models-root "$STORAGE_ROOT/models" \
  --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

`download` 启动后台任务；通过 `status` 等待 `model_download.running: false`、`model_download.status: succeeded` 和 `model_download.result.ok: true`。它不会准备 ComfyUI 的本地模型目录，GPU 启动仍需 `prepare`。

缓存校验与复用边界：

- 首次下载边写入边计算 SHA256，完整大小和清单 hash 匹配后才原子发布；不再额外完整读取一次刚下载的 Drive 成品。HTTP 续传保留 `.partial`，已有前缀需读一次建立 hash；服务端忽略 Range 时安全重启。
- 首次 `prepare` 把每个 Drive 文件复制到 VM `.partial`，在同一读取流中计算完整 SHA256，匹配后才发布本地文件，并写入两端的 verified receipt。旧缓存没有 receipt 时，也直接用这一遍复制流建立校验记录。
- Drive receipt 绑定清单 repo/revision/path/size/SHA 与文件 size/mtime；本地 receipt 还绑定当前 boot ID、device/inode/mtime/ctime。同一 VM 的文件身份全部匹配时可跳过复制和内容读取。结果 `verification: verified_receipt_metadata` 表示之前完整校验与本次元数据匹配，**不是本次重新完整 SHA 校验**。
- receipt 缺失或元数据变化时进行完整验证或拒绝；尺寸、SHA 错误的已有成品不会被覆盖。需要主动完整重查 Drive 时，给 `download` 或 `prepare` 添加 `--verify-cache`；对 `prepare` 而言这会增加一次完整 Drive 读取。
- 新 VM 仍须读取、复制约 40.07 GB，并在复制时校验；前轮 prepare 实测 516.032 秒。同 VM 完整身份匹配后的复用为 0.055 秒，不是重新读取 40 GB。此设计减少重复读取，不保证其他 Drive 吞吐或启动耗时。本地复制中断的 `.partial` 下次从头重复制，HTTP 下载续传是另一个步骤。

后续在另一新 G4 用相同 8 MiB chunk 做两种完整路径：先复制落盘 412.971 秒、随后本地完整 SHA 26.875 秒，总 439.911 秒；同机排第二的复制时同步 SHA 总 367.357 秒，四个模型均通过。未清缓存或反转整套顺序，复制阶段本身也有吞吐差异，不能把总差全部归因于省去 SHA 的读取。保留默认同步完整校验；详见 [计时口径和小文件补充](docs/test-report.md)。

receipt 用于专用、自有缓存的复用，不是防同账号恶意修改的完整性保证。一次只让一个 runtime 写同一缓存；本机文件锁不能当成跨 VM 分布式锁。

模型全部校验、G4 上的服务就绪后，在本机启动部署的 H3 API 工作流：

```bash
python3 scripts/colabctl.py -s "$SESSION" render --max-seconds 900
python3 scripts/colabctl.py -s "$SESSION" status
```

桥接会在 VM 上调用 `scripts/render_workflow.py`，提交真实工作流，跟踪队列和 history，检查首帧解码、分辨率、帧率、时长、音视频轨道及 API/存储文件校验值，并记录容器报告的帧数。等待 `render.running: false`、`render.status: succeeded` 和 `render.result.ok: true`；最初的 `started` 不表示视频已完成。超时或失败后先检查状态，不要重复提交未知状态的任务。

此前释放 G4 后，通过 Drive connector 再次下载生成的 MP4，SHA256 与运行时校验值相同。这证明该实测文件在停止计算资源后仍可取回；未验证长期稳定性、并发写入或所有 Colab 镜像。

## 关闭与复用

```bash
python3 scripts/ssh_forward.py -s "$SESSION" stop --local-port 8188
python3 scripts/colabctl.py -s "$SESSION" stop
colab --auth=oauth2 stop -s "$SESSION"
colab --auth=oauth2 sessions
```

第一条只关闭属于本启动器的本机 SSH 转发，第二条关闭远端服务与后台任务，第三条释放 Colab 计算资源。公开或邮箱模式没有 SSH 转发时可省略第一条。关闭终端不等于释放 runtime。只停止本轮创建或明确获授权停止的会话；不要批量关闭用户其他会话。停止服务不会删除 Drive 文件或专用 SSH 密钥。

同一 VM 可用 `status` 检查、用 `stop → start` 重启服务，本地 receipt 身份匹配时可复用已准备模型。新的 VM 要重新部署代码和依赖、挂载 Drive、运行 `prepare`；Drive 模型缓存保留，但 VM 的本地模型随释放消失，仍需重新复制与校验。Colab 回收、空闲期限和隧道 URL 都不提供永久在线保证。临时网址每次重启可能变化；固定域名和机器调用应另行设计命名 Tunnel 与服务身份认证。

## Codex skill

仓库包含 [.agents/skills/colab-comfyui/SKILL.md](.agents/skills/colab-comfyui/SKILL.md)。在仓库中启动 Codex，可调用 `$colab-comfyui`，或让它读取该文件。技能会检查已有会话、按授权使用 GPU、保留人工授权、验证结果并清理其创建的资源。也可以把本仓库交给其他能读 Markdown 并执行本地命令的 agent。[Codex 官方技能文档](https://learn.chatgpt.com/docs/build-skills)

## 开发与边界

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/bootstrap.sh
```

ComfyUI 源码 commit 与 cloudflared 二进制/校验值固定；其余环境仍依赖 Colab 镜像。安装复用系统 PyTorch，本次实测为 Python 3.13.15、PyTorch 2.11.0+cu130。新增包的记录在 [tested-overlay-requirements.txt](docs/tested-overlay-requirements.txt)，它是观测记录，不能称作完整环境锁文件。后续版本变更必须重新验证。

启动器代码部署在 `/content/colab-comfyui-launcher`；ComfyUI、虚拟环境、二进制、本地模型、PID、日志和进度位于 `/content/colab-comfyui-runtime`；Drive 保存模型缓存与资产。本机 SSH 的私有状态位于 `~/.local/state/colab-comfyui-launcher/ssh`，密钥位于所选的专用路径；它们均不进入仓库。公开仓库不包含邮箱、OAuth 链接、session 凭据、日志、模型、生成媒体或第三方 skill。模型清单和工作流本身不自动证明推理成功；本项目的成功结论来自实测记录。
