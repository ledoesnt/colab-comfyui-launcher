# 实测记录

## 2026-10-09 创建入口优先与 Colab 账号管理

首页恢复 **Create a new runtime** 为第一项，账号入口改为 **Manage Colab account**，放在 **Exit and keep resources** 前。账号页标题与未登录、退出后的入口提示同步使用新名称。账号管理仍提供登录状态检查、登录、切换账号与退出。

保留既有登录前置流程：未登录时直接选择新建会进入账号页，用户选择 **Sign in** 并通过验证后继续新建配置，无需先单独打开账号管理。既有认证回归验证这一跳转及登录后回到硬件选择，且未提前创建 VM。

625 项 unittest 全部通过，bootstrap shell 语法与 diff 检查通过。另用真实本机 PTY 运行生产文字界面，以拒绝非只读请求的本机 CLI fixture 核实首页顺序、选择新建进入账号页、Back 返回及正常退出。本轮没有登录真实 Google 账号、创建或操作 Colab 实例，fixture 与离线测试不作为云端验收证据。

## 2026-10-08 登录首步、账号切换与完整模型详情

首页将 Colab login 放在首项，流程栏加入 Colab 节点；小窗已登录首页只通过节点表示完成，不重复账号成功标题及正文。已确认的计算与存储选择使用 `#F2FF59`，当前步骤使用 `#E77012`，但确认配置不等于证明 VM 已分配或模型已就绪。配置摘要的 Runtime、Compute、Storage、Models、Parallel downloads、Browser、Drive directory 和 SSH key 字段名黄绿，具体值白色；值换行后的冒号不会被误当作另一个字段名。

模型菜单按可用终端宽度保留名称前缀并以 `...` 结束，显示缩略不会改动清单路径。大小窗口共用选中模型的完整 Name、Path、大小、清单和自动下载状态；宽屏显示在右侧，小窗显示在菜单下。正文超出视口时，只在底部键盘行提示左右翻页，输入页提示 Ctrl+B/F；不恢复 Details 计数、正文标题或额外提示行。菜单、输入与固定的 ABOUT THIS CHOICE 帮助不会随详情翻页执行操作。

按用户提供的旧界面恢复实心块 + 浅纹理进度条：阶段标题蓝色，进行中橙色，明确 SHA 验证或 receipt 复用后绿色；字节达到 100% 但最终验证未完成仍为待确认状态。宽屏逐文件显示百分比、大小与速率，小窗保留六条紧凑进度。Unicode 不可用时保留 ASCII fallback，未知总量不伪造完成百分比。

Colab login 新增 Switch account 与 Sign out，进入独立确认页并默认 Cancel。官方 CLI 0.7.4 没有本机 logout 子命令；生产后端先核对精确版本，再限定处理标准本机 OAuth 缓存，拒绝符号链接/非普通文件，不读取凭据，不撤销 Google 授权或退出浏览器。成功清除后撤销旧账号的列表、已选 VM 与 SSH 就绪证明，实际 VM、服务、Drive 和转发仍保留。切换随后进入既有控制子 PTY，再只读验证；无法从会话请求证明具体邮箱。缓存已经删除却在最终检查时被重建或发生错误，会撤销旧证明但显示未核实，不宣称退出成功或开始新授权。清除前失败、取消确认保留原证明；Back 后迟到的清除事实仍需撤销旧证明，迟到授权不得抢回页面。

验收在 Git 外复制脚本与模型清单，用真实 controlling PTY 操作生产向导和 Worker，并用隔离 profile 的实际本机 fake CLI 子进程验证退出、重新登录、切换及 /dev/tty 输入。截图覆盖首页、确认/取消、真实后台 Loading...、账号结果、GPU/存储选择、字段配色、120×36 / 80×30 / 80×24 / 50×24 缩放、完整模型详情、实际勾选保存与恢复、左右及 Ctrl+B/F 翻页、占位符消失、Back 与退出。六模型进度另用明确标识的 OFFLINE DEMO。33 张终端截图与逐步 Markdown 保存在 Git 外 `colab-comfyui-test-output/account-model-ui-20261008`，未保存授权画面。

本轮未操作真实 Colab OAuth 缓存、查询或变更用户 VM、分配 GPU、挂载 Drive、调用模型元数据 API 或下载权重；隔离 CLI 登录结果只证明本机协调与界面路径，不构成真实 Google OAuth 或云端 H3 验收。最终 625 项 unittest 通过，含 27 项账号后端、18 项账号 UI 、7 项模型详情/颜色测试与 3 项进度样式测试。Ruff check / format、bootstrap shell 语法、skill 校验与 diff 检查通过。正常界面验收进程均通过自己的退出选项退出，只清理本轮自有本机进程。

以下分轮保留真实执行证据；最新验收见首节，历史轮次中的“未执行”只描述当时状态。

## 2026-10-08 底部选择说明与方向键翻页

ABOUT THIS CHOICE 与正文固定在屏幕底部，采用蓝色帮助样式；从滚动详情中移除该块，大小窗口均只显示一次。模型路径、大小和自动下载标志保留在主要详情区，Enter 操作说明只在底部显示。数字行号范围不是任务进度，本轮移除 Details 1–4/4；详情全部显示时不额外提示，超出可见区才显示向前或向后翻页的提示。

↑/↓ 仍只选择菜单、Enter 确认；←/→ 翻看详情，PgUp/PgDn 保留兼容。输入页不将左右键解释为翻详情，改提示 Ctrl+B/F；该组合键也适用于其他页面。实际检查输入页的 Ctrl+F 向后、Ctrl+B 返回，网址保持不变且没有提交。翻页步长按实际可见行数计算并保留一行重叠，避免短窗口固定跳八行而漏掉内容；翻页末尾钳制，返回新页面清零。普通文字回退仍在菜单后提供选项说明。

本机最终 570 项 unittest 通过，包括 11 项新的底部帮助/键位回归；Ruff check / format、bootstrap shell 语法和 skill 校验通过。测试覆盖固定底部蓝色帮助、不重复指令、短/空详情无数字计数、左右键无执行副作用、输入不冲突、翻页边界以及每一行诊断均可读到。既有 80×24 六模型首屏、添加模型来源及目录、导航和授权测试继续通过。

实际 curses 子 PTY 在 Git 外隔离副本操作，Colab CLI 只允许本机只读 sessions fixture，拒绝其他请求。实际选择 View existing runtimes、在 80×24 / 120×36 / 50×24 间调整窗口，进入模型列表，按左右键翻页并确认选项与底部帮助不变；再进入网址输入并检查左右键不改变已输入内容。六模型展示另用明确 OFFLINE DEMO fixture：80×24 首屏保留六条进度；50×24 需要翻页查看全部模型，不作为六条同时可见的证明。图片、逐步 Markdown 和终端文本保存在 Git 外 `colab-comfyui-test-output/footer-ui-20261008`。

本轮没有调用真实 Google / Colab、创建或修改云端实例、挂载 Drive、查询模型 API 或下载权重。测试进度与登录结果是隔离 fixture，只证明本机界面行为；结束后只关闭自有本机测试 PTY，不释放用户 VM。

## 2026-10-08 TUI 说明去重与返回导航

紧凑布局原先先添加登录、选项和授权摘要，再拼接完整详情，导致登录正文、ABOUT THIS CHOICE 和模型来源重复。本轮按语义块省略已显示内容，保留其他状态与分页；登录状态已包含的 notice 不再再次列为 Next / status，空闲页也不重复显示 Now 加当前标题。没有对不同模型的同名阶段作全局字符串去重。

首页统一为一个 Colab login 入口。已验证账号页面显示 Recheck login 与 Back；未验证时明确区分只读 Check login status 和交互 Sign in。Manage models 不再附带后缀。可回退的计算、GPU、存储、设置、模型目录、模型添加、实例列表和登录页统一 Back；提供方确认、Exit、结束 VM 的确认保留各自含义。返回会清除上一页的导航提示，模型添加返回分类和网址时保留已填值。

实例列表、检查、登录检查和模型元数据等待页显示不可执行的 Loading...，方向键跳过该项，Back 保持可用；未知等待页不再暂用 Exit 菜单。返回后迟到的结果不能重开原等待页；取消另一实例的检查会恢复原实例配置及已验证状态，迟到结果不能覆盖原实例。已经确认的本机模型元数据保存可继续，结果更新清单而不抢回页面或清空后续表单。真实启动期间的退出保留和结束 VM 仍是明确的独立操作。

