# 交互式协议注册/授权 CLI

这是一个基于 Python 3.11 的命令行项目，用于执行 ChatGPT 账号注册、登录会话保存和 OAuth 授权流程。项目已内置 `utils.auth_core` 运行依赖，不需要依赖其他本地项目目录。

## 项目结构

```text
openai-auto/
├── pyproject.toml          # 项目元数据、依赖和命令行入口
├── uv.lock                 # uv 锁定文件
├── README.md               # 使用说明
├── config/                 # 本地配置目录，授权文件默认放这里
│   ├── protocol-reg.example.yaml # protocol-reg 配置示例
│   └── wenfxl.license      # 默认授权文件
├── data/                   # 运行输出目录，账号、token、会话和数据库默认放这里
├── protocol_reg/           # 注册、登录和授权 CLI 主代码
│   ├── cli.py              # 命令行入口与交互输入
│   ├── flow.py             # 注册、登录与授权主编排
│   ├── openai_http.py      # OpenAI Auth HTTP 请求与重定向
│   ├── auth_core_client.py # 内置 auth_core 薄封装
│   ├── oauth.py            # OAuth PKCE 与 token 交换
│   ├── storage.py          # TXT/JSONL/JSON 输出
│   ├── settings.py         # 运行配置
│   └── utils.py            # 密码、资料、邮箱脱敏等工具
├── scripts/
│   └── register_cli.py     # 本地开发启动脚本
└── utils/                  # 最小内置 auth_core 兼容依赖
    ├── auth_core*.so       # CPython 3.11 auth_core 扩展
    ├── config.py           # auth_core 所需最小配置
    └── db_manager.py       # auth_core 所需最小 KV 存储
```

`data/*`、虚拟环境、缓存和构建产物不会提交到 Git。默认授权文件 `config/wenfxl.license` 会随项目提交。
实际运行配置文件 `config/protocol-reg.yaml` 默认不提交，避免泄露邮箱验证码 API 密钥。

## 运行条件

必须使用 Python 3.11，因为内置 `auth_core` 依赖 CPython 3.11 运行环境。项目使用 `uv` 管理依赖。

首次同步依赖：

```bash
cd /mnt/e/code/openai-auto
uv sync
```

项目已内置根目录 `utils` 作为最小 `utils.auth_core` 运行依赖，运行时不会读取其他本地项目目录。

## 常用命令

直接运行，用上下键选择注册、登录或授权模式，回车确认；也可以直接按 `1/2/3`：

```bash
uv run protocol-reg
```

也可以继续用 `--mode` 指定模式，指定后不会出现模式选择提示。

注册新账号：

```bash
uv run protocol-reg --mode register --proxy http://127.0.0.1:7897
```

注册成功后默认只显示并保存支付长链接，不自动打开浏览器：

```bash
uv run protocol-reg --mode register --proxy http://127.0.0.1:7897
```

需要自动打开支付页面时：

```bash
uv run protocol-reg --mode register --proxy http://127.0.0.1:7897 --open-checkout
```

自动打开支付链接时使用无痕模式：

```bash
uv run protocol-reg --mode register --proxy http://127.0.0.1:7897 --open-checkout --incognito-checkout
```

仅登录并保存会话：

```bash
uv run protocol-reg --mode login --proxy http://127.0.0.1:7897
```

使用已保存会话授权：

```bash
uv run protocol-reg --mode authorize --proxy http://127.0.0.1:7897
```

不需要代理时：

```bash
uv run protocol-reg --mode register
uv run protocol-reg --mode login
uv run protocol-reg --mode authorize
```

也可以使用模块或开发脚本运行：

```bash
uv run python -m protocol_reg
uv run python scripts/register_cli.py
```

## 默认文件

运行配置默认路径：

```text
config/protocol-reg.yaml
```

生成默认运行配置：

```bash
uv run protocol-reg --init-config
```

也可以从示例配置复制：

```bash
cp config/protocol-reg.example.yaml config/protocol-reg.yaml
```

