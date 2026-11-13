# 本地改动说明

本分支为 [krkr521/character-knowledge-plugin](https://github.com/krkr521/character-knowledge-plugin/tree/local/custom) 的本地定制快照。
原项目：[QlzqQlzq/character-knowledge-plugin](https://github.com/QlzqQlzq/character-knowledge-plugin)。
对照基线：`e4310740b8e262cbd9247fb4963981c3e77ea76c`；整理日期：2026-11-13。

## 相对基线的主要改动

- 多角色识别、角色位置说明与识别结果的跨插件只读接口。
- 角色参考图存取、表情角色线索缓存及萌娘百科上下文联动。
- 视觉调用用量上报；角色图库及图片由安装者自行配置。

## 安装与配置

```sh
git clone --branch local/custom https://github.com/krkr521/character-knowledge-plugin.git
```

将仓库放入 MaiBot 插件目录，按原说明安装依赖并在自己的实例中配置。插件 ID 和原作者署名保留，替换同 ID 插件前请备份。
本分支来自定制宿主环境。涉及扩展 RPC、Decision 或后台任务接口的能力需要对应宿主支持；不能据此认定与所有官方 MaiBot 版本兼容。

直接调用计费使用 `MAIBOT_BILLING_CATALOG`（价格目录 JSON）和 `MAIBOT_BILLING_LEDGER`（SQLite 账本）。未设置时在本插件 `data/model-billing/` 查找；价格目录需自行提供，缺失时明确记录核算失败。

## 发布范围

保留原许可证及第三方署名。发布内容为源码、通用模板、文档和测试；不包含本机配置、配置备份、Cookie、访问令牌、语音样本、付款码、聊天记录、实际记忆或私有提交历史。
本地部署笔记已改写为通用说明，运行数据由安装者自行管理。
