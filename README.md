# NAI Style Studio

本机运行的中文 NovelAI 画风收藏与生图工作台，使用 Python、SQLite 和原生 HTML/CSS/JavaScript。

- 解析原图生成信息，编辑、收藏和复制画风配方，支持多张例图。
- 通过 OpenAI 兼容接口整理需求、参考图与画师串，再调用 NAI 生图。
- 支持参数调整、底图重绘、局部重绘和模型支持的参考图功能。
- 保存生成历史，切图同步参数与负面词，复用历史提示词直接重 Roll。

## 启动

使用 Windows、Python 3.11 或更新版本（已在 Python 3.14 验证）。在项目目录安装依赖：

```powershell
py -3 -m pip install -r requirements.txt
```

双击 `启动画风串收藏台.cmd`，或运行：

```powershell
py -3 -B app.py --open
```

浏览器打开 `http://127.0.0.1:8724/`，从顶部进入「NAI 生图」。端口占用时可使用 `py -3 -B app.py --port 8725 --open`。

## 配置与数据

首次启动收藏和历史为空。在「接口设置」填写自己的 NAI 地址与 Token、提示词助手地址与 API Key、模型名称；看图整理需要视觉模型。各图像功能的可用性取决于所选模型与接口服务的支持。

接口配置、数据库、上传图片和生成历史均保存在 `data/`。凭据使用 Windows 当前用户 DPAPI 加密。服务仅监听本机地址。仓库仅提供程序框架，个人画师串、图片和接口配置由使用者自行添加。

## 测试

```powershell
py -3 -B -m unittest test_generation test_reroll
```

测试使用虚构素材及模拟接口，临时文件保存在 `data/test-runs/`。
