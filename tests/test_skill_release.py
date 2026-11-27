"""Versioned Skill release manifests and rollback."""

from __future__ import annotations

from pathlib import Path

from krelcode.skills import SkillManager, SkillReleaseStore


def test_skill_release_records_versions_and_rolls_back(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "review"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: review\nversion: 1.0.0\ndescription: Review changes\n---\n"
        "Use checklist v1.\n",
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path, extra_paths=[root], home=tmp_path / "home")
    skill = manager.get("review")
    assert skill is not None
    store = SkillReleaseStore(tmp_path)

    first = store.record(skill)
    skill_file.write_text(
        "---\nname: review\nversion: 2.0.0\ndescription: Review changes\n---\n"
        "Use checklist v2.\n",
        encoding="utf-8",
    )
    manager.reload()
    second_skill = manager.get("review")
    assert second_skill is not None
    second = store.record(second_skill)

    releases = store.list("review")
    assert [item.release_id for item in releases] == [
        second.release_id, first.release_id]

    restored = store.rollback(second_skill)
    assert restored.release_id == first.release_id
    assert "checklist v1" in skill_file.read_text(encoding="utf-8")
    assert store.list("review")[0].skill_markdown.startswith("---")
