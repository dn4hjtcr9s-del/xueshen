"""维护教材 1–4 级标题栈，并为正文提供稳定章节路径。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class HeadingTracker:
    """同级标题覆盖、低级标题清空更深层级。"""

    _levels: dict[int, str] = field(default_factory=dict)

    def update(self, level: int, title: str) -> None:
        normalized = " ".join(title.split())
        if not normalized:
            return
        normalized_level = max(1, min(level, 4))
        for existing_level in tuple(self._levels):
            if existing_level >= normalized_level:
                del self._levels[existing_level]
        self._levels[normalized_level] = normalized

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self._levels[level] for level in sorted(self._levels))
