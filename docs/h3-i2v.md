# 官方 Popular H3 I2V 模板

`workflows/h3-i2v-ui.json` 原样保存官方 **MiniMax H3: Image to Video** 模板。来源为 [Comfy-Org/workflow_templates](https://github.com/Comfy-Org/workflow_templates/blob/0e5c5efb32ba6f3365d6da07da64aaf668157042/templates/video_minimax_h3_i2v.json)，commit `0e5c5efb32ba6f3365d6da07da64aaf668157042`；它与启动器所安装的 `comfyui-workflow-templates-json==0.1.102` 中的模板完全一致。文件为 71,812 字节，SHA256 为 `34ee39544808fd3b0dc8de9df082940d4d41c3771beb80d3988c1ea5531cec0d`。

这是浏览器图形工作流 JSON，包含官方子图和说明；不能直接作为 `/prompt` 的 API 格式提交。浏览器加载模板、选输入图片、点击 Run，与后端 API 测试分别记录，API 成功不能代替浏览器成功。

`workflows/h3-i2v-api.json` 来自固定版本的浏览器实际点击 Run 后提交的 26 个节点。仅将 `LoadImage.image` 改为启动器专用示例文件名，其余提示词、模型、seed、参数和节点原样保留。它是启动器 `render` 命令的默认 API 工作流；原有 `h3-api.json` 仍可通过 `--workflow` 明确选择。实际默认值为 1:1、0.4 MP、32 倍数（640×640）、5 秒输入时长、124 帧、24 fps、基础 20 步；Turbo 关闭，开启后使用 8 步。

官方导出中还包括未接到输出的草稿节点，例如没有图片输入的 `ImageScaleToTotalPixels`。渲染器保留这些节点，并与 ComfyUI 后端一致：检查所有节点的类型是否可用，只对输出节点及其依赖检查必需输入；未连接草稿不会阻止已接好的主流程。

## 模型和目录

模板扫描需要基础 FL2VA、Qwen 文本编码器、视频 VAE、音频 VAE和 8 步 Turbo LoRA。完整默认清单见 [models/h3.json](../models/h3.json)。Turbo LoRA 使用与官方模板相同的文件和 SHA256，固定到 Comfy-Org 模型仓库的 revision。默认 `turbo_mode` 关闭，走基础 20 步；打开后采用外层子图的 `turbo_steps` 参数。必须以浏览器实际显示和提交的 API 参数为准，子图内部 Primitive 的默认值可能被外层覆盖。

模板说明中的 `embeddings/minimaxh3_art_is_explosion.safetensors` 是可选风格 embedding。只有提示词使用 `embedding:minimaxh3_art_is_explosion` 才会引用；官方默认提示词没有引用它。它不是另一份 H3 主模型，添加到清单是为了与用户看到的目录说明对应。该 embedding 的社区来源见 [官方提示词说明](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-prompt-guide#prompt-embeddings)。模型许可证与本项目脚本的 MIT 许可证分别适用，见 [H3 说明](h3.md)。

本启动器通过 extra model paths 将这些类别映射到 VM 的 `/content/colab-comfyui-runtime/models`。例如 LoRA 位于其 `loras` 子目录，embedding 位于 `embeddings` 子目录。它们可被 ComfyUI 按文件名识别，模型不会直接从 Drive 加载。

## 示例图片和实际测试

官方默认输入是 [`transparent_rgb_gaming_mouse.png`](https://github.com/Comfy-Org/workflow_templates/blob/0e5c5efb32ba6f3365d6da07da64aaf668157042/input/transparent_rgb_gaming_mouse.png)，1024×1024 PNG，1,383,312 字节，SHA256 为 `49696748d2fff0e8c9b63c7173c6d6282b70eac0195def5b39402f9564410e75`。

API 测试使用专用文件名 `launcher-h3-i2v-example.png`，保存到当前 owned storage 的 `input` 目录。渲染器只在 API 工作流的 `LoadImage.image` 明确引用这个文件名时准备图片：固定来源、大小和 SHA，按本次整体 deadline 下载到临时文件，验证后发布。已有正确文件只读校验；已有错误文件或软链接会报错，不覆盖用户文件。T2V 测试和其他用户图片不触发下载。

浏览器验收步骤：

1. 打开 SSH 本地网址，Templates → Popular，选择 **MiniMax H3: Image to Video**；也可导入仓库的原样 UI JSON。
2. 在 Load Image 节点选择自己的图片，或上传上述官方示例。仅加载模板不会自动满足输入图片要求。
3. 核对模型无 Missing Models、图片预览正确及实际分辨率、时长、seed 和 Turbo 参数；点击 Run，等待完成并播放 Save Video 输出。
4. 开启 `turbo_mode` 再运行，确认实际队列使用 Turbo LoRA 和指定步数；记录两个任务各自结果。下载成功本身不证明 LoRA 已参与推理。
5. 顶部 **Graph** 旁的下拉箭头 → **Save As**，填写工作流名称并 Confirm；从左侧 **Workflows** 双击名称重新打开。该菜单的 **Export (API)** 可导出 API 图；不同版本也可能将这些操作放在 File 菜单。

`workflows/h3-i2v-api.json` 来自 2026-10-06 浏览器成功提交的基础模式队列，仅将 Load Image 的文件名换成启动器专用示例文件名。原样保留官方子图展开后的生成参数和未连到输出的草稿节点；必需输入按照输出依赖检查，与 ComfyUI 后端一致。浏览器基础20步和Turbo8步均实际成功；服务重启后另一次120秒API回归超时，未标记成功，详见 [实测记录](test-report.md)。

用户工作流的 Save / Save As 写入 ComfyUI 服务端 `user/default/workflows`，本启动器为当前存储根的 `user/default/workflows`：Drive 模式随同一目录在新 VM 保留，VM disk 模式随 VM 释放而消失。**Export** 则通过浏览器下载到本机。未明确保存的浏览器草稿不作为跨 VM 保留的保证。模型、输入素材与工作流 JSON 各自保存，JSON 不包含完整模型或输入图片。

## 上游模板许可证

以下为上游模板仓库的 MIT 许可证，保留于此以满足原样模板的版权声明要求。它不授予 H3 模型权重使用权。

```text
MIT License

Copyright (c) 2023-present Comfy Org

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
