# CodexDowngradedMonitor 发布包使用指引

这个压缩包已经包含 Windows amd64 版本的 C++ 采集器，使用时不需要安装 Visual Studio，也不需要重新构建 C++。它仍然只是一个仅供参考和学习的分析实验，不具备生产或长期监控价值。

## 环境要求

- Windows 10/11 或兼容的 Windows 环境。
- Python 3.8 或更高版本，并且 `python` 可以从终端调用。
- 本机已经安装并运行 Codex，使系统中出现 `codex.exe`。

## 启动

1. 解压整个目录，不要单独移动或删除 `cpp_collector`、`web` 和根目录的 Python 文件。
2. 双击 `start.bat`。
3. 启动器会自动打开本地面板，默认地址为 `http://localhost:48778/`。

如果不希望自动打开浏览器，可以在 PowerShell 中运行：

```powershell
python .\start.py --no-open
```

Codex 尚未运行时，面板显示“未发现 codex.exe”是正常状态；启动 Codex 后采集器会自动重连。

## 查看状态和停止

```powershell
python .\start.py --status
python .\start.py --stop
```

也可以双击 `stop-cpp.bat` 停止默认端口上的服务。

## 设置预期模型

可以在面板的“设置”中填写预期模型，也可以在启动时指定：

```powershell
python .\start.py --expect gpt-6-astra
```

设置会保存到同目录的 `config.json`。本包内的默认配置来自构建该 Release 时仓库中的 `config.json`。

## 数据位置

- `data/monitor.sqlite`：历史记录，首次运行时自动创建。
- `collector.log`：运行日志。
- `config.json`：当前配置。

这些文件只写入本机。默认 HTTP 服务只监听 `localhost`；如果把 `host` 改为 `0.0.0.0`，请确认当前网络可信，因为接口没有身份认证。

## 常见问题

- **提示找不到 C++ 采集器**：确认压缩包完整解压，并保留 `cpp_collector\build\collector_native.exe`。
- **提示未发现 codex.exe**：确认 Codex 已启动，并检查任务管理器中的进程名。
- **页面没有新记录**：响应对象生命周期很短，先在 Codex 中发起一次新请求，再查看面板的诊断信息。
- **降级记录显示为“不完整”**：请求侧证据没有采到，工具会保守地避免误报。
