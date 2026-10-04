# Colab ComfyUI Launcher

通过官方 Colab CLI 启动 ComfyUI，把模型、输入、输出放在 Google Drive，并创建 Cloudflare 临时网址。每次启动必须显式选择 `--public` 公开访问，或 `--allowed-email` 邮箱登录；不会自动开放网址。无需 SSH 密钥，也无需手动进入 `colab console`。

这是独立的基础设施工具，不依赖 `video-render-lab` 的 Render API。已实测 G4 分配、Drive 挂载、ComfyUI 队列与 WebSocket、4 个 H3 文件完整校验、音视频 MP4 生成及停止 G4 后从 Drive 取回同一文件。修正后的 bootstrap 已在另一个全新 G4 一次安装通过。**临时公开入口的公网 API/WebSocket、浏览器导入、Run 和图片预览已通过；邮箱模式的登录尝试未通过。** 浏览器验收使用独立 CPU 的无模型 PNG 工作流；H3 推理由 G4 上的 API 工作流验证。详见 [实测记录](docs/test-report.md) 与 [H3 调查](docs/colab-h3-research.md)。

## 准备

- 本机 Linux。启动器脚本与离线测试支持 Python 3.10+；官方 `google-colab-cli==0.7.4` 的独立工具环境需要 Python 3.12+。先完成 CLI 的 Google OAuth 登录。
- Colab 账号有可用资源。GPU 类型和计费随账号及供应变化；先查看 `colab --auth=oauth2 usage`。Colab 对主要通过 Web UI / SSH 使用 runtime 的限制见[官方 FAQ](https://research.google.com/colaboratory/faq.html)；使用符合要求的付费资源。
- 使用邮箱模式时，准备一个能收取 Cloudflare 登录验证码的邮箱；公开模式不需要邮箱。Google Drive 按提供方实际提示完成人工授权；不保存或重放授权链接。
- 默认仅启用官方 ComfyUI 节点，不安装第三方 Manager 或未知自定义节点。

```bash
uv tool install --python 3.13 google-colab-cli==0.7.4
colab --auth=oauth2 sessions
```

首次 CLI 登录会引导授权。不要把 OAuth code、token、cookie 或 SSH 私钥发到聊天、Git 或 issue。

## 启动

以下命令在本机仓库根目录执行。`SESSION` 选择一个新名称；已有会话请明确使用其名称，不要创建重名资源。

```bash
SESSION=comfyui-g4-demo
colab --auth=oauth2 new -s "$SESSION" --gpu G4
colab --auth=oauth2 status -s "$SESSION"
colab --auth=oauth2 drivemount -s "$SESSION" /content/drive

python3 scripts/colabctl.py -s "$SESSION" deploy
python3 scripts/colabctl.py -s "$SESSION" install
python3 scripts/colabctl.py -s "$SESSION" status
```

Drive 可能在首次使用或新的 runtime 中要求浏览器授权后回车。若等待授权期间出现 `mount failed`，完成授权后再执行一次挂载，并检查实际挂载结果；已有授权被接受时不必重复人工操作。不要根据 CLI exit 0 判断成功。

`install` 返回的是已开始安装。它有默认 900 秒上限，后台记录状态和日志；重复检查 `status`，待 `installation.status` 为 `ready` 才启动。安装失败时检查 VM 上 `/content/colab-comfyui-runtime/logs/install.log`。本机命令结束后，安装和服务仍在 VM 上运行。

以下示例显式选择临时公开网址：**知道网址的人可以操作 ComfyUI、提交任务并访问其输入和输出**。这是用户选择的访问方式，不是邮箱模式失败后自动采用的降级。用完后关闭服务和 runtime。

```bash
STORAGE_ROOT=/content/drive/MyDrive/colab-comfyui
python3 scripts/colabctl.py -s "$SESSION" start \
  --public --storage-root "$STORAGE_ROOT"
python3 scripts/colabctl.py -s "$SESSION" status
python3 scripts/colabctl.py -s "$SESSION" smoke
```

`start` 返回 `starting`；随后 `status` 应有 `http_ready: true`、两个服务存活、`startup.ok: true` 和期望的 `access_mode`（`public` 或 `email`）。打开返回的网址。`smoke` 是不下载模型的真实 `EmptyImage → SaveImage` 测试，会检查本地 WebSocket、history、64×64 PNG 和存储文件校验值；它不是视频模型测试。浏览器按 `Ctrl+O` 导入 [workflows/smoke-ui.json](workflows/smoke-ui.json)，点击 **Run**，检查任务完成与图片预览。首次页面加载可能需要等待；停在 Logo 时不能仅凭后台 ready 判断页面正常。

公开模式还可以在本机检查初始化 API、公网 WebSocket、任务提交及 PNG 下载。它会提交一个临时图片任务，默认总上限 60 秒；返回 `ok: true` 也不能代替浏览器页面验收。把 `PUBLIC_URL` 设为本次 `status` 返回的网址，不要把它提交到 Git：

```bash
uv run --with websocket-client==1.8.0 python scripts/check_public.py \
  --url "$PUBLIC_URL" --max-seconds 60
```

默认持久化目录为 `Drive/MyDrive/colab-comfyui/{models,input,output,user}`。可以给 `start` 添加 `--storage-root /content/drive/MyDrive/你的专用目录`。必须真的挂载 Drive，脚本不会创建一个普通目录来假装挂载。短测试可显式添加 `--ephemeral`，其文件随 runtime 回收消失。CPU 模式给 `new` 省略 `--gpu`，给 `start` 添加 `--cpu`。

需要邮箱保护时，停止已有服务，再以另一种显式方式启动；两个选项不能同时使用：

```bash
read -r -p '允许访问 ComfyUI 的邮箱: ' ALLOWED_EMAIL
python3 scripts/colabctl.py -s "$SESSION" start \
  --allowed-email "$ALLOWED_EMAIL" --storage-root "$STORAGE_ROOT"
```

邮箱模式传递 Cloudflare `--allowed-mail`。未认证页面应重定向到登录页，POST `/prompt` 应被拒绝，未认证 `/ws` 不能建立连接；这些拦截已实测。真实登录尝试在 Codex 内置浏览器发生回调缺少 authentication-state cookie 的错误；独立 Chrome 尝试显示 `ERR_BLOCKED_BY_CLIENT`。具体浏览器原因未确诊，邮箱登录后的成功访问尚未验收。公开模式由用户明确选择，不需要该登录步骤。两种方式都要另行验证页面、WebSocket 和浏览器任务；只有网址出现不代表端到端访问通过。[Cloudflare 官方说明](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

## MiniMax H3

[官方 FAQ](https://design.minimax.io/h3)明确支持 Colab GPU runtime；本项目已核对 G4 实际为约 96 GB 的 RTX PRO 6000 Blackwell。模型文件清单见 [models/h3.json](models/h3.json)，固定 4 个文件的 revision、大小和 SHA256，约 40.07 GB，已真实下载并通过全部校验。[workflows/h3-api.json](workflows/h3-api.json) 已完成真实推理：864×480、124 帧、24 fps、20 步、seed 20261004，生成约 5.17 秒、带立体声音频的 MP4；本轮生成与校验用时 99.251 秒。

H3 使用有地域和商业条件的社区许可证；本项目的 MIT 许可证只覆盖自己的脚本。下载、部署前阅读 [H3 说明](docs/h3.md)。不能单凭云节点名字自动判断使用主体是否获授权；也不能以文件可下载代替许可证说明。H3 基础权重与托管的 2K 流程有不同边界。

部署代码、挂载 Drive 后，下面命令仍在**本机仓库根目录**执行，下载到与服务相同的模型目录，不需要进入 console：

```bash
python3 scripts/colabctl.py -s "$SESSION" download \
  --models-root "$STORAGE_ROOT/models" \
  --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

`download` 启动后台任务；通过 `status` 等待 `model_download.running: false`、`model_download.status: succeeded` 和 `model_download.result.ok: true`。下载器保留 `.partial` 以便续传，校验完整大小与 SHA256 后才发布文件，合格文件会跳过。一次只让一个 runtime 写同一模型目录。自定义 `--storage-root` 时，把该目录的 `models` 子目录传给 `--models-root`；不要下载到另一套目录。

模型全部校验、G4 上的服务就绪后，在本机启动部署的 H3 API 工作流：

```bash
python3 scripts/colabctl.py -s "$SESSION" render --max-seconds 900
python3 scripts/colabctl.py -s "$SESSION" status
```

桥接会在 VM 上调用 `scripts/render_workflow.py`，提交真实工作流，跟踪队列和 history，检查首帧解码、分辨率、帧率、时长、音视频轨道及 API/存储文件校验值，并记录容器报告的帧数。等待 `render.running: false`、`render.status: succeeded` 和 `render.result.ok: true`；最初的 `started` 不表示视频已完成。超时或失败后先检查状态，不要重复提交未知状态的任务。

本轮释放 G4 后，通过 Drive connector 再次下载生成的 MP4，SHA256 与运行时校验值相同。这证明该实测文件在停止计算资源后仍可取回；未验证长期稳定性、并发写入或所有 Colab 镜像。

## 关闭与复用

```bash
python3 scripts/colabctl.py -s "$SESSION" stop
colab --auth=oauth2 stop -s "$SESSION"
colab --auth=oauth2 sessions
```

第一条只关闭属于本启动器的服务，第二条释放 Colab 计算资源。关闭终端不等于释放 runtime。只停止本轮创建或明确获授权停止的会话；不要批量关闭用户其他会话。停止服务不会删除 Drive 文件。

同一 VM 可用 `status` 检查、用 `stop → start` 重启服务。新的 VM 要重新部署代码和依赖、挂载 Drive；保存在 Drive 的资源可以复用。Colab 回收、空闲期限和隧道 URL 都不提供永久在线保证。临时网址每次重启可能变化；固定域名和机器调用应另行设计命名 Tunnel 与服务身份认证。

## Codex skill

仓库包含 [.agents/skills/colab-comfyui/SKILL.md](.agents/skills/colab-comfyui/SKILL.md)。在仓库中启动 Codex，可调用 `$colab-comfyui`，或让它读取该文件。技能会检查已有会话、按授权使用 GPU、保留人工授权、验证结果并清理其创建的资源。也可以把本仓库交给其他能读 Markdown 并执行本地命令的 agent。[Codex 官方技能文档](https://learn.chatgpt.com/docs/build-skills)

## 开发与边界

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/bootstrap.sh
```

ComfyUI 源码 commit 与 cloudflared 二进制/校验值固定；其余环境仍依赖 Colab 镜像。安装复用系统 PyTorch，本次实测为 Python 3.13.15、PyTorch 2.11.0+cu130。新增包的记录在 [tested-overlay-requirements.txt](docs/tested-overlay-requirements.txt)，它是观测记录，不能称作完整环境锁文件。后续版本变更必须重新验证。

代码、虚拟环境、二进制、PID、日志位于 `/content/colab-comfyui-runtime`；Drive 保存资产。公开仓库不包含邮箱、OAuth 链接、session 凭据、日志、模型、生成媒体或第三方 skill。模型清单和工作流本身不自动证明推理成功；本项目的成功结论来自实测记录。