配置文件支持直接设置代理，命令行 `--proxy` 会覆盖配置文件：

```yaml
proxy: "http://用户名:密码@主机:端口"
```

也可以配置多个代理，程序会在 CLI 运行、Web 任务、批量订阅类型刷新等出网调用之间按轮询平均分配；`proxies` 优先级高于单个 `proxy`：

```yaml
proxies:
  - "http://用户名:密码@主机1:端口"
  - "http://用户名:密码@主机2:端口"
```

环境变量也支持多代理：

```bash
PROTOCOL_REG_PROXIES="http://127.0.0.1:7897,http://127.0.0.1:7898" uv run protocol-reg-web
```

配置文件还支持设置 Web 端任务最大并发数，超出的任务会自动排队：

```yaml
max_concurrency: 3
```

配置文件支持设置注册邮箱随机后缀。注册模式下邮箱留空时，会从这些后缀中生成高熵随机邮箱，并避开本地已有记录；Web 端批量注册还会避开当前队列中已占用的邮箱：

```yaml
email_suffixes:
  - "example.com"
  - "example.net"
```

邮箱前缀本身会优先从名字表里拼出几种常见形态，例如 `firstname.lastname`、`firstnamelastname`、`flastname`，再带上两位数字后缀，最后才和随机后缀拼成完整地址。程序会把生成结果和已有记录做去重，CLI 会扫描 `accounts.txt`、`accounts_rt.txt`、`tokens.jsonl`、`sessions.json`，Web 会额外检查数据库和当前排队任务。

邮箱验证码配置里还可以调重试和代理策略：

```yaml
email_code:
  max_otp_retries: 5
  otp_poll_max_attempts: 20
  use_proxy: false
```

这个开关也兼容 `use_proxy_for_email` 写法。

注册账号输出：

```text
data/accounts.txt
```

账号主存储：

```text
data/data.db
```

账号数据库表为 `accounts`，字段包含 `email`、`password`、`subscription_type`、`refresh_token`、`session_json`、`checkout_url`、`stock_status`、`status`、`created_at`、`updated_at`、`last_login_at`、`last_authorized_at`。程序启动时会把 `data/accounts.txt` 和 `data/sessions.json` 导入数据库，再从数据库导出兼容的 `data/accounts.txt`。

启动本地账号管理 Web 页面：

```bash
uv run protocol-reg-web
```

默认监听 `0.0.0.0:8765`，会读取 `config/protocol-reg.yaml`，同一局域网设备可以访问 `http://本机局域网IP:8765`。页面支持搜索、筛选、新建、编辑、删除账号记录，可以直接把账号标记为“出库”或恢复为“未出库”，也支持多选后批量出库、批量恢复未出库、批量自动获取订阅类型和批量删除。页面也可以查看和复制账号的 checkout 长链接，以及手动同步导入和导出 `data/accounts.txt`。`/tasks` 页面里的“执行任务”面板可以直接执行 `register`、`login` 和 `authorize`；注册模式默认勾选随机邮箱、随机密码和生成 checkout，可填写任务数一次启动多个注册任务，多余任务会按 `max_concurrency` 排队。`/settings` 页面里可以调整自动注册的间隔、每轮注册数和是否生成 checkout，并单独启停自动注册。遇到邮箱验证码时任务会暂停并等待页面提交验证码，配置了 cloudflare-email 时仍会自动读取验证码。

需要指定数据库、配置文件、代理或端口时：

```bash
uv run protocol-reg-web --host 0.0.0.0 --port 8765 --db data/data.db --config config/protocol-reg.yaml --proxy http://127.0.0.1:7897 --max-concurrency 3 --output data/accounts.txt
```

账号文件格式：

```text
账号----密码----订阅类型----rt----session
```

订阅类型优先来自 `https://chatgpt.com/api/auth/session` 返回的 `account.planType`，OAuth `id_token` 中的 `chatgpt_plan_type` 会作为兜底。`session` 字段保存 `https://chatgpt.com/api/auth/session` 返回中的 `data` 对象单行 JSON；缺失字段会写为 `null`。`data/accounts.txt` 作为兼容导出文件保留，旧的 `data/accounts_rt.txt` 会在启动时自动合并进账号数据库，后续不再单独写入。

