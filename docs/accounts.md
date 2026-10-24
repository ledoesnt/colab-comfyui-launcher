# 多账号登录与候补启动

启动器可以保留多个 Colab 账号的本机登录，手动选择账号，或在**新建实例之前**发现当前账号余额显示为零时按顺序选择候补账号。切换保存的账号不会删除登录，也不会停止原账号的 VM、SSH 或预算守护。

## 在终端界面配置

运行 `python3 scripts/dashboard.py`，进入 **Manage Colab account → Saved accounts and fallback**。

1. `default` 使用原来的本机 Colab CLI 登录，不迁移、不复制原凭据。如果现有登录就是账号 A，可以直接把它当作 A 使用。
2. 选择 **Add a Colab account**，输入本机别名，例如 `account-b`。别名允许 1–32 个英文字母、数字、`_` 或 `-`，不是邮箱，也不用于证明 Google 身份。
3. 回到账号页选择 **Sign in**，在 Google 页面选中 B，按官方流程完成授权。浏览器可能默认上一个账号，请核对页面中的账号。授权 code 只在 TUI 遮罩输入，不发送到聊天。
4. 两个登录都保存后，在列表选择 **Use default** / **Use account-b** 即可切换。授权仍有效时不需要再次登录；Google 撤销或刷新失败后需要重新授权。
5. 勾选 `account-b as fallback`，将 **Automatic fallback** 改为 **ON**。多个候补按勾选顺序编号；取消再勾选会移到队尾。设置自动保存，初始默认关闭。
6. 回到首页，选择 **Create a new runtime**，照常确认硬件、存储、访问模式和本次预算。开启候补后先查询当前账号实时 CU 余额；显示为零则查询候补，找到正余额后只向选中的账号提交一次创建。成功后继续该账号下的部署、模型准备和 ComfyUI 启动。

**Replace login in this account** 是删除当前配置缓存后重新授权的旧操作。要保留两个登录，请使用 **Saved accounts and fallback**。**Sign out** 也只清除当前配置的登录；当前账号有未释放预算实例时，这两个删除凭据的操作仍会被阻止。选择另一个已保存账号则可以进行，因为旧守护仍持有原账号上下文。

## 候补的边界

- 只有成功解析的**实时余额为零**才继续下一个账号；余额查询失败、需要登录、网络错误、供应不足、GPU 权限错误或创建超时均不会被当作零余额。所有候选都是零时不创建实例；任一候选状态未知则停下核查。
- 正余额不能保证所选 GPU 有供应或权限，也不保证足以覆盖整个实例预算。预算仍是每次分配的独立保护，不会因为换账号而重置旧实例计时。
- 创建一旦提交，不会因失败或结果未知自动尝试其他账号。存在同名未完成预算记录时留在原账号核查，不跨账号重复分配。
- 这是新建时的候补选择。不会在已有 VM 运行期间自动启动替代 VM，也不会迁移正在运行的视频任务、内存状态或 ComfyUI 队列。再次新建会重新安装/恢复环境、准备模型和启动服务。
- 不同账号的 MyDrive 不会自动共享。Drive 模式需要按提供方流程授权，并事先处理模型、输入、输出和工作流的共享/复制。VM disk 模式在新 VM 重新下载模型；原 VM 释放后其临时文件不保留。
- 自动候补不会释放旧 VM。旧账号预算守护仍按原截止核查、释放；未配置预算的旧实例需要明确释放。两个账号使用同一本机 SSH 端口时，新转发会提示端口占用，需更改端口或明确停止旧转发。

## 命令行

这些账号设置命令不登录 Google、不创建实例，也不读取凭据内容：

```bash
python3 scripts/accounts.py list
python3 scripts/accounts.py add account-b
python3 scripts/accounts.py select account-b
python3 scripts/accounts.py fallback account-b
python3 scripts/accounts.py auto on
```

`fallback` 是切换勾选，重复执行会移除候补。交互登录仍在 dashboard 中完成。Dashboard 默认恢复上次选中的账号，也可显式指定本次账号和候补顺序：

```bash
python3 scripts/dashboard.py --account default --fallback-account account-b
```

自动化接口必须明确指定实例所属账号，**不会跟随界面的当前账号**：

```bash
python3 scripts/colabctl.py --account account-b -s "$SESSION" status
python3 scripts/colabctl.py --account account-b -s "$SESSION" budget
python3 scripts/colabctl.py --account account-b -s "$SESSION" release
```

`new` 等其他命令也支持全局 `--account`。低层 `colabctl.py` 不自动候补；使用原账号、session 和 request ID 恢复原操作。省略 `--account` 始终使用兼容旧版本的 `default`，不会因为在 TUI 中选过 B 而操作 B。

## 本机保存与隔离

元数据默认在 `~/.local/state/colab-comfyui-launcher/accounts/accounts.json`，命名账号的 CLI 缓存与启动器状态分别在 `accounts/<别名>/cli` 和 `accounts/<别名>/state`。支持 `XDG_STATE_HOME`、`COLAB_LAUNCHER_STATE_DIR`；可用 `COLAB_LAUNCHER_ACCOUNTS_DIR` 单独指定账号目录。目录须为本用户所有、权限 700，文件权限 600；符号链接和异常缓存拒绝使用。不要把这些目录放入 Git。

适配器在独立子进程运行已核实的 `google-colab-cli==0.7.4`，只重定向 CLI 专用缓存路径。凭据仍由官方 CLI 保存、读取和刷新；启动器不解析、复制或交换 token，不改动 HOME、浏览器、SSH 或代理环境。官方 `--config` 只选择会话文件，不能隔离 OAuth 缓存，因此这里同时隔离 token、会话、设置、日志和历史。版本不匹配时拒绝命名账号操作。

每个守护启动命令固定携带实例所属账号，预算记录也记录账号；界面账号偏好不改变已有守护。不同账号可以有同名 session，它们的预算、请求回执及 SSH 状态相互隔离。别名不能替代 Google 身份核验，请勿在一个别名里随意授权成另一个 Google 账号。