大小窗口共用步骤状态与颜色：已验证项黄绿、当前项橙色、待办和跳过项灰色；最终 Ready 的已完成步骤保持黄绿。空闲首页不会根据上一任务的 stage 误高亮模型阶段。紧凑布局增加 STARTUP FLOW、连接箭头和上下留白；50 列窗口自动折行。模型下载的六行首屏进度仍保留。窗口尺寸变化后进行完整重绘，保留色彩和当前选项，不触发提供方操作。

验收使用确定性的导航及终端帧回归，并在 Git 外隔离脚本和清单，以非 Demo 向导和真实 controlling PTY 实际操作。只读 Colab CLI fixture 可以延迟四秒回复，用于捕捉实际等待帧与返回后迟到结果；拒绝所有实例变更请求。验证首页、登录检查和 Back、新建流程各页的 Back、模型入口和添加表单回退、实例列表等待、完成后的列表及返回、同一 PTY 的 80×24 / 100×32 / 120×36 缩放。六模型进度及最终步骤色另用明确标记的离线 Demo fixture。截图和逐步 Markdown 保存在 Git 外 `colab-comfyui-test-output/navigation-ui-20261008`。

本轮没有登录真实 Google 账号、查询或操作用户实例、分配 GPU、挂载 Drive 或下载权重。以上证明界面与本机协调行为，不是新一轮云端渲染验收。

真实等待操作额外发现：登录检查中 Back 后，后台结束但账号仍残留 checking。修复为立即撤下检查状态、显示明确取消与未核实说明；不把取消的检查当成新的登录证明。新增真实 Worker 加延迟本机假 CLI 的整合测试，等待实际 busy=false，再检查账号不为 checking、页面仍在原处、原 VM / SSH 信息保留；实际 PTY 截图再次验证通过。失败帧另留 Git 外作诊断证据。

最终 559 项 unittest 通过，包括 19 项新增导航测试、22 项认证测试与 18 项紧凑布局测试；没有跳过。Ruff check / format、bootstrap shell 语法、skill 校验与 git diff 检查均通过。只清理本轮拥有的本机测试子进程，不释放用户 VM。

## 2026-10-07 模型清单交互、并发下载与登录前置检查

### 改动与验收范围

默认正式下载器同时处理两个文件，`--workers 1–4` 可配置；Drive 模式并发下载缺失文件，随后顺序复制并同步 SHA 到 VM。本轮没有分配新 Colab 实例、实际登录 Google、读取 Drive、下载真实 H3 权重或触碰用户正在运行的实例。2026-10-06 的真实两文件并发基准仍是独立样本，不能视为新版正式下载器在 Drive FUSE / Hugging Face HTTPS 上的云端验收。

正式下载器测试使用实际本机 HTTP 服务器及文件子进程，将 HTTPS 来源验证限定替换为本机 fixture。验证默认两路、单路及四路下载发生实际重叠，并检查 Range 恢复、错误 SHA、工作进程退出、阻塞连接的整体 deadline、完成文件保留、未完成进度与自有子进程回收。没有把假响应计时当作 CDN 吞吐，也没有验证 TLS、云端带宽或大文件 Drive 写入。

完整清单先检查格式、大小和路径冲突，再按严格布尔 `auto_download` 筛选；禁用项目仍参与冲突检查。默认六个内置文件明确设为 true。全取消可以启动编辑器，但不证明 H3 工作流所需权重可用。配置的并发数属于当前 TUI 配置，重新打开默认恢复为两路；模型勾选和添加则持久保存在本机清单。

### 实际本机终端操作与公开元数据

在 Git 外复制脚本和模型清单，以实际 controlling PTY 运行非 Demo 向导。Colab CLI 使用仅允许只读 `sessions` 的隔离 fixture；任何创建、停止或部署请求都会被拒绝。没有使用真实账号状态或授权截图。

实际上下键和 Enter 操作依次完成：打开模型管理、看到六项默认勾选、取消可选风格 embedding、粘贴公共 Hugging Face 文件网址、选择 embedding 目录、查看确认页、保存新文件、退出并重新启动。再次进入模型管理后，取消的选择和新增文件均恢复；随后再添加一个 embedding，并将窗口从 80×24 调整到 120×36 检查双栏。屏内添加只查询实际公开 Hugging Face API 元数据，不取回权重字节；固定 commit、发布的 LFS SHA256 与大小，写入独立的 `extra-added-*.json`。所有清单编辑只发生在隔离副本，仓库中的六项默认选择保持启用。

本轮读取并保存了官方仓库的 `minimaxh3_blooming_flowers.safetensors` 和 `minimaxh3_bullet_time.safetensors` 的公开元数据。追加到带 `total_size_bytes` 的清单时会更新总量；完整拟写清单在原子替换前验证，失败保留原文件。测试还实际构建并解包部署 archive，确认新增文件与禁用标记随清单传递给后续准备。

80×24、80×30、100×28 与 120×36 的六文件进度截图来自实际 curses PTY 的**离线 Demo fixture**。这些图用于验证首屏布局、百分比、阶段、详情翻页、上下选择和 Enter 行为，不代表真实下载进度。窗口不足 110×32 使用紧凑布局，至少 50×24；足够大的窗口保留双栏。GPU 标签说明项目的 H3 测试覆盖，不因创建实例成功自动变化。

### 登录测试与结果

首页先执行官方只读会话查询形式的检查，登录验证通过才允许创建、查询或接回实例；网络异常显示未核实。模型管理可以独立使用。登录授权留在屏内子 PTY，敏感输入遮罩并需 Enter；CLI 退出成功后仍需再次只读验证。创建前强制重新检查，取消不会创建或停止 VM。

21 项独立认证测试使用假 CLI 加真实 `/dev/tty`，覆盖未登录、已有登录、网络错误、授权成功/失败、code 输入、取消、超时、迟到事件与退出成功但身份仍未验证。没有在此轮再次完成真实 Google OAuth。

最终 533 项 unittest 通过，含 60 项下载器、43 项模型准备、14 项模型目录和 12 项紧凑布局测试。Ruff check / format、bootstrap shell 语法、skill 校验与 git diff 检查结果在发布前核对。操作截图、终端文本和逐步 Markdown 均保存在 Git 外的 `colab-comfyui-test-output/model-ui-20261007` 与 `compact-layout-20261007`，没有提交授权数据、模型权重或运行时文件。本轮只清理自己的本机测试 PTY / HTTP 进程，用户实例保持原状。

## 2026-10-06 官方 I2V 模板、追加模型与启动状态复验

### Drive FUSE 写入关闭后的元数据变化

本轮 Drive 缓存中新权重的下载曾报告 `Partial changed during download`。检查发现，下载器在 `flush/fsync` 后保存写文件描述符的 stat，关闭描述符后再与路径 stat 比较全部字段；Google Drive FUSE 会在写入关闭阶段更新元数据，这种合法变化可能被误认为文件遭到替换。

在同一本轮已创建的 G4 上，仅向专用 Drive 缓存目录写入两个独有 `.stat-probe-*` 临时文件。分别写入 16 MiB / 48 MiB，每 1 MiB 延迟 0.1 秒，使操作跨越时间戳的秒边界；执行 flush/fsync，并采集写 FD 关闭前的 fstat/lstat、关闭后的 lstat，以及重新只读打开的 fstat。探针上限 45 秒，不分配其他实例、不修改已有权重或 receipt。两个自有探针文件均已删除。

| 真实探针 | 写入及采样耗时 | 关闭前 FD 与路径 | 关闭后路径与先前 FD | 稳定字段 |
| --- | --- | --- | --- | --- |
| 16 MiB | 1.614 秒 | 路径 mtime 落后于写 FD | 路径 ctime 更新 | regular、device、inode、size 一致 |
| 48 MiB | 4.838 秒 | 路径 mtime 落后于写 FD | 路径 ctime 更新 | regular、device、inode、size 一致 |

重新只读打开的 fstat 与关闭后路径 stat 一致。这证明本轮 FUSE 的写入收尾存在时间戳变化；不能从这两份小探针推断所有 Drive 实现、其他挂载方式或模型传输吞吐。

修复仅用于**已挂载 Drive 中由下载器写入的 partial**：在写 FD 仍打开及关闭后都要求路径仍为普通文件，device、inode、size 与写 FD 一致；这两个写入检查容许 mtime/ctime 的变化。VM 本地文件仍比较完整 stat；只读 hash、Drive 复制源和 receipt 复用检查保持原有规则。完整下载大小、流 SHA256 与固定清单必须匹配后才发布，receipt 记录关闭后的元数据，不另外完整读取一遍刚下载的 Drive 成品。

