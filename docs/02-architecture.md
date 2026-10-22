# 技术架构

状态：首版开发目标，尚未实现。需求来源：[需求与范围](01-requirements.md)。

## 1. 模块边界

```text
CLI 命令 / 交互向导 / JSON 与进度展示
                  ↓
目录服务 / 资源解析服务 / 下载协调服务
           ↓                 ↓
SmartEdu 平台适配器       下载引擎 / PDF 书签
           ↓                 ↓
HTTP 客户端与签名       临时文件 / 校验 / 完成清单
                  ↓
         配置 / 目录缓存 / 凭据存储
```

依赖从展示层指向服务层。核心层返回模型、结果和事件，不导入 CLI 模块，不调用菜单、窗口、进度条或交互确认。平台字段和 URL 规则只出现在 `providers/smartedu/` 中。

目标目录：

```text
textbook-cli/
  pyproject.toml
  README.md
  LICENSE
  THIRD_PARTY_NOTICES.md
  docs/
  src/textbook_cli/
    __main__.py
    cli/              # commands、interactive、renderers
    core/             # models、errors、catalog、resolver、downloads
    providers/smartedu/  # catalog、resolver、auth、http
    storage/          # settings、cache、credentials、manifest
    download/         # engine、paths、integrity、bookmarks
  tests/
    fixtures/smartedu/
    unit/
    integration/
    cli/
```

模块拆分以职责为准，不要求每项能力立即拆成多个文件。原项目作为审查参考，不成为新项目的运行时导入来源。

## 2. 技术选型

