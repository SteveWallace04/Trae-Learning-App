# Trae-Learning App

本地 AI 编程学习应用：通过网页学习，保存聊天和认知断点，下次从具体问题继续。

**当前状态：已完成第一阶段的第一步——本地启动、网页入口和连接状态检查。尚未接入模型，也没有聊天保存或断点功能。**

## 已确定的方案

- 网页：HTML、CSS、JavaScript，聊天为主，学习记录按需展开。
- 本地程序：Python + FastAPI，提供网页、准备教学材料、调用模型并保存记录。
- 模型：第一版接入 DeepSeek API，具体模型和请求参数在实现时核对。
- 存储：教学材料用 Markdown，聊天、断点和学习观察用 JSON。
- 运行：启动一个本地程序，通过浏览器访问本机地址。
- 代码练习：第一版继续使用外部编辑器和终端。

本地保存不意味着完全离线：调用云端模型时，相关教学材料和消息会发送至模型服务。

## 文件地图

| 位置 | 职责 |
|---|---|
| [AGENTS.md](AGENTS.md) | 开发 Agent 的工作约定 |
| [docs/plan.md](docs/plan.md) | 已确定的需求、阶段和验收方式 |
| [web/README.md](web/README.md) | 网页职责和第一阶段交互 |
| [app/README.md](app/README.md) | 本地程序的模块职责 |
| [defaults/README.md](defaults/README.md) | 默认教学材料与空白模板 |
| [tests/README.md](tests/README.md) | 后续必要验证的范围 |
| data/ | 本机个人资料，内容不提交 Git |
| .env.example | 配置占位示例，不含真实密钥 |

## 使用与开发

需要 Python 3.12 或更新版本。当前支持从源码目录运行，尚未制作独立安装包。

在仓库根目录打开 PowerShell，首次安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

启动（之后只需执行这一条）：

```powershell
.\.venv\Scripts\python.exe -m app.main
```

打开 <http://127.0.0.1:8000>。看到“本地服务已连接”即表示网页已成功请求本地服务；这不代表 DeepSeek 已连接。输入框暂不可用，不会发送或保存消息。终端按 Ctrl+C 停止服务。

当前无需填写密钥，`.env.example` 仍为后续配置占位。程序只监听本机地址。

验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

检查会自动启停临时本地服务，不调用模型，也不写个人资料。具体范围见 [验证说明](tests/README.md)。

新用户将从 defaults/ 建立自己的 data/，后续读取和修改个人数据，不由模板升级覆盖个人修改。这一初始化行为尚未实现。

本仓库不包含原学习仓库的个人画像、学习证据、实验、历史评估或 Git 历史。原学习仓库继续保存原有正式学习记录；此处开发使用空白或示例数据，迁移另行决定。

工程计划与交接可在独立工程仓库中管理，但应用运行代码、必要测试和使用说明应完整保留在本仓库，不依赖工程仓库运行。
