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

直接启动时，接口配置、数据库、上传图片和生成历史保存在 `data/`。Windows 凭据使用当前用户 DPAPI 加密；Linux 使用数据目录中的 `.providers.key` 加密，备份时需连同该密钥文件一起保存。仓库仅提供程序框架，个人画师串、图片和接口配置由使用者自行添加。

## Docker 启动

安装并启动 Docker Desktop，在项目目录运行：

```powershell
docker compose up -d --build
```

访问 `http://127.0.0.1:8724/`。容器名为 `nai-style-studio`，端口仅开放给本机。运行数据挂载在项目的 `data/docker/`，容器重建后继续保留；Docker 启动后容器自动恢复运行。

```powershell
docker compose logs --tail=50
docker compose restart
docker compose down
```

容器访问宿主机接口时，地址中的本机主机名使用 `host.docker.internal`。详见 [Docker 网络说明](https://docs.docker.com/desktop/features/networking/networking-how-tos/)。Windows 的 DPAPI 凭据需在原账户解密后导入 Linux 容器，或在网页中重新填写；收藏数据库、图片与历史可复制到 `data/docker/`。

镜像构建仅收录源码与依赖；个人数据和配置通过本地目录挂载。Linux 凭据使用 [Fernet 认证加密](https://cryptography.io/en/stable/fernet/)。

## 测试

```powershell
py -3 -B -m unittest test_generation test_reroll test_provider_settings
```

测试使用虚构素材及模拟接口，临时文件保存在 `data/test-runs/`。

容器环境测试：`docker build --target test -t nai-style-studio:test .`。
