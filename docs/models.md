# 追加与准备模型

## 在 TUI 管理下载清单

首页、启动配置摘要或 Advanced actions → **Manage models · add / select files** 打开模型列表。`[✓]` 表示每次准备都纳入此文件，`[×]` 表示跳过；上下键仅选择，Enter 切换并立即保存。内置六个模型默认勾选，选择写入对应 JSON 的 `auto_download` 布尔字段，后续 TUI、CLI、部署和新 VM 准备都会读取。缺省该字段等同 `true`。取消勾选不删除或隐藏已有文件；仍在 VM 上的文件可被 ComfyUI 发现。关闭必需模型会使新 VM 上的相关工作流缺模型。所有文件均可关闭，用于只开编辑器；这不表示 H3 已具备推理条件。

选择 **Add model · Hugging Face file URL**，粘贴 `https://huggingface.co/owner/repo/blob/main/path/model.safetensors` 或 resolve 文件链接，再选择 loader 要求的目录类别，最后确认。仅查询公共元数据，将 main 等引用解析为固定 commit，获取 LFS SHA256 和大小，保存 `models/extra-added-*.json` 并默认勾选；没有下载模型、创建 VM 或安装节点。添加后回到高级操作选择 **Prepare / refresh models**，或在下一次完整启动时准备。文件名保留原名称，来源路径与本地目录可不同。

目前交互添加支持公开、提供 LFS SHA256 的 Hugging Face 模型文件。私有或 gated 文件、没有可靠 SHA256 的小文件，使用手动核实的清单；不要在网址中附 token。网络或元数据校验失败不改清单。无论是否勾选，所有清单先检查路径、固定来源及目的路径冲突；关闭一个文件不能绕过无效配置。

## 并发和缓存

正式下载默认 2 路文件并发，`download` / `prepare --workers 1` 可回退串行，允许范围 1–4；TUI 在 SSH and storage settings → Parallel downloads 设置当前流程的数量。Drive 模式先并发下载缺失文件，再顺序复制并同步 SHA 到 VM；临时模式直接并发下载到 VM。已验证文件仍按原规则复用。后台主进程统一发布逐文件进度及 receipt；整体截止或单文件失败会停止本批其他文件 worker，保留完整验证成功的文件和可续传 partial，不自动重新提交任务。

自动下载选择会长期保存到模型清单；并发数量是当前 TUI 配置，重新启动 TUI 默认回到 2。生产并发已由真实本地 HTTP 传输验证，不能拿此前的两文件 GPU 基准当作新实现的云端测速。

## 手工编辑清单

默认清单是 `models/h3.json`。把自己的附加文件写到 **`models/extra.json`** 的 `files` 列表；该文件初始为空，不额外下载模型。还可为其他仓库创建 **`models/extra-名称.json`**，例如 `models/extra-sd.json`。使用默认 H3 清单时，启动向导、下载、准备和启动前的本地就绪检查都会自动合并这些清单；`extra-*.json` 按文件名排序。无需为了追加模型修改 Python 代码或重建 VM。

附加清单文件名使用英文字母、数字、点、下划线或短横线，例如 `extra-sd.json`，不使用目录分隔符。默认 H3 清单包含四个基础权重、模板当前选择的 **8 步 Turbo LoRA**，以及一个可选风格 embedding `minimaxh3_art_is_explosion.safetensors`，共六文件、约 42.03 GB。

[官方模板内的 Guide](../workflows/h3-i2v-ui.json) 还列出了 `minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors`，这是 **4 步模式的另一份 LoRA**；默认下载和本轮所选 Turbo 配置使用 8 步权重，不需要同时下载 4 步版本。切换到 4 步方案时，应按模板要求同步调整 LoRA 与采样参数，并在附加清单中固定对应文件；本轮没有验证 4 步方案。

Guide 中的 **10 style embeddings** 是可选风格集合，默认只下载上述一个示例。没有使用风格 token 时不需要其余九个文件；要使用其他风格，可按需追加，并在提示词引用 `embedding:文件名`（去掉 `.safetensors` 后缀）。工作流 JSON、模型权重和输入图片分别保存，追加模型不会补齐缺失的输入图片。

清单按 Hugging Face 仓库组织。每份 JSON 用一个 `repo_id` 和一个固定的 40 位 commit `revision`；`files` 中每个文件都需要确切的大小与 SHA256。不要填写 `main`、临时签名网址或访问凭据。固定 commit 上的 Hugging Face 文件元数据可提供大文件的 LFS SHA256 和大小；未提供时应先独立取得可靠元数据，再填写清单。清单校验只验证格式，实际下载或首次复制还会校验内容。

`models/manifest.schema.json` 可供编辑器使用。下面是需要替换占位值的格式示例，不能直接运行：

```json
{
  "repo_id": "owner/model-repository",
  "revision": "REPLACE_WITH_40_LOWERCASE_HEX_CHARACTERS",
  "files": [
    {
      "path": "loras/my-model.safetensors",
      "source_path": "weights/my-model.safetensors",
      "size_bytes": 123456,
      "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS"
    }
  ]
}
```

`path` 是 ComfyUI 模型目录中的相对位置；`source_path` 是仓库里的相对路径，可省略，省略时与 `path` 相同。比如仓库文件位于 `weights/model.safetensors`，而 ComfyUI 需要它出现在 `loras`，就用上面的映射。当前启动器向 ComfyUI 注册 `diffusion_models`、`text_encoders`、`vae`、`checkpoints`、`loras`、`clip_vision`、`embeddings`、`controlnet`、`upscale_models` 和 `style_models`；文件应放到工作流加载节点要求的类别。不要把 LoRA 放到生成模型目录。

