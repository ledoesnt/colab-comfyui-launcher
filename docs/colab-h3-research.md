# MiniMax H3 在 Colab 上部署的公开证据

调查日期：2026-10-04。这份记录核对网页、仓库源码和 Hub 元数据；没有下载权重、运行第三方 notebook 或复现他人的视频结果。

## 找到了什么

| 来源 | 具体内容 | 能证明什么 |
| --- | --- | --- |
| [MiniMax 官方 H3 页面](https://design.minimax.io/h3) | 硬件 FAQ 明确写着 “If you have no GPU, Colab GPU runtimes work.”，并列出社区本地及云部署教程 | 官方认可 Colab 作为技术部署途径；没有说明所有用户与云地域组合的许可解释 |
| [ComfyUI 官方 H3 指南](https://docs.comfy.org/tutorials/video/minimax/minimax-h3) | 原生节点、官方模型重包和 T2V/I2V/R2V 模板 | ComfyUI 支持本地 H3 工作流；单凭指南不能证明我们自己的 runtime 已成功运行 |
| [soren-labs/minimax-h3-colab](https://github.com/soren-labs/minimax-h3-colab) | 作者公开 G4 部署脚本、固定版本、视频、workflow、日志和 GPU telemetry；报告原生 T2V 1344×768、124 帧生成约 355.830 秒 | 有可查验的社区实测记录；属于作者的结果，我们没有独立复现。细节见 [部署记录](https://github.com/soren-labs/minimax-h3-colab/blob/main/docs/deployment.md)和[产物索引](https://github.com/soren-labs/minimax-h3-colab/blob/main/docs/benchmarks.md) |
| [keboqi 的 Colab notebook](https://github.com/keboqi/minimax-h3/blob/main/minimax_h3_colab.ipynb) | GPU 检查、uv Python 3.12 环境、ComfyUI/Gradio、首用模型下载、可选 Drive 输出持久化、Gradio Share 与 Cloudflare URL | 公开源码展示了完整搭建方法；当前 notebook 没有保存执行输出，所以其存在本身不是成功测试记录 |

soren-labs 的路线使用 `colab new --gpu G4`、上传 runner、`colab exec`，将模型放在 `/content`，最后下载产物并停止 VM。它不要求挂载 Drive。keboqi 的 notebook 通过 `drive.mount` 和输出目录链接实现可选持久化。这些操作与本项目要测试的 Colab、ComfyUI、隧道和存储步骤相对应。

没有在当前 MiniMax 主仓库 README 或官方 integration 目录找到专用 Colab notebook。官方 H3 页面的 Colab 硬件建议和社区教程是另一类证据。不要把 Hugging Face 自动生成的 “Use this model / Google Colab” 菜单当作模型作者测试过的专用 notebook。

## 下载可访问性与许可解释

本次只读查询 [原模型 API](https://huggingface.co/api/models/MiniMaxAI/MiniMax-H3)和[Comfy 重包 API](https://huggingface.co/api/models/Comfy-Org/MiniMax-H3)，两者均返回 `private=false`、`gated=false`，模型卡未声明额外 gating 字段。因此，没有发现 Hub 仓库层面的人工下载审批门槛。该元数据不能证明所有网络地区都没有下载限制，也不能代替模型许可证。

[官方协议](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)定义了使用者、适用地域和排除地域，对适用地域外的行为设有限制。[官方问答](https://huggingface.co/MiniMaxAI/MiniMax-H3/discussions/12?expand=true)的答复主要围绕使用者所在地；社区有人追问应按物理 GPU、用户或法律主体所在地判断，核对时未发现对此的明确官方答复。

因此，“别人能在 Colab 跑起来”有技术和公开实测依据；“中国用户使用美国云实例”的具体许可解释仍未获这些资料明确回答。不能只用美国出口 IP 作确定判断，也不能把社区教程当成该场景的授权证明。本调查不会新增通用法务审批步骤；实际任务按用户授权推进，并诚实记录 H3 自身的验证状态和适用条款。
