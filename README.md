# adb-gopay 使用教程

这是一个基于 `adb`、Android UI dump 和可选 OCR 的 GoPay 自动化工具。

目前的主流程包括：

- 冷启动前自动清理 `com.gojek.gopay`
- 自动拒绝首次位置权限引导
- 自动进入手机号输入页
- 通过 NexSMS 购买印尼号码并等待 OTP
- OTP 后继续填写名称
- 进入 `Profil` -> `Pengaturan & keamanan` -> `Perlindungan akun`
- 点击 `Pasang PIN`，完成 PIN 和二次确认
- 如果 PIN 后跳到 WhatsApp OTP，会切到 `Coba Metode Lainnya`，再改走 `OTP via SMS`

# 省流:
```powershell
python -m adb_accessibility_assistant run-gopay --config config.gopay.yaml --mute --retry-on-otp-timeout
```

## 目录说明

- [config.gopay.yaml](./config.gopay.yaml)：GoPay 主配置
- [config.example.yaml](./config.example.yaml)：通用辅助模式示例配置
- [run-assistant.ps1](./run-assistant.ps1)：自动使用本地 `.venv` 的 PowerShell 启动脚本
- [adb_accessibility_assistant](./adb_accessibility_assistant)：主程序源码

## 环境要求

- Windows
- Python 3.12+
- 已安装并可直接执行 `adb`
- BlueStacks 或真实 Android 设备
- 安装仓库内的 `ADBKeyboard.apk`，方便稳定输入文本

如果没有安装 `pyttsx3`，命令行运行时请带 `--mute`。

## 安装

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果你不想每次手动激活虚拟环境，也可以使用项目自带脚本：

```powershell
.\run-assistant.ps1 doctor --config config.gopay.yaml --mute
.\run-assistant.ps1 gopay-to-phone-input --config config.gopay.yaml --mute
.\run-assistant.ps1 run-gopay --config config.gopay.yaml --mute --retry-on-otp-timeout
```

## 启动前检查

先确认设备在线：

```powershell
adb devices
```

再执行环境自检：

```powershell
python -m adb_accessibility_assistant doctor --config config.gopay.yaml --mute
```

如果这里看不到设备，后面的 GoPay 流程都不会成功。

## 配置说明

### 1. GoPay 主配置

[config.gopay.yaml](./config.gopay.yaml) 是完整流程使用的配置文件。

最关键的字段有：

- `adb_path`：`adb` 可执行文件路径
- `device_serial`：设备序列号，多设备时建议填写
- `target_package`：默认是 `com.gojek.gopay`
- `reset_app_on_start`：默认 `true`，每次启动前都清空应用数据
- `nexsms.api_key`：你的 NexSMS API Key
- `nexsms.proxy`：代理地址，默认空字符串，不走代理
- `nexsms.country_name`：默认 `Indonesia`
- `nexsms.default_price`：价格接口失效时的兜底价格
- `nexsms.same_number_retry_limit`：OTP 超时后，同一号码可整体重试的次数
- `credential.pin_length`：PIN 长度，当前默认 6
- `flow.credentials_path`：成功后写入账号信息的输出文件

默认策略是：

- 只购买印尼号码
- 优先使用平台返回的最低真实价格
- OTP 超时时优先复用同一个手机号继续等，直到达到重试上限或号码快过期

### 2. 通用辅助配置

[config.example.yaml](./config.example.yaml) 主要给这些命令用：

- `scan`
- `tap-text`
- `auto-step`
- `auto-loop`
- `assist`
- `gui`

## 快速开始

### 1. 只自动打开到手机号输入页

这条命令会：

- 清空 GoPay 本地数据
- 重新启动 App
- 自动拒绝首次位置权限引导
- 自动点击 landing 页 CTA
- 停在手机号输入页

```powershell
python -m adb_accessibility_assistant gopay-to-phone-input --config config.gopay.yaml --mute
```

适合先验证 ADB、页面识别和基础点击是否正常。

### 2. 运行完整注册流程

```powershell
python -m adb_accessibility_assistant run-gopay --config config.gopay.yaml --mute --retry-on-otp-timeout
```

这条命令会从干净状态完整执行 GoPay 流程，并在 OTP 超时时自动重试。

如果你不想启用 OTP 超时后的自动重试，可以去掉 `--retry-on-otp-timeout`：

