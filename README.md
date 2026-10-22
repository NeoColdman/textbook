# textbook-cli

以命令行交互界面浏览、搜索和下载[国家中小学智慧教育平台](https://basic.smartedu.cn/)中的电子教材。支持 Windows、macOS、Linux 和 Python 3.10+，不需要图形桌面。

项目参考 [tchMaterial-parser](https://github.com/happycola233/tchMaterial-parser) 的平台适配算法，提供独立的命令行、目录缓存、文件校验和下载清单。

## 安装与启动

推荐在本项目目录使用 [uv](https://docs.astral.sh/uv/)：

```sh
uv sync --locked
uv run textbook --help
uv run textbook
```

最后一条命令在交互终端中打开选书界面。也可以用已有 Python 创建虚拟环境并执行 `python -m pip install .`，激活后运行 `textbook`。开发依赖安装与最低 Python 版本组合以 `uv.lock` 为准。

## 交互界面

界面参考 Claude Code 的终端风格：暖橙色强调、`❯` 输入提示、紧凑的内联菜单，沿用终端背景并保留上方输出。启动后可直接输入 `数学 七年级 下册` 搜索，也可使用主页命令：

| 命令 | 用途 |
| --- | --- |
| `/browse` | 按分类浏览教材 |
| `/search [关键词]` | 搜索教材；省略关键词时打开搜索输入框 |
| `/paste` | 按行粘贴教材页面链接或资源 ID |
| `/selected` | 查看全部已选教材，包括当前筛选隐藏的选择 |
| `/download` | 打开已选教材的下载确认页 |
| `/auth` | 管理登录凭据 |
| `/help` | 查看命令与快捷键 |
| `/quit` | 退出 |

主页输入 `/` 查看命令提示，Tab 补全，↑↓ 查阅本次会话的输入历史；Ctrl+D 或 `/quit` 正常退出，Ctrl+C 取消并退出 130。菜单使用 ↑↓ 移动、Enter 确认，也可按数字 1–9 快捷选择；Esc 返回。粘贴多行内容时用 Ctrl+D 或 Ctrl+S 确认。

选书时用空格勾选，Enter 进入分类或继续，`/` 修改搜索，`a` 全选当前结果，`u` 清空当前结果，`v` 查看全部已选，`d` 确认下载，Esc 返回。搜索或切换分类会保留已选教材。设置 `NO_COLOR` 时使用无颜色、无光标控制的编号式文本向导。

## 搜索和下载

```sh
textbook catalog refresh
textbook search "数学 七年级 下册" --stage "初中" --edition "人教版"
textbook search "数学 七年级" --json
textbook browse --query "语文"
```

从搜索结果复制资源 ID，或复制平台普通教材的预览页面 URL。以下 ID 仅为占位说明：

```sh
textbook download --id RESOURCE_ID -o ./books
textbook download --id RESOURCE_ID --bookmarks
textbook download --input books.txt --jobs 2 --json
```

`books.txt` 是 UTF-8 文件，每行一个教材页面 URL 或资源 ID；`--input -` 可以从标准输入读取。多个 URL、重复的 `--id` 和输入文件可以合并。首版只支持普通电子教材 PDF，课程、音频与视频不在范围内。

默认保存到当前目录下的 `textbooks/`，按学段、学科、版本归类，文件名附资源 ID。书签默认关闭；开启后如书签处理失败，会保留已验证的原始 PDF并报告警告。

目录有 24 小时缓存，断网时可搜索已有完整快照并显示其时间。直接通过规范资源 ID 或 URL 下载不要求先获取完整目录。

## 登录凭据

部分文件需要你自己的有效登录凭据。先通过官方网页登录，按[上游凭据获取说明](https://github.com/happycola233/tchMaterial-parser/tree/244170e17bf8bc97401f65e04d261533d252572a#readme)取得包含 `access_token`、`mac_key`、整数 `diff` 的 JSON。

```sh
textbook auth import
textbook auth status
textbook auth clear
```

最省事的方式是让工具从浏览器导入：在已登录平台的 Chrome 里运行一次 `textbook auth grab`，或在向导启动引导中选择“从浏览器自动导入”。工具只读取本机浏览器本地存储中平台域的凭据记录并保存到系统钥匙串，不读取其他站点数据，也不会上传任何内容；也可以改用下述手动方式。

`auth import` 在终端隐藏输入，也可用 `--file credentials.json`。向导启动时若没有已保存的凭据，会自动提示一次导入并给出获取步骤，可稍后再说；随时可在“凭据管理”中重新导入。持久凭据只存到系统安全凭据库。没有可用凭据后端的服务器，可在一次下载中从文件读取，不保存到本工具配置：

```sh
textbook download --input books.txt --credentials-file credentials.json
```

文件参数只包含路径，不把 Token 放在命令行。自行妥善保存和删除原始凭据文件；不要提交到版本库或分享。凭据存在不代表仍有效，本工具不自动登录、读取浏览器凭据或刷新 Token。无凭据时仅尝试公开资源，不保证匿名下载可用。

## 已有文件与取消

- 同一输出根目录的 `.textbook/manifest.sqlite3` 保存已完成文件记录，核对资源身份、版本与本地文件摘要后才跳过。
- 同名但没有可信清单、现存文件损坏或源内容变化时，默认报告冲突。显式 `--overwrite` 才替换；新文件校验成功前保留原文件。
- Ctrl+C 取消批次，保留已完成文件，清理本次未完成临时文件。读取中的网络请求可能需要等待超时退出。
- 失败项可在向导重试；也可重新运行原命令。重试从头下载，不支持字节续传或跨次启动恢复未完成队列。
- SQLite 登记失败时保留已保存的完整 PDF，但结果报告失败；重跑不会仅凭其存在自动认领。

同一输出根目录只允许一个工具进程写入。首版的文件锁和原子发布面向本地文件系统，不承诺网络共享盘行为。

## 输出与排错

`search --json` 和 `download --json` 向 stdout 输出一个 JSON 对象，进度与诊断写入 stderr。退出码：0 成功，1 运行失败，2 输入错误，3 平台认证/权限问题，4 部分成功，130 用户中断。

```sh
textbook doctor
textbook catalog status
```

`doctor` 只做本地诊断，不联网、不读取秘密值。检查不到系统凭据库时可以使用临时凭据文件。HTTP 400 不一定是 Token 过期，可能涉及签名、参数或限流，应结合具体错误处理。

升级源码后在项目目录重新运行 `uv sync --locked`。卸载前可先执行 `textbook auth clear`；移除项目虚拟环境不会删除已下载教材、平台缓存或系统凭据。

## 开发与验证

```sh
uv sync --locked --group dev
uv run ruff check src tests
uv run pytest -m "not live"
uv build
```

默认测试使用脱敏样本、假凭据后端及本地 HTTP 服务，不访问平台、不读取用户凭据。[CI](.github/workflows/ci.yml) 配置了三平台及 Python 3.10/3.13 的检查；本机已执行结果与待验证范围以[实现与验收记录](docs/06-implementation-status.md)为准，不能把 CI 配置视为已运行结果。

完整设计、开发顺序和验收规范见[开发文档](docs/README.md)。依赖兼容范围记录在 `pyproject.toml`，精确解析结果记录在 `uv.lock`。平台网页接口会变化，新问题应提供脱敏错误、工具版本与教材资源 ID。

## 授权与资源来源

软件采用 [MIT](LICENSE)，上游复用和运行依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。工具不托管、不随发行包分发教材内容；教材版权与平台使用要求独立于软件许可证。所有访问直接由用户本机发起。
