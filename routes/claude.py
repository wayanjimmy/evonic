"""Claude provider setup routes: Anthropic API key and the Claude Code CLI."""

from flask import Blueprint, jsonify, request

from backend.provider.claude_cli import get_status
from backend.provider.claude_code import clear_credentials, save_api_key
from models.db import db


claude_bp = Blueprint("claude", __name__)

_FORMATS = ("anthropic", "claude_cli")


def _provider(provider_id):
    provider = db.get_provider(provider_id)
    return provider if provider and provider.get("api_format") in _FORMATS else None


@claude_bp.route("/api/providers/<provider_id>/auth/claude/status", methods=["GET"])
def claude_status(provider_id):
    provider = _provider(provider_id)
    if not provider:
        return jsonify({"error": "Claude provider not found."}), 404
    body = {"provider_id": provider_id, "provider_kind": "claude",
            "api_format": provider["api_format"]}
    if provider["api_format"] == "claude_cli":
        status = get_status(force=request.args.get("refresh") == "1")
        body.update(connected=status["logged_in"], cli=status)
    else:
        body.update(connected=bool(provider.get("api_key")))
    return jsonify(body)


@claude_bp.route("/api/providers/<provider_id>/auth/claude/secret", methods=["POST"])
def claude_secret(provider_id):
    provider = _provider(provider_id)
    if not provider or provider["api_format"] != "anthropic":
        return jsonify({"error": "Anthropic API provider not found."}), 404
    data = request.get_json(silent=True) or {}
    result = save_api_key(db, provider_id, data.get("secret", ""))
    return jsonify(result), (400 if result.get("error") else 200)


@claude_bp.route("/api/providers/<provider_id>/auth/claude/disconnect", methods=["POST"])
def claude_disconnect(provider_id):
    provider = _provider(provider_id)
    if not provider or provider["api_format"] != "anthropic":
        return jsonify({"error": "Anthropic API provider not found."}), 404
    clear_credentials(db, provider_id)
    return jsonify({"success": True})
