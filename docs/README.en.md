# Codex Downgraded Monitor 0.5

[中文](../README.md)

A local, experimental observer of request and response model metadata. It reads process memory and existing Codex logs without modifying the observed process or uploading data. Protocol fields do not prove actual model weights or capabilities. Use only where you are authorized to perform this observation; consult the project's Chinese README for its scope and limitations.

## Portable Windows x64 package

The archive contains one `CodexDowngradedMonitor.exe`, `start.bat`, `stop.bat`, and `Web/`. No Python, Rust, Visual Studio installation, or separate SQLite DLL is required at runtime.

Run start.bat or the EXE to start; run stop.bat to stop. `--status` reports the actual address and startup stage. The same EXE runs the host and its isolated scanner worker. A per-user Windows mutex and named pipe protect and control the instance across installation folders and ports. A Job Object cleans up the worker if the host exits.

To upgrade, stop the old version and copy **config.json and the entire data directory** into the new package. No conversion, import, reset, or new settings are required. Existing schema, payloads, config version 0.1, data version 0.3, and evidence index format 3 are preserved.

All application-owned output remains beside the EXE: config.json and data (including logs, temporary files, and the SQLite database). An unwritable directory is an error; there is no AppData or profile fallback. Existing Codex files are read-only inputs.

## Build and test

Use Rust 1.92.0 and VS2022 with the Windows SDK and C++ build tools (SQLite is compiled from its bundled C source). Direct dependency versions and the transitive Cargo.lock graph are fixed. No Python is used in the final build or tests.

```powershell
.\src\tools\build.ps1 -Tests
.\src\tools\test.ps1 -PerformanceRuns 20
.\src\tools\package_release.ps1 -Version 0.5.0
.\out\run\start.bat
```

All source, web assets, launcher templates, fixtures, and scripts live in src. Build output, dependency cache, test reports, and release archives live in ignored out. GitHub Actions runs the same Rust and process integration tests before packaging. Local builds do not publish anything.

The Web API retains snapshot, response pagination, configuration, diagnosis, log clearing, shutdown, and SSE routes. Clearing the live log view never deletes history. New evidence can update archived responses, while conflicting evidence stays ambiguous.

See [usage](RELEASE_USAGE.md) and the [Chinese README](../README.md) for the full API and evidence rules. Third-party license texts ship in Web/licenses. Project license: [MIT](../LICENSE).
