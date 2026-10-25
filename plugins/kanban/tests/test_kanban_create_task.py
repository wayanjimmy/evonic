"""
Unit tests for kanban_create_task tool.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestKanbanCreateTaskTool(unittest.TestCase):
    """Tests for kanban_create_task tool execution."""

    def setUp(self):
        """Create a temporary database for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test_kanban.db')
        from plugins.kanban.db import KanbanDB
        self.test_db = KanbanDB(db_path=self.db_path)

        # Patch kanban_db singleton in the db module
        import plugins.kanban.db as db_module
        self._original_db = db_module.kanban_db
        db_module.kanban_db = self.test_db

        # Also patch in the tool module since it imports at module level
        import skills.kanban.backend.tools.kanban_create_task as tool_module
        self._original_tool_db = tool_module.kanban_db
        tool_module.kanban_db = self.test_db

        # Pin the permission setting so the suite is deterministic regardless of
        # the ambient skill config in the developer/CI database. The tool reads
        # `create_task_super_only` (default True) via skills_manager.
        from unittest.mock import patch
        from backend.skills_manager import skills_manager
        self._config_patch = patch.object(
            skills_manager, 'get_skill_config',
            return_value={'create_task_super_only': True},
        )
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)

    def tearDown(self):
        """Restore original kanban_db and clean up."""
        import plugins.kanban.db as db_module
        db_module.kanban_db = self._original_db
        import skills.kanban.backend.tools.kanban_create_task as tool_module
        tool_module.kanban_db = self._original_tool_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_tool(self):
        """Import the tool fresh each time."""
        from skills.kanban.backend.tools.kanban_create_task import execute
        return execute

    def test_create_task_super_admin(self):
        """Test creating a task as super admin."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Test task', 'description': 'A test'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertIsNotNone(result['task'])
        self.assertEqual(result['task']['title'], 'Test task')
        self.assertEqual(result['task']['status'], 'todo')
        self.assertEqual(result['task']['priority'], 'low')

    def test_create_task_non_super_admin(self):
        """Test that non-super admin cannot create tasks."""
        execute = self._get_tool()
        agent = {'id': 'agent_1', 'is_super': False}
        args = {'title': 'Forbidden task'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')
        self.assertIn('authorized', result['message'].lower())

    def test_create_task_no_is_super_field(self):
        """Test that agent without is_super field is denied."""
        execute = self._get_tool()
        agent = {'id': 'agent_1'}
        args = {'title': 'No field task'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')

    def test_create_task_missing_title(self):
        """Test creating a task without title."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'description': 'No title here'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')
        self.assertIn('title', result['message'].lower())

    def test_create_task_empty_title(self):
        """Test creating a task with empty title."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': '   '}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')

    def test_create_task_whitespace_title(self):
        """Test creating a task with whitespace-only title."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': '\t\n  '}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')

    def test_create_task_with_priority_low(self):
        """Test creating a task with low priority."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Low priority', 'priority': 'low'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['priority'], 'low')

    def test_create_task_with_priority_medium(self):
        """Test creating a task with medium priority."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Medium priority', 'priority': 'medium'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['priority'], 'medium')

    def test_create_task_with_priority_high(self):
        """Test creating a task with high priority."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'High priority', 'priority': 'high'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['priority'], 'high')

    def test_create_task_invalid_priority(self):
        """Test creating a task with invalid priority."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Bad priority', 'priority': 'urgent'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')

    def test_create_task_invalid_priority_uppercase(self):
        """Test that uppercase priority is normalized."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Uppercase priority', 'priority': 'HIGH'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['priority'], 'high')

    def test_create_task_with_assignee(self):
        """Test creating a task with an assignee."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Assigned task', 'assignee': 'agent_42'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['assignee'], 'agent_42')

    def test_create_task_without_assignee(self):
        """Test creating a task without an assignee."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Unassigned task'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertIsNone(result['task']['assignee'])

    def test_create_task_with_empty_assignee(self):
        """Test creating a task with empty string assignee."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Empty assignee', 'assignee': ''}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertIsNone(result['task']['assignee'])

    def test_create_task_default_description(self):
        """Test that description defaults to empty string."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'No description'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['description'], '')

    def test_create_task_default_status(self):
        """Test that status defaults to 'todo'."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Default status'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['status'], 'todo')

    def test_create_task_default_priority(self):
        """Test that priority defaults to 'low'."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Default priority'}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['priority'], 'low')

    def test_create_task_strips_whitespace(self):
        """Test that title whitespace is stripped."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': '  Stripped title  '}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['title'], 'Stripped title')

    def test_create_task_strips_description(self):
        """Test that description whitespace is stripped."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Strip desc', 'description': '  Stripped  '}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['task']['description'], 'Stripped')

    def test_create_task_returns_task_id(self):
        """Test that created task has an ID."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Has ID'}
        result = execute(agent, args)
        self.assertIn('id', result['task'])
        self.assertIsNotNone(result['task']['id'])

    def test_create_task_returns_created_at(self):
        """Test that created task has created_at."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Has created_at'}
        result = execute(agent, args)
        self.assertIn('created_at', result['task'])
        self.assertIsNotNone(result['task']['created_at'])

    def test_create_task_returns_updated_at(self):
        """Test that created task has updated_at."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': 'Has updated_at'}
        result = execute(agent, args)
        self.assertIn('updated_at', result['task'])
        self.assertIsNotNone(result['task']['updated_at'])

    def test_create_task_multiple_tasks_different_ids(self):
        """Test that multiple tasks get different IDs."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        r1 = execute(agent, {'title': 'Task 1'})
        r2 = execute(agent, {'title': 'Task 2'})
        self.assertNotEqual(r1['task']['id'], r2['task']['id'])

    def test_create_task_args_none_title(self):
        """Test creating a task with None title."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        args = {'title': None}
        result = execute(agent, args)
        self.assertEqual(result['status'], 'error')

    def test_create_task_args_empty_dict(self):
        """Test creating a task with empty args dict."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        result = execute(agent, {})
        self.assertEqual(result['status'], 'error')

    def test_create_task_args_none(self):
        """Test creating a task with None args."""
        execute = self._get_tool()
        agent = {'id': 'siwa', 'is_super': True}
        with self.assertRaises(Exception):
            execute(agent, None)


if __name__ == '__main__':
    unittest.main()