校验含义没有扩大：`stream_sha256` 验证本次收到并写入的完整字节流，不是关闭文件后再做一次完整 Drive SHA。receipt 元数据复用也不是重新内容校验或抵御同账号恶意修改的保证。后续从 Drive 复制到 VM 时，仍对实际复制字节进行完整同步 SHA。

若一次异常已经留下满长 partial，下次恢复必须完整读取这个 partial 重建 SHA，因为上次进程的内存 digest 已丢失；不重新下载完整文件，也不能安全地跳过这次异常恢复校验。普通新下载不会因此新增完整 Drive reread。

本地离线回归：48 项下载器测试与 39 项模型准备测试通过，Ruff check / format 通过。新增回归覆盖 Drive 写入时间戳变化后仍验证完整流、不进行第二次 SHA 读取、VM 本地时间戳变化拒绝发布、device/inode/size 变化拒绝，以及未挂载 Drive 时不放宽比较。这些回归不访问真实模型主机，不能代替云端模型恢复验收。

### 同一 VM 的模型传输与并发样本

在同一本轮 G4 上，对固定清单的六个文件实测 HTTPS 顺序下载到独有 VM 临时目录。每个文件边写入边计算 SHA256，匹配大小及清单 SHA 后执行 flush/fsync 与发布；读取块为 4 MiB，与正式下载器一致。六文件共 42,030,547,279 字节（42.03 GB），传输轮次 405.142 秒，含基准控制器开销总计 407.794 秒，轮次吞吐 103.743 MB/s。六文件全部通过完整流校验。

首次正式 Drive 准备已经完成原有四文件的复制及同步 SHA，四文件共 40,073,842,159 字节。随后新增 LoRA 的 Drive 写入收尾失败，因而不能把失败任务的总时长当作六文件 Drive 准备结果。为避免拿不同文件集合比较，下表只对齐原有四文件：

| 同 VM、相同四文件 | 文件用时合计 | 吞吐（十进制 MB/s） |
| --- | --- | --- |
| 正式 Drive → VM 复制及同步 SHA | 792.101 秒 | 50.592 |
| 后续 HTTPS 基准中的同四文件 | 387.167 秒 | 103.505 |

这轮同文件集合的 HTTPS 观测约快 2.05 倍，但执行顺序固定、缓存与网络状态不同，不能称为随机化的冷缓存对照，也不能承诺以后每轮都更快。正式复制包含 receipt 等收尾，独立下载基准不写正式 receipt；两者都没有计入创建实例、Drive 授权、部署、安装或启动 ComfyUI 的时间。此前六文件 HTTPS 的 405.142 秒属于另一集合，不能直接与四文件 Drive 的 792.101 秒作等量比较。

另取 Turbo LoRA 与视频 VAE 两文件，共 4,767,258,184 字节，在同 VM、独有临时目的目录按以下固定顺序实测。每轮仍完整写入、同步 SHA、flush/fsync 和发布，所有文件均匹配清单：

| 实际执行顺序 | 工作线程 | 轮次耗时 | 吞吐（十进制 MB/s） |
| --- | --- | --- | --- |
| HTTPS 顺序下载，第 1 次 | 1 | 53.226 秒 | 89.566 |
| HTTPS 并发下载，第 1 次 | 2 | 39.347 秒 | 121.158 |
| Drive 复制及同步 SHA | 1 | 17.588 秒 | 271.057 |
| HTTPS 顺序下载，第 2 次 | 1 | 59.032 秒 | 80.757 |
| HTTPS 并发下载，第 2 次 | 2 | 45.020 秒 | 105.892 |

两个并发轮次在这个两文件样本中都比相应顺序轮次短，但提升约 24%–26%，没有达到速度翻倍；不代表六个大小悬殊文件、不同 CDN 时段或其他 VM 的收益。该基准只使用两条普通 urllib 下载流，没有测试多分片或 Xet，也没有把生产下载器改为并发。

**样本 Drive 一轮是暖缓存结果**：正式模型准备已读取这些文件，未清理 FUSE / VM 页缓存，之后才执行该轮。17.588 秒不能当作从远端 Drive 首次读取的速度，也不能据此推断 Drive 通常快于下载。HTTPS 轮次同样未驱逐 CDN 缓存，顺序固定，重复样本也没有消除执行次序的影响。

基准直接下载使用新文件，未测试断点续传、失败重试或 receipt 复用。样本总计 216.202 秒。最终只读核对两份报告均成功、未在运行；两个基准自有 scratch 目录均已删除，后台样本的原 PID 与启动 stamp 已不再对应活动进程。原有 Drive 权重、正式 VM 模型目录和 receipt 未由基准修改。清理这里只指基准临时数据及进程，实例与 SSH 的最终释放另列。完整脱敏 JSON 保存在 Git 外的本轮验收目录。

### 本轮浏览器与最终清理

本轮从真实上下选择 TUI 创建一台 G4，Drive 人工授权后核对 mounted / MyDrive；原四模型复制成功。新增 LoRA 首次下载在 FUSE 收尾误报，修复后显式接回同 VM，恢复满长 partial 的 SHA 并复制，embedding 下载通过。恢复任务约 9.568 秒，原四文件复用同 VM receipt；这是异常恢复和暖缓存结果，不能当全量首次准备成绩。六模型共 42.03 GB 全部进入 VM 普通模型文件，服务注册包括 embeddings 在内的十类目录。

顶部和右侧实测显示当前操作、等待时间与逐文件进度，选择说明采用蓝色独立标题。Advanced actions 的 Prepare / refresh models 在同 VM 实际完成，保留服务及 SSH、复用已验证文件；SSH 停止后左侧就绪状态撤下，继续恢复后自动变绿，无需重新加载 TUI。

在 SSH 本地网址，IAB 最终进入编辑器；Templates → Popular → MiniMax H3: Image to Video，未出现 Missing Models，仅提示输入图片缺失。通过节点上传官方 PNG 后点击 Run，基础模式20步成功；实际点击 turbo_mode 为 true，核对8步，再 Run 成功。基础 / Turbo 服务端执行时间为 74.290 / 19.746 秒。实际提交图的 switch 将 true 分支连向 Turbo LoRA 与8步值，完成记录中的缓存列表不包含该 LoRA 节点；据图和固定后端 lazy switch 逻辑确认使用 Turbo 分支，history 不提供独立 executing-node 事件日志。

两段 Drive 视频分别为 1,126,295 / 1,278,638 字节，存储文件与 `/view` 返回字节和 SHA256 相同。本机及 VM PyAV 均实际解码音视频首帧：H.264 640×640、24 fps、124帧、约5.166667秒；AAC立体声32kHz、约5.166688秒。基础 SHA 为 `a09ee0b57022c89dabe28beb82359d01a0a9b9c1a43c8889321c9cd7f8194735`，Turbo 为 `c61ae5f28ab08e8802834beb1bf894fe877bcb24eff6256a8e33bdfbaa989926`。

实际 Graph 下拉菜单 → Save As 保存命名工作流，服务端专用 Drive 的 `user/default/workflows` 文件为44,078字节，HTTP读取与Drive文件SHA一致。切换空白图后从 Workflows 双击重新打开；停止服务和SSH、Continue重启，再关闭本轮标签、新建标签，从 Workflows再次打开成功。本轮验证同 VM 服务重启恢复；没有创建另一台 VM 复验这个工作流的跨 VM 恢复。Export与Save的去向分别记录，未将浏览器草稿当成持久保存保证。

API文件来自上述成功的基础队列，仅更改启动器示例图片文件名。实际图含未连到输出、缺失输入的草稿节点，渲染器按输出依赖验证必需输入，保留全图节点类型检查。最后一次服务重启后的API回归成功下载/校验示例PNG、通过验证并提交任务，但120秒整体上限内未完成，结果为 `Overall render deadline exceeded`；截止时任务仍可能运行，不能标记API回归成功，也没有因此延长付费实例。默认较长deadline是否完成未由本轮验证。

