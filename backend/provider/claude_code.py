"""Anthropic API-key credentials.

Claude *subscription* access goes through the official CLI instead (see
``backend.provider.claude_cli``). Evonic does not read, store or refresh Claude
OAuth tokens and does not present itself as the Claude Code client.
"""

from typing import Any, Dict, Optional

# Claude subscription (OAuth / setup-token) credentials must never be stored here.
OAUTH_TOKEN_PREFIXES = ("sk-ant-oat", "sk-ant-ort", "cc-", "eyJ")


def save_api_key(db, provider_id: str, key: str) -> Dict[str, Any]:
    key = key.strip()
    if not key or key.startswith(OAUTH_TOKEN_PREFIXES):
        return {"error": "Enter an Anthropic API key. Claude subscription tokens are not "
                         "accepted; use the Claude Code (CLI) provider for a subscription."}
    db.update_provider(provider_id, {
        "api_key": key,
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "api_key",
        "credential_source": "api_key",
    })
    return {"success": True}


def resolve_api_key(db, provider_id: str) -> Optional[str]:
    provider = db.get_provider(provider_id)
    if not provider or provider.get("api_format") != "anthropic":
        return None
    return provider.get("api_key") or None


def clear_credentials(db, provider_id: str) -> bool:
    return db.update_provider(provider_id, {
        "api_key": "",
        "refresh_token": "",
        "token_expires_at": 0,
        "auth_type": "api_key",
        "credential_source": "api_key",
    })


def auth_headers(api_key: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": api_key,
    }
