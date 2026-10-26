# GitHub 部署步骤

## 0. 本地检查（不连接远端、不读取凭据）

在 PowerShell 中进入项目目录并选择 Python 3.12；仅检查部署文件时无需 PyNaCl：

```powershell
Set-Location 'E:\Deepseek Harness\定位工作台'
py -3.12 tools\deploy.py check
py -3.12 -B -m unittest discover -s tests -p test_deploy.py -v
```

`check` 仅核对部署白名单文件是否齐全，不验证 Secrets、账号接口、Actions 或网页是否成功。以下上传、触发、配置命令会操作远端，必须在获准部署时才执行。本次文档修订不执行这些命令。

实际部署前安装依赖：`py -3.12 -m pip install -r requirements-cloud.txt`。完整采集单元测试还需要 `pytest`。

## 1. 首次部署前准备

首次部署请使用 `all`，不要先运行 `upload`。部署工具会检查目标仓库；仓库不存在时只创建指定的新仓库，不会覆盖其他仓库。仓库可设为公开：定位状态以密文保存，但公开 Pages 的 HTML、JavaScript 和密文仍可被下载；访问密码由浏览器端用于解密。

不要上传整个项目，也不要把 `config.json`、`dist/凭据.txt`、SQLite、日志或明文位置加入仓库。

在 GitHub 仓库 `Settings -> Secrets and variables -> Actions` **先手动添加**：

- `XM_TOKEN`：小米 serviceToken
- `XM_UID`：小米 userId

只从本地凭据文件复制到 GitHub Secrets，不发给聊天，也不写入仓库。部署工具只核对 Secret 名称，不读取 Secret 值。

## 2. 首次部署

在项目目录运行：

```powershell
Set-Location 'E:\Deepseek Harness\定位工作台'
py -3.12 tools\deploy.py all 用户名/仓库名
```

工具会隐藏读取 PAT 和看板访问密码；密码至少 12 个字符。执行顺序为：设置 `LOCATOR_PASSWORD` → 检查必需 Secret 名称 → 上传非 workflow 文件 → 启用 Pages 并读回确认 `build_type=workflow` → 最后上传定时 workflow → 手动触发首次运行。

如果缺少 `XM_TOKEN` 或 `XM_UID`，命令会停止在 Secret 名称检查处；**不会上传部署文件、启用 Pages 或发布新 workflow**。先在 GitHub 补齐名称和值，再重新运行 `all`。`all` 会重新设置 `LOCATOR_PASSWORD`；已部署仓库更新文件时不要用它，避免意外改变访问密码。

PAT 通过隐藏输入读取，不写入命令行历史；需要仓库内容写入、workflow 文件更新、Actions 触发、Secrets 管理和 Pages 配置等对应权限；权限取决于所用 PAT 类型。

## 3. 已部署仓库的独立文件更新（`upload`）

`upload` 保留为单独的文件更新命令，不设置 Secrets，也不更改 Pages 配置。它会先核对三个必需 Secret 名称，并只读确认 Pages 的 `build_type=workflow`；任一条件不满足就停止，不上传文件。通过检查后按白名单上传，并将 workflow 放在最后。

首次准备必须先用 `all` 完成。只有仓库已有必需 Secrets 且 Pages 已配置为 GitHub Actions workflow 模式时，才使用；工具会再次只读核验 Pages 配置：

```powershell
py -3.12 tools\deploy.py upload 用户名/仓库名
```

## 4. 手动重跑与首次运行核验

需要手动触发时运行：

```powershell
py -3.12 tools\deploy.py run 用户名/仓库名
```

`run` 会先核对必需 Secret 名称，确保 Pages 已启用并读回确认 `build_type=workflow`，然后才触发 Actions。回到仓库 Actions 页面检查最新的 `定位工作台` 运行：采集加密、加密状态提交、Pages 发布步骤都应成功。随后访问 `https://用户名.github.io/仓库名/`，用 `LOCATOR_PASSWORD` 解锁，确认设备数量、位置时间及数据更新时间。

首次没有历史数据且设备列表不可用时，采集步骤会失败，不会生成有效位置。当前 workflow 会继续发布静态页面，随后明确把整体运行标为失败；因此网页可打开不代表定位采集成功。首次采集拿到设备列表但没有任何可用位置时，采集器也会停止，不写入空状态。仍须检查页面能否解锁、设备数量及位置时间，不能只凭发布步骤绿色判断定位可用。

## 5. 设备计划（T3）

计划结构示例：

```json
{
  "schedule": {"enabled": true, "skip_holidays": false,
    "default": {"enabled": true, "start": "06:00", "end": "23:00", "interval_minutes": 10}},
  "devices": {
    "xiaomi-设备fid": {"enabled": true, "interval_minutes": 20},
    "xiaomi-另一fid": {"enabled": false,
      "plan_json": {"enabled": true, "default": {"enabled": true, "start": "22:00", "end": "02:00", "interval_minutes": 10}}}
  },
  "holidays": {"statutory": [], "workdays": []}
}
```