```powershell
python -m adb_accessibility_assistant run-gopay --config config.gopay.yaml --mute
```

如果你已经有一个现成手机号，想直接复用：

```powershell
python -m adb_accessibility_assistant run-gopay --config config.gopay.yaml --mute --phone +62857xxxxxxxx
```

## 常用命令

### GoPay 专用命令

```powershell
python -m adb_accessibility_assistant gopay-register --config config.gopay.yaml --mute
python -m adb_accessibility_assistant gopay-to-phone-input --config config.gopay.yaml --mute
python -m adb_accessibility_assistant gopay-inspect --config config.gopay.yaml --mute
python -m adb_accessibility_assistant gopay-next --config config.gopay.yaml --mute
python -m adb_accessibility_assistant gopay-tap "OTP via SMS" --config config.gopay.yaml --mute
python -m adb_accessibility_assistant gopay-input 85712345678 --config config.gopay.yaml --mute
python -m adb_accessibility_assistant run-gopay --config config.gopay.yaml --mute --retry-on-otp-timeout
```

说明：

- `gopay-register`：运行 GoPay 状态机主流程
- `gopay-to-phone-input`：只走到手机号页并停住
- `gopay-inspect`：识别当前 GoPay 页面，并把快照保存到 `artifacts/gopay`
- `gopay-next`：在当前已识别页面上尝试点下一步
- `gopay-tap`：按 `content-desc` 文字直接点击
- `gopay-input`：在手机号页输入号码并尝试点 `Lanjut`
- `run-gopay`：完整端到端执行，包含 NexSMS 买号、OTP 轮询、PIN 设置和后续页面跳转

### 通用辅助命令

```powershell
python -m adb_accessibility_assistant doctor --config config.example.yaml --mute
python -m adb_accessibility_assistant start-app --config config.example.yaml --mute
python -m adb_accessibility_assistant scan --config config.example.yaml --mute
python -m adb_accessibility_assistant dump-ui --config config.example.yaml --mute
python -m adb_accessibility_assistant tap-text "Oke, lanjut" --config config.example.yaml --mute
python -m adb_accessibility_assistant auto-step --config config.example.yaml --mute
python -m adb_accessibility_assistant auto-loop --config config.example.yaml --mute --max-steps 10
python -m adb_accessibility_assistant assist --config config.example.yaml --mute
python -m adb_accessibility_assistant gui --config config.example.yaml --mute
```

## HTTP API

如果你需要让别的程序远程调用这套流程，可以启动内置 API server：

```powershell
python -m adb_accessibility_assistant api-server --config config.gopay.yaml --host 127.0.0.1 --port 8787 --mute
```

这会启动一个单机 HTTP 服务。它的特点是：

- 任务按队列串行执行，适合单设备/单模拟器场景
- 支持后台执行
- 支持查询任务状态
- 支持取消正在运行的任务
- 支持把最终结果回调到你的业务系统

### API 端点

- `GET /api/health`：健康检查
- `GET /api/tasks`：查看任务列表
- `GET /api/tasks/{task_id}`：查看单个任务详情
- `POST /api/tasks/{task_id}/cancel`：取消任务
- `POST /api/tasks/run-gopay`：提交完整 GoPay 注册任务
- `POST /api/tasks/prepare-phone-input`：提交“只走到手机号页”的任务
- `POST /api/tasks/inspect`：提交当前页面识别任务
- `POST /api/tasks`：通用提交入口，手动指定 `task_type`

### 提交完整注册任务

```powershell
curl -X POST http://127.0.0.1:8787/api/tasks/run-gopay ^
  -H "Content-Type: application/json" ^
  -d "{\"retry_on_otp_timeout\": true, \"max_steps\": 200, \"callback_url\": \"http://127.0.0.1:9000/callback\"}"
```

请求体支持的常用字段：

- `config_path`：可选。不传就使用启动 `api-server` 时的 `--config`
- `adb_path`：可选，覆盖默认 adb 路径
- `device_serial`：可选，覆盖默认设备序列号
- `mute`：可选，默认继承 server 启动参数
- `max_steps`：最大步骤数
- `poll_timeout`：OTP 等待超时秒数
- `step_delay`：步骤间隔秒数
- `retry_on_otp_timeout`：OTP 超时后是否自动重试
- `phone`：直接复用现成手机号
- `callback_url`：任务状态变化回调地址
- `callback_headers`：回调请求附加 HTTP 头
- `metadata`：业务侧自定义字段，会原样带回

