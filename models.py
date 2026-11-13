from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReferenceImage:
    """管理员保存的角色参考图；文件名相对于角色库数据目录。"""

    file: str
    sha256: str
    variant: str = "standard"

    @classmethod
    def from_dict(cls, value: Any) -> "ReferenceImage | None":
        if not isinstance(value, dict):
            return None
        file = str(value.get("file") or "").strip()
        digest = str(value.get("sha256") or "").strip().lower()
        if not file or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return None
        return cls(file=file, sha256=digest, variant=str(value.get("variant") or "standard").strip())

    def to_dict(self) -> dict[str, str]:
        return {"file": self.file, "variant": self.variant, "sha256": self.sha256}


@dataclass(frozen=True)
class Character:
    """本地角色库中的一个权威角色条目。"""

    character_id: str
    name: str
    aliases: tuple[str, ...]
    relationship: str
    appearance_cards: tuple[str, ...]
    reference_images: tuple[ReferenceImage, ...] = ()


@dataclass(frozen=True)
class VisionCandidate:
    """视觉模型针对一张图片输出的受限候选。"""

    kind: str
    name: str
    franchise: str
    evidence: tuple[str, ...]
    conflicts: tuple[str, ...]
    location: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> "VisionCandidate | None":
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind") or "unknown").strip().lower()
        if kind not in {"private", "public", "unknown"}:
            return None
        name = str(value.get("name") or "").strip()
        franchise = str(value.get("franchise") or "").strip()
        evidence = value.get("evidence")
        conflicts = value.get("conflicts")
        if not isinstance(evidence, list) or not isinstance(conflicts, list):
            return None
        return cls(
            kind=kind,
            name=name,
            franchise=franchise,
            evidence=tuple(str(item).strip() for item in evidence if str(item).strip()),
            conflicts=tuple(str(item).strip() for item in conflicts if str(item).strip()),
            location=" ".join(str(value.get("location") or "").split())[:160],
        )


@dataclass(frozen=True)
class VisionResult:
    """一次视觉请求返回的通用描述与角色候选。"""

    description: str
    is_anime_character: bool
    candidate: VisionCandidate | None
    candidates: tuple[VisionCandidate, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> "VisionResult | None":
        if not isinstance(value, dict):
            return None
        description = str(value.get("description") or "").strip()
        if not description or len(description) > 160:
            return None
        is_anime_character = value.get("is_anime_character")
        if not isinstance(is_anime_character, bool):
            return None
        raw_candidates = value.get("candidates")
        candidates = (
            tuple(candidate for item in raw_candidates if (candidate := VisionCandidate.from_dict(item)) is not None)
            if isinstance(raw_candidates, list)
            else ()
        )
        legacy_candidate = VisionCandidate.from_dict(value)
        if not candidates and legacy_candidate is not None:
            candidates = (legacy_candidate,)
        return cls(
            description=description,
            is_anime_character=is_anime_character,
            candidate=candidates[0] if candidates else None,
            candidates=candidates[:10],
        )