| 技术 | 用途 | 约束与依据 |
| --- | --- | --- |
| Python 3.10+ | 业务核心 | 保留上游语言基础；实际依赖版本须支持最低版本 |
| [Typer](https://typer.tiangolo.com/) | 参数、子命令、帮助 | 命令函数只装配输入输出和服务 |
| [prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/dialogs.html) | 选书、过滤、多选 | 只在 TTY 中启用；与下载进度展示分阶段使用 |
| [Rich](https://rich.readthedocs.io/en/stable/progress.html) | 表格、逐项进度 | 所有下载进度写 stderr，不污染 JSON stdout |
| [Requests](https://requests.readthedocs.io/en/latest/user/quickstart/) | 同步网络访问 | 每工作线程独立 Session，默认超时 `(10, 60)` |
| [platformdirs](https://platformdirs.readthedocs.io/en/latest/) | 配置与缓存路径 | 用平台标准用户目录，不覆盖上游应用配置 |
| [keyring](https://keyring.readthedocs.io/en/latest/) | 系统凭据存储 | 仅接受经确认的系统安全后端，不静默回退明文文件 |
| [filelock](https://py-filelock.readthedocs.io/en/latest/) | 输出目录互斥 | 在支持的平台使用原生文件锁；首版面向本地文件系统 |
| pypdf | 可选书签处理 | 延迟导入；迁移上游大图 PDF 修复并保留其相关测试 |
| sqlite3、hashlib、pathlib | 完成清单、校验、路径 | Python 标准库 |

开发依赖使用 pytest、Ruff 和构建工具。P0 阶段选择支持 Python 3.10 的具体版本，记录兼容范围并锁定开发及 CI 环境；不在所有依赖上无条件追随最新版。不携带 Tkinter、Pillow、sv-ttk、psutil、pywin32 等上游桌面功能依赖。

## 3. 上游复用映射

以下路径基于 [基线提交](https://github.com/happycola233/tchMaterial-parser/tree/244170e17bf8bc97401f65e04d261533d252572a)。

| 上游来源 | 迁入位置 | 需要调整的内容 |
| --- | --- | --- |
| [catalog.py](../tchMaterial-parser/src/tchmaterial_parser/catalog.py) | provider 目录适配器 | 验证响应、保留多分类路径、扁平教材索引和完整缓存 |
| [api.py](../tchMaterial-parser/src/tchmaterial_parser/api.py) | provider 资源解析器 | 首版限定 PDF；返回大小、MD5、资源身份；结构化异常替代 `None` |
| [auth.py](../tchMaterial-parser/src/tchmaterial_parser/auth.py) | provider 签名模块 | 保留中文路径、查询串、时钟差与动态 nonce 规则，使用显式传入凭据 |
| [network.py](../tchMaterial-parser/src/tchmaterial_parser/network.py) | provider HTTP 客户端 | 移除共享可变全局状态，限制请求范围，统一超时、重试和脱敏 |
| [ui/download_panel.py](../tchMaterial-parser/src/tchmaterial_parser/ui/download_panel.py) | download 与核心协调服务 | 抽取路径、流式写入、镜像、取消；去掉控件、全局任务列表和 Tk 调度 |
| [ui/resource_tree.py](../tchMaterial-parser/src/tchmaterial_parser/ui/resource_tree.py) | core 目录服务 | 迁出 URL 构建、遍历与分类选择纯函数；明确区分分类和教材 |
| [bookmarks.py](../tchMaterial-parser/src/tchmaterial_parser/bookmarks.py) | download 书签模块 | 接收独立输入输出路径，不让书签失败损坏已校验的原文件 |

复制代码前保留 MIT 声明并记录出处；不复制桌面资源图片、注册表配置和窗口代码。迁移测试时同时检查是否仍间接导入 GUI。

## 4. 核心模型与接口契约

模型可以使用数据类实现。下表定义必需语义，字段类型随实际契约测试细化。

| 模型 | 关键字段与语义 |
| --- | --- |
| `Book` | `resource_id` 优先取 `content_id`，缺失时取目录 `id`；另存 `catalog_id`；包含 `title`、`resource_type`、多条 `category_paths`、规范路径 `primary_category_path`、可用分类标签与规范页面 URL |
| `Category` | 明确的分类 ID、名称、子分类与教材引用；空分类没有教材身份 |
| `Artifact` | `resource_id`、`artifact_id`、格式、下载候选地址、可选修订标识、`expected_size`、`expected_md5`、章节；带凭据的地址不进入持久清单 |
| `Credentials` | `access_token`、`mac_key`、整数 `diff`；禁止默认字符串表示输出秘密字段 |
| `CatalogSnapshot` | `schema_version`、内容获取时间 `fetched_at`、最近成功检查时间 `checked_at`（均为 UTC）、平台目录版本、分类摘要、教材索引、数据来源和必要警告 |
| `DownloadTask` | 本次任务 ID、资源与文件身份、目标路径、状态、取消信号；执行状态仅在内存中 |
| `ProgressEvent` | 任务 ID、阶段、尝试序号 `attempt`、本次尝试已收字节、可空总量、速度、消息；不包含认证头或秘密 URL |
| `DownloadResult` | 状态、路径、字节数、验证信息、书签结果、警告和结构化错误 |

`artifact_id` 优先用平台明确的文件标识；缺失时由规范文件路径等非秘密稳定字段生成摘要，不使用标题或 CDN 主机名作为唯一身份。修订标识、MD5 和大小均允许缺失，不制造平台没有提供的值。

服务层至少提供：目录查询与刷新、按 URL/ID 解析教材、凭据获取与保存、构造下载计划、执行批次、取消批次。业务异常至少区分 `INVALID_INPUT`、`UNSUPPORTED_RESOURCE`、`NETWORK_ERROR`、`AUTH_REQUIRED`、`AUTH_REJECTED`、`RESOURCE_NOT_FOUND`、`PROVIDER_SCHEMA_CHANGED`、`INTEGRITY_FAILED`、`FILE_CONFLICT`、`OUTPUT_BUSY`、`FILESYSTEM_ERROR`。映射到进程退出码由 CLI 完成。

## 5. 平台适配与目录缓存

上游当前使用的普通教材数据流程：

| 数据 | 基线端点 |
| --- | --- |
| 分类 | `https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json` |
| 目录版本与分片 URL | `https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json` |
| 普通教材详情 | `https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{content_id}.json` |
| 私有文件候选 CDN | `r1-ndr-private.ykt.cbern.com.cn`、`r2-ndr-private.ykt.cbern.com.cn`、`r3-ndr-private.ykt.cbern.com.cn` |

这些是审查到的网页数据端点，不应描述成平台承诺稳定的第三方 API。

目录刷新过程为：拉分类 → 拉版本索引 → 读取其全部分片 → 校验并归一化 → 生成新快照 → 原子替换缓存。优先保留全部分类路径。允许隔离少量明确无效的记录并给出计数警告；任何必需分片失败时，不将不完整目录替换为正式快照。平台版本、分片地址集合和分类摘要均未变化，且已有相同版本完整快照时，可以复用内容，仅更新 `checked_at`；分类变化时即使平台版本号未变，也要重新构建完整快照。

缓存写入 `platformdirs` 返回的用户缓存目录，TTL 自 `checked_at` 起计算。不足 24 小时可直接使用；达到 24 小时则尝试刷新，失败继续使用可读旧快照并显示获取时间、检查时间和警告，不能以失败检查更新时间。新快照的两个时间相同；复用快照只更新检查时间。没有可用缓存且刷新失败，目录命令失败，但规范资源 ID 或页面 URL 下载不依赖目录先成功。旧 `catalog_id` 别名仅在现有可用索引能够消歧时接受，无法消歧则明确报错，不强制刷新整个目录或猜测身份。显式 `catalog refresh` 失败返回错误，旧缓存仍保留。

搜索对名称和每条完整分类路径做 `casefold()`，空白拆分关键词后要求同一条可搜索路径全部命中。关键词与分类过滤也必须在同一条路径上联合命中，不能跨路径拼出不存在的组合。多个分类匹配同本教材时只返回一个 `resource_id`。分类过滤使用可确认的完整元数据值。

全部分类路径按逐层名称的 `casefold()` 元组排序，以原名称元组打破相同折叠值的平局，第一条定义为 `primary_category_path`。返回结果按规范路径、标题、资源 ID 稳定排序后分页，不假定拼音排序。规范路径用于首次保存，与用户从哪个分类或搜索结果选择无关；展示可以另用第一条匹配路径，但必须保留全部路径。

## 6. HTTP、签名与凭据

输入 URL 只接受支持的 HTTPS 平台页面格式。提取并验证内容 ID 后，由适配器构造元数据 URL，不直接请求任意用户地址。元数据和下载主机都使用适配器维护的显式允许列表；新增域名需依据真实响应和测试更新。

认证头只发往经确认需要认证的官方主机。禁用自动携带秘密头跨域重定向：逐跳验证目标，需要签名时按新 URL 重新生成；跨入未允许主机即失败。原始 Token 不拼到 URL。共享配置不可被下载线程原地修改，每个线程使用独立 Session 与本次凭据快照。

每次签名沿用上游的 `X-ND-AUTH` 规则：HMAC-SHA256 与 Base64，签名涵盖 nonce、方法、解码路径及原查询串、主机名；`diff` 为时钟差。保留中文路径和查询串的回归样本。此实现服务于用户自己的授权访问，不提供绕过认证的承诺。

凭据优先级：当前 `download --credentials-file` → 当前向导明确输入的临时凭据 → 系统凭据库 → 无凭据尝试公开资源。凭据文件是 UTF-8 JSON，校验三项字段后仅在内存使用，不复制到配置文件；用户自行管理原始文件。

浏览器自动导入是用户显式触发的导入渠道：解析本机 Chromium 系浏览器本地存储中平台域的凭据记录，结果与手动导入一致（保存到钥匙串或仅本次使用）。来源主机名按平台域名精确匹配，不接受同名前缀的第三方域名；先按数据库、键和写入序号还原每个键的最新状态再提取，先写入后删除的记录不导入。写入序号只在同一浏览器配置的数据库内有序，不跨配置比较：同一配置内取序号最新的一组，多个配置都有记录时让用户选择来源，不自动宣称“最新”。导入必须由用户在向导或 `auth grab` 中主动发起；不在启动时静默读取，不读取 Cookie 或其他站点数据，不支持自动登录与刷新。

持久保存采用系统安全凭据后端，服务名 `textbook-cli`、账户槽位 `default`，保存一个凭据对象。拒绝明文、空实现或无法确认安全性的后端。后端不可用时，独立 `auth import` 失败；向导可以明确选择“仅本次使用”，服务器可通过下载参数读取凭据文件。`auth status` 只报告存在、来源和后端，不暴露凭据，也不宣称凭据仍有效。

日志及输出在格式化异常前完成脱敏，覆盖 Token、MAC 密钥、认证头与已知秘密查询参数。`doctor` 不打印配置全文、凭据文件内容或完整环境变量。CLI 不从上游工具的注册表或 JSON 文件自动迁移凭据。

网络重试约定：

| 情况 | 处理 |
| --- | --- |
| 连接失败、读取超时、502/503/504 | 退避后重试；适配器确认等价时可尝试已允许镜像 |
| 429 | 尊重可解析的 `Retry-After`；等待超过单次 60 秒上限则报告限流并结束当前任务 |
| 私有文件 HTTP 400 | 保留状态和可读错误码；同地址新签名最多重试两次，不直接判定凭据过期 |
| 401/403 | 区分缺少凭据与凭据/权限被拒；不通过镜像轮换规避 |
| 404 或明显结构变化 | 分别报告资源缺失或适配失效，不无条件重试 |

每个逻辑 HTTP 请求总尝试次数最多 3 次，含镜像切换；响应体读取失败后重新发起下载也占用同一预算。默认重试等待 1 秒、3 秒，可加小幅抖动；所有等待可被取消。批次请求起始间隔至少 0.2 秒；并发 2、最多 3。连接和读取超时为 10/60 秒。响应正文、错误正文和元数据读取应设合理大小上限，避免无界内存使用。

## 7. 下载、校验与文件提交

状态机：

```text
queued → resolving → connecting → downloading → verifying
                                              → [bookmarking] → committing → completed
其他终态：skipped / failed / cancelled
```

允许 `connecting → connecting` 或 `downloading → connecting` 的重试回边，每次增加 `attempt`。流式下载失败后删除本次临时内容，从零开始新的尝试；进度字节只要求在单次尝试内单调递增。显示重试序号并重置本次进度，不把重复接收的网络字节计为最终文件大小。

解析结果只选择支持的 PDF 文件。由协调服务按 `resource_id + artifact_id` 去重并分配路径，工作线程负责传输和校验，展示层消费事件；清单由协调线程串行写入。

1. 输出默认 `./textbooks`，规范化为绝对路径；取得 `<output>/.textbook/write.lock` 的进程锁。另一工具进程正在使用同根目录时返回 `OUTPUT_BUSY`，不等待覆盖。
2. 按“学段/学科/版本”构造子目录，缺失层级省略。清理分隔符、控制字符、保留名称和过长字段；各平台均检查最终路径不得越出输出根目录。
3. 首次保存采用规范分类路径，文件名为清理后的标题加短资源 ID，例如 `教材标题--bdc00134.pdf`。短 ID 冲突时延长；同资源多文件时加文件身份后缀。分配路径时同时检查下载清单中其他资源已占用的路径：同标题、同目录且短 ID 相同的其他教材会使后缀继续延长，而不是报冲突或在覆盖时替换对方文件。相同资源已有可信清单记录时以其中的安全相对路径为准，避免分类或标题变化导致重复落盘。分类和排序不能改变规范资源身份。
4. 同目录独占创建唯一的 `.part` 临时文件。校验成功前不更改旧完成文件；本次失败或正常取消只删除本任务创建的临时文件。
5. 传输时计算源文件摘要并核对可用的元数据大小/MD5；`Content-Length` 仅在传输编码语义允许时用于额外核对。首版做轻量 PDF 检查：非空、前 1024 字节内有 `%PDF-` 头，拒绝明显 HTML 登录页等响应；不宣称据此验证了全部 PDF 内部结构。完整解析只在启用书签时执行。MD5 用于平台内容对照，不用于安全身份认证。
6. 若启用书签，在另一临时文件上修改，按实际写入的书签数报告结果：成功写入至少一条时采用修改版；失败或没有任何章节取得有效页码时保留已验证的原 PDF，返回警告并记录 `bookmarks_applied=false`。父章节缺页码时仍继续处理其页码有效的子章节。
7. 计算最终文件 SHA-256，关闭文件，原子发布目标，再用 SQLite 事务记录完成清单。无覆盖授权时，发布必须在目标已存在时失败，例如采用同文件系统的独占硬链接发布后删除临时链接；不能用“先检查再 `os.replace`”替代独占发布。不支持安全发布的文件系统明确报错。显式 `--overwrite` 时才使用原子替换。文件与数据库无法组成跨资源原子事务；数据库提交失败时保留完整 PDF，明确报告“文件已保存，清单登记失败”，不能声称任务完整成功或在重跑时盲目认领。
8. 提交临界区内延迟处理取消；提交完成后保留该文件，再停止剩余任务。释放锁并返回批次结果。

`--overwrite` 是明确的替换授权。没有该参数时，已有目标只有满足清单验证才能跳过；有文件但无可信清单、散列变化、源版本变化或书签选项不一致时报告冲突，提示用户选择其他目录或显式覆盖。清单指向的文件已缺失时允许重新下载，不把缺失当作成功。启动前已知冲突不影响其他有效任务执行。

临时文件和目标在同一文件系统，以满足原子替换条件。进程被强制终止或断电时可能留下 `.part`；首版只检测并提示，不把它当作完成文件，也不承诺续传或自动删除未知文件。

## 8. 完成清单与配置

每个输出根目录建立 `.textbook/manifest.sqlite3`，模式版本独立管理。数据库只登记完成文件，不保存未完成队列、认证头、凭据或含秘密的文件 URL。

| 清单字段 | 用途 |
| --- | --- |
| `resource_id`、`artifact_id` | 唯一身份及去重 |
| `upstream_revision`、`expected_md5`、`expected_size` | 可用时确认源内容版本与校验依据 |
| `relative_path`、`completed_at` | 本地路径和 UTC 完成时间 |
| `source_md5`、`source_sha256`、`source_size` | 书签改写前的传输文件对照 |
| `final_sha256`、`final_size` | 最终落地文件完整性 |
| `bookmarks_requested`、`bookmarks_applied`、`warnings` | 解释本地加工结果及警告 |

重新下载前先解析当前元数据，再与清单比对身份、可用版本信息、处理选项及本地散列。可确认同一源且文件校验通过时跳过；没有平台修订或摘要可比时，可以报告“本地已有且校验通过，平台内容版本未确认”的跳过及警告，但不能声称已验证最新。显式覆盖始终重新传输。

非秘密设置写入平台配置目录下的 `settings.json`，首版只需模式版本和必要偏好；默认值仍以 CLI 契约为准，不引入未文档化的环境变量覆盖。配置和缓存采用临时文件写入后原子替换，模式不兼容时可重建缓存，但不能擅自丢弃完成清单。只读、损坏和磁盘不足要形成结构化错误。

## 9. 实现前需以样本确认的事项

- 真实目录中年级标签、文件标识、MD5、大小和修订字段的覆盖率；缺失值按本文规则处理。
- 书签页码与章节树的对应关系，以及大图 PDF 的实际内存占用。
- 认证主机、重定向目标和限流响应的实际行为；未确认主机不自动放行。
- 三平台凭据后端的可用性，以及 Python 3.10 对应的依赖版本组合。

上述事项是开发任务，不是等待用户补充信息的阻塞项。通过脱敏响应样本、离线测试和有授权的真实平台联调逐项确认。开发顺序见 [开发计划](04-development-plan.md)，验收边界见 [测试与验收](05-testing.md)。