授权 token 输出：

```text
data/tokens.jsonl
```

支付链接输出：

```text
data/checkout_urls.jsonl
```

Web 或 CLI 生成 checkout 后会继续追加写入 `data/checkout_urls.jsonl`，并把长链接同步写入账号数据库的 `checkout_url` 字段；`data/accounts.txt` 仍保持五段兼容格式，不额外追加 checkout 字段。账号详情里可以点击“生成/重新生成”重新获取 checkout 长链接，适合原本没有链接或需要刷新链接的账号；也可以点击订阅类型旁边的“自动获取”，使用已保存 session 的 `accessToken` 请求 ChatGPT accounts/check 接口并更新 `subscription_type`。

登录会话输出：

```text
data/sessions.json
```

授权文件默认路径：

```text
config/wenfxl.license
```

需要指定其他授权文件时：

```bash
uv run protocol-reg --license-file /path/to/wenfxl.license
```

## 流程说明

注册流程：

- 交互式输入邮箱。
- 交互式输入密码，留空自动生成。
- 程序提交注册并触发邮箱验证码。
- 人工查看邮箱后在终端输入验证码。
- 程序继续创建账号，不走 OAuth 授权。
- 成功后请求 `https://chatgpt.com/api/auth/session` 获取身份信息。
- 成功后把账号数据写入 `data/data.db` 的 `accounts` 表，并同步导出 `data/accounts.txt`，缺失字段写 `null`。
- 程序调用 `https://chatgpt.com/backend-api/payments/checkout` 获取美区 Plus 0 刀试用 hosted checkout 链接。
- 没有获取到支付长链接时，支付自动化直接失败，但已注册账号不会丢失。
- 程序默认只显示并保存支付长链接，不自动打开浏览器；指定 `--open-checkout` 时才自动打开，配合 `--incognito-checkout` 会优先使用 Chrome/Edge/Brave/Chromium 无痕模式打开。

登录流程：

- 交互式输入已有账号邮箱和密码。
- 如触发邮箱二次验证，人工查看邮箱后在终端输入验证码。
- 程序请求 `https://chatgpt.com/api/auth/session` 获取身份信息。
- 程序更新 `data/data.db` 的 `accounts` 表，并同步导出 `data/accounts.txt`。
- 默认调用 `https://chatgpt.com/backend-api/payments/checkout` 获取美区 Plus 0 刀试用 hosted checkout 链接；指定 `--no-checkout` 时不请求 checkout。
- 程序保存登录 cookies，不换 token。

授权流程：

- 从账号数据库选择已有账号，自动读取邮箱和密码；也可以选择手动输入邮箱。
- 程序优先读取 `login` 模式保存的 cookies。
- 没有可用 cookies 时，已选择账号会直接使用保存的密码即时登录；手动输入邮箱时会再要求输入密码。
- 授权模式下的即时登录只用于获取 cookies，不请求 Stripe/checkout。
- 遇到账号选择页时，程序会自动选择当前输入邮箱。
- 程序走 OAuth PKCE 换取 token。
- 成功后请求 `https://chatgpt.com/api/auth/session` 获取身份信息。
- 成功后把邮箱、密码、token 和身份信息写入 JSONL 文件。
- 成功后同步更新账号数据库中该账号的 RT 和 session 字段，并导出 `data/accounts.txt`。

## 边界

- 当前版本只做邮箱验证码注册；触发手机号验证会停止并提示。
- `login` 和 `authorize` 已拆开；`authorize` 优先使用已保存 cookies，没有 cookies 时会要求输入密码并即时登录。
- `register` 和 `login` 不做 OAuth 授权，只获取 ChatGPT session 身份信息。
- 当前版本不自动读邮箱；验证码由手动输入。
- 授权、Sentinel token 生成和风控挑战依赖内置 `utils.auth_core`。
