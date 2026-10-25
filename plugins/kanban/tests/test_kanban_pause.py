"""Regression coverage for the board-wide Kanban scanner pause control."""

from unittest.mock import MagicMock, patch

import pytest

from plugins.kanban import handler


TODO_TASK = {
    'id': 10,
    'title': 'Queued task',
    'status': 'todo',
    'assignee': 'agent-a',
    'priority': 'medium',
}
STALE_TASK = {
    'id': 11,
    'title': 'Stale task',
    'status': 'in-progress',
    'assignee': 'agent-a',
}
DONE_TASK = {
    'id': 12,
    'title': 'Completed task',
    'status': 'done',
    'assignee': 'agent-a',
    'completed_at': '2026-10-01T12:00:00',
}
COMMENT = {
    'id': 1001,
    'task_id': 12,
    'author': 'owner',
    'content': 'Please fix the remaining issue.',
}


@pytest.fixture(autouse=True)
def reset_notifier_state():
    handler._set_notifier_paused(False)
    handler._classified_comments.clear()
    handler._pending_tasks.clear()
    handler._active_tasks.clear()
    handler._paused_tasks.clear()
    yield
    handler._set_notifier_paused(False)
    handler._classified_comments.clear()
    handler._pending_tasks.clear()
    handler._active_tasks.clear()
    handler._paused_tasks.clear()


def test_paused_scans_do_not_read_work_or_trigger_assigned_agents():
    """Pause is a hard boundary for todo, stale, and comment scanner entrypoints."""
    handler._set_notifier_paused(True)

    with (
        patch.object(handler, '_load_config') as load_config,
        patch.object(handler, '_get_kanban_skill_agents') as eligible_agents,
        patch.object(handler, '_load_tasks') as load_tasks,
        patch.object(handler, '_notify_agent') as notify_agent,
        patch.object(handler, '_notify_stale_task') as notify_stale,
        patch.object(handler, '_notify_agent_followup') as notify_followup,
        patch.object(handler, '_classify_followup') as classify_followup,
    ):
        result = handler._scan_and_notify()
        handler._scan_stale_tasks()
        handler._scan_comments_for_followup()

    assert result == {'notified': 0, 'failed': 0, 'details': [], 'paused': True}
    load_config.assert_not_called()
    eligible_agents.assert_not_called()
    load_tasks.assert_not_called()
    notify_agent.assert_not_called()
    notify_stale.assert_not_called()
    notify_followup.assert_not_called()
    classify_followup.assert_not_called()


def test_resume_reenables_todo_stale_and_comment_scans():
    """The same scanner entrypoints resume their regular delivery behavior."""
    handler._set_notifier_paused(True)
    handler._set_notifier_paused(False)

    comment_db = MagicMock()
    comment_db.get_comments_since.return_value = [dict(COMMENT)]
    comment_db.get_attachments_for_comment.return_value = []
    comment_db.get_last_comment_before.return_value = None
    comment_db.get.return_value = dict(DONE_TASK, status='in-progress', completed_at=None)

    stale_db = MagicMock()
    stale_db.get_active_task_for_agent.return_value = dict(STALE_TASK)

    with (
        patch.object(handler, '_load_config', return_value={}),
        patch.object(handler, '_get_kanban_skill_agents', return_value=['agent-a']),
        patch.object(handler, '_load_tasks', return_value=[dict(TODO_TASK)]),
        patch.object(handler, '_notify_agent', return_value={'success': True}) as notify_agent,
    ):
        assert handler._scan_and_notify()['notified'] == 1
    notify_agent.assert_called_once()

    with (
        patch.object(handler, '_load_config', return_value={}),
        patch.object(handler, '_get_kanban_skill_agents', return_value=['agent-a']),
        patch('plugins.kanban.db.kanban_db', stale_db),
        patch('backend.agent_runtime.agent_runtime.is_agent_busy', return_value=False),
        patch.object(handler, '_notify_stale_task') as notify_stale,
    ):
        handler._scan_stale_tasks()
    notify_stale.assert_called_once_with('agent-a', STALE_TASK, 'telegram', None)

    with (
        patch.object(handler, '_load_config', return_value={}),
        patch.object(handler, '_get_kanban_skill_agents', return_value=['agent-a']),
        patch.object(handler, '_load_tasks', return_value=[dict(DONE_TASK)]),
        patch('plugins.kanban.db.kanban_db', comment_db),
        patch.object(handler, '_classify_followup', return_value=True) as classify_followup,
        patch.object(handler, '_notify_agent_followup', return_value=True) as notify_followup,
    ):
        handler._scan_comments_for_followup()

    classify_followup.assert_called_once()
    notify_followup.assert_called_once()


def test_pause_blocks_direct_agent_notification_even_when_forced():
    """A force flag from a manual route cannot bypass the board-wide pause."""
    handler._set_notifier_paused(True)

    with patch.object(handler, '_agent_has_kanban_skill') as has_skill:
        result = handler._notify_agent(
            'agent-a', TODO_TASK, 'telegram', force=True, force_delay=True)

    assert result == {'success': False, 'reason': 'paused'}
    has_skill.assert_not_called()
