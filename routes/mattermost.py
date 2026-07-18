"""Mattermost integration callback routes."""

from flask import Blueprint, jsonify, request

from backend.channels.registry import channel_manager

mattermost_bp = Blueprint('mattermost', __name__)


@mattermost_bp.route('/api/channels/mattermost/actions', methods=['POST'])
def api_mattermost_action():
    payload = request.get_json(silent=True) or {}
    context = payload.get('context') or {}
    channel_id = context.get('channel_id')
    if not channel_id:
        return jsonify({'ephemeral_text': 'Missing channel id.'}), 400
    instance = channel_manager.get_channel_instance(channel_id)
    if not instance or getattr(instance, 'get_channel_type', lambda: None)() != 'mattermost':
        return jsonify({'ephemeral_text': 'Mattermost channel is not running.'}), 404
    response, status = instance.handle_approval_action(payload)
    return jsonify(response), status

