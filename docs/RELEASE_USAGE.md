# v0.5 发布包使用说明

适用于 Windows x64。解压后只有一个 EXE、两个 BAT 和 Web 目录；无需安装 Python、Rust、Visual Studio 或额外 SQLite DLL。

1. 将压缩包完整解压到可写目录。
2. 双击 `start.bat` 或 `CodexDowngradedMonitor.exe`。
3. 浏览器打开启动器显示的地址，默认 `http://localhost:48778/`。
4. 使用 `stop.bat` 关闭；也可以执行 `CodexDowngradedMonitor.exe --stop`。

从旧版升级：先正常关闭旧版，再将旧目录的 **config.json 和完整 data 目录** 复制到新包，直接启动。无需导入、转换、清库或重新配置。旧版仍运行时，新版会拒绝重复启动。

首次运行只在 EXE 目录创建 config.json 与 data。应用日志、数据库、缓存和临时文件均留在这里；目录不可写时会报错，不会回退用户目录。Codex 的日志和会话文件只是只读输入。

`--status` 显示主实例的 PID、实际地址、目录和启动阶段。`--stop` 通过独立命名管道控制，即使 HTTP 端口变化、worker 失效也能响应；只有主进程实际退出后才报告成功。不同解压目录共享同一 Windows 用户级单例。

设置页可调整预期模型、最小扫描间隔和线程数。命令行覆盖只影响当次运行；已有 config.json 不会在普通启动时重写。配置字段 cpp_port、配置版本 0.1、历史数据版本 0.3 和索引格式 3 均保留。

排障查看 `data/logs/collector.log`，其中记录控制管道、数据库、HTTP 和停止阶段耗时。若提示旧版占用单例，先用旧版停止脚本关闭，不能绕过 Mutex 另起扫描器。

更完整说明在包内 `Web/help.html`，第三方许可在 `Web/licenses/`。本工具是实验性字段观测工具，协议字段不能证明真实模型权重或能力。
