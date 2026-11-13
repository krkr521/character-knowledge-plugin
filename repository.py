from io import BytesIO
from pathlib import Path

import hashlib
import json

from PIL import Image

from .models import Character, ReferenceImage


class CharacterRepository:
    """加载并维护管理员确认的本地角色档案和参考图。"""

    MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
    MAX_REFERENCE_IMAGE_PIXELS = 40_000_000

    def __init__(self, path: Path) -> None:
        self._path = path
        self._characters: tuple[Character, ...] = ()

    def reload(self) -> None:
        if not self._path.exists():
            self._characters = ()
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        entries = raw.get("characters") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise ValueError("角色库必须包含 characters 列表")
        characters: list[Character] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            character_id = str(entry.get("id") or "").strip()
            name = str(entry.get("name") or "").strip()
            aliases = entry.get("aliases") or []
            appearance_cards = entry.get("appearance_cards") or []
            references = entry.get("reference_images")
            if not character_id or not name or not isinstance(aliases, list) or not isinstance(appearance_cards, list):
                continue
            characters.append(
                Character(
                    character_id=character_id,
                    name=name,
                    aliases=tuple(str(item).strip() for item in aliases if str(item).strip()),
                    relationship=str(entry.get("relationship") or "").strip(),
                    appearance_cards=tuple(str(item).strip() for item in appearance_cards if str(item).strip()),
                    reference_images=tuple(
                        reference
                        for item in (references if isinstance(references, list) else [])
                        if (reference := ReferenceImage.from_dict(item)) is not None
                    ),
                )
            )
        self._characters = tuple(characters)

    def ensure_exists(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({"characters": []})

    def private_catalog(self, *, limit: int = 40) -> list[dict[str, object]]:
        return [
            {
                "id": character.character_id,
                "name": character.name,
                "aliases": list(character.aliases),
                "appearance_cards": list(character.appearance_cards),
            }
            for character in self._characters[:limit]
        ]

    def _write(self, raw: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)

    def find_name(self, proposed_name: str) -> Character | None:
        normalized = proposed_name.strip().casefold()
        if not normalized:
            return None
        for character in self._characters:
            names = (character.name, *character.aliases)
            if any(normalized == name.casefold() for name in names):
                return character
        return None

    def upsert(
        self,
        *,
        name: str,
        relationship: str,
        appearance_cards: list[str],
        image_bytes: bytes | None = None,
        variant: str = "standard",
    ) -> Character:
        """写入管理员确认的新角色，名称相同则覆盖其外观卡。"""

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("角色名不能为空")
        cards = [card.strip() for card in appearance_cards if card.strip()]
        if len(cards) < 2:
            raise ValueError("至少需要两条稳定外观卡")
        raw = {"characters": []}
        if self._path.exists():
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("characters"), list):
                raw = loaded
        existing_entry = next(
            (
                entry
                for entry in raw["characters"]
                if isinstance(entry, dict)
                and str(entry.get("name") or "").strip().casefold() == normalized_name.casefold()
            ),
            None,
        )
        for character in self._characters:
            if character.character_id == str((existing_entry or {}).get("id") or ""):
                continue
            if normalized_name.casefold() in {character.name.casefold(), *(alias.casefold() for alias in character.aliases)}:
                raise ValueError(f"名称“{normalized_name}”已被角色“{character.name}”使用")
        entries = [entry for entry in raw["characters"] if entry is not existing_entry]
        # Names are commonly Chinese, so an ASCII-only slug would collapse every such
        # entry to the same ID. The digest is stable and collision-resistant in practice.
        identifier = str((existing_entry or {}).get("id") or "").strip() or f"char-{hashlib.sha256(normalized_name.casefold().encode('utf-8')).hexdigest()[:12]}"
        entry = dict(existing_entry or {})
        entry.update(
            {
                "id": identifier,
                "name": normalized_name,
                "aliases": list((existing_entry or {}).get("aliases") or []),
                "relationship": relationship.strip(),
                "appearance_cards": cards[:5],
            }
        )
        if image_bytes is not None:
            self._append_reference(entry, self._persist_reference_image(identifier, image_bytes, variant))
        entries.append(entry)
        raw["characters"] = entries
        self._write(raw)
        self.reload()
        character = self.find_name(normalized_name)
        if character is None:
            raise RuntimeError("角色库写入后未找到新角色")
        return character

    def append_appearance_cards(
        self,
        *,
        name: str,
        appearance_cards: list[str],
        limit: int = 15,
        image_bytes: bytes | None = None,
        variant: str = "standard",
    ) -> Character:
        """合并管理员从已发图片确认的补充外观卡，保留既有身份和关系。"""

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("角色名不能为空")
        cards = [card.strip() for card in appearance_cards if card.strip()]
        if len(cards) < 2:
            raise ValueError("至少需要两条稳定外观卡")
        raw = {"characters": []}
        if self._path.exists():
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("characters"), list):
                raw = loaded
        target: dict[str, object] | None = None
        for entry in raw["characters"]:
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().casefold() == normalized_name.casefold():
                target = entry
                break
        if target is None:
            raise ValueError(f"角色库中不存在“{normalized_name}”")
        existing = target.get("appearance_cards")
        if not isinstance(existing, list):
            existing = []
        merged: list[str] = []
        seen: set[str] = set()
        for card in [*existing, *cards]:
            normalized_card = str(card).strip()
            key = normalized_card.casefold()
            if normalized_card and key not in seen:
                seen.add(key)
                merged.append(normalized_card)
        target["appearance_cards"] = merged[-limit:]
        if image_bytes is not None:
            self._append_reference(
                target,
                self._persist_reference_image(str(target.get("id") or ""), image_bytes, variant),
            )
        self._write(raw)
        self.reload()
        character = self.find_name(normalized_name)
        if character is None:
            raise RuntimeError("角色库修正后未找到角色")
        return character

    @classmethod
    def _reference_extension(cls, image_bytes: bytes) -> str:
        """拒绝非图片、损坏图和超限图，不修改管理员给出的原图。"""
        if not image_bytes or len(image_bytes) > cls.MAX_REFERENCE_IMAGE_BYTES:
            raise ValueError("参考图不能为空且不能超过 20 MiB")
        with Image.open(BytesIO(image_bytes)) as image:
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format or "")
            if extension is None:
                raise ValueError("生图参考图仅支持 PNG、JPEG 或 WebP；GIF 请先导出一帧")
            if image.width * image.height > cls.MAX_REFERENCE_IMAGE_PIXELS:
                raise ValueError("参考图像素数超过限制")
            image.verify()
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
        return extension

    def _reference_path(self, relative_file: str) -> Path:
        data_root = self._path.parent.resolve()
        root = (data_root / "reference_images").resolve()
        if not root.is_relative_to(data_root):
            raise ValueError("参考图目录不能指向角色库数据目录以外")
        relative = Path(relative_file)
        path = (data_root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or path == root:
            raise ValueError("参考图必须位于角色库的 reference_images 目录内")
        return path

    def _persist_reference_image(self, character_id: str, image_bytes: bytes, variant: str) -> ReferenceImage:
        extension = self._reference_extension(image_bytes)
        if not character_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in character_id):
            raise ValueError("角色 ID 不能用于参考图目录")
        normalized_variant = variant.strip() or "standard"
        if len(normalized_variant) > 64 or any(ord(char) < 32 for char in normalized_variant):
            raise ValueError("参考图变体标签无效")
        digest = hashlib.sha256(image_bytes).hexdigest()
        reference = ReferenceImage(
            file=f"reference_images/{character_id}/{digest}.{extension}",
            variant=normalized_variant,
            sha256=digest,
        )
        path = self._reference_path(reference.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(image_bytes)
            temporary.replace(path)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("已有参考图内容与文件哈希不符")
        return reference

    @staticmethod
    def _append_reference(entry: dict[str, object], reference: ReferenceImage) -> None:
        existing = entry.get("reference_images")
        values = list(existing) if isinstance(existing, list) else []
        if not any(isinstance(item, dict) and item.get("sha256") == reference.sha256 for item in values):
            values.append(reference.to_dict())
        entry["reference_images"] = values

    def store_reference_image(self, *, name: str, image_bytes: bytes, variant: str = "standard") -> ReferenceImage:
        """为已有角色保存原始参考图，同内容只保存一次。"""
        character = self.find_name(name)
        if character is None:
            raise ValueError(f"角色库中不存在“{name}”")
        reference = self._persist_reference_image(character.character_id, image_bytes, variant)
        self._update_entry(name=character.name, update=lambda entry: self._append_reference(entry, reference))
        return reference

    def read_reference_image(self, reference: ReferenceImage) -> bytes:
        """校验归属、大小和哈希后读取参考图，禁止读取库外文件。"""
        path = self._reference_path(reference.file)
        if path.stat().st_size > self.MAX_REFERENCE_IMAGE_BYTES:
            raise ValueError("参考图超过 20 MiB")
        image_bytes = path.read_bytes()
        if hashlib.sha256(image_bytes).hexdigest() != reference.sha256:
            raise ValueError("参考图内容与登记哈希不符")
        self._reference_extension(image_bytes)
        return image_bytes

    def list_characters(self) -> tuple[Character, ...]:
        return self._characters

    def set_relationship(self, *, name: str, relationship: str) -> Character:
        return self._update_entry(name=name, update=lambda entry: entry.__setitem__("relationship", relationship.strip()))

    def add_alias(self, *, name: str, alias: str) -> Character:
        normalized_alias = alias.strip()
        if not normalized_alias:
            raise ValueError("别名不能为空")
        conflict = self.find_name(normalized_alias)
        target = self.find_name(name)
        if conflict is not None and (target is None or conflict.character_id != target.character_id):
            raise ValueError(f"名称或别名“{normalized_alias}”已属于角色“{conflict.name}”")

        def update(entry: dict[str, object]) -> None:
            aliases = entry.get("aliases")
            values = [str(item).strip() for item in aliases] if isinstance(aliases, list) else []
            if normalized_alias.casefold() not in {item.casefold() for item in values}:
                values.append(normalized_alias)
            entry["aliases"] = values

        return self._update_entry(name=name, update=update)

    def remove_alias(self, *, name: str, alias: str) -> Character:
        normalized_alias = alias.strip()
        if not normalized_alias:
            raise ValueError("别名不能为空")

        def update(entry: dict[str, object]) -> None:
            aliases = entry.get("aliases")
            values = [str(item).strip() for item in aliases] if isinstance(aliases, list) else []
            filtered = [item for item in values if item.casefold() != normalized_alias.casefold()]
            if len(filtered) == len(values):
                raise ValueError(f"角色“{entry.get('name')}”没有别名“{normalized_alias}”")
            entry["aliases"] = filtered

        return self._update_entry(name=name, update=update)

    def _update_entry(self, *, name: str, update: object) -> Character:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("角色名不能为空")
        if not callable(update):
            raise TypeError("update 必须是可调用对象")
        raw = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {"characters": []}
        entries = raw.get("characters") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise ValueError("角色库必须包含 characters 列表")
        target = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict)
                and normalized_name.casefold()
                in {str(entry.get("name") or "").strip().casefold(), *(str(item).strip().casefold() for item in entry.get("aliases") or [])}
            ),
            None,
        )
        if target is None:
            raise ValueError(f"角色库中不存在“{normalized_name}”")
        update(target)
        self._write(raw)
        self.reload()
        character = self.find_name(str(target.get("name") or ""))
        if character is None:
            raise RuntimeError("角色库更新后未找到角色")
        return character
