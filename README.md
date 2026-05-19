# 交互式协议注册/授权 CLI

这是一个基于 Python 3.11 的命令行项目，用于执行 ChatGPT 账号注册、登录会话保存和 OAuth 授权流程。项目已内置 `utils.auth_core` 运行依赖，不需要依赖其他本地项目目录。

## 项目结构

```text
openai-auto/
├── pyproject.toml          # 项目元数据、依赖和命令行入口
├── uv.lock                 # uv 锁定文件
├── README.md               # 使用说明
├── config/                 # 本地配置目录，授权文件默认放这里
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

## 运行条件

必须使用 Python 3.11，因为内置 `auth_core` 依赖 CPython 3.11 运行环境。项目使用 `uv` 管理依赖。

首次同步依赖：

```bash
cd /mnt/e/code/openai-auto
uv sync
```

项目已内置根目录 `utils` 作为最小 `utils.auth_core` 运行依赖，运行时不会读取其他本地项目目录。

## 常用命令

注册新账号：

```bash
uv run protocol-reg --mode register --proxy http://127.0.0.1:7897
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
uv run python -m protocol_reg --mode register
uv run python scripts/register_cli.py --mode register
```

## 默认文件

注册账号输出：

```text
data/accounts.txt
```

授权 token 输出：

```text
data/tokens.jsonl
```

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
- 成功后把邮箱和密码按 `邮箱----密码` 写入 TXT 文件。

登录流程：

- 交互式输入已有账号邮箱和密码。
- 如触发邮箱二次验证，人工查看邮箱后在终端输入验证码。
- 程序请求 `https://chatgpt.com/api/auth/session` 获取身份信息。
- 程序调用 `https://chatgpt.com/backend-api/payments/checkout` 获取美区 Plus 0 刀试用 hosted checkout 链接。
- 程序保存登录 cookies，不换 token。

授权流程：

- 交互式输入已有账号邮箱。
- 程序优先读取 `login` 模式保存的 cookies。
- 没有可用 cookies 时，交互式输入已有账号密码并即时登录。
- 遇到账号选择页时，程序会自动选择当前输入邮箱。
- 程序走 OAuth PKCE 换取 token。
- 成功后请求 `https://chatgpt.com/api/auth/session` 获取身份信息。
- 成功后把邮箱、密码、token 和身份信息写入 JSONL 文件。

## 边界

- 当前版本只做邮箱验证码注册；触发手机号验证会停止并提示。
- `login` 和 `authorize` 已拆开；`authorize` 优先使用已保存 cookies，没有 cookies 时会要求输入密码并即时登录。
- `register` 和 `login` 不做 OAuth 授权，只获取 ChatGPT session 身份信息。
- 当前版本不自动读邮箱；验证码由手动输入。
- 授权、Sentinel token 生成和风控挑战依赖内置 `utils.auth_core`。