同一目的路径不能出现在两份清单中，即使 SHA 相同也会报错；文件路径和父目录冲突也会在下载、复制前报错。追加清单不会覆盖不匹配的现有模型，不删除用户文件，不替换正在加载的模型。

部署时，桥接工具在上传包内生成 `models/selected-extra-manifests.json`，记录这次实际选择的附加清单。它是自动生成的部署元数据，不需要手工编辑或提交。删除本机某份 `extra-*.json` 并重新部署后，远端遗留的旧 JSON 不再进入本次准备和就绪检查；已经下载的权重会保留，不自动删除。没有部署索引的本机 checkout 仍按 `extra.json` / `extra-*.json` 发现清单。

## 在现有实例追加

在本机仓库修改 `models/extra.json` 或添加 `models/extra-*.json` 后，在 TUI 中选回同一 GPU 实例，进入 **Advanced actions → Prepare / refresh models**，按 Enter。启动器会部署最新清单到该 VM、准备并验证模型，显示逐文件进度；保留现有 ComfyUI 和 SSH，不另建实例、不重启服务。成功后刷新浏览器查看模型列表。CPU 模式会明确提示跳过模型准备。

浏览器刷新适用于运行中的 ComfyUI 已注册相应目录的情况。旧版实例可能缺少新目录注册，或者没有保存目录记录；此时文件仍可准备和验证，但 TUI 会明确要求 **Stop services → Continue missing startup steps**，让新启动的服务注册模型路径。它不会自动重启服务，也不会把文件校验通过当作旧服务已能发现新目录。TUI 模型刷新会在部署、下载前拒绝不在上述十类映射中的目录，需先扩展启动器的目录映射。

正常 GPU 启动和恢复向导也会预先验证本机清单及目录类别；无效清单会在模型传输前停下。新建 GPU 时会在分配实例前检查，CPU 编辑器模式跳过模型清单检查。

已有模型任务正在运行时，该操作只等待原任务，不部署或重复提交；结束后的提示会说明本机清单改动尚未应用，需再选择一次 Prepare / refresh models 才会部署新清单。状态未知、Drive 未就绪或保存的配置不匹配时会停下，先检查同一实例。

也可在命令行用同一个实例部署，再准备模型。Drive 示例：

```bash
python3 scripts/colabctl.py -s "$SESSION" deploy
python3 scripts/colabctl.py -s "$SESSION" prepare \
  --download-missing --cache-root "$STORAGE_ROOT/models" --max-seconds 1800
python3 scripts/colabctl.py -s "$SESSION" status
```

明确选择 VM 临时存储时，准备命令改为：

```bash
python3 scripts/colabctl.py -s "$SESSION" prepare \
  --ephemeral --download-missing --max-seconds 1800
```

确认后台任务 `running: false`、`status: succeeded`、`result.ok: true`，以及 `models_ready: true` 后，再刷新 ComfyUI 编辑器的模型列表。无需关闭 SSH；准备脚本只下载和发布完整、验证成功的普通模型文件。为避免干扰正在执行的生成任务，先等待队列空闲再准备附加文件。如果浏览器列表仍未刷新，刷新网页；服务启动时缺少某种模型目录注册的情况应先检查目录映射，而不能仅靠重下权重解决。

Drive 模式会先保存缺失文件到专用 Drive 缓存，再复制并同步 SHA 到 VM。临时模式直接下载并同步 SHA 到 VM 模型目录，没有第二份权重缓存。已有同 VM 的有效校验记录继续复用，追加一个小模型不会导致已验证的全部 H3 权重重新读取一遍；更换 revision、文件内容或元数据后则按验证规则重新检查。`--verify-cache` 保留完整内容复查功能。

## 多个仓库与独立脚本

每份 `extra.json` 或 `extra-*.json` 中的文件应来自同一份 `repo_id` / `revision`。需要多个仓库时，每个仓库使用单独的 `extra-名称.json`；启动向导和 TUI 的 Prepare / refresh models 会一起处理它们，不需要进入 `colab console`。例如 `extra-sd.json` 与 `extra-upscale.json` 可以分别固定不同仓库的 commit。

独立下载与准备脚本另支持可重复的 `--extra-manifest`，用于没有采用上述自动发现名称的清单。下例在 Colab runtime 内执行，路径指向已经部署到 runtime 的附加清单：

```bash
python3 /content/colab-comfyui-launcher/scripts/prepare_models.py \
  --ephemeral --download-missing --max-seconds 1800 \
  --extra-manifest /content/colab-comfyui-launcher/models/my-other-models.json
```

使用默认 `h3.json` 时仍自动合并 `extra.json` 和 `extra-*.json`；相同附加清单路径只选择一次。`manifest.schema.json`、`h3-i2v` 模板元数据及其他 JSON 名称不会作为模型清单自动加载。用独立脚本显式指定别的 `--manifest` 时，不自动合并项目的附加清单，可用 `--extra-manifest` 明确选择。任何已选择的附加清单无效都会在下载、复制前报错，不跳过坏配置继续运行。追加清单改变的是文件准备范围；节点安装和其他模型架构支持仍由 ComfyUI 决定。
