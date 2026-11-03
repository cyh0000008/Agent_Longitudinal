# Windows Git 安装与更新

Git 仓库是代码同步源，本机 `.env`、车辆数据和数据库不提交 Git。

首次运行前把 `deploy\windows_run\env.example` 复制为工程根目录 `.env`，填写飞书、模型和 Base 配置；不要提交真实密钥。

首次克隆后安装当前 checkout：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows_run\install_blu_agent.ps1
```

以后更新默认执行 `git pull --ff-only`、刷新依赖并启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows_run\update_blu_agent.ps1
```

工作区不干净、没有 upstream 或无法快进时，脚本会停止，不会重置或覆盖本地修改。只安装不启动可加 `-NoStart`；已在外部完成 Git 同步可加 `-NoPull`。