### 只走到手机号页

```powershell
curl -X POST http://127.0.0.1:8787/api/tasks/prepare-phone-input ^
  -H "Content-Type: application/json" ^
  -d "{\"max_steps\": 10}"
```

### 页面识别

```powershell
curl -X POST http://127.0.0.1:8787/api/tasks/inspect ^
  -H "Content-Type: application/json" ^
  -d "{\"save_dir\": \"artifacts/gopay\"}"
```

### 查询任务

```powershell
curl http://127.0.0.1:8787/api/tasks
curl http://127.0.0.1:8787/api/tasks/<task_id>
```

### 取消任务

```powershell
curl -X POST http://127.0.0.1:8787/api/tasks/<task_id>/cancel
```

### 回调格式

如果提交任务时带了 `callback_url`，server 会在这些状态变化时回调：

- `task.queued`
- `task.started`
- `task.succeeded`
- `task.failed`
- `task.canceled`

回调是一个 `POST` JSON，请求体结构大致如下：

```json
{
  "event": "task.succeeded",
  "event_at": "2026-06-01T09:00:00+00:00",
  "task": {
    "task_id": "8c4b7d8d9e8f4d20b91dcb3a1f4b5c6d",
    "task_type": "run-gopay",
    "status": "succeeded",
    "request": {
      "retry_on_otp_timeout": true,
      "config_path": "config.gopay.yaml"
    },
    "metadata": {
      "order_id": "A10001"
    },
    "result": {
      "ok": true,
      "status": "success",
      "state": "registration_complete",
      "message": "Registration completed successfully.",
      "data": {
        "username": "Abcdefgh",
        "phone": "+62857xxxxxxx"
      }
    },
    "logs": [
      "State: waiting_phone_input",
      "Getting phone number from NexSMS..."
    ]
  }
}
```

注意：

- 这是单进程内存队列，重启 API server 后旧任务状态不会保留
- 同一时间只适合控制一台设备
- 如果回调地址不可达，任务本身不会因此失败，只会在任务日志里记一条 callback 失败信息

## 典型使用流程

推荐先按这个顺序测试：

1. `doctor`，确认 `adb`、设备、OCR 正常
2. `gopay-to-phone-input`，确认基础启动和页面识别正常
3. `gopay-inspect`，确认当前页识别结果和 `artifacts/gopay/*.json` 快照正常
4. `run-gopay --retry-on-otp-timeout`，执行完整流程

如果某一步页面识别不对，先不要继续硬跑，先保存当前截图，再执行：

```powershell
python -m adb_accessibility_assistant gopay-inspect --config config.gopay.yaml --mute
```

然后查看 `artifacts/gopay` 目录下生成的页面快照。

## 输出文件

运行过程中常见输出：

- `artifacts/gopay/*.json`：当前页面识别快照
- `artifacts/nexsms_activations.sqlite3`：本地号码复用数据库
- `credentials.json`：注册成功后输出的账号信息

## 常见问题

### 1. 启动就提示 `no devices/emulators found`

说明 `adb` 当前看不到设备。先检查：

```powershell
adb devices
```

如果 BlueStacks 开着但看不到，通常需要重连 ADB 或重启模拟器。

### 2. 页面识别错了

先执行：

```powershell
python -m adb_accessibility_assistant gopay-inspect --config config.gopay.yaml --mute
```

再把当前截图和 `artifacts/gopay` 里的最新 JSON 一起看。很多页面问题不是 OCR，而是 UI dump 文本变化。

### 3. OTP 超时

如果你是完整流程，建议加上：

```powershell
--retry-on-otp-timeout
```

当前逻辑会优先尝试复用同一个号码继续等；只有达到重试上限或号码剩余有效期太短时，才会放弃旧号并换新号。

### 4. 为什么命令都建议带 `--mute`

因为当前环境如果没安装 `pyttsx3`，不开 `--mute` 可能直接初始化失败。最稳妥的默认用法就是带 `--mute`。

## 友情链接
[LINUX DO - 新的理想型社区](https://linux.do/)
