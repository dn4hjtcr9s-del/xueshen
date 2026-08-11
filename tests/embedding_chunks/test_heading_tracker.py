"""验证教材标题层级继承与同级覆盖。"""

from scripts.embedding_chunks.heading_tracker import HeadingTracker


def test_heading_tracker_replaces_same_level_and_clears_deeper_levels() -> None:
    tracker = HeadingTracker()

    tracker.update(1, "第一章 函数")
    tracker.update(2, "1.1 集合")
    tracker.update(3, "一、定义")
    assert tracker.path == ("第一章 函数", "1.1 集合", "一、定义")

    tracker.update(2, "1.2 映射")
    assert tracker.path == ("第一章 函数", "1.2 映射")

    tracker.update(1, "第二章 极限")
    assert tracker.path == ("第二章 极限",)


def test_heading_tracker_ignores_empty_title_and_clamps_level() -> None:
    tracker = HeadingTracker()
    tracker.update(0, "章标题")
    tracker.update(9, "末级标题")
    tracker.update(2, "  ")

    assert tracker.path == ("章标题", "末级标题")
