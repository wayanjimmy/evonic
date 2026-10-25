"""Regression tests for the Kanban assignment-before-start invariant."""

from unittest.mock import MagicMock, patch

import pytest

from plugins.kanban import handler
from skills.kanban.backend.tools import kanban_update_status, kanban_update_task


class FakeKanbanDb:
    """Minimal in-memory Kanban store for status tool authorization tests."""

    def __init__(self, task):
        self.task = dict(task)
        self.updates = []

    def get(self, task_id):
        return dict(self.task) if str(task_id) == str(self.task['id']) else None

    def update(self, task_id, fields):
        self.updates.append((str(task_id), dict(fields)))
        self.task.update(fields)
        return dict(self.task)

    def log_task_status_change(self, *args):
        pass


@pytest.fixture(autouse=True)
def reset_handler_state():
    handler._pending_tasks.clear()
    handler._active_tasks.clear()
    handler._approval_granted.clear()
    handler._awaiting_approval.clear()
    yield
    handler._pending_tasks.clear()
    handler._active_tasks.clear()
    handler._approval_granted.clear()
    handler._awaiting_approval.clear()


def test_super_agent_cannot_update_status_of_unassigned_task():
    db = FakeKanbanDb({'id': '1', 'status': 'todo', 'assignee': None})

    with patch.object(kanban_update_status, 'kanban_db', db):
        result = kanban_update_status.execute(
            {'id': 'super-agent', 'is_super': True},
            {'task_id': '1', 'status': 'in-progress'},
        )

    assert result['status'] == 'error'
    assert 'no assignee' in result['message']
    assert db.task['status'] == 'todo'
    assert db.updates == []


def test_super_agent_can_update_status_of_task_assigned_to_another_agent():
    db = FakeKanbanDb({'id': '1', 'status': 'todo', 'assignee': 'worker-agent'})

    with patch.object(kanban_update_status, 'kanban_db', db):
        result = kanban_update_status.execute(
            {'id': 'super-agent', 'is_super': True},
            {'task_id': '1', 'status': 'in-progress'},
        )

    assert result['status'] == 'success'
    assert db.task['status'] == 'in-progress'


def test_regular_agent_cannot_update_another_agents_assigned_task():
    db = FakeKanbanDb({'id': '1', 'status': 'todo', 'assignee': 'worker-agent'})

    with patch.object(kanban_update_status, 'kanban_db', db):
        result = kanban_update_status.execute(
            {'id': 'other-agent', 'is_super': False},
            {'task_id': '1', 'status': 'in-progress'},
        )

    assert result['status'] == 'error'
    assert 'Only the assigned agent' in result['message']
    assert db.task['status'] == 'todo'


def test_super_agent_cannot_start_unassigned_task_with_update_task():
    db = FakeKanbanDb({'id': '1', 'status': 'todo', 'assignee': None})

    with patch.object(kanban_update_task, 'kanban_db', db):
        result = kanban_update_task.execute(
            {'id': 'super-agent', 'is_super': True},
            {'task_id': '1', 'status': 'in-progress'},
        )

    assert result['status'] == 'error'
    assert 'no assignee' in result['message']
    assert db.task['status'] == 'todo'
    assert db.updates == []


def test_regular_agent_cannot_assign_an_unassigned_task_to_another_agent_and_start():
    db = FakeKanbanDb({'id': '1', 'status': 'todo', 'assignee': None})

    with patch.object(kanban_update_task, 'kanban_db', db):
        result = kanban_update_task.execute(
            {'id': 'regular-agent', 'is_super': False},
            {'task_id': '1', 'status': 'in-progress', 'assignee': 'worker-agent'},
        )

    assert result['status'] == 'error'
    assert 'Assign this task to yourself' in result['message']
    assert db.task['status'] == 'todo'
    assert db.updates == []


def test_super_agent_can_assign_and_start_unassigned_task_in_one_update():
    db = FakeKanbanDb({'id': '1', 'status': 'todo', 'assignee': None})

    with patch.object(kanban_update_task, 'kanban_db', db):
        result = kanban_update_task.execute(
            {'id': 'super-agent', 'is_super': True},
            {'task_id': '1', 'status': 'in-progress', 'assignee': 'worker-agent'},
        )

    assert result['status'] == 'success'
    assert db.task['status'] == 'in-progress'
    assert db.task['assignee'] == 'worker-agent'


def test_activate_rejects_unassigned_task_before_status_promotion():
    db = MagicMock()
    db.get.return_value = {
        'id': '1',
        'title': 'Unassigned task',
        'status': 'todo',
        'assignee': None,
        'archived_at': None,
    }
    db.get_unmet_dependencies.return_value = []

    with (
        patch('plugins.kanban.db.kanban_db', db),
        patch.object(handler, '_is_autopilot', return_value=True),
    ):
        result = handler._state_handler(
            'super-agent', 'session-1', None, 'kanban:activate', {'task_id': '1'})

    assert result['result'] == 'error'
    assert 'assign it before starting' in result['message']
    db.update.assert_not_called()
    assert 'super-agent' not in handler._active_tasks
