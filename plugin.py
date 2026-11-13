import asyncio
import base64
import binascii
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import time
from typing import Any, Literal
from urllib.parse import urlparse

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from pydantic import field_validator

from .repository import CharacterRepository
from .vision import build_appearance_cards, describe_with_host_vlm, identify_image
from .reverse_image import ReverseImageHint, search_anime_trace
from .usage_accounting import record_usage


RecognitionResult = tuple[str, str, tuple[ReverseImageHint, ...]]
EnrichedRecognitionResult = tuple[str, str, str]

API_IMAGE_MAX_BYTES = 2 * 1024 * 1024
API_IMAGE_MAX_ENCODED_CHARS = 3 * 1024 * 1024
API_IMAGE_MAX_PIXELS = 16_000_000
API_RECOGNITION_TIMEOUT_SECONDS = 110


@dataclass
class ServiceHealth:
    consecutive_failures: int = 0
    open_until: float = 0.0


class PluginSettings(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "scan-face"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用图片角色知识注入")
    config_version: str = Field(default="1.0.7", description="配置版本")
    max_images_per_message: int = Field(default=4, ge=1, le=20, description="单条消息最多识别图片数，超出的图片标记为未识别")
    max_characters_per_image: int = Field(default=3, ge=1, le=10, description="单张图片最多保留的角色候选数")
    max_concurrency: int = Field(default=1, ge=1, le=4, description="视觉接口最大并发数")
    timeout_seconds: int = Field(default=25, ge=5, le=120, description="单张图片的视觉接口超时秒数")
    message_timeout_seconds: int = Field(default=110, ge=10, le=115, description="单条消息全部图片处理的总超时秒数")
    cache_max_entries: int = Field(default=256, ge=16, le=4096, description="图片识别缓存最大条目数")
    cache_ttl_seconds: int = Field(default=3600, ge=60, le=86400, description="图片识别缓存有效秒数")
    latest_image_max_entries: int = Field(default=64, ge=1, le=512, description="修正功能保留的最近图片最大用户数")
    latest_image_max_total_bytes: int = Field(default=16777216, ge=1048576, le=268435456, description="最近图片缓存总字节上限")
    circuit_breaker_failures: int = Field(default=2, ge=1, le=10, description="外部服务连续失败多少次后暂时熔断")
    circuit_breaker_cooldown_seconds: int = Field(default=60, ge=10, le=3600, description="外部服务熔断冷却秒数")


class VisionSettings(PluginConfigBase):
    __ui_label__ = "视觉识别"
    __ui_icon__ = "eye"
    __ui_order__ = 1

    enabled: bool = Field(default=False, description="启用后才会向配置的视觉接口发送图片")
    provider: Literal["gemini", "openai"] = Field(default="gemini", description="Gemini 原生接口或 OpenAI 兼容视觉接口")
    api_key: str = Field(default="", description="视觉接口密钥；建议通过服务器上的插件配置填写")
    base_url: str = Field(default="https://generativelanguage.googleapis.com/v1beta", description="Gemini API 根地址，OpenAI 模式填写 API 根地址")
    model: str = Field(default="gemini-3-flash", description="支持视觉输入和 JSON 输出的模型名")
    reasoning_effort: Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"] = Field(
        default="auto", description="OpenAI 兼容视觉接口的推理强度；auto 使用接口默认值，具体支持范围由模型决定"
    )
    max_upload_bytes: int = Field(default=4194304, ge=100000, le=20971520, description="发送给视觉服务前的最大图片字节数")

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def normalize_reasoning_effort(cls, value: Any) -> Any:
        """兼容旧配置的空值，并确保 WebUI 下拉选项使用非空值。"""
        return "auto" if value == "" else value


class LibrarySettings(PluginConfigBase):
    __ui_label__ = "私有角色库"
    __ui_icon__ = "book-user"
    __ui_order__ = 2

    enabled: bool = Field(default=False, description="启用本地 OC、自己和朋友等权威角色库")
    file_name: str = Field(default="characters.json", description="MaiBot 分配的插件持久化目录中的角色库 JSON 文件名")
    admin_qq: list[str] = Field(default_factory=list, description="允许通过聊天管理角色库的 QQ 号")
    allow_unrestricted_admin: bool = Field(default=False, description="允许任何用户管理角色库，仅用于受控的 WebUI 本地测试")
    max_prompt_characters: int = Field(default=40, ge=1, le=200, description="单次视觉请求最多携带的本地角色数")
    pending_addition_ttl_seconds: int = Field(default=300, ge=30, le=3600, description="角色添加等待参考图的有效秒数")


class ReverseImageSettings(PluginConfigBase):
    __ui_label__ = "反向搜图"
    __ui_icon__ = "image-search"
    __ui_order__ = 3

    anime_trace_enabled: bool = Field(default=True, description="启用 AnimeTrace 二次元角色检索；轻量联网模式会直接写入明确候选，插件 VLM 模式用于补充未命中的公共角色")
    anime_trace_url: str = Field(default="https://api.animetrace.com", description="AnimeTrace API 地址")
    anime_trace_max_upload_bytes: int = Field(default=900000, ge=100000, le=900000, description="AnimeTrace 上传前的最大图片字节数；超出时自动缩放并压缩为 JPEG")
    timeout_seconds: int = Field(default=15, ge=5, le=60, description="反向搜图请求超时秒数")


class EmojiSettings(PluginConfigBase):
    __ui_label__ = "表情包角色识别"
    __ui_icon__ = "smile-plus"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="对带图片数据的 QQ 表情包和 GIF 使用 AnimeTrace 补充角色候选")
    enrich_description: bool = Field(default=True, description="把明确角色候选写入表情包的画面语义描述，不写入情绪标签")
    cache_file: str = Field(default="emoji_roles.json", description="表情包角色识别持久缓存文件名")
    cache_max_entries: int = Field(default=4096, ge=128, le=50000, description="表情包角色识别缓存最大条目数")
    positive_cache_ttl_days: int = Field(default=365, ge=1, le=3650, description="明确角色识别结果的缓存天数")
    negative_cache_ttl_days: int = Field(default=7, ge=1, le=365, description="未识别结果的缓存天数")