编辑器首次加载需要几十秒；`ComfyApp graph accessed before initialization` 在本轮不是阻断异常，页面随后实际可运行。受控 Chrome 的 127.0.0.1 / localhost 标签均仍为 `ERR_BLOCKED_BY_CLIENT`，来源未确定；未改扩展、安全设置或权限。没有把IAB成功推为所有浏览器均通过。全量基准最初用前台 kernel subprocess 执行，暂时阻塞官方CLI状态查询；后续样本改用有硬截止的后台进程，TUI查询继续。提供方超时现在显示简洁的结果未知与先检查提示。

约北京时间21:37 owned-session保护计时器关闭本轮 G4；随后重复停止返回该会话不存在，官方sessions为空。本机8188的owned SSH `running`、`http_ready`、`listening_owned`均为false。两基准临时目录和后台进程已结束；保留Drive缓存、输入、视频及命名工作流。本轮截图与逐步Markdown在Git外，授权画面、链接、码未保存。最终470项unittest全部通过、无跳过；Ruff check/format、bootstrap语法、skill校验与git diff检查通过。官方实例列表确认所选VM不存在后，TUI也清除旧运行证明，避免在空列表继续显示已过期的绿色就绪步骤；列表失败不清除上次证明，只保留为待核实状态。

## 2026-10-05 启动向导、屏内授权与两条真实 G4 存储流程

### Question / Goal

按已确认的流程改为上下选择、Enter 确认的完整启动向导：新建或已有实例 → GPU/CPU → GPU 型号 → Drive 或临时 VM 磁盘 → 摘要 → 授权 → 安装与逐模型准备 → ComfyUI 与 SSH → 就绪、退出保留或明确释放。采用 ComfyUI `#F2FF59`、Colab `#E77012` / `#F9AA00`；普通输入高对比，开始输入后占位提示消失，说明和授权留在右侧。本轮使用真实 Colab 验证，离线 Demo 不作为云端验收。

### Environment

本机 Linux、120×36 的真实 PTY，官方 Colab CLI 0.7.4。一个新 CPU 做编辑器、PNG 和恢复回归；两个新 G4 分别测试 Drive 与临时存储，实际 GPU 为 RTX PRO 6000 Blackwell Server Edition、97,887 MiB，远端 Python 3.13.15。GPU 使用已获授权，分别设置 45 分钟 owned-session 关闭上限。Drive 仅访问已有专用缓存和输出目录；四模型清单共 40,073,842,159 字节。本轮没有启动 Cloudflare 或修改 Chrome 扩展权限。

### Steps

1. 从真实向导创建 CPU。上下移动不会创建实例，Enter 后才执行；临时存储跳过 Drive 与 H3，实际部署、安装、启动并建立 SSH。输入端口时对照空字段占位与输入后显示，Esc 留在 TUI 中。
2. IAB 导入 `workflows/smoke-ui.json`，点击 Run，核对 Completed 与 Gallery；实际图片 complete=true、64×64。退出保留后用最终版接回，恢复 CPU / 临时存储；停止服务保留 VM，Continue 只完成缺失步骤，随后明确释放 CPU。
3. 在真实向导分别选择 GPU、G4、Drive / VM disk，确认配置后创建两台实例。Drive 需要人工 Google 授权；用户离开电脑导致首次等待超时，完成授权后显式接回同一实例、重试挂载，实际 mounted 与 MyDrive 检查通过。未重复创建 VM。
4. 真实安装提交和只读状态查询遇到 CLI 超时 / WebSocket 关闭。错误留在右侧；检查远端状态后继续同一实例。只读状态最多尝试三次，写操作不因超时重发。已经运行的模型任务在退出 TUI 后继续，接回后等待原任务，不重复下载或复制。
5. 临时分支直接下载四模型到 VM `runtime/models`；Drive 分支从已有缓存复制到同一 VM 本地模型目录。全部文件都是匹配清单大小的普通文件、非软链接，每个结果均为 `stream_sha256`；本轮不跳过首次内容校验、不额外从 Drive 完整读一次 SHA。
6. 两分支均启动 ComfyUI 与 SSH。本轮 Chrome 成功打开 G4 的 8189 编辑器；Chrome 文件上传被扩展的文件访问权限阻止，保留设置，转用 IAB 完成导入、Run、Completed 与实际 PNG 解码。IAB 也显示随后生成的 H3 视频缩略图。
7. 从高级操作分别实际提交 H3 API 测试。两任务完成后均校验存储文件与 HTTP 返回 SHA、视频/音频元数据及首帧解码；持久输出位于 Drive，临时输出位于该 VM。退出后无 `--ephemeral` / 端口参数接回最终版，正确恢复已有存储、GPU 与 8189，服务没有重启。
8. Ready 显示真实 H3 结果。查看已有实例列表、End VM 的默认 Cancel、取消后仍可访问均实测；随后明确确认释放两台 owned G4，核对提供方会话与本机转发停止。逐步操作和截图保存在 Git 外。

### Result

| 检查 | 本轮实际结果 |
| --- | --- |
| 向导及按键 | 新建、型号、存储、摘要、屏内设置、恢复、已有实例列表和释放有真实 PTY 帧；上下选择，Enter 才执行 |
| Drive 授权 | 子 PTY 留在后台；人工授权、同 VM 超时恢复、实际 MyDrive 检查通过；不保存授权截图或链接 |
| Drive 模型准备 | 四文件从已有 Drive 缓存复制到 VM，同步 SHA 全通过；进度记录总耗时 726.705 秒 |
| 临时模型准备 | 四文件直接下载到 VM，同步 SHA 全通过；进度记录总耗时 305.849 秒；Drive 未挂载 |
| 模型加载 | 两分支的 model-paths 指向 VM 本地模型目录，四文件共 40.07 GB，regular=true、symlink=false |
| 第二模型缓存 | 临时 assets/models 目录为空，模型文件总字节为 0；没有第二份 40 GB 权重 |
| H3 临时输出 | succeeded，50.185 秒含媒体校验，575,001 字节 MP4，保存于 VM |
| H3 持久输出 | succeeded，64.515 秒含媒体校验，575,001 字节 MP4，保存于 Drive |
| H3 媒体 | 两文件均 864×480、24 fps、124 帧，约 5.167 秒；H.264 + AAC 立体声 32 kHz；音视频首帧实际解码 |
| 浏览器 | Chrome SSH 编辑器加载；IAB 真正导入、Run、PNG 解码和 H3 缩略图；不把 API 测试当作所有浏览器模板通过 |
| 退出及恢复 | 模型任务、VM、SSH 保留；最终版恢复专用 SSH 8189 与 key 路径，再校验 HTTP；已有服务不重复启动 |
| 清理 | 本轮 CPU 与两台 owned G4 已释放；官方 sessions 为空；本机 8188 / 8189 的 owned SSH、HTTP、listener 均 false；保护计时器已取消 |
| 离线检查 | 最终 390 项 unittest 全通过；Ruff check/format、bootstrap 语法、skill validation 与 diff 检查通过 |

### Known Limitations

本轮速度来自不同 VM 的 HTTP 下载与 Drive FUSE 复制路径，不能承诺临时模式总更快；此前同 VM 受控复制/SHA 对比仍见后文。网络状态查询可能较慢，TUI 显示最后核实时间及 stale，不保证固定十秒获得新状态。普通失败文字与凭据遮罩已修复，不把 `authorization did not complete` 当作凭据。

两次释放前的远端服务清理命令超时，界面明确保留 Cleanup warning；随后官方 VM 释放与会话检查通过。本机 SSH 先行停止，最终 8188 / 8189 无 owned listener；没有将这次远端优雅清理标记成功，也没有因为清理超时留下收费 VM。首次最终检查时真实 SSH 占用了本地端口，390 项中一项端口测试跳过；释放后重新运行，390 项均通过、无跳过。

只有 G4 与本清单实际验证；其他 GPU、Turbo LoRA、R2V、ControlNet 等模板未验证。用户图中 I2V 缺少约 1.82 GB Turbo LoRA，且尚未选择图片；基础四模型测试并不包含这些附加项。没有自动改变精度或下载更多模型。Cloudflare 的历史客户端拦截与邮箱登录仍未解决，本轮 SSH 成功不能推断它们已修复。

终端 PNG 是真实 PTY 的 cell 状态渲染，网页 JPEG 是真实浏览器截图；它们不是手绘 mock-up。首次捕获器漏处理 ncurses 局部滚动，造成部分早期残影帧；修复捕获器后重新截取，图文说明排除无效帧。终端授权内容保持瞬时内存，不保存到新增截图或日志；官方 CLI 自己的历史记录不由启动器控制。

