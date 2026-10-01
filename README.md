# site-cash-recon 网点现金对账系统

把快递网点分散在 **中天预付款账户**、**银行卡/支付宝/微信** 和 **门户网点日记账** 里的资金流水合到一处，每天回答三个问题：

1. **赚了还是亏了？** 现金口径经营利润（按科目），账单口径对照；亏损及时预警。
2. **钱在哪、去哪了？** 7 个账户逐日余额核对，内部划转、中天充值/提现去向跟踪。
3. **哪些要处理？** 日记账与银行重复登记、登记错误、未到账提现、未分类收支，一张清单列清楚。

结果以 **日报 / 周报 / 月报**（自包含 HTML，手机可读）推送到知识库和邮箱，另有 **本地网页控制台** 用于处理待核事项和录入手工账户余额。支持 **macOS 与 Windows**。

> 本仓库只包含代码、文档和合成测试数据。网点名称、账户、密钥和所有真实数据只保存在部署机的私有数据目录中。

## 快速开始

需要 Python 3.11+。推荐使用 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/wuxiaoxia88/site-cash-recon.git
cd site-cash-recon
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
```

Windows（PowerShell）：

```powershell
git clone https://github.com/wuxiaoxia88/site-cash-recon.git
cd site-cash-recon
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
```

初始化并自检：

```bash
.venv/bin/cashrecon init        # Windows: .venv\Scripts\cashrecon init
# 编辑 ~/.site-cash-recon/config.toml 与 secrets.env
.venv/bin/cashrecon doctor
```

常用命令（完整说明见 [运维手册](docs/06-operations.md)）：

| 命令 | 作用 |
| --- | --- |
| `cashrecon fetch --from 2026-09-01 --to 2026-09-30` | 采集/回补数据 |
| `cashrecon recon --from 2026-09-01 --to 2026-09-30` | 重算对账结果 |
| `cashrecon report daily --date 2026-09-30` | 生成日报（weekly / monthly 同理） |
| `cashrecon run daily` | 完整日任务：采集→对账→预警→报表→投递（自动补齐最近 7 天） |
| `cashrecon web` | 打开本地控制台 http://127.0.0.1:8765 |
| `cashrecon schedule install` | 注册定时任务（macOS launchd / Windows 任务计划） |

## 文档

| 文档 | 内容 |
| --- | --- |
| [01 需求规格说明](docs/01-requirements.md) | 用户需求追溯、功能/非功能需求、验收标准、待确认问题 |
| [02 系统架构](docs/02-architecture.md) | 模块、流程、部署视图、安全、技术决策 |
| [03 数据模型与业务规则](docs/03-data-model-and-rules.md) | 表结构、统一科目、对账与预警规则 |
| [04 测试计划](docs/04-test-plan.md) | 测试层次、数据、验收用例 |
| [05 部署手册](docs/05-deployment.md) | macOS / Windows 安装、配置、调度、升级回滚 |
| [06 运维手册](docs/06-operations.md) | 日常操作、排障、备份恢复 |
| [08 开发计划](docs/08-plan.md) | 里程碑 |

## 开发

```bash
.venv/bin/python -m pytest          # 测试
.venv/bin/ruff check src tests      # 代码检查
python3 scripts/leakcheck.py        # 防泄漏扫描（提交时自动运行）
git config core.hooksPath .githooks # 启用提交前检查
```

## 许可

[MIT](LICENSE)
