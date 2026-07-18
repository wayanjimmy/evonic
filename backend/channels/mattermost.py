"""Mattermost channel adapter."""

import json
import logging
import os
import re
import threading
import time
import base64
import secrets
from collections import deque
from typing import Any, Dict, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from backend.channels.base import BaseChannel, strip_system_tags

_logger = logging.getLogger(__name__)

_MAX_POST_CHARS = 15000


def _split_message(text: str, limit: int = _MAX_POST_CHARS):
    text = text or ''
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip('\n')
    if text:
        yield text


def session_key_for(channel_type: str, channel_id: str, post: Dict[str, Any]) -> Tuple[str, str]:
    """Return (external_session_key, root_post_id)."""
    if channel_type in ('D', 'G'):
        return f"mm:dm:{channel_id}", ''
    root_id = post.get('root_id') or post.get('id') or ''
    return f"mm:thread:{channel_id}:{root_id}", root_id


def strip_bot_mention(text: str, username: str) -> str:
    if not username:
        return text or ''
    return re.sub(rf'@{re.escape(username)}(?![A-Za-z0-9_-])', '', text or '').strip()


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', os.path.basename(name or 'file'))[:120]


def _human_size(size: int) -> str:
    size = int(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f"{size:.1f}{unit}" if unit != 'B' else f"{size}B"
        size /= 1024


class MattermostChannel(BaseChannel):
    def __init__(self, channel_id: str, agent_id: str, config: Dict[str, Any]):
        super().__init__(channel_id, agent_id, config)
        self.base_url = (config.get('base_url') or '').rstrip('/')
        self.bot_token = config.get('bot_token') or ''
        self.bot_user_id: Optional[str] = None
        self.bot_username: Optional[str] = None
        self._http = requests.Session()
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._reply_roots: Dict[str, Tuple[str, str]] = {}
        self._last_progress_patch: Dict[str, float] = {}
        self._progress_posts: Dict[str, str] = {}
        self._progress_handler = None
        self._approval_required_handler = None
        self._approval_resolved_handler = None
        self._pending_approval_posts: Dict[str, str] = {}
        self._processed_post_ids = deque(maxlen=2000)
        self._processed_post_id_set = set()
        self._processed_lock = threading.Lock()

    @staticmethod
    def get_channel_type() -> str:
        return 'mattermost'

    def get_system_instructions(self) -> Optional[str]:
        return (
            "You are responding via Mattermost. Mattermost supports Markdown, so you may "
            "use **bold**, *italic*, lists, links, `inline code`, and fenced code blocks."
        )

    def start(self):
        if not self.base_url:
            raise ValueError("Mattermost base_url is required.")
        if not self.bot_token:
            raise ValueError("Mattermost bot_token is required.")
        self._ensure_action_token()
        self._http.headers.update({'Authorization': f'Bearer {self.bot_token}'})
        me = self._request('GET', '/api/v4/users/me')
        self.bot_user_id = me.get('id')
        self.bot_username = me.get('username')
        if not self.bot_user_id:
            raise RuntimeError("Mattermost token verification did not return a user id.")
        self._stop.clear()
        self._thread = threading.Thread(target=self._websocket_loop, daemon=True)
        self._thread.start()
        self._register_event_listeners()
        self._running = True

    def _ensure_action_token(self):
        """Persist a random Mattermost action token for callback authenticity."""
        if self.config.get('action_token'):
            return
        token = secrets.token_urlsafe(32)
        self.config['action_token'] = token
        try:
            from models.db import db
            channel = db.get_channel(self.channel_id)
            config = channel.get('config') if channel else None
            if isinstance(config, dict):
                config['action_token'] = token
                db.update_channel(self.channel_id, {'config': config})
        except Exception:
            _logger.warning("Mattermost channel %s: failed to persist action token", self.channel_id, exc_info=True)

    def stop(self):
        self._running = False
        self._stop.set()
        self._unregister_event_listeners()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _request(self, method: str, path: str, **kwargs):
        url = self._resolve_request_url(path)
        timeout = kwargs.pop('timeout', 20)
        method_upper = method.upper()
        max_attempts = 3 if method_upper in {'GET', 'HEAD', 'OPTIONS'} else 1
        resp = None
        last_exc = None
        for attempt in range(max_attempts):
            try:
                resp = self._http.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == max_attempts - 1:
                    raise
                time.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code < 500:
                resp.raise_for_status()
                if not resp.content or not resp.content.strip():
                    return {}
                content_type = resp.headers.get('content-type', '')
                if 'json' not in content_type.lower():
                    raise ValueError(f"Mattermost API returned non-JSON response: {content_type or 'unknown content type'}")
                try:
                    return resp.json()
                except ValueError:
                    raise ValueError("Mattermost API returned invalid JSON")
            if attempt == max_attempts - 1:
                break
            try:
                resp.close()
            except Exception:
                pass
            time.sleep(0.5 * (2 ** attempt))
        if resp is not None:
            resp.raise_for_status()
        if last_exc is not None:
            raise last_exc
        return {}

    def _resolve_request_url(self, path: str) -> str:
        if not path.startswith(('http://', 'https://')):
            return self.base_url + path
        base = urlparse(self.base_url)
        target = urlparse(path)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise ValueError("Mattermost request URL must match configured base_url origin")
        return path

    def _websocket_loop(self):
        try:
            import websocket  # websocket-client
        except ImportError:
            _logger.error("Mattermost channel %s: websocket-client not installed", self.channel_id)
            self._running = False
            return
        backoff = 1
        ws_url = self.base_url.replace('https://', 'wss://').replace('http://', 'ws://') + '/api/v4/websocket'
        while not self._stop.is_set():
            try:
                self._ws = websocket.create_connection(
                    ws_url, timeout=30, header=[f'Authorization: Bearer {self.bot_token}']
                )
                backoff = 1
                while not self._stop.is_set():
                    raw = self._ws.recv()
                    if raw:
                        self._handle_ws_event(json.loads(raw))
            except Exception as e:
                if not self._stop.is_set():
                    _logger.warning("Mattermost websocket reconnecting after error: %s", e)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    def _handle_ws_event(self, event: Dict[str, Any]):
        if event.get('event') != 'posted':
            return
        data = event.get('data') or {}
        try:
            post = data.get('post')
            if isinstance(post, str):
                post = json.loads(post)
            if not isinstance(post, dict):
                return
            self._handle_post(post, data)
        except Exception as e:
            _logger.error("Mattermost channel %s: failed to handle event: %s", self.channel_id, e, exc_info=True)

    def _handle_post(self, post: Dict[str, Any], data: Dict[str, Any]):
        from models.db import db
        from backend.channels.pairing import extract_pair_code

        post_id = post.get('id') or ''
        if post_id and not self._mark_post_processing(post_id):
            return
        user_id = post.get('user_id') or ''
        if not user_id or user_id == self.bot_user_id:
            return
        mm_channel_id = post.get('channel_id') or data.get('channel_id') or ''
        channel_type = data.get('channel_type') or self._get_mm_channel_type(mm_channel_id)
        mentions = data.get('mentions') or []
        mentioned = bool(self.bot_user_id and self.bot_user_id in mentions)
        root_id = post.get('root_id') or post.get('id') or ''
        owned = bool(root_id and db.get_mattermost_thread(self.channel_id, mm_channel_id, root_id))
        is_dm = channel_type in ('D', 'G')
        if not is_dm and not mentioned and not owned:
            return

        text = strip_system_tags(strip_bot_mention(post.get('message') or '', self.bot_username or ''))
        user_name = (data.get('sender_name') or user_id).strip()

        if not db.is_user_allowed(self.channel_id, user_id):
            raw_code = extract_pair_code(text) if text else None
            if raw_code:
                claim = db.claim_and_approve_pending_for_user(raw_code, self.channel_id, user_id)
                if claim == 'approved':
                    self._post(mm_channel_id, "✅ You're now approved! Welcome aboard.", root_id=root_id if root_id else None)
                    return
                if claim == 'wrong_channel':
                    self._post(mm_channel_id, "❌ That pairing code is not valid for this channel.", root_id=root_id if root_id else None)
                    return
                if claim == 'wrong_user':
                    self._post(mm_channel_id, "❌ That pairing code is not valid for this account.", root_id=root_id if root_id else None)
                    return
            allowed, pair_code = self._check_allowlist(user_id, user_name)
            if not allowed and pair_code:
                self._post(mm_channel_id, "👋 You're not yet approved to chat here. Please ask the administrator for a pairing code, then send it here.", root_id=root_id if root_id else None)
            return

        if user_name:
            cur = db.get_user_display_name(self.channel_id, user_id)
            if cur in ('unknown', user_id):
                db.set_user_display_name(self.channel_id, user_id, user_name)

        external_key, thread_root = session_key_for(channel_type, mm_channel_id, post)
        self._reply_roots[external_key] = (mm_channel_id, thread_root)
        if not is_dm and thread_root:
            db.upsert_mattermost_thread(self.channel_id, self.agent_id, mm_channel_id, thread_root, user_id)

        session_id = db.get_or_create_session(self.agent_id, external_key, self.channel_id, 'mattermost')
        image_url, video_url, info_lines = self._ingest_attachments(
            post, self.agent_id, session_id, external_key, self.channel_id, db,
        )
        if info_lines:
            text = "\n".join(info_lines) + (f"\n{text}" if text else '')
        if not text and (image_url or video_url):
            text = '[Image]' if image_url else '[Video]'
        elif not text and (post.get('metadata') or {}).get('files'):
            text = '[Attachment]'
        if not db.is_session_bot_enabled(session_id, agent_id=self.agent_id):
            if text:
                db.add_chat_message(session_id, 'user', text, agent_id=self.agent_id)
            return
        if not text:
            return

        from backend.agent_runtime import agent_runtime
        result = agent_runtime.handle_message(
            self.agent_id, external_key, text, self.channel_id,
            image_url=image_url, video_url=video_url,
        )
        if result.get('buffered'):
            return
        response = result.get('response') or ''
        if response and response != '(No response)':
            self._do_send(external_key, response)

    def _mark_post_processing(self, post_id: str) -> bool:
        with self._processed_lock:
            if post_id in self._processed_post_id_set:
                return False
            if len(self._processed_post_ids) == self._processed_post_ids.maxlen:
                old = self._processed_post_ids[0]
                self._processed_post_id_set.discard(old)
            self._processed_post_ids.append(post_id)
            self._processed_post_id_set.add(post_id)
            return True

    def _ingest_attachments(self, post, agent_id, session_id, external_key,
                            channel_id, db):
        files = ((post.get('metadata') or {}).get('files') or [])
        if not files:
            return None, None, []
        cfg = db.get_agent_attachment_config(agent_id)
        agent = db.get_agent(agent_id)
        max_bytes = cfg['max_size_mb'] * 1024 * 1024
        image_url = None
        video_url = None
        info_lines = []
        for f in files:
            file_id = f.get('id')
            if not file_id:
                continue
            original_filename = f.get('name') or 'file'
            mime_type = (f.get('mime_type') or 'application/octet-stream').split(';')[0].strip()
            size_bytes = int(f.get('size') or 0)
            is_image = mime_type.startswith('image/')
            is_video = mime_type.startswith('video/')
            is_audio = mime_type.startswith('audio/')

            if size_bytes and size_bytes > max_bytes:
                _logger.info("Mattermost file %s skipped: too large", file_id)
                continue
            data = None
            needs_bytes = cfg['enabled'] or (is_image and agent and agent.get('vision_enabled')) or (is_video and agent and agent.get('video_enabled'))
            if not needs_bytes:
                continue
            try:
                data = self._download_file(file_id)
            except Exception as e:
                _logger.error("Mattermost channel %s: failed downloading file %s: %s", channel_id, file_id, e, exc_info=True)
                continue
            real_size = len(data)
            if real_size > max_bytes:
                continue

            if is_image and agent and agent.get('vision_enabled') and image_url is None:
                try:
                    image_url = self._to_jpeg_data_url(data)
                except Exception:
                    pass
            elif is_video and agent and agent.get('video_enabled') and video_url is None:
                b64 = base64.b64encode(data).decode('utf-8')
                video_url = f"data:{mime_type};base64,{b64}"

            if not cfg['enabled']:
                continue
            safe = _sanitize_filename(original_filename)
            target_dir = os.path.join('data', 'attachments', agent_id, session_id)
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, f"{int(time.time())}_{safe}")
            with open(target_path, 'wb') as out:
                out.write(data)
            file_type = 'photo' if is_image else 'audio' if is_audio else 'video' if is_video else 'document'
            attachment_id = db.save_attachment(
                agent_id=agent_id,
                session_id=session_id,
                filename=os.path.basename(target_path),
                file_path=target_path,
                external_user_id=external_key,
                channel_id=channel_id,
                channel_type='mattermost',
                original_filename=original_filename,
                mime_type=mime_type,
                file_type=file_type,
                size_bytes=real_size,
            )
            info_line = (
                f"[Attached: {original_filename} ({mime_type}, "
                f"{_human_size(real_size)}) id={attachment_id} path={target_path}]"
            )
            if is_audio and agent and agent.get('audio_enabled'):
                info_line += "\nUse the `transcribe_audio` tool to listen to this audio."
            info_lines.append(info_line)
        return image_url, video_url, info_lines

    def _download_file(self, file_id: str) -> bytes:
        resp = self._http.get(f"{self.base_url}/api/v4/files/{file_id}", timeout=30)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _to_jpeg_data_url(data: bytes) -> str:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:image/jpeg;base64,{b64}"

    def _get_mm_channel_type(self, mm_channel_id: str) -> str:
        try:
            return (self._request('GET', f'/api/v4/channels/{mm_channel_id}') or {}).get('type', '')
        except Exception:
            return ''

    def _post(self, mm_channel_id: str, message: str, root_id: Optional[str] = None,
              file_ids: Optional[Sequence[str]] = None):
        payload = {'channel_id': mm_channel_id, 'message': message or ''}
        if root_id:
            payload['root_id'] = root_id
        if file_ids is not None:
            if isinstance(file_ids, (str, bytes)):
                raise ValueError("file_ids must be a sequence of file id strings, not a string")
            payload['file_ids'] = [str(file_id) for file_id in file_ids]
        return self._request('POST', '/api/v4/posts', json=payload)

    def _do_send(self, external_user_id: str, text: str):
        target = self._reply_roots.get(external_user_id)
        if not target and external_user_id.startswith('mm:dm:'):
            target = (external_user_id.split(':', 2)[2], '')
        if not target and external_user_id.startswith('mm:thread:'):
            _, _, mm_channel_id, root_id = external_user_id.split(':', 3)
            target = (mm_channel_id, root_id)
        if not target:
            raise RuntimeError(f"Cannot resolve Mattermost target for {external_user_id}")
        mm_channel_id, root_id = target
        chunks = list(_split_message(text))
        progress_post_id = self._progress_posts.pop(external_user_id, None)
        if not progress_post_id and root_id:
            try:
                from models.db import db
                row = db.get_mattermost_thread(self.channel_id, mm_channel_id, root_id)
                progress_post_id = (row or {}).get('progress_post_id')
            except Exception:
                progress_post_id = None
        if progress_post_id:
            self._discard_progress_post(progress_post_id, mm_channel_id, root_id)
        for chunk in chunks:
            self._post(mm_channel_id, chunk, root_id=root_id or None)
        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'mattermost', 'channel_id': self.channel_id,
            'external_user_id': external_user_id, 'message': text,
        })

    def _discard_progress_post(self, progress_post_id: str, mm_channel_id: str,
                               root_id: str) -> None:
        """Remove progress placeholder before posting the final answer.

        Mattermost marks patched posts as Edited and keeps their original thread
        position. Reusing a progress post as the final answer can therefore make
        the next answer appear as an edit of an earlier bot message. Use a fresh
        final reply instead and delete the placeholder best-effort.
        """
        try:
            self._request('DELETE', f'/api/v4/posts/{progress_post_id}')
        except Exception:
            try:
                self._request('PUT', f'/api/v4/posts/{progress_post_id}/patch', json={'message': '✅ Done'})
            except Exception:
                pass
        if root_id:
            try:
                from models.db import db
                db.set_mattermost_thread_progress_post(self.channel_id, mm_channel_id, root_id, None)
            except Exception:
                pass

    def _do_send_file(self, external_user_id: str, file_path: str,
                      caption: Optional[str] = None, mime_type: Optional[str] = None) -> bool:
        if not os.path.isfile(file_path):
            return False
        target = self._reply_roots.get(external_user_id)
        if not target:
            return False
        mm_channel_id, root_id = target
        with open(file_path, 'rb') as fh:
            resp = self._request('POST', '/api/v4/files', data={'channel_id': mm_channel_id},
                                 files={'files': (os.path.basename(file_path), fh, mime_type or 'application/octet-stream')})
        file_infos = resp.get('file_infos') or []
        file_ids = [f['id'] for f in file_infos if f.get('id')]
        if not file_ids:
            return False
        self._post(mm_channel_id, caption or '', root_id=root_id or None, file_ids=file_ids)
        return True

    def patch_progress(self, external_user_id: str, message: str) -> None:
        """Best-effort progress patch hook for future runtime integration."""
        now = time.time()
        if now - self._last_progress_patch.get(external_user_id, 0) < 1.5:
            return
        self._last_progress_patch[external_user_id] = now
        target = self._reply_roots.get(external_user_id)
        if not target:
            return
        mm_channel_id, root_id = target
        post_id = self._progress_posts.get(external_user_id)
        if not post_id and root_id:
            try:
                from models.db import db
                row = db.get_mattermost_thread(self.channel_id, mm_channel_id, root_id)
                post_id = (row or {}).get('progress_post_id')
                if post_id:
                    self._progress_posts[external_user_id] = post_id
            except Exception:
                pass
        try:
            if not post_id:
                post = self._post(mm_channel_id, message, root_id=root_id or None)
                post_id = post.get('id')
                if post_id:
                    self._progress_posts[external_user_id] = post_id
                    if root_id:
                        try:
                            from models.db import db
                            db.set_mattermost_thread_progress_post(self.channel_id, mm_channel_id, root_id, post_id)
                        except Exception:
                            pass
            else:
                self._request('PUT', f'/api/v4/posts/{post_id}/patch', json={'message': message})
        except Exception:
            pass

    def _register_event_listeners(self):
        from backend.event_stream import event_stream

        labels = {
            'turn_begin': '⏳ Preparing context…',
            'llm_thinking': '• Calling model…',
            'tool_call_started': '• Running tool…',
            'tool_executed': '• Reviewing tool results…',
        }

        def _on_progress(data):
            if data.get('channel_id') != self.channel_id:
                return
            external_user_id = data.get('external_user_id')
            if not external_user_id:
                return
            event = data.get('_event') or ''
            text = labels.get(event)
            if event == 'tool_call_started':
                tool = str(data.get('tool_name') or '').split('.')[-1][:80]
                text = f"• Running tool: `{tool}`…" if tool else labels[event]
            if text:
                self.patch_progress(external_user_id, text)

        self._progress_handler = _on_progress
        for event_name in labels:
            event_stream.on(event_name, _on_progress)

        def _on_approval_required(data):
            if not self._is_super_agent_channel():
                return
            if data.get('channel_id') != self.channel_id:
                return
            external_user_id = data.get('external_user_id')
            approval_id = data.get('approval_id') or ''
            if not external_user_id or not approval_id:
                return
            target = self._reply_roots.get(external_user_id)
            if not target:
                return
            try:
                post = self._send_approval_prompt(target[0], target[1], approval_id, data)
                if post.get('id'):
                    self._pending_approval_posts[approval_id] = post['id']
            except Exception as e:
                _logger.error("Failed to send Mattermost approval prompt: %s", e, exc_info=True)

        def _on_approval_resolved(data):
            if not self._is_super_agent_channel():
                return
            if data.get('channel_id') != self.channel_id:
                return
            approval_id = data.get('approval_id') or ''
            post_id = self._pending_approval_posts.pop(approval_id, None)
            if not post_id:
                return
            timed_out = data.get('timed_out', False)
            decision = data.get('decision', 'reject')
            label = 'Timed out — auto-rejected.' if timed_out else 'Approved.' if decision == 'approve' else 'Rejected.'
            try:
                self._request('PUT', f'/api/v4/posts/{post_id}/patch', json={'message': label, 'props': {}})
            except Exception:
                pass

        self._approval_required_handler = _on_approval_required
        self._approval_resolved_handler = _on_approval_resolved
        event_stream.on('approval_required', _on_approval_required)
        event_stream.on('approval_resolved', _on_approval_resolved)

    def _unregister_event_listeners(self):
        if not self._progress_handler:
            return
        from backend.event_stream import event_stream
        for event_name in ('turn_begin', 'llm_thinking', 'tool_call_started', 'tool_executed'):
            event_stream.off(event_name, self._progress_handler)
        self._progress_handler = None
        if self._approval_required_handler:
            event_stream.off('approval_required', self._approval_required_handler)
            self._approval_required_handler = None
        if self._approval_resolved_handler:
            event_stream.off('approval_resolved', self._approval_resolved_handler)
            self._approval_resolved_handler = None

    def _send_approval_prompt(self, mm_channel_id: str, root_id: str,
                              approval_id: str, data: Dict[str, Any]):
        callback_url = (self.config.get('action_callback_url') or '').rstrip('/')
        if not callback_url:
            # Deployment has not exposed an HTTPS callback to Mattermost. The web UI
            # remains the safe fallback, per the plan.
            return self._post(
                mm_channel_id,
                self._format_approval_text(data) + "\n\nApproval buttons are unavailable; use the Evonic web UI to approve or reject.",
                root_id=root_id or None,
            )
        token = self.config.get('action_token') or self.channel_id
        context = {'channel_id': self.channel_id, 'approval_id': approval_id, 'token': token}
        payload = {
            'channel_id': mm_channel_id,
            'message': self._format_approval_text(data),
            'props': {
                'attachments': [{
                    'actions': [
                        {'name': 'Approve', 'integration': {'url': callback_url, 'context': {**context, 'decision': 'approve'}}},
                        {'name': 'Reject', 'integration': {'url': callback_url, 'context': {**context, 'decision': 'reject'}}},
                    ]
                }]
            }
        }
        if root_id:
            payload['root_id'] = root_id
        return self._request('POST', '/api/v4/posts', json=payload)

    @staticmethod
    def _format_approval_text(data: Dict[str, Any]) -> str:
        tool_name = data.get('tool_name', '')
        info = data.get('approval_info', {}) or {}
        reasons = data.get('reasons') or []
        risk = info.get('risk_level', 'medium')
        desc = info.get('description', 'This action requires careful consideration.')
        source_agent = data.get('source_agent_name')
        header = f"⚠️ Approval Required (agent: {source_agent})" if source_agent else "⚠️ Approval Required"
        tool_args = data.get('tool_args') or {}
        code_snippet = tool_args.get('script') or tool_args.get('code') or ''
        code_lang = 'bash' if 'script' in tool_args else 'python'
        if code_snippet and len(code_snippet) > 500:
            code_snippet = code_snippet[:500] + '\n... (truncated)'
        code_block = f"\n```{code_lang}\n{code_snippet}\n```" if code_snippet else ''
        return (
            f"{header}\n"
            f"Tool: {tool_name}\n"
            f"Risk: {risk}\n"
            f"{desc}\n"
            f"Reasons: {', '.join(reasons) if reasons else '-'}"
            f"{code_block}"
        )

    def handle_approval_action(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        context = payload.get('context') or {}
        if context.get('channel_id') != self.channel_id:
            return {'ephemeral_text': 'Invalid approval channel.'}, 403
        expected = self.config.get('action_token') or self.channel_id
        if context.get('token') != expected:
            return {'ephemeral_text': 'Invalid approval token.'}, 403
        approval_id = context.get('approval_id') or ''
        decision = context.get('decision') or ''
        if decision not in ('approve', 'reject') or not approval_id:
            return {'ephemeral_text': 'Invalid approval action.'}, 400
        from backend.agent_runtime.approval import approval_registry
        ok = approval_registry.resolve(approval_id, decision)
        if ok:
            label = 'Approved by user.' if decision == 'approve' else 'Rejected by user.'
            return {'update': {'message': label, 'props': {}}, 'ephemeral_text': label}, 200
        return {'ephemeral_text': 'This approval has already been resolved or expired.'}, 200
