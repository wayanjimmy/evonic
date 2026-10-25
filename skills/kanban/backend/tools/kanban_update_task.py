"""
Kanban update task tool — update assignee and/or status at once.

This is the successor to kanban_update_status. It supports updating
status, assignee, title, description, and priority in a single call.

Permission is controlled by setting 'kanban:edit_task_super_only':
- When enabled (default): only super agent can edit title, description, or priority
- When disabled: regular agents can edit task metadata
- Status updates are always gated by the assignee check
"""

import re
from datetime import datetime, timezone
from plugins.kanban.db import kanban_db


def _is_subagent_of(assignee_id: str, agent_id: str) -> bool:
    if not assignee_id or not agent_id:
        return False
    return bool(re.match(f"^{re.escape(agent_id)}_sub_\\d+$", assignee_id))


def _now():
    return datetime.now(timezone.utc).isoformat()


def execute(agent: dict, args: dict) -> dict:
    agent_id = agent.get('id', '')
    task_id = args.get('task_id', '').strip().lstrip('#')
    new_status = args.get('status')
    new_assignee = args.get('assignee')
    new_title = args.get('title')
    new_description = args.get('description')
    new_priority = args.get('priority')
    new_dependencies = args.get('dependencies')

    if not task_id:
        return {'status': 'error', 'message': 'task_id is required'}

    # At least one field must be provided
    if new_status is None and new_assignee is None and new_title is None and new_description is None and new_priority is None and new_dependencies is None:
        return {
            'status': 'error',
            'message': 'At least one of "status", "assignee", "title", "description", "priority", or "dependencies" must be provided',
        }

    # Validate status if provided
    if new_status is not None and new_status not in ('in-progress', 'paused', 'done'):
        return {
            'status': 'error',
            'message': "status must be 'in-progress', 'paused', or 'done'",
        }

    # Validate priority if provided
    if new_priority is not None and new_priority not in ('low', 'medium', 'high'):
        return {
            'status': 'error',
            'message': "priority must be 'low', 'medium', or 'high'",
        }

    task = kanban_db.get(task_id)
    if not task:
        return {'status': 'error', 'message': f'Task {task_id} not found'}

    # Authorization: only the assignee (or a super agent)
    # may update the task's progress status.
    if new_status is not None:
        parent_id = agent.get('parent_id', '')
        task_assignee = task.get('assignee')
        # An unassigned task may only start when this request also assigns it.
        claimed_assignee = new_assignee.strip() if new_assignee else ''
        if not task_assignee and not claimed_assignee:
            return {
                'status': 'error',
                'message': 'This task has no assignee. Include a non-empty assignee with this status update before starting it.',
            }
        if not task_assignee and not agent.get('is_super') and claimed_assignee != agent_id:
            return {
                'status': 'error',
                'message': 'Assign this task to yourself before starting it.',
            }
        if task_assignee and task_assignee != agent_id and task_assignee != parent_id and not _is_subagent_of(task_assignee, agent_id) and not agent.get('is_super'):
            return {
                'status': 'error',
                'message': 'Only the assigned agent or a super agent can update this task',
            }

    # Check edit_task_super_only setting for title/description/priority edits
    is_editing_metadata = (new_title is not None or new_description is not None or new_priority is not None)
    if is_editing_metadata and not agent.get('is_super'):
        try:
            from backend.skills_manager import skills_manager
            config = skills_manager.get_skill_config('kanban')
            super_only = bool(config.get('edit_task_super_only', True))
        except Exception:
            super_only = True  # fail closed
        if super_only:
            return {
                'status': 'error',
                'message': 'You are not authorized to edit task details. Only the super agent can edit task title, description, or priority.',
            }

    # Guard: prevent assignee changes on done or archived tasks
    if new_assignee is not None:
        if task.get('status') == 'done' or task.get('archived_at'):
            return {
                'status': 'error',
                'message': 'Cannot change assignee on a completed or archived task.',
            }

    # Build update fields
    fields = {'updated_at': _now()}

    if new_status is not None:
        fields['status'] = new_status
        # Auto-set completed_at when status becomes 'done'
        if new_status == 'done' and not task.get('completed_at'):
            fields['completed_at'] = _now()
        # Auto-set started_at when status becomes 'in-progress'
        if new_status == 'in-progress' and not task.get('started_at'):
            fields['started_at'] = _now()
        # Track paused_at when pausing/resuming
        if new_status == 'paused':
            fields['paused_at'] = _now()
        elif new_status == 'in-progress' and task.get('paused_at'):
            fields['paused_at'] = None

    if new_assignee is not None:
        stripped = new_assignee.strip() or None
        # Regular agents cannot reassign tasks to super agents
        if stripped and not agent.get('is_super'):
            try:
                from models.db import db
                target = db.get_agent(stripped)
                if target and target.get('is_super'):
                    return {
                        'status': 'error',
                        'message': 'You cannot assign tasks to the super agent. Only the super agent can manage their own tasks.'
                    }
            except Exception:
                pass  # fail open if DB is not available
        # Block assignment to sub-agents — assign to parent instead
        if stripped and _is_subagent_of(stripped, agent_id):
            return {
                'status': 'error',
                'message': 'Assign the task to yourself instead — your sub-agents can process it even when it\'s assigned to you.'
            }
        fields['assignee'] = stripped

    if new_title is not None:
        fields['title'] = new_title

    if new_description is not None:
        fields['description'] = new_description

    if new_priority is not None:
        fields['priority'] = new_priority

    updated = kanban_db.update(task_id, fields)

    # Log status change if status was updated
    old_status = task.get('status')
    if new_status is not None and old_status != new_status:
        kanban_db.log_task_status_change(task_id, old_status, new_status)

    # Emit event
    try:
        from backend.event_stream import event_stream
        event_stream.emit('kanban_task_updated', {'task': updated})
    except Exception:
        pass

    # Handle dependencies if provided
    if new_dependencies is not None:
        try:
            deps = [int(d) for d in new_dependencies]
            kanban_db.set_dependencies(int(task_id), deps)
        except (ValueError, TypeError) as e:
            return {'status': 'error', 'message': str(e)}

    return {'status': 'success', 'task': updated}