Inspect 不保证修复所有异常状态：残留 installing、模型控制器 interrupted、旧 SSH 记录缺配置或多个转发需要明确处理，见 [恢复边界](startup-wizard.md)。本轮 CLI 账号已登录；首次账号 OAuth code 路径只有真实本地子 PTY 回归，没有退出用户账号进行云端重登。

### Architectural Decision

使用 Python 标准库的单帧 curses 向导和后台 Worker；授权由独立控制终端承接，主 TUI 不 endwin 切走。新增临时模型准备复用已有下载器及同 boot 验证记录；SSH 恢复仅读取工具私有状态、校验进程与监听所有权，不读私钥、不猜端口。保留高级接口和显式 `--classic`，不改主项目的 Domain 或 Render API。

## 2026-10-05 输入确认、屏内交互与新建 CPU 回归

### Question / Goal

修复 TUI 新建 CPU 后自动刷新报缺少 `/content/colab-comfyui-launcher`，并实际验证操作码必须 Enter 确认、普通操作留在界面内。此前彩色 Demo 验收没有覆盖真实 `C` 创建路径；本轮使用真实官方 CLI 和远端实例。

### Environment

本机真实 120×36 PTY，运行未加 `--demo` 的 dashboard；官方 Colab CLI 0.7.4，远端 CPU / Standard / DEFAULT、Python 3.13.15。测试开始时没有活动会话。第一实例用于完整 CPU 启动，显式 ephemeral / local-only；第二短暂实例只验证最终版创建和部署状态。两者分别设置 25 分钟、3 分钟 owned-session 保护上限，完成后提前释放。未挂载 Drive、下载 H3、分配 GPU 或启动 Cloudflare。

### Steps

1. 只输入 `C`，真实官方 sessions 仍为空；按 Enter 后创建 CPU，自动刷新显示 `not_deployed`，没有目录异常。`C` 的创建已成功，原错误来自随后在尚未部署的目录执行状态脚本，与 Drive 无关。
2. `e` + Enter 在屏内加载真实会话列表；输入序号并 Enter 选择。`c` + Enter 在屏内逐项填写 Storage、Access、CPU、SSH 端口、密钥及缓存审计；选择 ephemeral / local-only / CPU，不经过 Drive 授权。
3. `f` + Enter 在屏内确认配置，实际部署、安装、等待服务启动并建立 SSH。安装期间 Tab 可切换菜单；最终 HTTP、ComfyUI、SSH 都就绪。
4. 第一遍真实测试发现部署后的旧 footer 和未启动时的 Access `None`。保留原始截图，修复后 `q` + Enter 退出保留资源，再用最终版连接同一 CPU；明确核对 VM、服务和 SSH 仍在运行。
5. 最终版 `t` + Enter 执行真实 PNG smoke。另在 IAB 打开 SSH 地址，编辑器加载后实际点击 Run，查看 Completed 和 Gallery；两个任务的 history 都有 execution_success，下载响应都是 64×64 PNG，Gallery 图片 complete=true、naturalWidth/naturalHeight=64。
6. 只输入 `s` 不按 Enter，SSH/HTTP 仍就绪；Enter 后停止服务和转发，官方 status 确认 VM 保留。最终版再次 `f` + Enter，实际完整启动并重新建立 SSH。
7. 只输入 `X` 不按 Enter，官方 sessions 仍包含第一实例；Enter 后串行清理并释放，TUI 留在界面且清空已释放会话。官方 sessions 为空、owned SSH 状态为 false 后取消第一保护计时器。
8. 同一最终版 TUI 再次 `C` + Enter 创建第二短暂 CPU，正常显示未部署；`d` + Enter 仅部署代码。实际状态变成 deployed，旧缺部署提示消失，服务未启动时 Access 显示 local-only。`X` + Enter 释放第二实例，官方 sessions 为空后取消第二计时器。
9. 最终版仅输入 `q` 时程序仍活着；Enter 后正常退出，返回码 0。关闭本轮浏览器标签和 PTY 捕获工具，保留已有 Drive 缓存及 SSH 密钥。

### Commands

以下为脱敏复现方式；每个操作码和每个表单值都需要 Enter。第二条命令是接回已存在的 CPU，不创建实例。

```bash
python3 scripts/dashboard.py
# C + Enter → c + Enter，配置 ephemeral / local-only / CPU yes
# f + Enter → 确认各字段 → 等 HTTP/SSH ready → t + Enter
# s + Enter 保留 VM；X + Enter 释放；q + Enter 只退出界面
python3 scripts/dashboard.py -s "$CPU_SESSION" --cpu --ephemeral
colab --auth=oauth2 sessions
python3 scripts/ssh_forward.py -s "$CPU_SESSION" status --local-port 8188
```

### Result

| 检查 | 实际结果 |
| --- | --- |
| 最终版新建 CPU | C 单独不执行；C+Enter 创建并正常报告尚未部署，无缺目录错误 |
| 屏内交互 | 真实会话选择、设置、完整启动、停止和释放均保留 TUI |
| Enter 语义 | C、s、X、q 单独输入不执行；Enter 后执行，q 返回码 0 |
| 最终版启动 | 同一真实 CPU 停止后完整启动成功，ComfyUI/HTTP/SSH 就绪 |
| PNG | TUI smoke 为 421 字节；浏览器 Run 为 1,576 字节，均为 64×64、history success；Gallery 实际解码 |
| 新实例部署转换 | not_deployed → deployed，缺部署提示消失；Access None 回退为配置值 |
| 留屏证据 | 两次 dashboard 运行的原始 ANSI 只有两对 alternate-screen 进入/退出，均对应启动和明确退出 |
| 图文记录 | 63 张真实 PTY 终端帧、3 张真实网页截图及逐步 Markdown 保存于 Git 外 |
| 清理 | 两个 owned CPU 均释放；官方无活动会话，SSH 停止，两个保护计时器和捕获工具已退出 |
| 离线检查 | 231 项 unittest，包括 69 项 dashboard；Ruff check/format、bootstrap 语法及 skill validation 通过 |

### Known Limitations

终端 PNG 根据真实 PTY 的 ANSI/cell 状态渲染，保留对应文本与原始输出；它们不是桌面截屏，也不是 Demo 或手工重画的状态。网页 JPEG 是实际浏览器截图。原始记录含本次实例信息，只保存在 Git 外；公开记录使用脱敏接口。

本轮未重新验证 G4 创建、Drive OAuth、40 GB 模型准备、Cloudflare、邮箱登录或 H3 推理；这些不能从 CPU 图片测试推断。Drive 授权仍通过提供方要求的真实终端/浏览器交互，属于会暂时切回终端的明确例外。Esc/取消、异步表单输入残留、超时不重试和错误不被自动刷新覆盖有离线回归；没有将它们都描述成真实远端测试。

### Architectural Decision

保留 Python 标准库实现；原切屏是主动调用 endwin 和同步终端输入造成，语言并不要求这种交互。普通创建和释放改为捕获输出的后台任务，配置和会话选择改为界面内表单。状态适配器明确区分尚未部署与执行失败；已有服务记录但控制代码缺失时仍拒绝假装已清理。所有业务动作先输入代码再 Enter，导航键直接生效。未改主 Render API。

## 2026-10-05 四模型先复制后 SHA 与同步 SHA 对比

### Question / Goal

直接测量整套 40.07 GB 模型先复制到 VM、本地再计算完整 SHA256 的耗时；在同一新 G4 上再测复制时同步 SHA，并检查是否有理由取消或调整默认校验。此前 516.032 秒是另一 VM 的同步复制结果，不是这次受控比较的基线。

### Environment

新 G4，实际 RTX PRO 6000 Blackwell Server Edition 97,887 MiB，系统 RAM 189,928,599,552 字节，Python 3.13.15。Drive 已有固定 revision 的四模型，共 40,073,842,159 字节。VM 初始可用磁盘 207,496,339,456 字节，为两个完整副本、小文件补充测试及 8 GiB 余量预检空间。两种方式使用相同 8 MiB chunk、常规文件与源身份检查、显式 flush/fsync 和匹配后原子发布。

只读已知四个 Drive 模型，目标为专有 VM 测试目录；不修改 Drive 权重或 receipt、不重新下载、不安装或启动 ComfyUI、不进行 H3 推理。内部 probe 上限 25 分钟，独立 owned-session watchdog 上限 35 分钟；提前完成并显式停止 G4。

### Steps

