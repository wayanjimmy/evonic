"""
Kanban update status tool — move a task to in-progress or done.
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
    new_status = args.get('status', '').strip()

    if not task_id:
        return {'status': 'error', 'message': 'task_id is required'}
    if new_status not in ('in-progress', 'paused', 'done'):
        return {'status': 'error', 'message': "status must be 'in-progress', 'paused', or 'done'"}

    task = kanban_db.get(task_id)
    if not task:
        return {'status': 'error', 'message': f'Task {task_id} not found'}

    task_assignee = task.get('assignee')
    if not task_assignee:
        return {'status': 'error', 'message': 'This task has no assignee. Assign it before updating its status.'}
    parent_id = agent.get('parent_id', '')
    is_parent_of_assignee = _is_subagent_of(task_assignee, agent_id)
    if task_assignee != agent_id and task_assignee != parent_id and not is_parent_of_assignee and not agent.get('is_super'):
        return {'status': 'error', 'message': 'Only the assigned agent or a super agent can update this task'}

    old_status = task.get('status')
    fields = {'status': new_status, 'updated_at': _now()}
    if new_status == 'done' and not task.get('completed_at'):
        fields['completed_at'] = _now()
    if new_status == 'paused':
        fields['paused_at'] = _now()
    elif new_status == 'in-progress':
        if not task.get('started_at'):
            fields['started_at'] = _now()
        if task.get('paused_at'):
            fields['paused_at'] = None

    updated = kanban_db.update(task_id, fields)

    # Log status change to activity log
    if old_status and old_status != new_status:
        kanban_db.log_task_status_change(task_id, old_status, new_status)

    try:
        from backend.event_stream import event_stream
        event_stream.emit('kanban_task_updated', {'task': updated})
    except Exception:
        pass

    result: dict = {'status': 'success', 'task': updated}

    if new_status == 'done':
        # Read finish reminder from kanban plugin config
        finish_reminder = ''
        try:
            import os, json as _json
            config_path = os.path.join(
                os.path.dirname(__file__), '..', '..', '..', '..', '..', 'plugins', 'kanban', 'config.json'
            )
            with open(os.path.normpath(config_path)) as _f:
                finish_reminder = _json.load(_f).get('FINISH_REMINDER', '').strip()
        except Exception:
            pass

        note = f"Task marked as done. Next: call state('kanban:finish', {{'task_id': '{task_id}'}}) to close the workflow."
        if finish_reminder:
            note += f"\n[REMINDER] {finish_reminder}"
        result['note'] = note

    return result
