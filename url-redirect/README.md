# 本地支付长链生成器 | 二开自 https://linux.do/t/topic/2189355

一个纯本地运行的小工具：粘贴 ChatGPT `accessToken` 后，在本机创建 hosted checkout 支付会话，并返回支付链接。

## 隐私说明

- 服务端代码不会保存 token、代理、优惠码或生成结果。
- 代理地址只保存在用户自己浏览器的 `localStorage`。
- 服务端请求日志已关闭。
- 发布包不包含任何个人 token、代理、服务器 IP、域名或本机路径。

## Windows 启动

1. 安装 Python 3。
2. 解压本目录。
3. 双击 `run.bat`。
4. 浏览器会自动打开：

```text
http://127.0.0.1:7790/
```

首次启动会自动创建 `.venv` 并安装依赖。

## 手动启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py
```

## 使用

1. 点击页面中的“复制 Session 地址”。
2. 在已登录 ChatGPT 的浏览器中打开该地址。
3. 复制返回 JSON 里的 `accessToken`，或复制整段 JSON。
4. 粘贴到本工具。
5. 根据需要选择 Plus / Team、地区、代理和优惠码。
6. 点击“生成支付长链”。

## 代理

支持：

```text
http://127.0.0.1:7890
socks5h://user:pass@host:port
socks5://user:pass@host:port
```

检测代理时会优先使用 `http://iprust.io/ip.json`，并自动尝试常见代理协议。