1. 用户完成本次 Drive 授权，首次挂载等待超时后重试成功。部署固定清单和私有测试 probe；脚本的本地小 fixture 测试、语法与 Ruff 检查通过。
2. split：先把全部四个模型复制到 VM 的新 `.partial` 并 flush/fsync，复制阶段不计算 hash；四个文件都复制完毕后，逐一从本地完整读取并计算 SHA256，大小/hash 匹配后才发布。
3. inline：使用另一个 VM 目标目录，在每次 Drive read / 本地 write 之间同步 SHA.update；每个文件 flush/fsync、确认大小/hash 后发布。
4. 两种完整方法均成功后，用 605,254,808 字节 audio VAE 在新的目标目录做两组逆序补充：inline→split，然后 split→inline；每次均完整校验。
5. 保存完整结果到 Git 外，并核对两次完整方法均含四个 verified 文件、全部六个 pass 成功。官方 stop 确认 G4 terminated，sessions 查询无活动会话后取消其 watchdog。没有删除 Drive 缓存或 SSH 密钥。

### Commands

以下为实际接口的脱敏示例。probe 为本轮临时计时工具，不是产品启动步骤，不提交私有路径或原始运行日志；正常使用继续执行 README 的 prepare。

```bash
colab --auth=oauth2 new -s "$G4_SESSION" --gpu G4
colab --auth=oauth2 drivemount -s "$G4_SESSION" /content/drive
python3 scripts/colabctl.py -s "$G4_SESSION" deploy
colab --auth=oauth2 exec -s "$G4_SESSION" -f "$PRIVATE_BENCHMARK_LAUNCH" --timeout 60
# 后台 probe 在独立 VM 目录执行有界对比，短只读 probe 读取最终结果。
colab --auth=oauth2 exec -s "$G4_SESSION" -f "$PRIVATE_BENCHMARK_STATUS" --timeout 60
colab --auth=oauth2 stop -s "$G4_SESSION"
colab --auth=oauth2 sessions
```

### Result

| 完整四模型步骤 | split：先复制后本地 SHA | inline：复制时同步 SHA |
| --- | --- | --- |
| Drive read / VM write，扣除 SHA.update | 403.730 秒 | 333.443 秒 |
| 显式 flush/fsync | 9.240 秒 | 13.042 秒 |
| SHA 计时口径与耗时 | 本地完整 read + SHA，26.875 秒 | SHA.update 调用墙钟，19.796 秒 |
| 发布、stat 与状态等剩余开销 | 0.065 秒 | 1.077 秒 |
| 四模型 pass 总耗时 | **439.911 秒** | **367.357 秒** |
| 大小与 SHA256 | 四文件全部匹配固定清单 | 四文件全部匹配固定清单 |

split 的复制加显式落盘共 412.971 秒；随后完整本地 SHA 26.875 秒。逐文件本地 SHA 为 diffusion 16.745 秒、text encoder 8.306 秒、video VAE 1.505 秒、audio VAE 0.319 秒。完整 benchmark 含两次四模型和四次小文件补充，总计 818.097 秒；不包含前面的 VM 分配、授权等待或安装。

两种方法的 SHA.update 调用墙钟分别为 19.745 / 19.796 秒，几乎相等；扣除 SHA 后的复制循环却相差 70.288 秒，其中 text encoder 相差 61.928 秒。这进一步说明总差主要出现在传输/写入阶段，不能全算作校验策略的净收益。

| audio VAE 补充组 | 执行顺序 | 单次 pass 总耗时 |
| --- | --- | --- |
| 1 | inline → split | 3.704 → 2.344 秒 |
| 2 | split → inline | 2.390 → 2.386 秒 |

小文件每次本地 read + SHA 约 0.320 秒，inline SHA.update 约 0.298 秒；显式 flush/fsync 为 1.790–3.159 秒，支配这组总时间。此组不能代表全套模型，反而说明总时间也受落盘波动影响，不能只看 SHA 调用。

### Known Limitations

固定顺序 split→inline，未驱逐 Drive/FUSE 或系统 page cache，也未反转整套四模型顺序。split 的本地 SHA 是刚复制后的读，flush/fsync 不等于冷缓存；第二次 Drive 读取也可能已有缓存。观察到 inline 总计少 72.554 秒，不能全部归因于 hash 安排，不能宣称固定提速百分比。

两列 SHA 定义不同：split 包含本地读取，inline 只包 SHA.update 的墙钟调用时间，后者不是 CPU time。方法间可比较完整 pass 总耗时；不能把这两列当作相同微基准。总耗时从 pass 开始到完成，不含 manifest/磁盘预检和完成后的最终结果落盘，也不是完整 CLI 启动时间。

没有测完整 40 GB 的独立 Drive SHA，没有重复新 VM 冷缓存 A/B，也没有此次重新测 receipt 复用、ComfyUI 加载或 H3。之前的 516.032 秒不能与本轮 367.357 秒拼成确定的版本优化幅度。

### Architectural Decision

保留首次复制时完整 SHA：少一次 VM 本地全读，验证实际复制字节，并避免复制前额外完整读取 Drive。新 VM 的主要工作仍是把全部模型搬到本地。此轮本地 SHA 为 26.875 秒，不支持为了速度取消校验；默认策略与同 VM receipt 复用边界不变。专有测试资源已关闭，结果与限制脱敏记录。

## 2026-10-05 彩色 TUI 与 Chrome 访问上下文复验

### Question / Goal

改善终端状态总览、模型进度和操作菜单的可读性，并区分浏览器客户端拦截与 SSH / ComfyUI 服务故障。界面预览使用离线 fixture；实际网页测试使用独立 CPU，不下载 H3 或挂载 Drive。

### Environment

标准库 curses 界面，真实 PTY 验证 120×32、80×24、窄屏和 `TERM=dumb`。独立 CPU 使用固定 ComfyUI / frontend / cloudflared，显式 public / ephemeral / cpu；同一后台另建 SSH loopback 转发。CPU 设置 20 分钟关闭上限，完成后提前释放。浏览器为受控 Chrome、用户日常 Chrome 和 Codex IAB。

### Steps

1. 给 dashboard 加入暖橙/青色强调、状态卡片、进度条和响应终端尺寸的布局。增加 `--demo` 与 prepare/ready/error fixture，所有创建、认证、浏览器和资源动作均禁用；非交互 Demo 打印一次快照。
2. 在真实 PTY 查看宽屏总览、80×24 菜单与 Tab 模型详情，验证动作禁用与 q 正常退出。无颜色及 `TERM=dumb` 使用文字回退。另用最终终端 cells 回归检查窄屏、完整动作菜单与错误/清理提示。
3. 用自有最小诊断页分别在本机随机端口及 8188 测试受控 Chrome，HTML、本地 JS 与健康 API 都成功。停掉自有 8188 诊断服务，再在同一个地址建立实际 ComfyUI 的 SSH 转发。
4. 同一受控 Chrome 标签普通刷新后显示 `ERR_BLOCKED_BY_CLIENT`；实际 ComfyUI 的 system_stats 和新 Cloudflare 根页面也被阻断。没有读取认证数据、更改扩展或关闭保护。
5. 独立 HTTP 检查实际 SSH 首页为 200，完整读取 20,059 字节；无重定向，响应没有 CSP。SSH ownership / HTTP readiness 通过，远端 PNG smoke 收到 execution_success，API 与临时文件一致。
6. 请用户在日常 Chrome 手动打开同一个 SSH 地址；用户报告“正常看到 ComfyUI 编辑器”。这是用户确认，未把它描述成 agent 观察的 Chrome Queue 测试。
7. 在新 IAB 标签实际导入 smoke-ui，点击 Run，查看 Completed，打开 Gallery；图片 complete 为 true，解码尺寸 64×64。保存测试截图在 Git 外。CPU 未执行 H3 或 Drive 持久化测试。
8. 停止 owned SSH 转发与远端服务，官方 CLI 确认 CPU terminated；随后 sessions 仅剩另行计时的 owned G4。取消 CPU watchdog、停止自有诊断服务，保留 Drive 缓存和 SSH 密钥。G4 清理另见复制对比记录。

### Commands

以下为脱敏复现接口；界面截图、PTY 捕获、自有浏览器 probes 与临时访问地址不进入 Git。

