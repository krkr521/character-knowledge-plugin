> **定制 fork**：本仓库基于 [QlzqQlzq/character-knowledge-plugin](https://github.com/QlzqQlzq/character-knowledge-plugin)。定制代码位于 `local/custom`，功能差异和安装限制见 [本地改动说明](LOCAL_CHANGES.md)。原作者及许可证保留。

# MaiBot Character Knowledge Plugin

保留原图，通过 AnimeTrace 等联网 API 以及可选本地角色库补充人物位置、可见外观与角色身份对应的 MaiBot SDK v2 插件。画面内容由 MaiBot 的多模态 Planner 自行理解。带真实图片数据的 QQ 表情包和 GIF 会走独立的 AnimeTrace 角色识别与持久缓存，不使用通用 VLM 猜角色名。

示例：

```text
[原图保留]
[第1张图角色身份辅助：第一格左侧，蓝白发、蓝色圆环 → 私库角色A；第二格中央，白发、深色圆环 → 私库角色B。画面含义由 Planner 查看原图。]
```

从 1.0.11 起，普通聊天中的图片不再被描述文字替换：每张原图后仅追加该图的角色身份辅助，不注入通用场景转述。视觉候选包含 `location`（分格与位置）和 `evidence`（该人物的可见外观），相同角色出现在不同位置时分别保留对应信息。身份有冲突或无法确认时不强行赋名；AnimeTrace 返回的无位置候选明确标记为“未定位”，不得按名字列表顺序分配给人物。无候选、异常、超时及超过识别数量限制的图片均保留。

## Configuration

编辑 `config.toml`，或在 MaiBot WebUI 中配置。默认启用 AnimeTrace 联网检索；视觉服务和本地角色库默认关闭，且不包含任何 API Key。

```toml
[vision]
enabled = false
provider = "gemini" # gemini or openai
api_key = ""
base_url = "https://generativelanguage.googleapis.com/v1beta"
model = "gemini-3-flash"
reasoning_effort = "auto" # OpenAI 兼容接口可设 low/medium/high/xhigh/max；auto 使用接口默认值
max_upload_bytes = 4194304

[library]
enabled = false
file_name = "characters.json"
admin_qq = []
allow_unrestricted_admin = false
max_prompt_characters = 40
pending_addition_ttl_seconds = 300

[reverse_image]
anime_trace_enabled = true
anime_trace_url = "https://api.animetrace.com"
anime_trace_max_upload_bytes = 900000
timeout_seconds = 15

[emoji]
enabled = true
enrich_description = true
cache_file = "emoji_roles.json"
cache_max_entries = 4096
positive_cache_ttl_days = 365
negative_cache_ttl_days = 7
```

`plugin.max_characters_per_image` 控制单张图片最多保留的角色候选数，默认 3，可设置为 1–10；多人图较多时可以适当调高。

默认模式通过 SDK 的 `llm.generate` capability 调用 MaiBot 已配置的 `vlm` 任务生成通用图片描述，不需要在插件里重复填写模型密钥。

本插件也内置可单独配置的 Gemini 或 OpenAI-compatible VLM。启用后，它会代替默认描述调用，并额外负责二次元人物判断和本地角色库匹配；图片会发送到这里配置的 `base_url`，并使用这里的 `api_key` 和 `model`。

OpenAI 兼容接口可通过 `vision.reasoning_effort` 指定推理强度，例如 GPT-6.1 Sol 使用 `model = "gpt-6.1-sol"`、`reasoning_effort = "xhigh"`。启用推理后，请求省略 `temperature`，并为思考与 JSON 输出提供 8192 token 的合计预算；实际支持的推理强度以模型为准。识别图片和创建、修正角色外观卡都会使用该设置。

`auto` 表示使用接口默认推理强度，不会向上游发送 `reasoning_effort`。旧配置中的空字符串仍会被兼容读取为 `auto`。

`anime_trace_enabled` 启用后，图片会上传到 AnimeTrace 用于二次元角色检索。图片会自动为该请求创建压缩副本，原图不会被改写。

`emoji.enabled` 启用后，适配器标记为 `emoji` 且携带图片二进制的收藏表情、动态表情和 GIF 会直接使用 AnimeTrace。明确候选按图片哈希保存在插件持久化目录的 `emoji_roles.json`；同一表情不会在缓存有效期内重复联网。单个明确候选会被采用；多个候选只有全部来自同一作品时才会保留，互相冲突的作品结果会整体丢弃。角色候选写在表情包描述的“画面/主体”语义字段中，不会混入“开心、无语”等情绪标签。QQ 原生小黄脸没有图片二进制，仍按普通文本表情处理。

## Recognition Modes

### Plugin VLM Mode

`vision.enabled = true` 时：

```text
plugin VLM: locate people + classify anime character + check private library
  -> not an anime character: do not query AnimeTrace
  -> anime character: query AnimeTrace if no private-library label exists
```

该模式由插件 VLM 提供人物位置、外观证据与私库身份匹配；原图和身份辅助一起进入 MaiBot。VLM 的通用描述仅供只读识别 API 使用，不替代原图，也不作为聊天画面解释注入 Planner。

### MaiBot VLM + Online Mode

`vision.enabled = false` 且 `anime_trace_enabled = true` 时：

```text
MaiBot vlm task and AnimeTrace run in parallel
  -> match: retain original image + unlocated AnimeTrace character candidates
  -> no match: retain original image
```

只读识别 API 继续沿用 MaiBot 的图片描述提示词风格。聊天始终保留原图；AnimeTrace 只负责补充候选，无法定位时不会把名字强配给图中的某个人物。

AnimeTrace 仅接受其返回的明确角色候选；低置信候选不会自动写入标签。多人图会保留多个明确角色标签，例如 `图片[角色A、角色B]`。

## Private Character Library

启用 `[library].enabled` 后，插件从 MaiBot 为本插件分配的持久化数据目录读取 `characters.json`。插件升级或替换源码目录不会覆盖角色库；旧版本源码目录中的 `data/characters.json` 会在首次启动时自动迁移。关系标签只来自本地库，例如 `图片[角色名]（自己）`。

生产环境应将允许管理角色库的 QQ 号写入：

```toml
[library]
admin_qq = ["123456789"]
```

`admin_qq` 留空时，聊天管理命令默认对所有人关闭。仅在受控的本地 WebUI 测试中，可显式设置 `allow_unrestricted_admin = true`；服务器上不应开启它。

### Commands

以下命令都需要本地角色库开启，并受 `admin_qq` 限制。命令会主动发送可见回执，适用于 QQ 和 WebUI。

```text
/角色添加 角色名 [关系]
/取消角色添加
/识图修正 角色名
/角色列表
/查看人设 角色名
/设置关系 角色名 关系
/添加别名 角色名 别名
/删除别名 角色名 别名
```

`/角色添加` 后，下一条独立图片会创建该角色的外观卡；等待默认在 5 分钟后失效，也可用 `/取消角色添加` 主动取消。`/识图修正` 会优先使用引用的旧图；不支持引用的平台会使用同一发送者在当前聊天最近识别过的图片。角色名称与别名必须保持唯一。

### 生图参考图

管理员添加角色或使用 `/识图修正` 补充角色时，会同时保存原始参考图。原图位于本插件持久化数据目录的 `reference_images/` 下，按内容 SHA-256 去重；更新外观卡、关系和别名会保留已有参考图。旧角色库继续可读，但只存了外观卡的条目需要补充原图后才能作为图片参考。

角色条目中的 `reference_images` 是元数据列表，格式为 `{"file":"reference_images/<角色ID>/<SHA256>.png","variant":"standard","sha256":"<SHA256>"}`。`file` 相对于 `characters.json` 所在目录；`variant` 可标记 `standard`、`chibi` 等外观版本。只接受可直接用于生图的有效 PNG、JPEG、WebP，单张最多 20 MiB、4000 万像素，不允许引用库外文件；GIF 需要先导出一帧。

只读工具 `get_character_knowledge(name)` 让聊天模型查询外观卡、是否保存原图及外观版本，返回 `character://角色名` 句柄，不返回本地绝对路径或图片 base64。配套 OAImage 的 `generate_image` 可传入 `characters: ["DeepSeek", "GLM"]`，将两位角色的已存原图一起用于生成。没有原图时，工具会明确返回 `has_reference_images=false`。

## Read-only Cross-plugin APIs

1.0.10 新增两个公开的 SDK 插件 API，版本均为 `1`。调用方需要在自己的 manifest 中声明 `api.call`，通过现有 Host 转发；不需要读取或复制本插件密钥。

```python
await ctx.api.call(
    "github.QlzqQlzq.character-knowledge-plugin.character.identify_image",
    version="1", image_base64=encoded_jpeg_or_png,
)
await ctx.api.call(
    "github.QlzqQlzq.character-knowledge-plugin.character.knowledge",
    version="1", name="角色名称或别名",
)
```

`character.identify_image` 复用当前配置下的私库视觉识别、AnimeTrace 和萌百核验，输入仅为 JPEG/PNG 的纯 base64 字符串，不接受路径、URL、聊天 ID 或管理指令。编码长度最多 3 MiB 字符，解码后最多 2 MiB、1600 万像素。图片仍只会发给本插件已启用的视觉和搜图服务。整个识别流程（含并发排队）最多 110 秒；调用方应按自己的剩余任务预算设置更短超时。

返回 `success/status/source/description/label/is_anime_character/candidates/knowledge_context/limitations`。每个候选保留 `source/kind/name/franchise/location/evidence/conflicts/not_confident/confirmed/accepted_in_label`；`location` 是分格与画面位置，旧结果及无位置的 AnimeTrace 候选为空。私库候选只有通过既有外观证据与冲突检查时才会 `confirmed=true`；AnimeTrace 候选始终 `confirmed=false`，`accepted_in_label` 仅表示服务报告置信达到了现有标签采用条件，不能当作独立身份事实。冲突特征、低置信候选和覆盖限制会保留，供视频后台等调用方交叉核对。失败会返回 `success=false` 和具体 `status`；取消会继续向上传播。

该 API 使用相同的识别缓存与并发信号量，但不注册聊天消息，不发送消息，不修改私库，也不改动“最近图片修正”和待添加角色状态。调用方应先选择少量代表帧，再按具体疑点补看或识别，避免每帧重复调用占用聊天图片识别容量。

`character.knowledge` 按名称或别名查询已启用的私库，只返回 `success/status/name/aliases/appearance_cards/has_reference_images`。它不返回关系、参考图内容、文件路径或生图参数。关闭、未找到和查询失败均有明确状态；两个 API 都不会绕过插件或角色库启用配置。

## Limits And Privacy

- `max_images_per_message` 默认 4，超过的图片不会由插件请求外部服务。
- `max_concurrency` 默认 1，避免大量图片同时触发限流。
- `message_timeout_seconds` 默认 110 秒；默认模式中的 MaiBot VLM 与 AnimeTrace 并行执行，超时或描述失败时图片会原样交还 MaiBot 后续链路。
- 相同图片按哈希进行有界、限时缓存，避免重复请求和内存持续增长。
- 缓存优先使用 MaiBot 提供的 SHA-256 图片唯一 ID；字段缺失时按图片内容计算 SHA-256，QQ 与 WebUI 可复用同一逻辑。
- 发送给 MaiBot VLM、插件视觉服务和 AnimeTrace 的图片副本都有体积上限，超限时会缩放并转为 JPEG。
- 视觉服务或 AnimeTrace 连续失败 2 次后会暂停请求 60 秒，随后自动恢复尝试。
- 开启 `vision` 时，图片会发送给你配置的视觉服务。
- 开启 AnimeTrace 时，图片会发送给 AnimeTrace。
- 本地角色库不会因为普通聊天自动新增或修改；只有管理员命令可以写入。
