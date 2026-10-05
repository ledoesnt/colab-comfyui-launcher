# Colab ComfyUI Launcher

通过官方 Colab CLI 启动 ComfyUI：**Drive 长期保存模型缓存 → 每个新 VM 复制并校验到本地磁盘 → ComfyUI 从本地加载模型 → 输出保存到 Drive**。在本机仓库根目录运行终端界面即可管理这个流程，无需手动进入 `colab console`：

```bash
python3 scripts/dashboard.py
```

默认通过 `local-only` 服务与 SSH 转发，在本机 `http://127.0.0.1:8188` 访问。也可以显式选择 `--public` Cloudflare 临时公开网址，或 `--allowed-email` 邮箱登录；不会自动公开服务。SSH 模式需要专用密钥，创建密钥必须由你明确选择。

这是独立的基础设施工具，不依赖 `video-render-lab` 的 Render API。2026-10-05 已确认四个模型均从 VM 本地磁盘加载，H3 音视频生成、输出保存及释放 G4 后从 Drive 取回通过。最新整套 40.07 GB 计时：先复制、落盘再本地 SHA 共 439.911 秒，其中本地 SHA 26.875 秒；同机随后同步复制校验共 367.357 秒。固定执行顺序和暖缓存影响比较，不能承诺固定提速。前轮同 VM receipt 元数据复用为 0.055 秒。

TUI 完整启动与 `X` 释放已有实测，新增彩色界面另有离线 Demo 和布局回归。**同一 CPU 上 SSH 和临时公开入口均通过 IAB 浏览器导入、Run 和图片预览；该轮 SSH 加载更快。后续用户日常 Chrome 手动打开 SSH 入口正常，受控 Chrome 标签仍被客户端拦截；邮箱模式登录尚未通过。** 浏览器测试使用无模型 PNG；H3 用 G4 API 验证。详见 [实测记录](docs/test-report.md) 与 [H3 调查](docs/colab-h3-research.md)。

## 准备

- 本机 Linux。启动器脚本与离线测试支持 Python 3.10+；官方 `google-colab-cli==0.7.4` 的独立工具环境需要 Python 3.12+。先完成 CLI 的 Google OAuth 登录。
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

界面采用暖橙强调色、青色模型卡片、状态颜色和字节进度条；宽屏左右分区，80×24 紧凑排列，`Tab` 可查看四个模型的完整文件名与进度。仍使用 Python 标准库，无需安装额外 TUI 依赖。

先看离线界面，不分配 Colab、不访问凭据、不打开浏览器：

```bash
python3 scripts/dashboard.py --demo
python3 scripts/dashboard.py --demo --demo-state ready
python3 scripts/dashboard.py --demo --demo-state error
```

Demo 只允许 `Tab`、翻页和 `q` 等浏览操作，创建/启动/释放按键均禁用。彩色界面要求交互终端与 `TERM` 支持；`--no-color` 或 `NO_COLOR` 环境变量关闭配色。`TERM=dumb` 或 curses 初始化失败时退回无 ANSI 的文字交互，非交互 Demo 只打印一次快照。

启动 `dashboard.py` 后，按 `n` 新建 G4，或按 `e` 选择一个已有会话。按 `c` 配置专用 Drive 目录、访问方式和本机 SSH 端口；默认目录是 `/content/drive/MyDrive/colab-comfyui-launcher-test`，可改为自己的专用目录。

按 `f` 执行完整启动流程：确认配置、人工挂载 Drive、部署脚本、安装并等待完成、下载缺失的模型缓存、复制并校验到 VM、启动 ComfyUI；默认 `local-only` 模式随后启动 SSH 转发。界面显示安装阶段、每个模型的 download/copy/hash 字节进度、速率、服务状态和访问地址。授权提示保留在真实终端和浏览器中，不捕获验证码或认证链接。按 `o` 打开就绪的访问地址。

SSH 默认密钥路径是 `~/.ssh/colab_comfyui_launcher`。该密钥缺失时，配置会询问是否在 SSH 启动时创建；只有明确回答 `yes` 才创建专用无口令 Ed25519 密钥。已有密钥不会被覆盖；已有加密密钥不会被修改，后台转发不支持其交互口令输入。

更改本机 SSH 端口或密钥前，界面会实时检查旧配置的转发。已运行或状态无法确认时拒绝修改；先用 `H` 关闭原转发，再重新配置。切换访问模式也需先停止已运行服务，完整流程不会把旧服务健康当成新启动成功。

| 按键 | 操作 |
| --- | --- |
| `n` / `C` / `e` | 新建 G4 / 新建 CPU / 选择已有会话 |
| `c` / `m` / `f` | 配置 / 人工挂载 Drive / 完整启动流程 |
| `d` / `i` / `w` / `p` / `a` | 部署 / 安装 / 仅下载缓存 / 准备本地模型 / 启动服务 |
| `h` / `H` | 启动 / 停止本机 SSH 转发 |
| `o` / `r` / `t` | 打开浏览器 / H3 API 渲染 / PNG smoke 测试 |
| `Tab` / `PgUp` / `PgDn` | 切换菜单与详情 / 翻页，查看完整模型名称和进度 |
| `s` | 停止转发和服务，保留 VM |
| `X` | 停止转发和服务，并释放选中的 VM |
| `q` | 退出界面，保留 VM、已启动服务和转发 |

自动状态刷新期间可排入一次手动操作；完整流程等待期间可按 `s` 或 `X`，中断后续启动并串行清理该流程原会话。正在执行的短 CLI 命令会先返回。`q` 不释放计算资源；进行中的远端任务也可能继续运行。完整流程出现失败或超时会停止协调，不自动重试未知状态的操作。返回界面查看状态后再处理，使用 `X` 释放自己创建或明确获授权停止的会话。

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