```bash
python3 scripts/dashboard.py --demo
python3 scripts/dashboard.py --demo --demo-state ready
python3 scripts/dashboard.py --demo --demo-state error
python3 scripts/dashboard.py --demo --no-color
colab --auth=oauth2 new -s "$CPU_SESSION"
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" install
python3 scripts/colabctl.py -s "$CPU_SESSION" status
python3 scripts/colabctl.py -s "$CPU_SESSION" start --cpu --ephemeral --public
python3 scripts/ssh_forward.py -s "$CPU_SESSION" start \
  --local-port 8188 --identity "$HOME/.ssh/colab_comfyui_launcher"
python3 scripts/colabctl.py -s "$CPU_SESSION" smoke
# 手动打开 localhost；IAB 导入 smoke-ui、Run、Completed、Gallery。
python3 scripts/ssh_forward.py -s "$CPU_SESSION" stop --local-port 8188
python3 scripts/colabctl.py -s "$CPU_SESSION" stop
colab --auth=oauth2 stop -s "$CPU_SESSION"
colab --auth=oauth2 sessions
```

### Result

| 检查 | 本轮结果 |
| --- | --- |
| 彩色 TUI | 真实 120×32 PTY 输出 orange 208 / cyan 81，状态卡片和四模型进度可见；离线 fixture |
| Demo 隔离 | 创建/释放/打开浏览器等动作禁用，q 正常退出，无外部命令或资源 |
| 终端回退 | 无颜色、TERM=dumb 和非交互 Demo 验证通过；窄屏布局另有最终 cells 回归 |
| 受控 Chrome 诊断页 | 两个端口均通过 HTML / JS / API |
| 受控 Chrome 实际 ComfyUI | SSH 根页面、system_stats、Cloudflare 根页面均客户端拦截 |
| 用户日常 Chrome | 用户手动确认同一 SSH 地址的编辑器正常；未验证该 Chrome 的 Queue |
| 新 IAB SSH 浏览器 | 导入 / Run / Completed / Gallery 通过，64×64 图片实际解码 |
| 远端 PNG smoke | 421 字节 PNG、WS execution_success、API/临时文件一致 |
| CPU 清理 | 转发和服务停止，官方 CPU terminated，取消其 watchdog；自有诊断服务停止 |
| 离线验证 | 204 项 unittest、Ruff check / format、bootstrap 语法和 skill validation 通过；含 76 列菜单回归 |

远端 smoke PNG SHA256 为 `6ae680b12006912199536479a23626a990cec6f0dd3079e4de5ffdf647ab770f`；它与浏览器单独提交的任务不是同一文件。

### Known Limitations