页面解锁后可导出当前设备计划；编辑文件后运行：

```powershell
py -3.12 tools\deploy.py plans 用户名/仓库名 --plans cloud-plans.json
```

计划保存为 `XM_PLANS` Secret，之后由定时任务读取。

## 6. 密码轮换与数据保留

轮换看板密码时，先在 Actions Secrets 添加 `LOCATOR_PREVIOUS_PASSWORD`（旧密码），再把 `LOCATOR_PASSWORD` 更新为新密码，随后触发一次 `run`。确认运行成功、页面可用新密码解锁并显示原有数据后，再删除 `LOCATOR_PREVIOUS_PASSWORD`。如果旧状态无法用当前或旧密码解锁，系统只能从新采集重新建立状态，历史位置无法继承；若本次没有取得可保存位置，则保留旧加密文件并停止发布。

定时任务配置为每 10 分钟发起一次，但实际启动可能延迟；页面分别显示本次查询时间和位置时间。
本地目录无需是 Git 工作区：部署脚本通过 GitHub API 上传白名单文件。文件上传采用逐文件提交；现有 workflow 在更新期间仍可能按原计划运行，最后上传 workflow 不会暂停既有任务。

### 从本地数据库提取计划（只读）

`tools/build_cloud_plans.py` 读取项目 `config.json` 的实际 `data_dir`；支持
`WORKBENCH_CONFIG` 覆盖配置路径。相对 `data_dir` 与本地服务一致，按进程当前目录
解析。空 `data_dir` 使用现有默认数据目录规则，不创建目录、配置或数据库。
脚本不调用有写入副作用的 `common.load_config()` / `store` 读取函数。

只提取已存 `schedule`、`holidays` 和云端设备的 `enabled`、
`interval_minutes`、`plan_json`；将 0/1 开关转为 JSON 布尔值，过滤额外字段，
并调用云端计划校验（最小间隔 10 分钟）。空独立计划保留继承含义。
缺少全局计划/节假日、非法数据、非空 WAL 或读取期间数据库变化时停止，
stderr 只输出固定失败说明，退出码为 1。SQLite 使用只读 immutable 连接，
不会创建或更新 WAL/SHM；有未合并 WAL 时需在数据库正常完成检查点后重试。
不要为导出而直接删除 WAL/SHM 文件。

成功时 stdout 仅输出 UTF-8 JSON，退出码为 0。JSON 必须包含设备标识作为映射键，
因此应由父进程在内存中接收，不直接显示、记录日志或粘贴到聊天。
不导出凭据、设备名称、位置、地址或历史查询状态，也不上传 Secret 或触发任务。
以下是父进程内存接收示例；运行它会实际提取计划，但不会打印 JSON：

```python
import json
from pathlib import Path
import subprocess

root = Path(r"E:\Deepseek Harness\定位工作台")
result = subprocess.run(
    [str(root / "work/venv/Scripts/python.exe"), "-B",
     str(root / "tools/build_cloud_plans.py")],
    cwd=root, capture_output=True, check=False,
)
if result.returncode:
    raise SystemExit("Cloud plan export failed; no plan was exported.")
plans = json.loads(result.stdout)  # 仅留在内存；不要打印 plans/result.stdout。
```

仅查看帮助，不读取配置或数据库：

```powershell
.\work\venv\Scripts\python.exe -B tools\build_cloud_plans.py --help
```


## 网页直接保存计划（2026-10-26新增）

解锁工作台后展开「计划管理」，选择设备、调整时段和间隔。网页提交需要限定到本仓库的fine-grained Token，日常仅需Actions读写及Metadata只读权限；部署用的Contents/Workflows/Secrets权限应在部署后去除。不要在聊天发送Token。可勾选本机加密记住授权，之后重新解锁可用「恢复本机加密授权」。

点击「加密提交计划」后204只代表已接收，须等待「已生效」；可用「刷新发布状态」。若提示版本变化，先载入最新计划再修改，避免覆盖。云端保存加密的cloud/plans.enc.json，优先于XM_PLANS。保留此文件和site/state.enc.json，不要上传旧本地凭据覆盖云端。

可选Secret：DEEPSEEK_API_KEY用于已完成月份的汇总说明；XM_MAIL_CONFIG用于只读IMAP，JSON包含enabled/host/port/user/password/folder/sender_contains/subject_contains/device_id。至少一个明确邮件过滤，两个均设置则同时匹配。当前没有实际邮箱账号授权码，disabled为正常提示。

历史和报告均随state.enc.json加密发布。历史保留上月/当月，正式报告保留最近12个月。当月预览无需AI，月末统计可添加DeepSeek说明；不发送设备名称、ID或坐标。
