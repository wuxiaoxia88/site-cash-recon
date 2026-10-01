# 05 部署手册（macOS / Windows）

版本：v1.0 ｜ 日期：2026-10-01

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| 系统 | macOS 13+ 或 Windows 10/11；系统时区设为中国标准时间（UTC+8） |
| Python | 3.11 或更高（推荐用 [uv](https://docs.astral.sh/uv/) 安装管理） |
| 网络 | 能访问 zto-cli 主/备服务（局域网）与知识库 Agent 接口 |
| 可选 | macOS 上已登录的 `agently-cli`（Agent 邮箱）；或任意 SMTP 邮箱 |
| 上游数据（仅 macOS 正式机） | `advance-payment-monitor/state/monitor.db`、`bank-monitor/ledger.sqlite`、支付宝官方账单目录，均只读访问 |

## 2. 安装

macOS：

```bash
git clone https://github.com/wuxiaoxia88/site-cash-recon.git ~/site-cash-recon
cd ~/site-cash-recon
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python .
.venv/bin/cashrecon init
```

Windows（PowerShell）：

```powershell
git clone https://github.com/wuxiaoxia88/site-cash-recon.git $HOME\site-cash-recon
cd $HOME\site-cash-recon
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe .
.venv\Scripts\cashrecon init
```

`init` 在数据目录（macOS `~/.site-cash-recon`，Windows `%USERPROFILE%\.site-cash-recon`）生成 `config.toml` 与 `secrets.env` 模板。

## 3. 配置

1. **密钥**：编辑 `secrets.env`，填写 zto-cli 主/备地址与 Key、知识库令牌、（可选）SMTP。
2. **账户**：运行 `cashrecon accounts` 列出门户日记账账户与编码，填入 `config.toml` 的 `[[accounts]]`（`portal_code`）。中天主账户 `type = "ZT_PREPAY"`；有自动采集的账户 `collection = "auto"`。
3. **数据源**：
   - `ZT_SUMMARY`、`JOURNAL`、`BILL_PROFIT`：只需 zto-cli，任意系统可用。
   - `ZT_FLOW`：macOS 正式机用 `impl = "monitor_db"` 指向上游监控库；其他机器用 `impl = "api"`（如接口不可用可设 `enabled = false`，中天利润仍由 `ZT_SUMMARY` 提供）。
   - `BANK_LEDGER`：指向上游 `ledger.sqlite`，配置三个账户对应关系与 `recon_labels`、`alipay_bill_dir`。没有该上游的机器设 `enabled = false`。
4. **投递**：确认无误后再开启 `[delivery.kb] enabled = true` 与 `[delivery.mail] enabled = true`。
5. 运行 `cashrecon doctor`，直到没有错误。

## 4. 首次数据与验收

```bash
cashrecon fetch --from 2026-09-01 --to 2026-09-30     # 回补历史
cashrecon recon --from 2026-09-01 --to 2026-09-30
cashrecon report daily --date 2026-09-30 --open
cashrecon run daily --dry-run                         # 端到端演练，只生成邮件预览
```

## 5. 调度

```bash
cashrecon schedule install --with-console   # 日报 12:10、补跑 18:00、周报周一 12:20、月报每月 3 日 12:30，控制台常驻
cashrecon schedule status
```

- macOS：写入 `~/Library/LaunchAgents/com.sitecashrecon.*.plist` 并加载；任务输出写入 `logs/launchd-*.log`。
- Windows：在任务计划程序 `\SiteCashRecon\` 下创建任务，错过的运行会在开机后补做。
- 移除：`cashrecon schedule uninstall`。

## 6. 升级与回滚

```bash
cd ~/site-cash-recon && git fetch --tags
cashrecon backup                       # 先备份数据库
git checkout v0.x.y && uv pip install --python .venv/bin/python .
cashrecon doctor && cashrecon run daily --dry-run --no-deliver
```

回滚：`git checkout <上一版本标签>` 后重新安装；数据库结构只做追加式迁移，旧版本可读新库的既有表。如需恢复数据，把 `backups/cashrecon-YYYYMMDD.db` 复制为 `data/cashrecon.db`。

## 7. 迁移到正式设备

1. 在正式设备按第 2～3 节安装与配置（上游库路径改为正式设备路径）。
2. 将本机 `~/.site-cash-recon/data/cashrecon.db` 复制过去可保留人工处理记录与历史；也可直接在新设备回补。
3. 正式设备开启投递并安装调度后，**在本机执行 `cashrecon schedule uninstall`**，避免两台机器重复发送。