受控 Chrome 和用户日常 Chrome 的结果不同，说明不能把所有 Chrome 访问描述为失败。最小页在同一端口成功也不支持“8188 始终被封”。具体拦截组件、规则或自动化上下文原因未定位；不能仅据错误码认定广告扩展，更不能宣称其已修复。[Chromium 当前错误定义](https://chromium.googlesource.com/chromium/src/+/HEAD/net/base/net_error_list.h)仅表示客户端选择阻断；[官方修订](https://chromium.googlesource.com/chromium/src/+/3de98e520cea74e1f1bdc8a112bb66a8f2a19541)明确区分扩展与其他客户端来源。

本轮没有做新的 Cloudflare 页面性能对比，也没有邮箱正向登录。IAB 成功和用户 Chrome 页面确认分别记录。离线 Demo 的进度与状态均为 fixture，不能作为真实模型复制或 GPU 运行证据；本轮视觉升级没有重新跑真实 TUI 的完整 GPU 启动，原协调流程的实测见前轮，修改后的安全语义有离线回归。

### Architectural Decision

保留标准库实现和原有资源所有权/退出语义，用可关闭的配色和终端尺寸布局改善操作。SSH 为已实测的默认访问方式，用户手动浏览器访问可用；受控浏览器拦截作为独立未解决边界保留。未修改主 Render 项目的接口。

## 2026-10-05 新 G4 本地模型缓存、TUI 与 H3

### Question / Goal

验证 Drive 保存模型、新 VM 复制到本地并在复制时校验、ComfyUI 从本地加载、输出保存回 Drive。测量已有缓存准备与同 VM 复用，并实际操作终端界面的完整启动、状态和资源释放。

### Environment

独立 G4，实际 NVIDIA RTX PRO 6000 Blackwell Server Edition，97,887 MiB；Python 3.13.15、PyTorch 2.11.0+cu130，沿用固定 ComfyUI commit、模型 revision 和四文件清单。Drive 已有前轮完整下载的 40,073,842,159 字节缓存，仅使用专用测试目录。代码、依赖、进度和加载模型位于 VM 本地，input/output/user 位于 Drive。设置本会话 45 分钟关闭上限，完成后提前关闭。

### Steps

1. 新 G4 分配，用户完成 Drive 授权；首次等待授权导致 mount 超时，重试接受已有授权并实际挂载。修正的 bootstrap 在该新 G4 一次安装到 ready。
2. 通过后台 `prepare --download-missing` 读取四个已有 Drive 文件，复制到 VM `.partial` 时同步完整 SHA256；大小与 hash 匹配后发布本地模型并写入 receipts。记录完成时间和逐文件进度。
3. 同 VM 用新 Python 子进程再次运行 prepare，验证四文件均以 `verified_receipt_metadata` 跳过。另对本地 605,254,808 字节 audio VAE 进行完整 SHA256，匹配清单。使用 ComfyUI 的真实 `folder_paths` 与生成的 extra-path 配置，逐一确认四个文件解析到 `/content/colab-comfyui-runtime/models`。
4. 在真实 TTY 操作 TUI `f`：配置、Drive 挂载、部署、安装等待、已在进行的模型准备等待、local-only 启动与 SSH 转发成功。安装/复制时自动更新阶段与模型进度，详情可翻页。
5. 验收时发现旧界面自动刷新期间会丢弃手动按键；修正为允许排入一次操作。并加入 pipeline 等待时 `s`/`X` 取消后续协调、串行清理原会话，确定性测试覆盖准备等待中取消且不再 start。此中途取消行为没有在真实 40 GB 复制中注入故障测试。
6. 确认之前 TUI 渲染按键没有提交任务后，通过 CLI **只提交一次**固定 H3 工作流。history 成功，检查 MP4 首音视频帧、元数据、API/Drive 大小与 hash。H3 渲染本轮经 CLI 验收，不声称经 TUI `r` 实测。
7. 新 TUI 自动 status 运行时按 `X`，成功排队，关闭 SSH 和服务并调用官方 stop，返回 Session terminated；随后 sessions 无活跃会话，forward 的 listener/HTTP 为 false，取消仅属于本轮的 watchdog。
8. G4 释放后，通过独立 Drive connector 获取实际新输出并下载到 Git 外，完整大小与 hash 一致；本地 PyAV 完整解码验证另列 Result。首次 streamed URL 下载返回 HTTP 403，新 fetch 返回的 stream 下载成功，没有转用 inline base64。

### Commands

以下为脱敏的实际操作接口；TUI `f` 包含相关后台命令及等待。复用/hash/path probes 仅用于测试，保存在 Git 外。

```bash
colab --auth=oauth2 new -s "$G4_SESSION" --gpu G4
colab --auth=oauth2 drivemount -s "$G4_SESSION" /content/drive
python3 scripts/colabctl.py -s "$G4_SESSION" deploy
python3 scripts/colabctl.py -s "$G4_SESSION" install
python3 scripts/colabctl.py -s "$G4_SESSION" prepare \
  --download-missing --cache-root "$STORAGE_ROOT/models" --max-seconds 1800
python3 scripts/dashboard.py -s "$G4_SESSION"
# f：配置/挂载后执行完整流程，等待模型复制，local-only + SSH。
python3 scripts/colabctl.py -s "$G4_SESSION" render --max-seconds 600
python3 scripts/colabctl.py -s "$G4_SESSION" status
# 新 TUI 中 X：停止转发、停止服务、释放该 VM。
colab --auth=oauth2 sessions
```

### Result

| 检查 | 本轮结果 |
| --- | --- |
| Drive → VM 复制与流式完整 SHA | 四文件共 40,073,842,159 字节；516.032 秒，全部 copied |
| 同 VM 再次 prepare | 新 Python 子进程计时 0.055 秒；四文件元数据 receipts 匹配并跳过内容读取 |
| 本地 audio VAE SHA | 605,254,808 字节，0.325 秒；完整 hash 匹配，暖缓存读 |
| ComfyUI 模型来源 | 四个实际 folder_paths 解析均在 VM 本地模型根目录 |
| TUI | 完整启动、自动状态、local-only SSH 及 `X` 排队释放实际通过；中途取消由离线测试验证 |
| H3 作业 | API 渲染与输出验证共 58.662 秒；不是纯推理耗时 |
| MP4 | 575,001 字节，H.264 864×480、124 帧、24 fps、5.166667 秒；AAC stereo 32,000 Hz、5.166688 秒 |
| 输出完整性 | API / Drive hash 一致；停止 G4 后独立取回大小、完整 hash 一致 |
| 停止后本地完整解码 | PyAV 19.0.1：124 个视频帧、162 个音频帧，均无解码错误 |
| 资源 | 本轮 CPU 与 G4 已释放，SSH forward 已停止；未停止其他会话或删除 Drive 缓存 |

新输出 SHA256 为 `df92b9467069520c7bb50df39b0e7f2350b6c90a9174cdc3cbd21eb49eaa2cf3`，工作流 SHA256 为 `07a30c993ba61426f6e2943652ebac0c90784d0ac779bad86dee158a64cd28a9`。与前轮固定 seed 输出恰好一致，不保证其他环境逐字节相同。

### Known Limitations

没有做完整 40 GB 的 Drive 与本地独立 hash 受控 A/B；小文件本地暖缓存读不能代表全部模型速度。本轮优化消除了复制前的额外 Drive 完整读取，不取消首次复制流中的 SHA。receipt 只证明先前校验与本次文件身份匹配，不能抵抗同账号恶意修改。新 VM 仍需读取和复制全部模型；Drive 性能没有时限保证。

本轮缓存已经存在，没有真实触发新模型网络下载；缺失下载/HTTP 续传由离线测试与历史下载实测支持。没有比较 BF16 权重、最低显存、峰值显存或画面质量；不能用 58.662 与历史 99.251 秒声称确定的推理提速。浏览器 SSH / Cloudflare 对比单列，Chrome 拦截仍未解决。

最终审查另以离线回归修正了旧服务健康掩盖新启动失败、失败 supervisor 清理旧请求服务、修改 SSH 端口遗漏原转发等边界。失败清理现在绑定本次 startup request 身份；显式用户 stop 继续关闭其 owned 服务。这些故障分支没有再次占用 GPU 验收。

最终本机离线验收：188 项 unittest 全通过；固定 Ruff 0.16.0 的 isolated check / format、bootstrap Shell 语法、skill quick_validate、Markdown 链接/代码块与 git diff 检查均通过。主 Render 项目仅补实验文档，64 项 pytest、Ruff、mypy 全通过，接口未修改。

### Architectural Decision

Drive 作为持久模型缓存与资产存储，VM 本地磁盘作为本次运行的模型加载目录；复制与完整 SHA 共用一次读取流，同 VM 用严格文件身份 receipts 复用。终端界面协调已有基础设施命令，默认 SSH loopback，Cloudflare 仍需显式选择。本启动器与主项目的 Application / Domain、RenderWorker / StorageBackend 契约保持独立。

## 2026-10-05 同一 CPU 的 SSH / Cloudflare 对比

### Question / Goal

对同一个 ComfyUI 后台比较 SSH 本地转发和临时公开 Cloudflare 入口，确认 Logo 等待是否包含网络传输因素，并分别验收浏览器任务。此测试不下载模型、不挂载 Drive、不占用 GPU。

### Environment

独立 CPU，CLI 0.7.4、固定 ComfyUI commit 与 cloudflared 2026.9.3，使用已公开基线 `e798dc6`，保持官方响应压缩开启。显式 public / ephemeral / cpu。SSH helper 使用官方 WebSocket ProxyCommand 与 OpenSSH，监听本机 127.0.0.1:8188，专用 key/known_hosts 与进程归属记录保存在 Git 外。设本会话 20 分钟关闭上限。页面对比使用同一个 Codex IAB；另用本机 Chrome 检查 localhost。

### Steps

1. 新 CPU 一次安装成功，启动 ComfyUI 和公开入口，建立 SSH 转发；helper 同时确认 owned listener 与 ComfyUI HTTP 健康。
2. 为两个 origin 分别打开新的 IAB 标签。SSH 在 47.209 秒观察到编辑器；Cloudflare 在 62.307 和 131.154 秒仍为 Logo，在 206.242 秒观察到编辑器。它们是轮询观察的上界，不能当成精确首次可交互时间。
3. 同机进行两轮短 HTTP 诊断，再进行两轮较暖的请求；完整读取 gzip 正文，记录状态、大小与时间。两条链路收到相同节点信息内容，均为 HTTP 200。
4. 两个页面均通过 Ctrl+O 导入 smoke-ui、Run、Completed 和 Gallery 图片预览。Cloudflare 第二次提交复用了同后台已经计算的工作流，不能据此比较推理速度。
5. DOM 观察到 128 个 modulepreload 链接。两页均记录过 `ComfyApp graph accessed before initialization`，所以不能用该消息单独归因 Cloudflare。
6. Chrome 对新的 localhost 页面也返回 ERR_BLOCKED_BY_CLIENT；具体客户端设置/扩展原因未定位，没有关闭保护或绕过拦截。
7. 显式停止 SSH 转发、启动器服务和该 CPU；CLI 确认 Session terminated，再移除属于它的 watchdog。没有停止其他会话。

### Commands

以下为脱敏复现示例；临时 URL、runtime 身份和认证状态不进入 Git。

```bash
colab --auth=oauth2 new -s "$CPU_SESSION"
python3 scripts/colabctl.py -s "$CPU_SESSION" deploy
python3 scripts/colabctl.py -s "$CPU_SESSION" install
python3 scripts/colabctl.py -s "$CPU_SESSION" status
python3 scripts/colabctl.py -s "$CPU_SESSION" start --cpu --ephemeral --public
python3 scripts/ssh_forward.py -s "$CPU_SESSION" start \
  --identity "$HOME/.ssh/colab_comfyui_launcher" --create-key
python3 scripts/ssh_forward.py -s "$CPU_SESSION" status
# 分别在浏览器打开最新 status 的公网 URL 与 http://127.0.0.1:8188。
# 两页分别导入 smoke-ui.json，Run，检查 Completed 与 Gallery。
python3 scripts/ssh_forward.py -s "$CPU_SESSION" stop
python3 scripts/colabctl.py -s "$CPU_SESSION" stop
colab --auth=oauth2 stop -s "$CPU_SESSION"
```

### Result

| 同一后台请求/操作 | SSH localhost | Cloudflare |
| --- | --- | --- |
| 首页 HTML，4 次完整读取 | 0.535–1.334 秒 | 2.972–3.465 秒 |
| object_info，最初 2 次 | 1.437 / 2.039 秒 | 14.439 / 11.783 秒 |
| object_info，随后 2 次 | 1.499 / 2.338 秒 | 6.856 / 7.741 秒 |
| 入口 JS，2 次 | 0.556 / 0.782 秒 | 2.899 / 2.945 秒 |
| 编辑器观察时间上界 | 47.209 秒 | 206.242 秒 |
| 导入 / Run / Completed / 预览 | 通过 | 通过，第二次任务有缓存 |

节点信息为 1,913,711 字节，gzip 正文 214,746 字节；入口 JS 为 3,234 字节，Cloudflare 压缩正文 1,750 字节。SSH forward 的 listener/HTTP 真实就绪检查通过，停止后两者均为 false。

### Known Limitations

这组结果确认本轮公网路径的响应延迟和大正文传输比 SSH 慢。页面还需要加载许多前端模块，延迟可以累积；这是基于请求时间和 DOM 的解释，不是逐请求的完整关键路径分析。测试含浏览器与诊断并发、不同 origin 缓存及后台工作流缓存，不能承诺其他网络上的固定倍数。CPU 没有 H3 或 Drive，因此这轮 Logo 等待不能归因于 H3 权重下载/校验。

Chrome localhost 仍被客户端拦截；IAB 成功不证明该 Chrome profile 可用。具体拦截原因和邮箱模式登录仍未确诊。无需更换隧道就可以消除所有客户端问题的说法不成立。

### Architectural Decision

加入独立的 localhost SSH 转发与 TUI 访问选择，保留 Cloudflare 的显式公开/邮箱模式。默认 local-only 可减少本轮实测的公网链路开销；它仍经 Colab WebSocket 跨网。服务只监听 VM loopback，SSH 只监听本机 loopback；停止转发与释放 VM 是不同操作。供应商与连接方式继续仅属于基础设施工具。

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