class CharacterKnowledgeConfig(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    library: LibrarySettings = Field(default_factory=LibrarySettings)
    reverse_image: ReverseImageSettings = Field(default_factory=ReverseImageSettings)
    emoji: EmojiSettings = Field(default_factory=EmojiSettings)


class CharacterKnowledgePlugin(MaiBotPlugin):
    config_model = CharacterKnowledgeConfig

    def __init__(self) -> None:
        super().__init__()
        self._repository: CharacterRepository | None = None
        self._cache: OrderedDict[str, tuple[float, RecognitionResult]] = OrderedDict()
        self._recognition_details: dict[str, dict[str, Any]] = {}
        self._knowledge_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._emoji_role_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._latest_recognitions: OrderedDict[tuple[str, str], tuple[bytes, str]] = OrderedDict()
        self._semaphore = asyncio.Semaphore(1)
        self._repository_lock = asyncio.Lock()
        self._emoji_role_cache_lock = asyncio.Lock()
        self._pending_additions: dict[tuple[str, str], tuple[float, str, str]] = {}
        self._background_tasks: set[asyncio.Task[EnrichedRecognitionResult]] = set()
        self._config_generation = 0
        self._service_health = {"vision": ServiceHealth(), "anime_trace": ServiceHealth()}

    async def on_load(self) -> None:
        self._reload_repository()
        self._load_emoji_role_cache()
        self._semaphore = asyncio.Semaphore(self.config.plugin.max_concurrency)
        self.ctx.logger.info(
            "角色知识插件已加载：视觉=%s 私库=%s AnimeTrace=%s 表情角色=%s 缓存=%s",
            self.config.vision.enabled,
            self.config.library.enabled,
            self.config.reverse_image.anime_trace_enabled,
            self.config.emoji.enabled,
            len(self._emoji_role_cache),
        )

    async def on_unload(self) -> None:
        self._cache.clear()
        self._recognition_details.clear()
        self._knowledge_cache.clear()
        self._emoji_role_cache.clear()
        self._latest_recognitions.clear()
        self._pending_additions.clear()
        self._config_generation += 1
        self._service_health = {"vision": ServiceHealth(), "anime_trace": ServiceHealth()}
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.ctx.logger.info("角色知识插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        self._config_generation += 1
        self._cache.clear()
        self._recognition_details.clear()
        self._knowledge_cache.clear()
        self._latest_recognitions.clear()
        self._pending_additions.clear()
        async with self._repository_lock:
            self._reload_repository()
        self._load_emoji_role_cache()
        self._semaphore = asyncio.Semaphore(self.config.plugin.max_concurrency)
        self.ctx.logger.info("角色知识插件配置已更新：config_version=%s", self.config.plugin.config_version)

    @HookHandler(
        "chat.receive.before_process",
        name="inject_character_knowledge",
        description="保留原图，在对应图片后补充人物位置、外观与角色身份辅助。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=120000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_character_knowledge(self, message: Any, **kwargs: Any) -> dict[str, Any] | None:
        del kwargs
        if not self.config.plugin.enabled:
            return None
        if self._is_character_add_command(message):
            return None
        images = self._image_components(message)
        if not images:
            return None
        key = self._pending_key(message)
        pending = self._pending_additions.pop(key, None)
        if pending is not None:
            created_at, name, relationship = pending
            if time.monotonic() - created_at <= self.config.library.pending_addition_ttl_seconds:
                return await self._create_pending_character(message, images, (created_at, name, relationship))
            stream_id = str(message.get("session_id") or "") if isinstance(message, dict) else ""
            if stream_id:
                await self.ctx.send.text("角色添加已超时，本次图片将按普通图片处理。", stream_id)
        replacements = await self._build_replacements(message, images)
        modified = dict(message)
        components = list(modified.get("raw_message") or [])
        output: list[dict[str, Any]] = []
        image_index = 0
        for component in components:
            if isinstance(component, dict) and component.get("type") in {"image", "emoji"}:
                output.extend(replacements[image_index])
                image_index += 1
            else:
                output.append(component)
        modified["raw_message"] = output
        return {"action": "continue", "modified_kwargs": {"message": modified}}

    async def _build_replacements(
        self, message: dict[str, Any], images: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        limit = self.config.plugin.max_images_per_message
        generation = self._config_generation
        tasks = [asyncio.create_task(self._recognize_limited(image, generation)) for image in images[:limit]]
        try:
            done, pending = await asyncio.wait(tasks, timeout=self.config.plugin.message_timeout_seconds)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    self._track_background_task(task)
            raise
        if pending:
            self.ctx.logger.warning(
                "单条消息图片识别超过总预算，%s 张未完成图片将原样交给 MaiBot", len(pending)
            )
            for task in pending:
                self._track_background_task(task)
        recognized: list[EnrichedRecognitionResult | None] = []
        for task in tasks:
            if task not in done:
                recognized.append(None)
                continue
            try:
                recognized.append(task.result())
            except Exception as exc:
                self.ctx.logger.warning("单张图片识别异常，将原图交给 MaiBot: %s", exc)
                recognized.append(None)
        replacements: list[list[dict[str, Any]]] = []
        for index, image in enumerate(images):
            if index >= limit:
                replacements.append([image])
                continue
            result = recognized[index]
            if result is None:
                replacements.append([image])
                continue
            identity_context, label, knowledge_context = result
            knowledge_suffix = f" {knowledge_context}" if knowledge_context else ""
            image_bytes = self._decode_image(image)
            if image_bytes is not None:
                self._remember_latest(self._pending_key(message), image_bytes, label)
            if image.get("type") == "emoji":
                if label == "图片[未识别]":
                    replacements.append([image])
                else:
                    replacements.append(
                        [image, {"type": "text", "data": f"[表情角色识别：{label}]{knowledge_suffix}"}]
                    )
                continue
            # 始终保留图片与原有元数据，原生多模态理解交给 Planner；
            # 角色信息紧跟对应原图，避免多图、多人时把名字误配到另一张图。
            replacement = [image]
            if identity_context:
                replacement.append({
                    "type": "text",
                    "data": (
                        f"[第{index + 1}张图角色身份辅助（仅对应紧邻前面的原图）：{identity_context}。"
                        "人物外观、动作、文字和画面含义请自行查看原图；身份提示可能有误，"
                        f"未定位候选不得按列表顺序分配给人物。]{knowledge_suffix}"
                    ),
                })
            replacements.append(replacement)
        return replacements

    async def _recognize_limited(
        self, image: dict[str, Any], generation: int
    ) -> EnrichedRecognitionResult:
        async with self._semaphore:
            if image.get("type") == "emoji":
                identity_context, label, hints = await self._recognize_emoji_component(image)
            else:
                details: dict[str, Any] = {}
                _, label, hints = await self._recognize_image(image, generation=generation, details=details)
                identity_context = self._format_character_assignments(details)
            knowledge_context = await self._build_moegirl_context(hints)
            if generation != self._config_generation:
                return "", "图片[未识别]", ""
            return identity_context, label, knowledge_context

    def _format_character_assignments(self, details: dict[str, Any]) -> str:
        """只传人物位置、外观与身份依据，不把场景转述注入聊天。"""
        assignments: list[dict[str, Any]] = []
        unlocated: list[dict[str, str]] = []
        for candidate in details.get("candidates", []):
            location = " ".join(str(candidate.get("location") or "").split())[:160]
            evidence = [str(value)[:80] for value in candidate.get("evidence", [])[:3]]
            name = str(candidate.get("name") or "")[:200]
            if candidate.get("source") == "private_library_visual" and candidate.get("confirmed"):
                if not location and not evidence:
                    unlocated.append({"name": name, "source": "私库候选，未定位"})
                    continue
                assignment: dict[str, Any] = {
                    "location": location, "appearance": evidence, "name": name,
                    "source": "私库视觉匹配",
                }
                character = self._repository.find_name(name) if self.config.library.enabled and self._repository else None
                if character and character.relationship:
                    assignment["relationship"] = character.relationship[:80]
                assignments.append(assignment)
            elif candidate.get("source") == "AnimeTrace" and candidate.get("accepted_in_label"):
                unlocated.append({"name": name, "work": str(candidate.get("franchise") or "")[:200], "source": "AnimeTrace候选，未定位"})
            elif location or evidence:
                assignments.append({"location": location, "appearance": evidence, "name": "身份未确认"})
        if not assignments and not unlocated:
            return ""
        return json.dumps({"人物对应": assignments, "未定位候选": unlocated}, ensure_ascii=False, separators=(",", ":"))

    @HookHandler(
        "emoji.register.after_build_description",
        name="enrich_emoji_character_description",
        description="使用 AnimeTrace 为新表情包的画面语义补充明确角色候选，保持情绪标签不变。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=20000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def enrich_emoji_character_description(
        self,
        emoji: Any,
        description: str,
        image_format: str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        del kwargs
        if not (
            self.config.plugin.enabled
            and self.config.emoji.enabled
            and self.config.emoji.enrich_description
            and self.config.reverse_image.anime_trace_enabled
        ):
            return None
        if not isinstance(emoji, dict):
            return None
        emoji_hash = str(emoji.get("file_hash") or "").strip()
        full_path = str(emoji.get("full_path") or "").strip()
        if not emoji_hash or not full_path:
            return None
        path = Path(full_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            image_bytes = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            self.ctx.logger.warning("读取待注册表情包失败，跳过角色增强: %s", exc)
            return None
        async with self._semaphore:
            hints = await self._get_emoji_role_hints(emoji_hash, image_bytes)
        enriched = self._enrich_emoji_description(description, hints)
        if enriched == description:
            return None
        self.ctx.logger.info(
            "已为表情包语义补充 AnimeTrace 角色候选: hash=%s roles=%s",
            emoji_hash[:12],
            "、".join(hint.name for hint in hints if not hint.not_confident),
        )
        return {
            "action": "continue",
            "modified_kwargs": {
                "emoji": emoji,
                "description": enriched,
                "image_format": image_format,
            },
        }

    def _track_background_task(self, task: asyncio.Task[EnrichedRecognitionResult]) -> None:
        if task in self._background_tasks:
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_background_recognition)

    def _finish_background_recognition(self, task: asyncio.Task[EnrichedRecognitionResult]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.ctx.logger.warning("后台图片识别异常: %s", exc)

    @Command(
        "character_add",
        description="管理员创建角色库条目，随后发送一张参考图自动生成外观卡。",
        pattern=r"^/角色添加\s+(?P<name>\S+)(?:\s+(?P<relationship>[^\[\]\r\n]{1,32}?))?(?:\s+\[图片[：:].*)?$",
        timeout_ms=5000,
    )
    async def add_character(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        raw_message = message if isinstance(message, dict) else {}
        stream_id = str(raw_message.get("session_id") or "")
        if not self.config.library.enabled:
            return await self._command_feedback(stream_id, "私有角色库当前未启用。", success=False)
        if not self.config.plugin.enabled or not self.config.vision.enabled:
            return await self._command_feedback(stream_id, "视觉识别当前未启用，无法从参考图创建角色。", success=False)
        if not self._is_admin(raw_message):
            return await self._command_feedback(stream_id, "没有角色库管理权限。", success=False)
        matched = kwargs.get("matched_groups")
        name = str(matched.get("name") or "").strip() if isinstance(matched, dict) else ""
        relationship = str(matched.get("relationship") or "").strip() if isinstance(matched, dict) else ""
        if not name:
            return await self._command_feedback(stream_id, "用法：/角色添加 名字 [关系]，然后发送一张参考图。", success=False)
        self._discard_expired_additions()
        self._pending_additions[self._pending_key(raw_message)] = (time.monotonic(), name, relationship)
        while len(self._pending_additions) > self.config.plugin.latest_image_max_entries:
            self._pending_additions.pop(next(iter(self._pending_additions)))
        prompt = f"已准备创建“{name}”。请在本聊天单独发送一张参考图，下一条带图消息会用于建库。"
        return await self._command_feedback(stream_id, prompt, success=True)

    @Command("character_add_cancel", description="取消当前聊天中等待参考图的角色添加。", pattern=r"^/取消角色添加\s*$", timeout_ms=5000)
    async def cancel_character_add(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        raw_message = message if isinstance(message, dict) else {}
        stream_id = str(raw_message.get("session_id") or "")
        if not self._is_admin(raw_message):
            return await self._command_feedback(stream_id, "没有角色库管理权限。", success=False)
        removed = self._pending_additions.pop(self._pending_key(raw_message), None)
        text = "已取消等待中的角色添加。" if removed is not None else "当前没有等待参考图的角色添加。"
        return await self._command_feedback(stream_id, text, success=True)

    @Command(
        "character_correct",
        description="管理员修正本聊天最近一次识图结果；引用旧图时优先修正引用图。",
        pattern=r"^/识图修正\s+(?P<name>\S+)\s*$",
        timeout_ms=120000,
    )
    async def correct_character(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        raw_message = message if isinstance(message, dict) else {}
        stream_id = str(raw_message.get("session_id") or "")
        if not self.config.library.enabled:
            return await self._command_feedback(stream_id, "私有角色库当前未启用。", success=False)
        if not self.config.plugin.enabled or not self.config.vision.enabled:
            return await self._command_feedback(stream_id, "视觉识别当前未启用，无法使用引用图片修正角色。", success=False)
        if not self._is_admin(raw_message):
            return await self._command_feedback(stream_id, "没有角色库管理权限。", success=False)
        matched = kwargs.get("matched_groups")
        name = str(matched.get("name") or "").strip() if isinstance(matched, dict) else ""
        target = self._repository.find_name(name) if name and self._repository else None
        if target is None:
            return await self._command_feedback(stream_id, f"角色库中不存在“{name}”。", success=False)
        canonical_name = target.name
        try:
            image_bytes, old_label = await self._image_for_correction(raw_message)
            async with self._semaphore:
                cards = await build_appearance_cards(
                    provider=self.config.vision.provider,
                    api_key=self.config.vision.api_key,
                    base_url=self.config.vision.base_url,
                    model=self.config.vision.model,
                    image_bytes=image_bytes,
                    timeout_seconds=self.config.plugin.timeout_seconds,
                    max_upload_bytes=self.config.vision.max_upload_bytes,
                    reasoning_effort=self.config.vision.reasoning_effort,
                    usage_callback=self._record_vision_usage,
                )
            async with self._repository_lock:
                character = await asyncio.to_thread(
                    self._repository.append_appearance_cards,
                    name=canonical_name,
                    appearance_cards=cards,
                    image_bytes=image_bytes,
                )
        except Exception as exc:
            self.ctx.logger.warning("引用图片修正角色库失败: name=%s error=%s", canonical_name, exc)
            return await self._command_feedback(stream_id, "引用图片修正失败，详情见日志。", success=False)
        self._invalidate_recognition_cache()
        self.ctx.logger.info("管理员引用图片修正角色库成功: name=%s cards=%s", character.name, len(character.appearance_cards))
        return await self._command_feedback(
            stream_id,
            f"✅ 识图修正完成\n{old_label} → 图片[{character.name}]\n已补充外观卡并保存参考图，当前共 {len(character.appearance_cards)} 条外观卡、{len(character.reference_images)} 张参考图。",
            success=True,
        )

    async def _image_for_correction(self, message: dict[str, Any]) -> tuple[bytes, str]:
        """Prefer a quoted image, otherwise use this sender's latest recognized image."""
        reference_id = self._referenced_message_id(message)
        stream_id = str(message.get("session_id") or "")
        if reference_id:
            referenced = await self.ctx.message.get_by_id(
                reference_id, stream_id=stream_id, include_binary_data=True
            )
            source = referenced.get("message") if isinstance(referenced, dict) and referenced.get("success") else None
            images = self._image_components(source)
            if not images:
                raise ValueError("被引用消息中没有可读取的图片")
            image_bytes = self._decode_image(images[0])
            if image_bytes is None:
                raise ValueError("被引用图片的二进制数据不可用")
            return image_bytes, "图片[原识别结果未知]"
        latest = self._latest_recognitions.get(self._pending_key(message))
        if latest is None:
            raise ValueError("未找到本聊天中你最近一次识别过的图片，请先发送图片后再修正")
        return latest

    async def _command_feedback(self, stream_id: str, text: str, *, success: bool) -> tuple[bool, str, int]:
        """Commands always send visible feedback because WebUI does not render return text."""
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return success, "", 1

    async def _admin_command(self, message: Any) -> tuple[dict[str, Any], str] | None:
        raw_message = message if isinstance(message, dict) else {}
        stream_id = str(raw_message.get("session_id") or "")
        if not self.config.library.enabled:
            await self._command_feedback(stream_id, "私有角色库当前未启用。", success=False)
            return None
        if not self._is_admin(raw_message):
            await self._command_feedback(stream_id, "没有角色库管理权限。", success=False)
            return None
        return raw_message, stream_id

    @Command("character_list", description="管理员查看本地角色库。", pattern=r"^/(?:角色列表|角色库)$", timeout_ms=5000)
    async def list_characters(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        authorized = await self._admin_command(message)
        if authorized is None:
            return False, "", 1
        _, stream_id = authorized
        characters = self._repository.list_characters() if self._repository else ()
        if not characters:
            return await self._command_feedback(stream_id, "角色库为空。", success=True)
        lines = ["角色库："]
        for character in characters:
            relation = f"（{character.relationship}）" if character.relationship else ""
            lines.append(f"- {character.name}{relation}：{len(character.appearance_cards)} 条外观卡，{len(character.reference_images)} 张参考图")
        return await self._command_feedback(stream_id, "\n".join(lines), success=True)

    @Command("character_view", description="管理员查看角色档案。", pattern=r"^/(?:查看人设|查看角色)\s+(?P<name>\S+)\s*$", timeout_ms=5000)
    async def view_character(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        authorized = await self._admin_command(message)
        if authorized is None:
            return False, "", 1
        _, stream_id = authorized
        matched = kwargs.get("matched_groups")
        name = str(matched.get("name") or "").strip() if isinstance(matched, dict) else ""
        character = self._repository.find_name(name) if self._repository else None
        if character is None:
            return await self._command_feedback(stream_id, f"角色库中不存在“{name}”。", success=False)
        aliases = "、".join(character.aliases) or "无"
        lines = [
            f"角色：{character.name}",
            f"关系：{character.relationship or '未设置'}",
            f"别名：{aliases}",
            f"参考图：{len(character.reference_images)} 张",
            "外观卡：",
        ]
        lines.extend(f"- {card}" for card in character.appearance_cards)
        return await self._command_feedback(stream_id, "\n".join(lines), success=True)

    @staticmethod
    def _validated_api_image(image_base64: str) -> bytes:
        """Accept bounded image bytes only; API callers cannot select local files."""
        if not isinstance(image_base64, str) or not image_base64:
            raise ValueError("image_base64必须是非空base64字符串")
        if len(image_base64) > API_IMAGE_MAX_ENCODED_CHARS:
            raise ValueError("图片base64超过3 MiB字符上限")
        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片base64编码无效，不接受URL或路径") from exc
        if not image_bytes or len(image_bytes) > API_IMAGE_MAX_BYTES:
            raise ValueError("图片不能为空且不能超过2 MiB")
        from PIL import Image

        try:
            with Image.open(BytesIO(image_bytes)) as image:
                if image.format not in {"JPEG", "PNG"}:
                    raise ValueError("识别API仅接受JPEG或PNG图片")
                if image.width * image.height > API_IMAGE_MAX_PIXELS:
                    raise ValueError("图片像素数超过1600万上限")
                image.verify()
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("图片内容无效或已损坏") from exc
        return image_bytes

    @staticmethod
    def _recognition_api_failure(status: str, limitation: str) -> dict[str, Any]:
        return {
            "success": False, "status": status, "source": "character-knowledge-plugin",
            "description": "", "label": "图片[未识别]", "is_anime_character": None,
            "candidates": [], "knowledge_context": "", "limitations": [limitation],
        }

    @API(
        "character.identify_image",
        description="只读识别JPEG/PNG图片中的角色，返回私库视觉依据、AnimeTrace候选及萌百核验，不修改聊天状态",
        version="1",
        public=True,
        timeout_ms=115000,
    )
    async def identify_image_api(self, image_base64: str = "") -> dict[str, Any]:
        """Use the same bounded recognizer without registering a chat message."""
        if not self.config.plugin.enabled:
            return self._recognition_api_failure("disabled", "角色知识插件已关闭")
        generation = self._config_generation

        async def recognize() -> dict[str, Any]:
            try:
                image_bytes = await asyncio.to_thread(self._validated_api_image, image_base64)
            except ValueError as exc:
                return self._recognition_api_failure("invalid_image", str(exc))
            async with self._semaphore:
                if generation != self._config_generation or not self.config.plugin.enabled:
                    return self._recognition_api_failure("config_changed", "识别期间配置已更新，请重新识别")
                details: dict[str, Any] = {}
                description, _, hints = await self._recognize_image(
                    {"type": "image", "binary_data_base64": base64.b64encode(image_bytes).decode("ascii"),
                     "hash": hashlib.sha256(image_bytes).hexdigest()},
                    generation=generation, details=details,
                )
                knowledge = await self._build_moegirl_context(hints)
                if generation != self._config_generation or not self.config.plugin.enabled:
                    return self._recognition_api_failure("config_changed", "识别期间配置已更新，旧结果未采用")
                candidates = details.get("candidates", [])
                names = list(dict.fromkeys(
                    candidate["name"] for candidate in candidates
                    if candidate.get("accepted_in_label") and candidate.get("name")
                ))
                label = f"图片[{'、'.join(names[: self.config.plugin.max_characters_per_image])}]" if names else "图片[未识别]"
                limitations = list(details.get("limitations", []))
                limitations.append("候选及服务报告置信不等于独立身份事实；应与画面、作品和查询材料交叉核对")
                failed = description in {"图片识别失败", "图片未能读取", ""} and not names
                return {
                    "success": not failed, "status": "unavailable" if failed else "ok",
                    "source": "character-knowledge-plugin", "description": description,
                    "label": label, "is_anime_character": details.get("is_anime_character"),
                    "candidates": candidates, "knowledge_context": knowledge[:3200],
                    "limitations": limitations,
                }

        try:
            return await asyncio.wait_for(recognize(), timeout=API_RECOGNITION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return self._recognition_api_failure("timeout", "角色识别超过110秒总预算（含并发排队），未确认身份")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ctx.logger.exception("只读角色图片API执行失败")
            return self._recognition_api_failure("error", "角色识别服务执行失败，未确认身份")

    @API(
        "character.knowledge",
        description="按名称或别名只读查询私库角色外观，不返回关系、原图、路径或生图参数",
        version="1",
        public=True,
        timeout_ms=5000,
    )
    async def get_character_knowledge_api(self, name: str = "") -> dict[str, Any]:
        if not self.config.plugin.enabled or not self.config.library.enabled or self._repository is None:
            return {"success": False, "status": "disabled", "message": "私有角色库当前未启用"}
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            return {"success": False, "status": "invalid_query", "message": "角色名称必须为1至128字符的字符串"}
        generation = self._config_generation
        try:
            async with self._repository_lock:
                await asyncio.to_thread(self._repository.reload)
                if generation != self._config_generation or not self.config.plugin.enabled or not self.config.library.enabled:
                    return {"success": False, "status": "config_changed", "message": "角色库配置已更新，请重新查询"}
                character = self._repository.find_name(name)
                if character is None:
                    return {"success": False, "status": "not_found", "message": "角色库中未找到该名称或别名"}
                return {
                    "success": True, "status": "ok", "name": character.name,
                    "aliases": list(character.aliases), "appearance_cards": list(character.appearance_cards),
                    "has_reference_images": bool(character.reference_images),
                }
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ctx.logger.exception("只读角色知识API执行失败")
            return {"success": False, "status": "error", "message": "角色库读取失败"}

    @Tool(
        "get_character_knowledge",
        description=(
            "按角色名或别名查询私有角色库的权威外观、人设和参考图状态。"
            "绘制库中角色前可用本工具核对身份；用 generate_image 的 characters:[角色名,...] 引用已保存原图，"
            "可同时指定多个角色。has_reference_images=false 表示库中没有原图，不能声称已看过其参考图。"
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "要查询的角色名称或别名"}},
            "required": ["name"],
        },
    )
    async def get_character_knowledge(self, name: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if not self.config.plugin.enabled or not self.config.library.enabled or self._repository is None:
            return {"success": False, "content": "私有角色库当前未启用。"}
        async with self._repository_lock:
            await asyncio.to_thread(self._repository.reload)
            character = self._repository.find_name(name)
        if character is None:
            return {"success": False, "content": f"角色库中不存在“{name}”。", "has_reference_images": False}
        return {
            "success": True,
            "content": (
                f"角色：{character.name}；别名：{'、'.join(character.aliases) or '无'}；"
                f"关系：{character.relationship or '未设置'}。\n"
                f"外观卡：{'；'.join(character.appearance_cards)}\n"
                f"已保存参考图：{len(character.reference_images)} 张；"
                f"版本：{'、'.join(dict.fromkeys(image.variant for image in character.reference_images)) or '无'}。\n"
                f"生图可传 characters={json.dumps([character.name], ensure_ascii=False)}；"
                "只有已保存参考图时才能声称以库内图片为参考。"
            ),
            "name": character.name,
            "aliases": list(character.aliases),
            "relationship": character.relationship,
            "appearance_cards": list(character.appearance_cards),
            "has_reference_images": bool(character.reference_images),
            "reference_images": [
                {"reference": f"character://{character.name}", "variant": reference.variant}
                for reference in character.reference_images
            ],
            "generation_arguments": {"characters": [character.name]},
        }

    @Command("character_relationship", description="管理员设置角色关系。", pattern=r"^/设置关系\s+(?P<name>\S+)\s+(?P<relationship>.{1,32})\s*$", timeout_ms=5000)
    async def set_relationship(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        authorized = await self._admin_command(message)
        if authorized is None:
            return False, "", 1
        _, stream_id = authorized
        matched = kwargs.get("matched_groups")
        name = str(matched.get("name") or "").strip() if isinstance(matched, dict) else ""
        relationship = str(matched.get("relationship") or "").strip() if isinstance(matched, dict) else ""
        try:
            async with self._repository_lock:
                character = self._repository.set_relationship(name=name, relationship=relationship) if self._repository else None
            if character is None:
                raise ValueError("角色库不可用")
        except ValueError as exc:
            return await self._command_feedback(stream_id, str(exc), success=False)
        self._invalidate_recognition_cache()
        return await self._command_feedback(stream_id, f"已将“{character.name}”的关系设为“{character.relationship or '未设置'}”。", success=True)

    @Command("character_alias_add", description="管理员添加角色别名。", pattern=r"^/添加别名\s+(?P<name>\S+)\s+(?P<alias>\S+)\s*$", timeout_ms=5000)
    async def add_alias(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._change_alias(message, kwargs, add=True)

    @Command("character_alias_delete", description="管理员删除角色别名。", pattern=r"^/删除别名\s+(?P<name>\S+)\s+(?P<alias>\S+)\s*$", timeout_ms=5000)
    async def delete_alias(self, message: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._change_alias(message, kwargs, add=False)

    async def _change_alias(self, message: Any, kwargs: dict[str, Any], *, add: bool) -> tuple[bool, str, int]:
        authorized = await self._admin_command(message)
        if authorized is None:
            return False, "", 1
        _, stream_id = authorized
        matched = kwargs.get("matched_groups")
        name = str(matched.get("name") or "").strip() if isinstance(matched, dict) else ""
        alias = str(matched.get("alias") or "").strip() if isinstance(matched, dict) else ""
        try:
            async with self._repository_lock:
                character = (
                    self._repository.add_alias(name=name, alias=alias)
                    if add and self._repository
                    else self._repository.remove_alias(name=name, alias=alias)
                    if self._repository
                    else None
                )
            if character is None:
                raise ValueError("角色库不可用")
        except ValueError as exc:
            return await self._command_feedback(stream_id, str(exc), success=False)
        action = "添加" if add else "删除"
        self._invalidate_recognition_cache()
        return await self._command_feedback(stream_id, f"已{action}“{character.name}”的别名“{alias}”。", success=True)

    def _reload_repository(self) -> None:
        filename = Path(self.config.library.file_name).name
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / filename
        legacy_path = Path(__file__).parent / "data" / filename
        if not path.exists() and legacy_path.exists():
            try:
                shutil.copy2(legacy_path, path)
                self.ctx.logger.info("已将旧式角色库迁移到持久化目录: %s", path)
            except OSError as exc:
                self.ctx.logger.warning("迁移旧式角色库失败，将使用空角色库: %s", exc)
        repository = CharacterRepository(path)
        repository.ensure_exists()
        try:
            repository.reload()
        except (OSError, ValueError) as exc:
            backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                path.replace(backup)
                self.ctx.logger.error("角色库损坏，已备份到 %s 并创建空角色库: %s", backup, exc)
                repository.ensure_exists()
                repository.reload()
            except OSError:
                raise RuntimeError(f"角色库损坏且无法备份: {path}") from exc
        self._repository = repository

    def _emoji_role_cache_path(self) -> Path:
        filename = Path(self.config.emoji.cache_file).name
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / filename

    def _load_emoji_role_cache(self) -> None:
        path = self._emoji_role_cache_path()
        loaded: list[tuple[str, dict[str, Any]]] = []
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                entries = payload.get("entries") if isinstance(payload, dict) else None
                if isinstance(entries, dict):
                    for image_hash, raw_entry in entries.items():
                        if not isinstance(raw_entry, dict):
                            continue
                        normalized_hash = str(image_hash or "").strip()
                        updated_at = raw_entry.get("updated_at")
                        characters = raw_entry.get("characters")
                        if not normalized_hash or not isinstance(updated_at, (int, float)) or not isinstance(characters, list):
                            continue
                        normalized_characters: list[dict[str, str]] = []
                        for character in characters:
                            if not isinstance(character, dict):
                                continue
                            name = str(character.get("name") or "").strip()
                            franchise = str(character.get("franchise") or "").strip()
                            if name:
                                normalized_characters.append({"name": name, "franchise": franchise})
                        loaded.append(
                            (
                                normalized_hash,
                                {"updated_at": float(updated_at), "characters": normalized_characters},
                            )
                        )
            except (OSError, ValueError, TypeError) as exc:
                backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
                try:
                    path.replace(backup)
                    self.ctx.logger.warning("表情角色缓存损坏，已备份到 %s: %s", backup, exc)
                except OSError:
                    self.ctx.logger.warning("表情角色缓存损坏且无法备份: %s", exc)
        loaded.sort(key=lambda item: float(item[1]["updated_at"]))
        max_entries = self.config.emoji.cache_max_entries
        self._emoji_role_cache = OrderedDict(loaded[-max_entries:])

    def _emoji_role_cache_get(self, image_hash: str) -> tuple[ReverseImageHint, ...] | None:
        entry = self._emoji_role_cache.get(image_hash)
        if entry is None:
            return None
        characters = entry.get("characters")
        if not isinstance(characters, list):
            self._emoji_role_cache.pop(image_hash, None)
            return None
        ttl_days = (
            self.config.emoji.positive_cache_ttl_days
            if characters
            else self.config.emoji.negative_cache_ttl_days
        )
        if time.time() - float(entry.get("updated_at") or 0) > ttl_days * 86400:
            self._emoji_role_cache.pop(image_hash, None)
            return None
        self._emoji_role_cache.move_to_end(image_hash)
        return tuple(
            ReverseImageHint(
                provider="AnimeTrace",
                text=f"{character['name']}｜{character.get('franchise', '')}｜持久缓存",
                name=str(character["name"]),
                franchise=str(character.get("franchise") or ""),
                not_confident=False,
            )
            for character in characters
            if isinstance(character, dict) and character.get("name")
        )

    async def _emoji_role_cache_put(self, image_hash: str, hints: tuple[ReverseImageHint, ...]) -> None:
        characters: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for hint in hints:
            if hint.provider != "AnimeTrace" or hint.not_confident or not hint.name:
                continue
            key = (hint.name, hint.franchise)
            if key in seen:
                continue
            seen.add(key)
            characters.append({"name": hint.name, "franchise": hint.franchise})
        async with self._emoji_role_cache_lock:
            self._emoji_role_cache[image_hash] = {
                "updated_at": time.time(),
                "characters": characters[: self.config.plugin.max_characters_per_image],
            }
            self._emoji_role_cache.move_to_end(image_hash)
            while len(self._emoji_role_cache) > self.config.emoji.cache_max_entries:
                self._emoji_role_cache.popitem(last=False)
            payload = {"version": 1, "entries": dict(self._emoji_role_cache)}
            path = self._emoji_role_cache_path()

            def write_cache() -> None:
                temporary = path.with_name(f"{path.name}.tmp")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(path)

            await asyncio.to_thread(write_cache)

    @staticmethod
    def _pending_key(message: dict[str, Any]) -> tuple[str, str]:
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        return str(message.get("session_id") or ""), str(user.get("user_id") or "")

    def _is_admin(self, message: dict[str, Any]) -> bool:
        if self.config.library.allow_unrestricted_admin:
            return True
        allowed = {item.strip() for item in self.config.library.admin_qq if item.strip()}
        if not allowed:
            return False
        return self._pending_key(message)[1] in allowed

    def _discard_expired_additions(self) -> None:
        now = time.monotonic()
        ttl = self.config.library.pending_addition_ttl_seconds
        expired = [key for key, (created_at, _, _) in self._pending_additions.items() if now - created_at > ttl]
        for key in expired:
            self._pending_additions.pop(key, None)

    async def _create_pending_character(
        self, message: dict[str, Any], images: list[dict[str, Any]], pending: tuple[float, str, str]
    ) -> dict[str, Any]:
        created_at, name, relationship = pending
        stream_id = str(message.get("session_id") or "")
        if not self._is_admin(message) or not self._repository:
            if stream_id:
                await self.ctx.send.text("角色库创建权限失效，已取消。", stream_id)
            return {"action": "abort", "custom_result": {"reason": "角色库创建权限失效"}}
        if len(images) != 1:
            self._pending_additions[self._pending_key(message)] = pending
            if stream_id:
                await self.ctx.send.text("请单独发送一张参考图；角色添加仍在等待中。", stream_id)
            return {"action": "abort", "custom_result": {"reason": "角色添加需要单张参考图"}}
        encoded = images[0].get("binary_data_base64")
        if not isinstance(encoded, str) or not encoded:
            if stream_id:
                await self.ctx.send.text("参考图不可读取，已取消创建。", stream_id)
            return {"action": "abort", "custom_result": {"reason": "参考图不可读取"}}
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
            async with self._semaphore:
                cards = await build_appearance_cards(
                    provider=self.config.vision.provider,
                    api_key=self.config.vision.api_key,
                    base_url=self.config.vision.base_url,
                    model=self.config.vision.model,
                    image_bytes=image_bytes,
                    timeout_seconds=self.config.plugin.timeout_seconds,
                    max_upload_bytes=self.config.vision.max_upload_bytes,
                    reasoning_effort=self.config.vision.reasoning_effort,
                    usage_callback=self._record_vision_usage,
                )
            async with self._repository_lock:
                character = await asyncio.to_thread(
                    self._repository.upsert,
                    name=name,
                    relationship=relationship,
                    appearance_cards=cards,
                    image_bytes=image_bytes,
                )
        except Exception as exc:
            self.ctx.logger.warning("创建角色库条目失败: %s", exc)
            if time.monotonic() - created_at <= self.config.library.pending_addition_ttl_seconds:
                self._pending_additions[self._pending_key(message)] = pending
            if stream_id:
                await self.ctx.send.text("创建角色库条目失败，仍可在有效期内重新发送一张参考图；详情见日志。", stream_id)
            return {"action": "abort", "custom_result": {"reason": "创建角色库条目失败"}}
        self.ctx.logger.info("管理员创建角色库条目成功: name=%s relationship=%s", name, relationship)
        self._invalidate_recognition_cache()
        if stream_id:
            await self.ctx.send.text(
                f"已创建角色“{name}”，外观卡 {len(cards)} 条，参考图 {len(character.reference_images)} 张。",
                stream_id,
            )
        return {"action": "abort", "custom_result": {"reason": f"已创建角色库条目：{name}"}}

    @staticmethod
    def _is_character_add_command(message: Any) -> bool:
        if not isinstance(message, dict):
            return False
        text = str(message.get("processed_plain_text") or "").strip()
        if not text:
            raw_message = message.get("raw_message")
            if isinstance(raw_message, list):
                text = " ".join(
                    str(item.get("data") or "")
                    for item in raw_message
                    if isinstance(item, dict) and item.get("type") == "text"
                ).strip()
        return text == "/角色添加" or text.startswith("/角色添加 ")

    @staticmethod
    def _image_components(message: Any) -> list[dict[str, Any]]:
        if not isinstance(message, dict):
            return []
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return []
        return [
            item
            for item in raw_message
            if isinstance(item, dict) and item.get("type") in {"image", "emoji"}
        ]

    @staticmethod
    def _decode_image(image: dict[str, Any]) -> bytes | None:
        encoded = image.get("binary_data_base64")
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return None

    @staticmethod
    def _referenced_message_id(message: dict[str, Any]) -> str:
        """Read both the normalized reply_to field and the raw reply component.

        Some adapters only preserve the latter in command payloads.
        """
        direct = str(message.get("reply_to") or "").strip()
        if direct:
            return direct
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return ""
        for component in raw_message:
            if not isinstance(component, dict) or component.get("type") != "reply":
                continue
            data = component.get("data")
            if isinstance(data, dict):
                reference_id = str(data.get("target_message_id") or "").strip()
            else:
                reference_id = str(data or "").strip()
            if reference_id:
                return reference_id
        return ""

    async def _recognize_image(
        self, image: dict[str, Any], *, generation: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> RecognitionResult:
        generation = self._config_generation if generation is None else generation
        image_hash = str(image.get("hash") or "").strip()
        encoded = image.get("binary_data_base64")
        if not isinstance(encoded, str) or not encoded:
            return "图片未能读取", "图片[未识别]", ()
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return "图片未能读取", "图片[未识别]", ()
        if not image_hash:
            image_hash = hashlib.sha256(image_bytes).hexdigest()
        cache_key = f"{generation}:{image_hash}"
        cached = self._cache_get(cache_key)
        if cached is not None and (details is None or cache_key in self._recognition_details):
            if details is not None:
                details.update(deepcopy(self._recognition_details[cache_key]))
            return cached
        diagnostics: dict[str, Any] = {"is_anime_character": None, "candidates": [], "limitations": []}

        def finish(recognized: RecognitionResult, *, cacheable: bool = True) -> RecognitionResult:
            for hint in recognized[2]:
                accepted = hint.provider == "AnimeTrace" and bool(hint.name) and not hint.not_confident
                diagnostics["candidates"].append({
                    "source": hint.provider, "kind": "public", "name": hint.name[:200],
                    "franchise": hint.franchise[:200], "not_confident": bool(hint.not_confident),
                    "location": "", "evidence": [], "conflicts": [], "confirmed": False,
                    "accepted_in_label": accepted,
                })
            if cacheable:
                self._cache_put(cache_key, recognized, details=diagnostics)
            if details is not None:
                details.update(deepcopy(diagnostics))
            return recognized

        catalog = (
            self._repository.private_catalog(limit=self.config.library.max_prompt_characters)
            if self.config.library.enabled and self._repository
            else []
        )
        if self.config.library.enabled and self._repository:
            total = len(self._repository.list_characters())
            if total > len(catalog):
                self.ctx.logger.warning("本地角色库共 %s 个角色，本次仅有前 %s 个进入视觉提示词", total, len(catalog))
                diagnostics["limitations"].append("私有角色库超过视觉提示容量，本次仅匹配部分外观档案")
        if not self.config.library.enabled:
            diagnostics["limitations"].append("私有角色库未启用，本次没有私库身份依据")
        if not self.config.vision.enabled:
            diagnostics["limitations"].append("插件私库视觉识别未启用，通用图片描述没有匹配私库外观")
            anime_trace_was_available = self._service_available("anime_trace")
            description, reverse_candidates = await asyncio.gather(
                self._describe_with_host_vlm(image_bytes),
                self._reverse_search(image_bytes),
            )
            online_label = self._online_label(reverse_candidates)
            recognized = (description or "图片识别失败", online_label, reverse_candidates)
            cacheable = (
                description
                and anime_trace_was_available
                and self._service_health["anime_trace"].consecutive_failures == 0
            )
            return finish(recognized, cacheable=bool(cacheable))

        result = await self._identify_with_vision(image_bytes, catalog)
        if result is None:
            diagnostics["limitations"].append("视觉识别未完成；联网候选不能替代私库视觉核对")
            reverse_candidates = await self._reverse_search(image_bytes)
            recognized = ("图片识别失败", self._online_label(reverse_candidates), reverse_candidates)
            return finish(recognized, cacheable=False)
        diagnostics["is_anime_character"] = result.is_anime_character
        vision_candidates = result.candidates or ((result.candidate,) if result.candidate else ())
        for candidate in vision_candidates[: self.config.plugin.max_characters_per_image]:
            accepted = bool(result.is_anime_character and self._label_candidate(candidate) != "图片[未识别]")
            character = self._repository.find_name(candidate.name) if accepted and self._repository else None
            diagnostics["candidates"].append({
                "source": "private_library_visual" if candidate.kind == "private" else "visual_candidate",
                "kind": candidate.kind, "name": (character.name if character else candidate.name)[:200],
                "franchise": candidate.franchise[:200], "not_confident": not accepted,
                "location": candidate.location,
                "evidence": [value[:160] for value in candidate.evidence[:3]],
                "conflicts": [value[:160] for value in candidate.conflicts[:3]],
                "confirmed": accepted, "accepted_in_label": accepted,
            })
            if candidate.conflicts:
                diagnostics["limitations"].append("视觉候选存在冲突特征，未将该候选确认为私库角色")
        if not result.is_anime_character:
            recognized = (result.description, "图片[未识别]", ())
            return finish(recognized)

        label = self._label_candidates(result.candidates or ((result.candidate,) if result.candidate else ()))
        reverse_candidates: tuple[ReverseImageHint, ...] = ()
        if label == "图片[未识别]":
            reverse_candidates = await self._reverse_search(image_bytes)
            label = self._online_label(reverse_candidates)
        recognized = (result.description, label, reverse_candidates)
        return finish(recognized)

    async def _recognize_emoji_component(self, image: dict[str, Any]) -> RecognitionResult:
        image_bytes = self._decode_image(image)
        if image_bytes is None:
            return "表情包未能读取", "图片[未识别]", ()
        image_hash = str(image.get("hash") or "").strip() or hashlib.sha256(image_bytes).hexdigest()
        hints = await self._get_emoji_role_hints(image_hash, image_bytes)
        return "表情包", self._online_label(hints), hints

    async def _get_emoji_role_hints(
        self,
        image_hash: str,
        image_bytes: bytes,
    ) -> tuple[ReverseImageHint, ...]:
        if not (
            self.config.emoji.enabled
            and self.config.reverse_image.anime_trace_enabled
            and image_hash
            and image_bytes
        ):
            return ()
        cached = self._emoji_role_cache_get(image_hash)
        if cached is not None:
            return cached
        anime_trace_was_available = self._service_available("anime_trace")
        if not anime_trace_was_available:
            return ()
        raw_hints = await self._reverse_search(image_bytes)
        hints = self._consistent_emoji_hints(raw_hints)
        if self._service_health["anime_trace"].consecutive_failures == 0:
            await self._emoji_role_cache_put(image_hash, hints)
        return hints

    @staticmethod
    def _consistent_emoji_hints(hints: tuple[ReverseImageHint, ...]) -> tuple[ReverseImageHint, ...]:
        """Reject mutually conflicting AnimeTrace identities for compact stickers.

        A single clear result is usable. Multiple people are accepted only when
        AnimeTrace places every one of them in the same non-empty work; unrelated
        works on one tiny sticker are much more likely to be false positives.
        """
        confident = tuple(
            hint
            for hint in hints
            if hint.provider == "AnimeTrace" and not hint.not_confident and hint.name.strip()
        )
        if len(confident) <= 1:
            return confident
        franchises = {hint.franchise.strip().casefold() for hint in confident if hint.franchise.strip()}
        if len(franchises) == 1 and all(hint.franchise.strip() for hint in confident):
            return confident
        return ()

    @staticmethod
    def _enrich_emoji_description(description: str, hints: tuple[ReverseImageHint, ...]) -> str:
        confident: list[ReverseImageHint] = []
        seen: set[tuple[str, str]] = set()
        for hint in hints:
            key = (hint.name.strip(), hint.franchise.strip())
            if hint.provider != "AnimeTrace" or hint.not_confident or not key[0] or key in seen:
                continue
            seen.add(key)
            confident.append(hint)
        if not confident:
            return description
        identities: list[str] = []
        for hint in confident:
            name = hint.name.replace(",", "／").replace("，", "／").strip()
            franchise = hint.franchise.replace(",", "／").replace("，", "／").strip()
            identities.append(f"{name}（{franchise}）" if franchise else name)
        identity_text = f"AnimeTrace角色候选：{'、'.join(identities)}"
        if identity_text in description:
            return description
        parts = [part.strip() for part in description.replace("，", ",").split(",") if part.strip()]
        for index, part in enumerate(parts):
            if part.startswith(("画面:", "画面：", "主体:", "主体：")):
                # 核心会按中英文分号继续拆字段；句号可确保身份仍属于画面语义，
                # 不会被 _normalize_emoji_tag_text 误当成情绪标签。
                parts[index] = f"{part}。{identity_text}"
                return ",".join(parts)
        # 旧版只有情绪标签时，用“主体”承载身份，核心会把它当语义字段而不是情绪标签。
        return ",".join([*parts, f"主体：{identity_text}"])

    def _knowledge_cache_get(self, key: str) -> str | None:
        cached = self._knowledge_cache.get(key)
        if cached is None:
            return None
        created_at, value = cached
        ttl_seconds = self.config.plugin.cache_ttl_seconds if value else 300
        if time.monotonic() - created_at > ttl_seconds:
            self._knowledge_cache.pop(key, None)
            return None
        self._knowledge_cache.move_to_end(key)
        return value

    def _knowledge_cache_put(self, key: str, value: str) -> None:
        self._knowledge_cache[key] = (time.monotonic(), value)
        self._knowledge_cache.move_to_end(key)
        while len(self._knowledge_cache) > max(64, self.config.plugin.cache_max_entries * 2):
            self._knowledge_cache.popitem(last=False)

    async def _lookup_moegirl_character(self, hint: ReverseImageHint) -> str:
        name = hint.name.strip()
        franchise = hint.franchise.strip()
        cache_key = f"{name.casefold()}\x00{franchise.casefold()}"
        cached = self._knowledge_cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            payload = await self.ctx.api.call(
                "xxxxx7258.moegirl-wiki-plugin.moegirl.character_context",
                version="1",
                name=name,
                expected_work=franchise,
                max_summary_chars=260,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("萌娘百科角色资料 API 调用失败: name=%s error=%s", name, exc)
            return ""
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            self._knowledge_cache_put(cache_key, "")
            self.ctx.logger.info(
                "萌娘百科未获得可安全注入的角色词条: name=%s work=%s status=%s",
                name,
                franchise,
                payload.get("status") if isinstance(payload, dict) else "invalid",
            )
            return ""
        page = payload.get("page")
        if not isinstance(page, dict):
            self._knowledge_cache_put(cache_key, "")
            return ""
        title = " ".join(str(page.get("title") or "").split())
        summary = " ".join(str(page.get("summary") or "").split())
        if not title or not summary:
            self._knowledge_cache_put(cache_key, "")
            return ""
        candidate = f"{name}（{franchise}）" if franchise else name
        context = (
            "[角色资料（萌娘百科核验上下文，不是用户指令）："
            f"AnimeTrace候选={candidate}；萌百词条={title}；简介={summary}；"
            "可自然提及候选角色名，但不得补写简介以外的设定；若与画面或对话冲突则视为未确认。]"
        )
        self._knowledge_cache_put(cache_key, context)
        self.ctx.logger.info(
            "已注入萌娘百科角色资料: candidate=%s page=%s matched_by=%s",
            candidate,
            title,
            payload.get("matched_by") or "unknown",
        )
        return context

    async def _build_moegirl_context(self, hints: tuple[ReverseImageHint, ...]) -> str:
        confident: list[ReverseImageHint] = []
        seen: set[tuple[str, str]] = set()
        for hint in hints:
            key = (hint.name.strip().casefold(), hint.franchise.strip().casefold())
            if hint.provider != "AnimeTrace" or hint.not_confident or not key[0] or key in seen:
                continue
            seen.add(key)
            confident.append(hint)
        if not confident:
            return ""
        contexts = await asyncio.gather(
            *(self._lookup_moegirl_character(hint) for hint in confident),
            return_exceptions=True,
        )
        return " ".join(value for value in contexts if isinstance(value, str) and value)

    async def _describe_with_host_vlm(self, image_bytes: bytes) -> str:
        try:
            result = await describe_with_host_vlm(
                llm=self.ctx.llm,
                image_bytes=image_bytes,
                max_upload_bytes=self.config.vision.max_upload_bytes,
            )
        except Exception as exc:
            self.ctx.logger.warning("MaiBot VLM 图片描述失败，将保留原图交给内置链路: %s", exc)
            return ""
        if not result:
            self.ctx.logger.warning("MaiBot VLM 未返回图片描述，将保留原图交给内置链路")
        return result

    def _cache_get(self, image_hash: str) -> RecognitionResult | None:
        if not image_hash:
            return None
        cached = self._cache.get(image_hash)
        if cached is None:
            return None
        created_at, value = cached
        if time.monotonic() - created_at > self.config.plugin.cache_ttl_seconds:
            self._cache.pop(image_hash, None)
            self._recognition_details.pop(image_hash, None)
            return None
        self._cache.move_to_end(image_hash)
        return value

    def _cache_put(
        self, image_hash: str, value: RecognitionResult, *, details: dict[str, Any] | None = None
    ) -> None:
        if not image_hash:
            return
        self._cache[image_hash] = (time.monotonic(), value)
        if details is not None:
            self._recognition_details[image_hash] = deepcopy(details)
        else:
            self._recognition_details.pop(image_hash, None)
        self._cache.move_to_end(image_hash)
        while len(self._cache) > self.config.plugin.cache_max_entries:
            evicted, _ = self._cache.popitem(last=False)
            self._recognition_details.pop(evicted, None)

    def _remember_latest(self, key: tuple[str, str], image_bytes: bytes, label: str) -> None:
        self._latest_recognitions[key] = (image_bytes, label)
        self._latest_recognitions.move_to_end(key)
        while len(self._latest_recognitions) > self.config.plugin.latest_image_max_entries:
            self._latest_recognitions.popitem(last=False)
        while self._latest_recognitions and sum(len(value[0]) for value in self._latest_recognitions.values()) > self.config.plugin.latest_image_max_total_bytes:
            self._latest_recognitions.popitem(last=False)

    def _invalidate_recognition_cache(self) -> None:
        self._cache.clear()
        self._recognition_details.clear()

    async def _record_vision_usage(
        self, model: str, base_url: str, usage: dict[str, Any] | None, time_cost: float,
    ) -> None:
        """直连视觉请求不经过宿主 LLM 统计；本地 Codex OAuth 按 API 等价价值记录。"""
        endpoint = urlparse(base_url)
        oauth_local = endpoint.hostname in {"127.0.0.1", "localhost", "::1"} and endpoint.port == 18081
        await record_usage(
            self.ctx,
            provider="openai",
            model=model,
            request_type="character_knowledge.vision",
            usage=usage,
            time_cost=time_cost,
            billing_mode="api_equivalent" if oauth_local else "api",
        )

    async def _identify_with_vision(self, image_bytes: bytes, catalog: list[dict[str, object]]) -> Any:
        if not self._service_available("vision"):
            return None
        try:
            result = await identify_image(
                provider=self.config.vision.provider,
                api_key=self.config.vision.api_key,
                base_url=self.config.vision.base_url,
                model=self.config.vision.model,
                image_bytes=image_bytes,
                private_catalog=catalog,
                max_candidates=self.config.plugin.max_characters_per_image,
                timeout_seconds=self.config.plugin.timeout_seconds,
                max_upload_bytes=self.config.vision.max_upload_bytes,
                reasoning_effort=self.config.vision.reasoning_effort,
                usage_callback=self._record_vision_usage,
            )
            if result is None:
                raise ValueError("视觉服务返回无效识别结构")
            self._service_succeeded("vision")
            return result
        except Exception as exc:
            self._service_failed("vision", exc)
            self.ctx.logger.warning("图片角色识别失败: %s", exc)
            return None

    def _service_available(self, name: str) -> bool:
        health = self._service_health[name]
        if not health.open_until:
            return True
        if time.monotonic() < health.open_until:
            return False
        health.consecutive_failures = 0
        health.open_until = 0.0
        self.ctx.logger.info("外部服务 %s 熔断冷却结束，恢复尝试", name)
        return True

    def _service_succeeded(self, name: str) -> None:
        health = self._service_health[name]
        health.consecutive_failures = 0
        health.open_until = 0.0

    def _service_failed(self, name: str, error: Exception) -> None:
        health = self._service_health[name]
        health.consecutive_failures += 1
        if health.consecutive_failures < self.config.plugin.circuit_breaker_failures:
            return
        health.open_until = time.monotonic() + self.config.plugin.circuit_breaker_cooldown_seconds
        self.ctx.logger.warning(
            "外部服务 %s 连续失败 %s 次，熔断 %s 秒: %s",
            name,
            health.consecutive_failures,
            self.config.plugin.circuit_breaker_cooldown_seconds,
            error,
        )

    def _online_label(self, hints: tuple[ReverseImageHint, ...]) -> str:
        """Label every distinct confident AnimeTrace person in a multi-character image."""
        names: list[str] = []
        for hint in hints:
            if hint.provider != "AnimeTrace" or not hint.name or hint.not_confident or hint.name in names:
                continue
            names.append(hint.name)
        if not names:
            return "图片[未识别]"
        return f"图片[{'、'.join(names[: self.config.plugin.max_characters_per_image])}]"

    def _online_description(self, hints: tuple[ReverseImageHint, ...]) -> str:
        candidates = [hint for hint in hints if hint.provider == "AnimeTrace" and hint.name and not hint.not_confident]
        names = []
        for candidate in candidates:
            if candidate.name not in names:
                names.append(candidate.name)
        if names:
            limit = self.config.plugin.max_characters_per_image
            return f"二次元角色图片，联网候选：{'、'.join(names[:limit])}"
        return "二次元图片（联网未获得明确角色）"

    def _merge_labels(self, primary: str, secondary: str) -> str:
        def names(label: str) -> list[str]:
            if not label.startswith("图片[") or "]" not in label:
                return []
            closing = label.index("]")
            body = label[3:closing]
            if body == "未识别":
                return []
            suffix = label[closing + 1 :]
            values = [item.strip() for item in body.split("、") if item.strip()]
            if suffix and len(values) == 1:
                values[0] += suffix
            return values

        merged: list[str] = []
        seen: set[str] = set()
        for value in [*names(primary), *names(secondary)]:
            plain = value.split("（", 1)[0].casefold()
            if plain and plain not in seen:
                seen.add(plain)
                merged.append(value)
        limit = self.config.plugin.max_characters_per_image
        return f"图片[{'、'.join(merged[:limit])}]" if merged else "图片[未识别]"

    def _label_candidate(self, candidate: Any) -> str:
        if candidate is None or candidate.conflicts or len(candidate.evidence) < 2:
            return "图片[未识别]"
        if candidate.kind == "private" and self.config.library.enabled and self._repository:
            character = self._repository.find_name(candidate.name)
            if character is None:
                return "图片[未识别]"
            suffix = f"（{character.relationship}）" if character.relationship else ""
            return f"图片[{character.name}]{suffix}"
        return "图片[未识别]"

    def _label_candidates(self, candidates: tuple[Any, ...]) -> str:
        labels = [
            self._label_candidate(candidate)
            for candidate in candidates[: self.config.plugin.max_characters_per_image]
        ]
        result = "图片[未识别]"
        for label in labels:
            result = self._merge_labels(result, label)
        return result

    async def _reverse_search(self, image_bytes: bytes) -> tuple[ReverseImageHint, ...]:
        settings = self.config.reverse_image
        results: list[ReverseImageHint] = []
        if settings.anime_trace_enabled and self._service_available("anime_trace"):
            try:
                anime_trace = await search_anime_trace(
                    image_bytes=image_bytes,
                    base_url=settings.anime_trace_url,
                    timeout_seconds=settings.timeout_seconds,
                    max_upload_bytes=settings.anime_trace_max_upload_bytes,
                    max_candidates=self.config.plugin.max_characters_per_image,
                )
                results.extend(anime_trace)
                self._service_succeeded("anime_trace")
                self.ctx.logger.info(
                    "AnimeTrace 返回 %s 个候选: %s",
                    len(anime_trace),
                    "; ".join(
                        f"{item.name}（{item.franchise}，{'低置信' if item.not_confident else '明确'}）"
                        for item in anime_trace
                    )
                    or "无",
                )
            except Exception as exc:
                self._service_failed("anime_trace", exc)
                self.ctx.logger.info("AnimeTrace 未获得结果: %s", exc)
        return tuple(results[: self.config.plugin.max_characters_per_image])


def create_plugin() -> CharacterKnowledgePlugin:
    return CharacterKnowledgePlugin()
