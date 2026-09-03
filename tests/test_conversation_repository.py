import json

import pytest

from social_ops_agent.conversation import ConversationCoordinator
from social_ops_agent.conversation_repository import ConversationRepository, WorkspaceStore


def test_background_save_does_not_change_selection_or_write_legacy_snapshot(tmp_path):
    one = ConversationCoordinator(tmp_path)
    two = ConversationCoordinator(tmp_path, create_new=True)
    workspace = WorkspaceStore(tmp_path)
    workspace.save([one.conversation_id, two.conversation_id], one.conversation_id)
    original = workspace.path.read_bytes()
    turn = two.begin_turn("后台任务")
    two.mark_failed(turn, stage="planning", error="测试失败")
    assert workspace.path.read_bytes() == original
    assert not (tmp_path / "conversations" / "active.json").exists()
    assert ConversationCoordinator(tmp_path).conversation_id == one.conversation_id


def test_legacy_import_once_does_not_overwrite_newer_transcript(tmp_path):
    current = ConversationCoordinator(tmp_path)
    legacy_path = current.path.parent / "active.json"
    legacy = current.path.read_bytes()
    legacy_path.write_bytes(legacy)
    turn = current.begin_turn("新任务不能被旧缓存覆盖")
    current.mark_failed(turn, stage="planning", error="错误")
    repo = ConversationRepository(tmp_path)
    migrated = repo.migrate_legacy()
    assert migrated.turns[0].user_message == "新任务不能被旧缓存覆盖"
    current.path.unlink()  # A later explicit removal must not resurrect the legacy copy.
    assert repo.migrate_legacy() is None
    assert repo.catalog() == []
    assert legacy_path.read_bytes() == legacy


def test_workspace_only_writes_when_tab_state_changes(tmp_path, monkeypatch):
    from social_ops_agent import conversation_repository as module
    writes = []
    monkeypatch.setattr(module, "write_json", lambda *args: writes.append(args))
    workspace = WorkspaceStore(tmp_path)
    workspace.save(["one", "two"], "one")
    for _ in range(20):
        workspace.save(["one", "two"], "one")
    assert len(writes) == 1
    workspace.save(["one", "two"], "two")
    assert len(writes) == 2


@pytest.mark.parametrize("value", ["../../outside", "conversation-123", "", "workspace"])
def test_repository_rejects_untrusted_ids(tmp_path, value):
    with pytest.raises(ValueError):
        ConversationRepository(tmp_path).load(value)


def test_corrupt_workspace_or_transcript_does_not_destroy_legacy(tmp_path):
    conversation = ConversationCoordinator(tmp_path)
    workspace = WorkspaceStore(tmp_path)
    workspace.path.write_text('["invalid"]')
    assert workspace.load() == {}
    invalid = conversation.path.parent / ("conversation-" + "f" * 32 + ".json")
    invalid.write_text("{broken")
    assert len(ConversationRepository(tmp_path).catalog()) == 1
