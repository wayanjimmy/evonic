r"""Reusable, parameterized agent templates — the template layer.

A *template* is a JSON blueprint that captures the FULL agent spec: basic and
advanced settings, tools, skills, variables, knowledge-base files and a
parameterized system prompt.  Users pick a template in the UI (or a plugin
calls this module directly) to instantiate a real agent.

This module is deliberately free of Flask dependencies.  The dependency
direction is strictly one-way::

    backend.agent_templates  ->  backend.agent_factory  ->  db / filesystem

The template layer never re-implements spec normalisation: every default,
override and instantiation payload is validated and coerced by
:func:`backend.agent_factory.normalize_spec` / ``create_agent``, which is the
single source of truth for the mutable agent field surface.

Storage
-------

Templates are read from one root, relative to ``base_dir`` (``config.BASE_DIR``
by default): ``agent_templates/``, in two storage shapes:

``agent_templates/<id>.json``
    The canonical, writable location (single-file form).  ``create_template``,
    ``update_template`` and ``delete_template`` only ever touch this root.
``agent_templates/<id>/``
    The additive, opt-in **directory form**: ``meta.json`` (metadata plus the
    discovery marker), a prompt file (``system.md`` by default, overridable
    with ``prompt_file`` in ``meta.json``) and ``kb/<name>.md|.txt`` files.
    The loader re-inlines the prompt and the knowledge base into the exact
    same dict the single-file loader returns, so the HTTP wire format,
    rendering, the placeholder audit and instantiation are all shared.
    ``system_prompt`` and ``kb_files`` are forbidden inside a directory
    template's ``meta.json`` (exactly one source of truth) and knowledge-base
    content is copied **verbatim** (no placeholder rendering).  When both
    ``<id>.json`` and ``<id>/`` exist the id is a hard validation error on
    both entries and :func:`list_collisions` reports it, so precedence is
    never silent.


Path safety
-----------

Every template id is validated against :data:`SLUG_RE`, then the target path is
canonicalized with ``os.path.realpath`` and asserted to stay directly inside
the template root (this also rejects ``<id>.json`` symlinks that escape the
root).  Knowledge-base keys are validated by reusing
:func:`backend.agent_portability._safe_kb_path` — never re-implemented here.

Rendering rules (system prompt)
-------------------------------

* Placeholders are ``{{ name }}`` where *name* matches :data:`PARAM_NAME_RE`
  (ASCII only).  Whitespace around the name is allowed.
* The renderer is a **single pass**: the substituted output is never scanned
  again, so a parameter value that itself contains ``{{other}}`` stays
  literal (no recursive/self-injection).
* Unknown placeholders are a HARD ERROR listing the offending names — both at
  save time (:func:`validate_template`) and at render time
  (:func:`render_system_prompt`).
* A malformed ``{{`` (anything that does not match the placeholder pattern) is
  also a hard error; a literal ``{{`` must be escaped as ``\{{``.
* Resolution order for a parameter is *caller value > declared default >* ``''``.
  ``required: true`` means the **rendered value must be non-empty**.
* Parameter values are length-capped (:data:`MAX_PARAM_VALUE_LENGTH`) and the
  rendered prompt is length-capped (:data:`agent_factory.MAX_SYSTEM_PROMPT_LENGTH`,
  enforced AFTER rendering).

Secrets
-------

Templates store variable **declarations only** (``key``, ``is_secret``,
``required``, ``description``); a secret declaration may not carry a ``default``
or ``value``.  Actual secret values are supplied at instantiate time through
``overrides['variables']`` and flow straight into the agent's variable store
via ``agent_factory``.  Values are never written back to the template file and
never logged — error messages reference keys/names only.

Public API
----------

``list_templates(*, base_dir=None) -> list[dict]``
``has_template(template_id, *, base_dir=None) -> bool``
``get_template(template_id, *, base_dir=None) -> dict``
``list_collisions(*, base_dir=None) -> list[dict]``
``create_template(data, *, base_dir=None, shape=None) -> dict``
``update_template(template_id, data, *, base_dir=None) -> dict``
``delete_template(template_id, *, base_dir=None) -> bool``
``validate_template(payload) -> dict``
``resolve_parameters(template, params=None, *, strict=True) -> dict``
``render_system_prompt(template, params=None, *, strict=True) -> str``
``preview_template(template_id, params=None, *, base_dir=None, strict=False) -> dict``
``resolve_template(template_id, *, base_dir=None) -> dict``
``create_agent_from_template(template_id, params=None, overrides=None, *, ...) -> str``

``get_template`` returns the canonical template with a read-only ``_meta``
block (``source``, ``legacy``, ``writable``, ``file``).  ``_meta`` is stripped
on write, so a fetch → edit → ``update_template`` round-trip works unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend import agent_factory
from backend.agent_factory import (
    MAX_KB_PATH_LENGTH,
    MAX_SYSTEM_PROMPT_LENGTH,
    SPEC_FIELD_ALLOWLIST,
    AgentFactoryError,
    SpecValidationError,
)
from backend.agent_portability import (
    CONFIG_KEYS,
    MAX_KB_FILE_LENGTH,
    VARIABLE_KEY_RE,
    AgentPortabilityError,
    _safe_kb_path,
)
from models.boolean import normalize_bool

__all__ = [
    # Constants
    "TEMPLATE_SCHEMA_VERSION",
    "SLUG_RE",
    "PARAM_NAME_RE",
    "PARAM_TYPES",
    "MAX_PARAM_VALUE_LENGTH",
    "MAX_PARAM_COUNT",
    "MAX_TEMPLATE_ITEMS",
    "MAX_TEMPLATE_BYTES",
    "MAX_TEMPLATE_DIR_BYTES",
    "SHAPE_FILE",
    "SHAPE_DIR",
    "DIR_TEMPLATE_META",
    "DIR_TEMPLATE_PROMPT",
    "DIR_TEMPLATE_KB",
    "OVERRIDE_KEYS",
    # Errors
    "TemplateError",
    "TemplateValidationError",
    "TemplateRenderError",
    "TemplateNotFoundError",
    "TemplateExistsError",
    "TemplateResolveError",
    # Storage helpers
    "templates_dir",
    # Read API
    "list_templates",
    "has_template",
    "get_template",
    "list_collisions",
    # Write API
    "create_template",
    "update_template",
    "delete_template",
    # Validation / rendering
    "validate_template",
    "resolve_parameters",
    "render_system_prompt",
    "preview_template",
    # Resolution / instantiation
    "resolve_template",
    "create_agent_from_template",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version of the canonical template format.
TEMPLATE_SCHEMA_VERSION = 1

#: Template ids are lowercase slugs; they are also the file stem, so they must
#: never contain a path separator, a leading dot or a traversal segment.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Parameter names MUST match the placeholder charset (ASCII only).
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: One-pass placeholder matcher.  ``\w`` is deliberately avoided: it would
#: accept non-ASCII word characters that :data:`PARAM_NAME_RE` rejects.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: Escape sequence for a literal ``{{``.
ESCAPE_SEQUENCE = "\\{{"

PARAM_TYPES = frozenset({"text", "number", "boolean", "select"})

MAX_PARAM_VALUE_LENGTH = 4000
MAX_PARAM_COUNT = 64
MAX_PARAMETER_OPTIONS = 200
MAX_PARAM_NAME_LENGTH = 64
MAX_LABEL_LENGTH = 200
MAX_PLACEHOLDER_LENGTH = 200
MAX_TEMPLATE_DESCRIPTION_LENGTH = 2000
MAX_CATEGORY_LENGTH = 64
MAX_ICON_LENGTH = 64
MAX_TOOL_ID_LENGTH = 200

#: Upper bound for the number of entries in tools/skills/variables/kb_files.
MAX_TEMPLATE_ITEMS = 256

#: Upper bound for the serialized template file.
MAX_TEMPLATE_BYTES = 4 * 1024 * 1024

#: Upper bound for the *total* bytes of every file inside a directory-form
#: template (prompt + ``meta.json`` + every ``kb/**`` file).  Guards against a
#: template tree with thousands of small files or a single huge blob that would
#: otherwise only be bounded by the per-file caps.
MAX_TEMPLATE_DIR_BYTES = 4 * 1024 * 1024

# --- Directory-form template layout -----------------------------------------
#
# Additive, opt-in alternative to the single-file ``<id>.json`` form.  The
# prompt and knowledge base live as real files so diffs stay readable:
#
#     agent_templates/<id>/
#         meta.json            discovery marker + metadata (no prompt/kb inline)
#         system.md            the system prompt, real markdown
#         kb/<name>.md         one file per knowledge-base entry
#
# ``meta.json`` is the discovery marker: a directory without it is reported as
# an invalid entry (never a hard failure).  ``system_prompt`` and ``kb_files``
# are forbidden inside ``meta.json`` so there is exactly one source of truth.

#: Discovery marker file name inside a directory-form template.
DIR_TEMPLATE_META = "meta.json"

#: Default prompt file name inside a directory-form template.
DIR_TEMPLATE_PROMPT = "system.md"

#: Knowledge-base sub-directory inside a directory-form template.
DIR_TEMPLATE_KB = "kb"

#: Keys that a directory-form ``meta.json`` must NOT carry inline.
_DIR_META_FORBIDDEN_KEYS = frozenset({"system_prompt", "kb_files"})

#: Extra keys accepted inside a directory-form ``meta.json`` (beyond
#: :data:`_TEMPLATE_KEYS` minus the forbidden inline content keys).
_DIR_META_EXTRA_KEYS = frozenset({"prompt_file"})

#: File extensions allowed for knowledge-base files in a directory template.
_DIR_KB_EXTENSIONS = frozenset({".md", ".txt"})

#: Marker for the collision report produced when ``<id>.json`` and ``<id>/``
#: both exist (never silent precedence).
SHAPE_FILE = "file"
SHAPE_DIR = "dir"

#: Keys accepted at the top level of a canonical template.
_TEMPLATE_KEYS = frozenset({
    "id",
    "name",
    "description",
    "category",
    "icon",
    "schema_version",
    "parameters",
    "system_prompt",
    "defaults",
    "tools",
    "skills",
    "variables",
    "kb_files",
})

#: Keys accepted inside a parameter definition.
_PARAM_KEYS = frozenset({
    "name",
    "label",
    "type",
    "required",
    "default",
    "options",
    "description",
    "placeholder",
    "min",
    "max",
    "integer",
})

#: Keys accepted inside a variable declaration.
_VARIABLE_DECLARATION_KEYS = frozenset({
    "key",
    "is_secret",
    "required",
    "description",
    "default",
})

#: Metadata keys added by the loader and stripped on write.
_META_KEY = "_meta"

#: Structural keys a caller may override at instantiate time, on top of the
#: allowlisted agent columns (:data:`agent_factory.SPEC_FIELD_ALLOWLIST`).
_STRUCTURAL_OVERRIDE_KEYS = frozenset({
    "id",
    "tools",
    "skills",
    "variables",
    "knowledge_base",
})

#: Every key accepted in ``create_agent_from_template(overrides=...)``.
OVERRIDE_KEYS = frozenset(SPEC_FIELD_ALLOWLIST | _STRUCTURAL_OVERRIDE_KEYS)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TemplateError(ValueError):
    """Base class for every template-layer failure."""


class TemplateValidationError(TemplateError):
    """A template, parameter or override payload is invalid."""


class TemplateRenderError(TemplateValidationError):
    """The system prompt cannot be rendered (unknown/malformed placeholders)."""


class TemplateNotFoundError(TemplateError):
    """No template with the requested id exists in the template root."""


class TemplateExistsError(TemplateError):
    """A template with that id already exists (in either storage shape)."""


class TemplateResolveError(TemplateError):
    """A template declares a tool/skill that cannot be resolved right now.

    The full report produced by :func:`resolve_template` is attached as
    :attr:`report` so an editor can show precisely what is missing.
    """

    def __init__(self, message: str, report: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.report: Dict[str, Any] = report or {}


# ---------------------------------------------------------------------------
# Lazy global accessors (keep Flask out of the import graph)
# ---------------------------------------------------------------------------

def _default_base_dir() -> str:
    from config import BASE_DIR
    return BASE_DIR


def templates_dir(base_dir: Optional[str] = None) -> str:
    """Return the canonical (writable) template root."""
    root = base_dir if base_dir is not None else _default_base_dir()
    return os.path.join(root, "agent_templates")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TemplateValidationError(message)


def _escape_hint() -> str:
    """Human-readable reminder of the literal-brace escape sequence."""
    return "escaped as '\\{{'"


def _snippet(source: str, offset: int, length: int = 40) -> str:
    """Return a short, single-line excerpt of *source* for error messages."""
    raw = source[offset:offset + length]
    return raw.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _format_value(value: Any) -> str:
    """Render one resolved parameter value for prompt substitution."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_list(value: Any, field: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TemplateValidationError(
            "Template field '%s' must be a list." % field
        )
    if len(value) > MAX_TEMPLATE_ITEMS:
        raise TemplateValidationError(
            "Template field '%s' has too many entries (max %d)."
            % (field, MAX_TEMPLATE_ITEMS)
        )
    return list(value)


def _string_list(value: Any, field: str, *, max_length: int = MAX_TOOL_ID_LENGTH) -> List[str]:
    """Validate a list of unique, non-empty id strings (order preserved)."""
    items = _as_list(value, field)
    out: List[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise TemplateValidationError(
                "Template field '%s' must contain non-empty strings." % field
            )
        cleaned = item.strip()
        if len(cleaned) > max_length:
            raise TemplateValidationError(
                "Template field '%s' contains an entry that is too long (max %d characters)."
                % (field, max_length)
            )
        if cleaned not in out:
            out.append(cleaned)
    return out


def _optional_string(
    value: Any,
    field: str,
    *,
    default: str = "",
    max_length: int,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TemplateValidationError("Template field '%s' must be a string." % field)
    if len(value) > max_length:
        raise TemplateValidationError(
            "Template field '%s' is too long (max %d characters)." % (field, max_length)
        )
    return value


def _validate_template_id(template_id: Any) -> str:
    """Validate a template id used to build a filesystem path."""
    if not isinstance(template_id, str) or not template_id.strip():
        raise TemplateValidationError("Template id must be a non-empty string.")
    cleaned = template_id.strip()
    if not SLUG_RE.fullmatch(cleaned):
        raise TemplateValidationError(
            "Template id '%s' is invalid: use lowercase letters, digits, '_' or '-' "
            "(1-64 characters, starting with a letter or digit)." % cleaned[:80]
        )
    # Defence in depth: the regex already rejects these, but the id decides a
    # filesystem path, so never trust a single layer.
    if cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned or os.sep in cleaned:
        raise TemplateValidationError("Template id must not contain a path separator.")
    return cleaned


def _template_path(root: str, template_id: str) -> str:
    """Return the canonical file path for *template_id*, or raise.

    The resolved parent directory must be exactly the template root, which
    rejects ``..`` traversal and any ``<id>.json`` symlink that escapes it.
    """
    cleaned = _validate_template_id(template_id)
    root_real = os.path.realpath(root)
    path = os.path.join(root_real, cleaned + ".json")
    if os.path.islink(path):
        raise TemplateValidationError(
            "Template file '%s.json' must not be a symbolic link." % cleaned
        )
    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != root_real:
        raise TemplateValidationError(
            "Template id '%s' resolves outside the template directory." % cleaned
        )
    return path


def _template_dir_path(root: str, template_id: str) -> str:
    """Return the canonical directory path for *template_id*, or raise.

    Sibling of :func:`_template_path` with the same guards: the id is validated
    against :data:`SLUG_RE`, the directory itself must not be a symlink and its
    resolved parent must stay directly inside *root*.
    """
    cleaned = _validate_template_id(template_id)
    root_real = os.path.realpath(root)
    path = os.path.join(root_real, cleaned)
    if os.path.islink(path):
        raise TemplateValidationError(
            "Template directory '%s/' must not be a symbolic link." % cleaned
        )
    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != root_real:
        raise TemplateValidationError(
            "Template id '%s' resolves outside the template directory." % cleaned
        )
    return path


def _safe_relative_file(path: Any, *, field: str) -> str:
    """Validate a relative file path inside a template directory.

    Reuses :func:`backend.agent_portability._safe_kb_path` (never re-implemented)
    so a ``prompt_file`` or a ``kb/**`` path cannot be absolute, use backslashes
    or escape its base directory.
    """
    try:
        return _safe_kb_path(path)
    except AgentPortabilityError as exc:
        raise TemplateValidationError("%s is invalid: %s" % (field, exc)) from exc


def _atomic_write_template(path: str, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically: temp file in the same dir, fsync, then replace."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise TemplateValidationError(
            "Template payload is not JSON serializable: %s." % exc.__class__.__name__
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateValidationError(
            "Template file is too large (max %d bytes)." % MAX_TEMPLATE_BYTES
        )

    handle = None
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".tmp-%s." % os.path.basename(path),
            suffix=".tmp",
            dir=directory,
        )
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - defensive
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:  # pragma: no cover - defensive
                pass


def _atomic_write_text(path: str, text: str) -> None:
    """Write *text* atomically: temp file in the same dir, fsync, then replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    data = text.encode("utf-8")
    descriptor: Optional[int] = None
    temp_path: Optional[str] = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".tmp-", suffix=".tmp", dir=directory
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:  # pragma: no cover - defensive
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:  # pragma: no cover - defensive
                pass


# ---------------------------------------------------------------------------
# Directory-form templates (additive: meta.json + prompt file + kb/**)
# ---------------------------------------------------------------------------

def _read_text_file(path: str, *, field: str, max_bytes: int) -> str:
    """Read a UTF-8 text file with a size guard, mapping errors to template ones."""
    try:
        if os.path.getsize(path) > max_bytes:
            raise TemplateValidationError(
                "%s is too large (max %d bytes)." % (field, max_bytes)
            )
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise TemplateValidationError("%s is not valid UTF-8 text." % field) from exc
    except OSError as exc:
        raise TemplateValidationError(
            "%s could not be read (%s)." % (field, exc.__class__.__name__)
        ) from exc


def _check_dir_kb_extension(relative: str) -> None:
    """Refuse a knowledge-base file whose extension is not in the allowlist."""
    extension = os.path.splitext(relative)[1].lower()
    if extension not in _DIR_KB_EXTENSIONS:
        raise TemplateValidationError(
            "Knowledge-base file '%s' in a directory template must end in %s."
            % (relative, " or ".join(sorted(_DIR_KB_EXTENSIONS)))
        )


def _read_dir_kb(kb_root: str, template_id: str) -> Tuple[Dict[str, str], int]:
    """Walk ``kb/**`` into ``{relative_path: content}`` plus a running byte total.

    Content is copied **verbatim** (no placeholder rendering, no placeholder
    audit).  Dotfiles/dot-directories are skipped, symlinks are refused, only
    ``.md`` / ``.txt`` files are accepted and the walk order is deterministic
    (sorted).
    """
    files: Dict[str, str] = {}
    total = 0
    if os.path.islink(kb_root):
        raise TemplateValidationError(
            "Template knowledge-base directory 'kb' must not be a symbolic link."
        )
    if not os.path.isdir(kb_root):
        return files, total
    for dirpath, dirnames, names in os.walk(kb_root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                raise TemplateValidationError(
                    "Knowledge-base directories in a directory template must not be "
                    "symbolic links."
                )
        for name in sorted(names):
            if name.startswith("."):
                continue
            absolute = os.path.join(dirpath, name)
            relative = os.path.relpath(absolute, kb_root).replace(os.sep, "/")
            if os.path.islink(absolute):
                raise TemplateValidationError(
                    "Knowledge-base file '%s' must not be a symbolic link." % relative
                )
            safe_path = _safe_relative_file(relative, field="Knowledge-base path")
            _check_dir_kb_extension(safe_path)
            try:
                if os.path.getsize(absolute) > MAX_KB_FILE_LENGTH:
                    raise TemplateValidationError(
                        "Knowledge-base file '%s' is too large (max %d bytes)."
                        % (safe_path, MAX_KB_FILE_LENGTH)
                    )
                with open(absolute, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except UnicodeDecodeError as exc:
                raise TemplateValidationError(
                    "Knowledge-base file '%s' is not valid UTF-8 text." % safe_path
                ) from exc
            except OSError as exc:
                raise TemplateValidationError(
                    "Knowledge-base file '%s' could not be read (%s)."
                    % (safe_path, exc.__class__.__name__)
                ) from exc
            if len(files) >= MAX_TEMPLATE_ITEMS:
                raise TemplateValidationError(
                    "Template declares too many knowledge-base files (max %d)."
                    % MAX_TEMPLATE_ITEMS
                )
            total += len(content.encode("utf-8"))
            if total > MAX_TEMPLATE_DIR_BYTES:
                raise TemplateValidationError(
                    "Directory template '%s' exceeds the total size budget (%d bytes)."
                    % (template_id, MAX_TEMPLATE_DIR_BYTES)
                )
            files[safe_path] = content
    return files, total


def _read_dir_template(dir_path: str, template_id: str) -> Dict[str, Any]:
    """Load a directory-form template into the JSON loader's dict shape.

    ``meta.json`` is the discovery marker; the system prompt is read from
    ``prompt_file`` (default :data:`DIR_TEMPLATE_PROMPT`) and ``kb/**`` is walked
    into ``kb_files``.  ``system_prompt`` / ``kb_files`` are forbidden inside
    ``meta.json`` so there is exactly one source of truth.  The returned mapping
    feeds :func:`validate_template` unchanged, so rendering, the placeholder
    audit, resolution and instantiation are all shared with the single-file form.
    """
    if os.path.islink(dir_path):
        raise TemplateValidationError(
            "Template directory '%s/' must not be a symbolic link." % template_id
        )
    dir_real = os.path.realpath(dir_path)
    meta_path = os.path.join(dir_path, DIR_TEMPLATE_META)
    meta = _read_json_object(meta_path)

    forbidden = sorted(_DIR_META_FORBIDDEN_KEYS & set(meta))
    if forbidden:
        raise TemplateValidationError(
            "Directory template '%s' must not declare %s in %s: the prompt and the "
            "knowledge base live in the template directory itself."
            % (template_id, ", ".join(forbidden), DIR_TEMPLATE_META)
        )

    unknown = sorted(set(meta) - _TEMPLATE_KEYS - _DIR_META_EXTRA_KEYS)
    if unknown:
        raise TemplateValidationError(
            "Directory template '%s' %s contains unsupported key(s): %s."
            % (template_id, DIR_TEMPLATE_META, ", ".join(unknown))
        )

    declared_id = meta.get("id")
    if declared_id is not None and declared_id != template_id:
        raise TemplateValidationError(
            "Directory template '%s' declares id '%s' which does not match its "
            "directory name." % (template_id, declared_id)
        )

    prompt_file = meta.pop("prompt_file", None) or DIR_TEMPLATE_PROMPT
    prompt_file = _safe_relative_file(prompt_file, field="Template prompt_file")
    prompt_path = os.path.join(dir_path, *prompt_file.split("/"))
    if os.path.islink(prompt_path):
        raise TemplateValidationError(
            "Template prompt file '%s' must not be a symbolic link." % prompt_file
        )
    if not os.path.isfile(prompt_path):
        raise TemplateNotFoundError(
            "Template prompt file '%s' does not exist in '%s/'."
            % (prompt_file, template_id)
        )
    resolved_prompt = os.path.realpath(prompt_path)
    if resolved_prompt != dir_real and not resolved_prompt.startswith(dir_real + os.sep):
        raise TemplateValidationError(
            "Template prompt file '%s' resolves outside the template directory."
            % prompt_file
        )
    system_prompt = _read_text_file(
        prompt_path,
        field="Template prompt file '%s'" % prompt_file,
        max_bytes=MAX_TEMPLATE_BYTES,
    )

    kb_files, kb_bytes = _read_dir_kb(
        os.path.join(dir_path, DIR_TEMPLATE_KB), template_id
    )
    total = kb_bytes + len(system_prompt.encode("utf-8"))
    try:
        total += os.path.getsize(meta_path)
    except OSError:  # pragma: no cover - defensive
        pass
    if total > MAX_TEMPLATE_DIR_BYTES:
        raise TemplateValidationError(
            "Directory template '%s' is too large (%d bytes; max %d)."
            % (template_id, total, MAX_TEMPLATE_DIR_BYTES)
        )

    data: Dict[str, Any] = dict(meta)
    data["system_prompt"] = system_prompt
    data["kb_files"] = kb_files
    return data


def _dir_meta_payload(template: Mapping[str, Any], *, prompt_file: str) -> Dict[str, Any]:
    """Split a canonical template into its ``meta.json`` payload.

    The inline ``system_prompt`` / ``kb_files`` are dropped (they become real
    files) and ``prompt_file`` is only recorded when it differs from the default.
    """
    meta = {
        key: value
        for key, value in template.items()
        if key != _META_KEY and key not in _DIR_META_FORBIDDEN_KEYS
    }
    if prompt_file and prompt_file != DIR_TEMPLATE_PROMPT:
        meta["prompt_file"] = prompt_file
    return meta


def _populate_dir_template(
    base: str,
    *,
    prompt_file: str,
    prompt_text: str,
    kb_files: Mapping[str, str],
    meta_serialized: str,
) -> None:
    """Write the prompt and every ``kb/**`` file first, ``meta.json`` LAST.

    ``meta.json`` is the discovery marker, so writing it last means a crash can
    never leave a *discoverable* half-built template behind.  Each file is
    replaced atomically.
    """
    _atomic_write_text(os.path.join(base, *prompt_file.split("/")), prompt_text)
    for relative, content in sorted(kb_files.items()):
        target = os.path.join(base, DIR_TEMPLATE_KB, *relative.split("/"))
        _atomic_write_text(target, content)
    _atomic_write_text(os.path.join(base, DIR_TEMPLATE_META), meta_serialized)


def _prune_dir_kb(base: str, keep: List[str]) -> None:
    """Delete ``kb/**`` files the payload dropped and any directories left empty.

    Only called when the update payload actually carried ``kb_files``, so a
    partial edit never removes a knowledge-base file by omission.
    """
    kb_root = os.path.join(base, DIR_TEMPLATE_KB)
    if os.path.islink(kb_root) or not os.path.isdir(kb_root):
        return
    keep_set = set(keep)
    for dirpath, dirnames, names in os.walk(kb_root, topdown=False):
        for name in names:
            if name.startswith("."):
                continue
            absolute = os.path.join(dirpath, name)
            relative = os.path.relpath(absolute, kb_root).replace(os.sep, "/")
            if relative in keep_set:
                continue
            try:
                os.unlink(absolute)
            except FileNotFoundError:
                pass
        for name in sorted(dirnames, reverse=True):
            try:
                os.rmdir(os.path.join(dirpath, name))
            except OSError:
                pass


def _write_dir_template(
    root: str,
    template_id: str,
    template: Mapping[str, Any],
    *,
    prompt_file: str = DIR_TEMPLATE_PROMPT,
) -> str:
    """Persist a canonical template as a directory-form template.

    A brand-new template is built inside a hidden staging directory and renamed
    into place (atomic); an existing template is updated file by file with
    ``meta.json`` written last.  All content is serialized before anything
    touches the disk, so a rejected payload leaves the directory byte-identical.
    """
    dir_path = _template_dir_path(root, template_id)
    prompt_file = _safe_relative_file(prompt_file, field="Template prompt_file")
    prompt_text = template.get("system_prompt")
    if not isinstance(prompt_text, str):
        raise TemplateValidationError(
            "Template field 'system_prompt' is required and must be a string."
        )
    kb_files = _normalize_kb_files(template.get("kb_files"))
    for _relative in kb_files:
        _check_dir_kb_extension(_relative)
    meta = _dir_meta_payload(template, prompt_file=prompt_file)
    try:
        meta_serialized = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise TemplateValidationError(
            "Template metadata is not JSON serializable: %s." % exc.__class__.__name__
        ) from exc

    total = len(prompt_text.encode("utf-8")) + len(meta_serialized.encode("utf-8"))
    total += sum(len(content.encode("utf-8")) for content in kb_files.values())
    if total > MAX_TEMPLATE_DIR_BYTES:
        raise TemplateValidationError(
            "Directory template '%s' is too large (%d bytes; max %d)."
            % (template_id, total, MAX_TEMPLATE_DIR_BYTES)
        )

    os.makedirs(root, exist_ok=True)
    if os.path.lexists(dir_path) and not os.path.isdir(dir_path):
        raise TemplateExistsError(
            "A file named '%s' already exists where the template directory would go."
            % template_id
        )

    if os.path.isdir(dir_path):
        _populate_dir_template(
            dir_path,
            prompt_file=prompt_file,
            prompt_text=prompt_text,
            kb_files=kb_files,
            meta_serialized=meta_serialized,
        )
    else:
        staging = tempfile.mkdtemp(prefix=".%s.stage-" % template_id, dir=root)
        try:
            _populate_dir_template(
                staging,
                prompt_file=prompt_file,
                prompt_text=prompt_text,
                kb_files=kb_files,
                meta_serialized=meta_serialized,
            )
            os.replace(staging, dir_path)
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    return dir_path


def _read_json_object(path: str) -> Dict[str, Any]:
    """Read a template file as a JSON object (size- and symlink-guarded)."""
    if os.path.islink(path):
        raise TemplateValidationError("Template files must not be symbolic links.")
    try:
        if os.path.getsize(path) > MAX_TEMPLATE_BYTES:
            raise TemplateValidationError(
                "Template file is too large (max %d bytes)." % MAX_TEMPLATE_BYTES
            )
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise TemplateNotFoundError(
            "Template file '%s' does not exist." % os.path.basename(path)
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TemplateValidationError(
            "Template file '%s' is not valid UTF-8 JSON." % os.path.basename(path)
        ) from exc
    except OSError as exc:
        raise TemplateValidationError(
            "Template file '%s' could not be read (%s)."
            % (os.path.basename(path), exc.__class__.__name__)
        ) from exc
    if not isinstance(payload, Mapping):
        raise TemplateValidationError(
            "Template file '%s' must contain a JSON object." % os.path.basename(path)
        )
    return dict(payload)


def _strip_meta(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if key != _META_KEY}


# ---------------------------------------------------------------------------
# Parameter validation / coercion
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_text(param_name: str, value: Any, *, cap: int = MAX_PARAM_VALUE_LENGTH) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = _format_value(value)
    elif isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        raise TemplateValidationError(
            "Parameter '%s' must be a text value." % param_name
        )
    if len(text) > cap:
        raise TemplateValidationError(
            "Parameter '%s' exceeds the maximum length of %d characters."
            % (param_name, cap)
        )
    return text


def _coerce_boolean(param_name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        raise TemplateValidationError(
            "Parameter '%s' must be a boolean (true/false)." % param_name
        )
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise TemplateValidationError(
        "Parameter '%s' must be a boolean (true/false)." % param_name
    )


def _coerce_number(param: Mapping[str, Any], value: Any) -> Any:
    name = param.get("name")
    if isinstance(value, bool) or value is None:
        raise TemplateValidationError(
            "Parameter '%s' must be a number." % name
        )
    if isinstance(value, (int, float)):
        number: Any = value
    elif isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"[+-]?\d+", stripped):
            number = int(stripped)
        else:
            try:
                number = float(stripped)
            except ValueError as exc:
                raise TemplateValidationError(
                    "Parameter '%s' must be a number." % name
                ) from exc
    else:
        raise TemplateValidationError("Parameter '%s' must be a number." % name)
    if isinstance(number, float) and (number != number or number in (float("inf"), float("-inf"))):
        raise TemplateValidationError("Parameter '%s' must be a finite number." % name)
    if param.get("integer"):
        if isinstance(number, float):
            if not number.is_integer():
                raise TemplateValidationError(
                    "Parameter '%s' must be a whole number." % name
                )
            number = int(number)
        elif not isinstance(number, int):
            raise TemplateValidationError(
                "Parameter '%s' must be a whole number." % name
            )
    minimum = param.get("min")
    maximum = param.get("max")
    if minimum is not None and number < minimum:
        raise TemplateValidationError(
            "Parameter '%s' must be greater than or equal to %s." % (name, minimum)
        )
    if maximum is not None and number > maximum:
        raise TemplateValidationError(
            "Parameter '%s' must be less than or equal to %s." % (name, maximum)
        )
    return number


def _coerce_param_value(param: Mapping[str, Any], value: Any) -> Any:
    """Type-check and coerce one parameter value.

    Error messages reference the parameter *name* and the constraint only —
    never the offending value (it may be sensitive).
    """
    name = param.get("name")
    param_type = param.get("type") or "text"
    if param_type == "text":
        return _coerce_text(name, value)
    if param_type == "select":
        options = param.get("options") or []
        if not isinstance(value, str):
            raise TemplateValidationError(
                "Parameter '%s' must be one of the declared options." % name
            )
        stripped = value.strip()
        if stripped not in options:
            raise TemplateValidationError(
                "Parameter '%s' must be one of: %s." % (name, ", ".join(options))
            )
        if len(stripped) > MAX_PARAM_VALUE_LENGTH:
            raise TemplateValidationError(
                "Parameter '%s' exceeds the maximum length of %d characters."
                % (name, MAX_PARAM_VALUE_LENGTH)
            )
        return stripped
    if param_type == "boolean":
        return _coerce_boolean(name, value)
    if param_type == "number":
        return _coerce_number(param, value)
    raise TemplateValidationError(  # pragma: no cover - guarded at save time
        "Parameter '%s' has an unsupported type." % name
    )


def _normalize_parameters(raw: Any) -> List[Dict[str, Any]]:
    items = _as_list(raw, "parameters")
    if len(items) > MAX_PARAM_COUNT:
        raise TemplateValidationError(
            "Template declares too many parameters (max %d)." % MAX_PARAM_COUNT
        )
    parameters: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise TemplateValidationError("Each template parameter must be an object.")
        unknown = sorted(set(item) - _PARAM_KEYS)
        if unknown:
            raise TemplateValidationError(
                "Template parameter contains unsupported key(s): %s." % ", ".join(unknown)
            )
        name = item.get("name")
        if not isinstance(name, str) or not PARAM_NAME_RE.fullmatch(name):
            raise TemplateValidationError(
                "Parameter names must start with a letter or underscore and contain only "
                "letters, digits and underscores."
            )
        if len(name) > MAX_PARAM_NAME_LENGTH:
            raise TemplateValidationError(
                "Parameter name '%s' is too long (max %d characters)."
                % (name[:MAX_PARAM_NAME_LENGTH], MAX_PARAM_NAME_LENGTH)
            )
        if name in seen:
            raise TemplateValidationError("Duplicate parameter name '%s'." % name)
        seen.add(name)

        param_type = item.get("type", "text")
        if not isinstance(param_type, str) or param_type.strip().lower() not in PARAM_TYPES:
            raise TemplateValidationError(
                "Parameter '%s' has an unsupported type; expected one of: %s."
                % (name, ", ".join(sorted(PARAM_TYPES)))
            )
        param_type = param_type.strip().lower()

        param: Dict[str, Any] = {
            "name": name,
            "label": _optional_string(
                item.get("label"), "parameters.label", default=name, max_length=MAX_LABEL_LENGTH
            ) or name,
            "type": param_type,
            "required": normalize_bool(item.get("required"), default=False),
            "default": None,
            "options": [],
            "description": _optional_string(
                item.get("description"), "parameters.description",
                default="", max_length=MAX_TEMPLATE_DESCRIPTION_LENGTH,
            ),
            "placeholder": _optional_string(
                item.get("placeholder"), "parameters.placeholder",
                default="", max_length=MAX_PLACEHOLDER_LENGTH,
            ),
            "min": None,
            "max": None,
            "integer": False,
        }

        if param_type == "select":
            options = _as_list(item.get("options"), "parameters.options")
            if not options:
                raise TemplateValidationError(
                    "Select parameter '%s' must declare a non-empty 'options' list." % name
                )
            if len(options) > MAX_PARAMETER_OPTIONS:
                raise TemplateValidationError(
                    "Select parameter '%s' has too many options (max %d)."
                    % (name, MAX_PARAMETER_OPTIONS)
                )
            cleaned_options: List[str] = []
            for option in options:
                if not isinstance(option, str) or not option.strip():
                    raise TemplateValidationError(
                        "Select parameter '%s' options must be non-empty strings." % name
                    )
                if option not in cleaned_options:
                    cleaned_options.append(option)
            param["options"] = cleaned_options
        elif item.get("options") not in (None, [], (), ""):
            raise TemplateValidationError(
                "Parameter '%s': 'options' is only valid for select parameters." % name
            )

        if param_type == "number":
            minimum = item.get("min")
            maximum = item.get("max")
            for label, bound in (("min", minimum), ("max", maximum)):
                if bound is not None and not _is_number(bound):
                    raise TemplateValidationError(
                        "Parameter '%s': '%s' must be a number." % (name, label)
                    )
            if minimum is not None and maximum is not None and minimum > maximum:
                raise TemplateValidationError(
                    "Parameter '%s': 'min' must not be greater than 'max'." % name
                )
            param["min"] = minimum
            param["max"] = maximum
            param["integer"] = normalize_bool(item.get("integer"), default=False)
        elif (
            item.get("min") is not None
            or item.get("max") is not None
            or normalize_bool(item.get("integer"), default=False)
        ):
            # A canonical template always carries these keys (as ``None`` /
            # ``False``), so only a *set* bound or flag is an error.  Otherwise
            # the file written by ``create_template`` could not be read back.
            raise TemplateValidationError(
                "Parameter '%s': 'min', 'max' and 'integer' are only valid for number "
                "parameters." % name
            )

        if "default" in item and item.get("default") is not None:
            # An invalid default makes the template broken: reject it here.
            try:
                param["default"] = _coerce_param_value(param, item["default"])
            except TemplateValidationError as exc:
                raise TemplateValidationError(
                    "Parameter '%s' has an invalid default: %s" % (name, exc)
                ) from exc

        parameters.append(param)
    return parameters


# ---------------------------------------------------------------------------
# Variables (declarations only) / knowledge base / defaults
# ---------------------------------------------------------------------------

def _normalize_variable_declarations(raw: Any) -> List[Dict[str, Any]]:
    items = _as_list(raw, "variables")
    declarations: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise TemplateValidationError(
                "Each template variable must be an object with a 'key'."
            )
        unknown = sorted(set(item) - _VARIABLE_DECLARATION_KEYS)
        if unknown:
            raise TemplateValidationError(
                "Template variable contains unsupported key(s): %s." % ", ".join(unknown)
            )
        key = item.get("key")
        if not isinstance(key, str) or not VARIABLE_KEY_RE.fullmatch(key):
            raise TemplateValidationError(
                "Variable keys must start with a letter or underscore and contain only "
                "letters, digits and underscores."
            )
        if key in seen:
            raise TemplateValidationError("Duplicate variable declaration '%s'." % key)
        seen.add(key)
        is_secret = normalize_bool(item.get("is_secret"), default=False)
        default_value = item.get("default")
        if default_value is not None and not isinstance(default_value, str):
            raise TemplateValidationError(
                "Variable '%s' must declare its default as a string." % key
            )
        if is_secret and default_value is not None:
            raise TemplateValidationError(
                "Template variable '%s' is secret: a template stores declarations only, "
                "so it must not embed a default value." % key
            )
        declarations.append({
            "key": key,
            "is_secret": is_secret,
            "required": normalize_bool(item.get("required"), default=False),
            "description": _optional_string(
                item.get("description"), "variables.description",
                default="", max_length=MAX_TEMPLATE_DESCRIPTION_LENGTH,
            ),
            "default": default_value,
        })
    return declarations


def _normalize_kb_files(raw: Any) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TemplateValidationError(
            "Template field 'kb_files' must be an object mapping relative paths to "
            "file contents."
        )
    if len(raw) > MAX_TEMPLATE_ITEMS:
        raise TemplateValidationError(
            "Template declares too many knowledge-base files (max %d)." % MAX_TEMPLATE_ITEMS
        )
    files: Dict[str, str] = {}
    for path, content in raw.items():
        try:
            safe_path = _safe_kb_path(path)
        except AgentPortabilityError as exc:
            raise TemplateValidationError(
                "Knowledge-base path in template 'kb_files' is invalid: %s" % exc
            ) from exc
        if len(safe_path) > MAX_KB_PATH_LENGTH:
            raise TemplateValidationError(
                "Knowledge-base path is too long (max %d characters)." % MAX_KB_PATH_LENGTH
            )
        if safe_path in files:
            raise TemplateValidationError(
                "Duplicate knowledge-base path '%s' in template 'kb_files'." % safe_path
            )
        if not isinstance(content, str):
            raise TemplateValidationError(
                "Knowledge-base file '%s' must have string content." % safe_path
            )
        if len(content) > MAX_KB_FILE_LENGTH:
            raise TemplateValidationError(
                "Knowledge-base file '%s' is too large (max %d characters)."
                % (safe_path, MAX_KB_FILE_LENGTH)
            )
        files[safe_path] = content
    return files


def _normalize_defaults(raw: Any) -> Dict[str, Any]:
    """Validate ``defaults`` against the agent-factory field allowlist.

    Config keys are coerced by :func:`agent_factory.normalize_spec` so that a
    template default is written exactly like a UI-created agent setting
    (booleans become 0/1, enum aliases are canonicalized, ...).
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TemplateValidationError("Template field 'defaults' must be an object.")

    unknown = sorted(set(raw) - set(SPEC_FIELD_ALLOWLIST))
    if unknown:
        raise TemplateValidationError(
            "Template 'defaults' contains unsupported key(s): %s." % ", ".join(unknown)
        )

    config_values = {key: value for key, value in raw.items() if key in CONFIG_KEYS}
    identity_values = {
        key: value for key, value in raw.items() if key not in CONFIG_KEYS
    }
    spec: Dict[str, Any] = {"id": "_template_defaults"}
    if config_values:
        spec["configuration"] = config_values
    spec.update(identity_values)
    try:
        normalized = agent_factory.normalize_spec(spec)
    except SpecValidationError as exc:
        raise TemplateValidationError("Template 'defaults' is invalid: %s" % exc) from exc
    return dict(normalized["values"])


# ---------------------------------------------------------------------------
# Placeholder tokenizer / renderer
# ---------------------------------------------------------------------------

def _tokenize_prompt(source: str) -> List[Tuple[str, Any]]:
    """Split *source* into ``('lit', text)`` and ``('ph', name)`` tokens.

    One pass, escape-aware, and never re-scans substituted output.  A ``{{``
    that is neither a valid placeholder nor escaped raises
    :class:`TemplateRenderError`.
    """
    tokens: List[Tuple[str, Any]] = []
    literal: List[str] = []
    index = 0
    length = len(source)
    while index < length:
        if source.startswith(ESCAPE_SEQUENCE, index):
            literal.append("{{")
            index += len(ESCAPE_SEQUENCE)
            continue
        if source.startswith("{{", index):
            match = PLACEHOLDER_RE.match(source, index)
            if match is None:
                raise TemplateRenderError(
                    "Malformed placeholder at offset %d ('%s'): a placeholder must look "
                    "like '{{ name }}' and a literal '{{' must be %s."
                    % (index, _snippet(source, index), _escape_hint())
                )
            if literal:
                tokens.append(("lit", "".join(literal)))
                literal = []
            tokens.append(("ph", match.group(1)))
            index = match.end()
            continue
        literal.append(source[index])
        index += 1
    if literal:
        tokens.append(("lit", "".join(literal)))
    return tokens


def placeholder_names(source: str) -> List[str]:
    """Return every placeholder name referenced by *source*, in order."""
    names: List[str] = []
    for kind, payload in _tokenize_prompt(source):
        if kind == "ph" and payload not in names:
            names.append(payload)
    return names


def _render_source(source: str, declared: Mapping[str, Any], resolved: Mapping[str, Any]) -> str:
    """Render *source* with already-resolved values (single pass)."""
    tokens = _tokenize_prompt(source)
    unknown = sorted({
        payload for kind, payload in tokens if kind == "ph" and payload not in declared
    })
    if unknown:
        raise TemplateRenderError(
            "Unknown placeholder(s): %s. Declared parameters: %s."
            % (
                ", ".join(unknown),
                ", ".join(declared.keys()) or "(none)",
            )
        )
    parts: List[str] = []
    for kind, payload in tokens:
        if kind == "ph":
            parts.append(_format_value(resolved.get(payload, "")))
        else:
            parts.append(payload)
    return "".join(parts)


def _declared_parameters(template: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = template.get("parameters")
    if raw is None:
        return {}
    if not isinstance(raw, (list, tuple)):
        raise TemplateValidationError("Template field 'parameters' must be a list.")
    declared: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise TemplateValidationError("Each template parameter must be an object.")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise TemplateValidationError("Each template parameter must declare a 'name'.")
        declared[name] = dict(item)
    return declared


def resolve_parameters(
    template: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Resolve every declared parameter to a validated value.

    Resolution order is *caller value > declared default >* ``''``.  Caller
    parameters that the template does not declare are rejected.  With
    ``strict=True`` (the default) a ``required`` parameter that resolves to an
    empty rendered value is an error.
    """
    if not isinstance(template, Mapping):
        raise TemplateValidationError("Template must be a mapping.")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise TemplateValidationError("Template parameters must be provided as an object.")

    declared = _declared_parameters(template)
    unknown = sorted(key for key in params if key not in declared)
    if unknown:
        raise TemplateValidationError(
            "Undeclared parameter(s) for template '%s': %s. Only declared parameters may "
            "be supplied." % (template.get("id") or "?", ", ".join(unknown))
        )

    resolved: Dict[str, Any] = {}
    empty_required: List[str] = []
    for name, param in declared.items():
        if name in params and params[name] is not None:
            value = _coerce_param_value(param, params[name])
        elif param.get("default") is not None:
            value = _coerce_param_value(param, param["default"])
        else:
            value = ""
        if param.get("required") and not _format_value(value).strip():
            empty_required.append(name)
        resolved[name] = value

    if strict and empty_required:
        raise TemplateValidationError(
            "Required parameter(s) resolved to an empty value: %s."
            % ", ".join(empty_required)
        )
    return resolved


def render_system_prompt(
    template: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    *,
    strict: bool = True,
) -> str:
    """Render a template's system prompt with validated parameters.

    Unknown or malformed placeholders raise :class:`TemplateRenderError`; the
    rendered prompt is length-capped AFTER rendering.
    """
    if not isinstance(template, Mapping):
        raise TemplateValidationError("Template must be a mapping.")
    prompt = template.get("system_prompt")
    if not isinstance(prompt, str):
        raise TemplateValidationError("Template 'system_prompt' must be a string.")
    declared = _declared_parameters(template)
    resolved = resolve_parameters(template, params, strict=strict)
    rendered = _render_source(prompt, declared, resolved)
    if len(rendered) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateRenderError(
            "Rendered system prompt is too long (%d characters; max %d)."
            % (len(rendered), MAX_SYSTEM_PROMPT_LENGTH)
        )
    return rendered


# ---------------------------------------------------------------------------
# Template validation
# ---------------------------------------------------------------------------

def validate_template(payload: Any) -> Dict[str, Any]:
    """Validate a raw template object and return its canonical form.

    ``_meta`` (added by the loader) is ignored so that a fetch → edit → save
    round-trip needs no massaging.  Raises :class:`TemplateValidationError` /
    :class:`TemplateRenderError` for anything malformed.
    """
    if not isinstance(payload, Mapping):
        raise TemplateValidationError("Template must be a JSON object.")
    data = _strip_meta(payload)

    unknown = sorted(set(data) - _TEMPLATE_KEYS)
    if unknown:
        raise TemplateValidationError(
            "Template contains unsupported key(s): %s." % ", ".join(unknown)
        )

    template_id = _validate_template_id(data.get("id"))

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TemplateValidationError("Template field 'name' is required.")
    if len(name) > 200:
        raise TemplateValidationError("Template name is too long (max 200 characters).")

    description = _optional_string(
        data.get("description"), "description",
        default="", max_length=MAX_TEMPLATE_DESCRIPTION_LENGTH,
    )
    category = _optional_string(
        data.get("category"), "category", default="general", max_length=MAX_CATEGORY_LENGTH
    ) or "general"
    icon = _optional_string(
        data.get("icon"), "icon", default="", max_length=MAX_ICON_LENGTH
    )

    schema_version = data.get("schema_version", TEMPLATE_SCHEMA_VERSION)
    if isinstance(schema_version, str) and re.fullmatch(r"\d+", schema_version.strip()):
        schema_version = int(schema_version.strip())
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise TemplateValidationError("Template field 'schema_version' must be an integer.")
    if schema_version != TEMPLATE_SCHEMA_VERSION:
        raise TemplateValidationError(
            "Unsupported template schema_version %d (expected %d)."
            % (schema_version, TEMPLATE_SCHEMA_VERSION)
        )

    parameters = _normalize_parameters(data.get("parameters"))

    system_prompt = data.get("system_prompt")
    if not isinstance(system_prompt, str):
        raise TemplateValidationError(
            "Template field 'system_prompt' is required and must be a string."
        )
    if len(system_prompt) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateValidationError(
            "Template system prompt is too long (max %d characters)." % MAX_SYSTEM_PROMPT_LENGTH
        )

    tools = _string_list(data.get("tools"), "tools")
    skills = _string_list(data.get("skills"), "skills")
    variables = _normalize_variable_declarations(data.get("variables"))
    kb_files = _normalize_kb_files(data.get("kb_files"))
    defaults = _normalize_defaults(data.get("defaults"))

    canonical: Dict[str, Any] = {
        "id": template_id,
        "name": name,
        "description": description,
        "category": category,
        "icon": icon,
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "parameters": parameters,
        "system_prompt": system_prompt,
        "defaults": defaults,
        "tools": tools,
        "skills": skills,
        "variables": variables,
        "kb_files": kb_files,
    }

    # Placeholder audit: every placeholder must be declared, at save time too.
    declared = {param["name"]: param for param in parameters}
    for label, source in (("system_prompt", system_prompt),):
        unknown_placeholders = sorted(
            name for name in placeholder_names(source) if name not in declared
        )
        if unknown_placeholders:
            raise TemplateRenderError(
                "Template '%s' uses undeclared placeholder(s) in %s: %s. Declared "
                "parameters: %s."
                % (
                    template_id,
                    label,
                    ", ".join(unknown_placeholders),
                    ", ".join(declared) or "(none)",
                )
            )
    for key in ("name", "description"):
        value = defaults.get(key)
        if isinstance(value, str) and "{{" in value:
            unknown_placeholders = sorted(
                placeholder for placeholder in placeholder_names(value)
                if placeholder not in declared
            )
            if unknown_placeholders:
                raise TemplateRenderError(
                    "Template '%s' uses undeclared placeholder(s) in defaults.%s: %s."
                    % (template_id, key, ", ".join(unknown_placeholders))
                )
    return canonical


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _scan_dir(root: str) -> Tuple[List[str], List[str]]:
    """Return ``(json file names, candidate directory names)`` inside *root*.

    Dotfiles, dot-directories and symlinks are ignored.  A directory name is a
    *candidate* for the directory-based template form; it becomes a template only
    when it carries a ``meta.json`` discovery marker.
    """
    if not os.path.isdir(root):
        return [], []
    files: List[str] = []
    directories: List[str] = []
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        if os.path.isfile(path):
            if name.endswith(".json"):
                files.append(name)
        elif os.path.isdir(path):
            directories.append(name)
    return files, directories


def _scan_canonical(
    root: str,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Map id -> entry for every canonical template, plus shape collisions.

    Two entry shapes are recognised: the classic single-file form (``<id>.json``)
    and the additive directory form (``<id>/meta.json`` + prompt + ``kb/**``).
    When *both* shapes exist for the same id the id is a hard validation error on
    the surviving entry and a collision record is emitted, so precedence is never
    silent.
    """
    entries: Dict[str, Dict[str, Any]] = {}
    collisions: List[Dict[str, Any]] = []
    files, directories = _scan_dir(root)

    for file_name in files:
        stem = file_name[: -len(".json")]
        entry: Dict[str, Any] = {
            "id": stem,
            "source": "agent_templates",
            "shape": SHAPE_FILE,
            "file": file_name,
            "dir": None,
            "valid": False,
            "error": None,
            "template": None,
            "raw": None,
        }
        try:
            raw = _read_json_object(os.path.join(root, file_name))
        except TemplateError as exc:
            entry["error"] = str(exc)
            entry["raw"] = None
            entries[stem] = entry
            continue
        entry["raw"] = raw
        declared_id = raw.get("id")
        if declared_id is not None and declared_id != stem:
            entry["error"] = (
                "Template file '%s' declares id '%s' which does not match its file name."
                % (file_name, declared_id)
            )
            entries[stem] = entry
            continue
        try:
            entry["template"] = validate_template({**raw, "id": stem})
            entry["valid"] = True
        except (TemplateValidationError, AgentFactoryError) as exc:
            entry["error"] = str(exc)
        entries[stem] = entry

    for dir_name in directories:
        existing = entries.get(dir_name)
        if existing is not None:
            message = (
                "Template id '%s' is provided both as '%s.json' and as a directory "
                "'%s/'; remove one of them." % (dir_name, dir_name, dir_name)
            )
            existing["error"] = message
            existing["valid"] = False
            existing["template"] = None
            existing["collision"] = "both_shapes"
            collisions.append({
                "id": dir_name,
                "kind": "canonical_shape",
                "chosen": None,
                "shadowed": None,
                "canonical_file": existing.get("file"),
                "canonical_dir": dir_name,
                "legacy_file": None,
                "duplicate_file": None,
                "message": message,
            })
            continue
        entry = {
            "id": dir_name,
            "source": "agent_templates",
            "shape": SHAPE_DIR,
            "file": None,
            "dir": dir_name,
            "valid": False,
            "error": None,
            "template": None,
            "raw": None,
        }
        try:
            raw = _read_dir_template(os.path.join(root, dir_name), dir_name)
        except TemplateError as exc:
            entry["error"] = str(exc)
            entry["raw"] = None
            entries[dir_name] = entry
            continue
        entry["raw"] = raw
        try:
            entry["template"] = validate_template({**raw, "id": dir_name})
            entry["valid"] = True
        except (TemplateValidationError, AgentFactoryError) as exc:
            entry["error"] = str(exc)
        entries[dir_name] = entry

    return entries, collisions





# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def _entry_summary(entry: Mapping[str, Any], *, shadowed: bool, shadows: bool) -> Dict[str, Any]:
    """Project a scan entry onto the public list-summary shape."""
    template = entry.get("template")
    if template is not None:
        summary = {
            "id": template["id"],
            "name": template["name"],
            "description": template["description"],
            "category": template["category"],
            "icon": template["icon"],
            "schema_version": template["schema_version"],
            "parameter_count": len(template["parameters"]),
            "tool_count": len(template["tools"]),
            "skill_count": len(template["skills"]),
            "variable_count": len(template["variables"]),
            "kb_file_count": len(template["kb_files"]),
        }
    else:
        raw = entry.get("raw") or {}
        summary = {
            "id": entry.get("id"),
            "name": raw.get("name") or entry.get("id"),
            "description": raw.get("description") or "",
            "category": "general",
            "icon": "",
            "schema_version": raw.get("schema_version") or TEMPLATE_SCHEMA_VERSION,
            "parameter_count": len(raw.get("parameters") or []),
            "tool_count": len(raw.get("tools") or []),
            "skill_count": len(raw.get("skills") or []),
            "variable_count": len(raw.get("variables") or []),
            "kb_file_count": len(raw.get("kb_files") or {}),
        }
    summary.update({
        "source": entry.get("source"),
        "shape": entry.get("shape") or SHAPE_FILE,
        "file": entry.get("file"),
        "legacy": False,
        "writable": True,
        "shadowed": bool(shadowed),
        "shadows": bool(shadows),
        "collision": bool(shadowed),
        "valid": bool(entry.get("valid")),
        "error": entry.get("error"),
    })
    return summary


def list_templates(
    *,
    base_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List template summaries from the template root.

    Both canonical shapes are listed (``shape`` is ``"file"`` or ``"dir"``).
    """
    root = templates_dir(base_dir)
    canonical, _shape_collisions = _scan_canonical(root)
    return [
        _entry_summary(canonical[template_id], shadowed=False, shadows=False)
        for template_id in sorted(canonical)
    ]


def has_template(template_id: str, *, base_dir: Optional[str] = None) -> bool:
    """Return whether *template_id* exists in the template root (either shape)."""
    root = templates_dir(base_dir)
    cleaned = _validate_template_id(template_id)
    canonical_map, _shape_collisions = _scan_canonical(root)
    return cleaned in canonical_map


def list_collisions(*, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Report every id that exists in more than one place.

    The only collision kind is the same-root shape collision (``<id>.json`` +
    ``<id>/``), a hard validation error on both entries.  Collisions are never
    resolved silently.
    """
    root = templates_dir(base_dir)
    _canonical, shape_collisions = _scan_canonical(root)
    return list(shape_collisions)


def get_template(template_id: str, *, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load a template (either storage shape) as a canonical mapping.

    Both canonical shapes are supported: the single-file ``<id>.json`` form and
    the directory form (``<id>/meta.json`` + prompt + ``kb/**``), which is
    re-inlined into the exact same shape.

    The returned mapping carries a read-only ``_meta`` block describing where
    the template came from and whether it is writable.
    """
    canonical_root = templates_dir(base_dir)
    cleaned = _validate_template_id(template_id)

    canonical_path = _template_path(canonical_root, cleaned)
    dir_path = _template_dir_path(canonical_root, cleaned)
    is_file = os.path.isfile(canonical_path)
    is_dir = os.path.isdir(dir_path) and not os.path.islink(dir_path)
    if is_file and is_dir:
        raise TemplateValidationError(
            "Template id '%s' exists both as '%s.json' and as a directory '%s/'; "
            "remove one of them." % (cleaned, cleaned, cleaned)
        )
    if is_file:
        raw = _read_json_object(canonical_path)
        declared_id = raw.get("id")
        if declared_id is not None and declared_id != cleaned:
            raise TemplateValidationError(
                "Template file '%s.json' declares id '%s' which does not match its file name."
                % (cleaned, declared_id)
            )
        template = validate_template({**raw, "id": cleaned})
        template[_META_KEY] = {
            "source": "agent_templates",
            "legacy": False,
            "writable": True,
            "file": os.path.basename(canonical_path),
        }
        return template

    if is_dir:
        raw = _read_dir_template(dir_path, cleaned)
        template = validate_template({**raw, "id": cleaned})
        template[_META_KEY] = {
            "source": "agent_templates",
            "legacy": False,
            "writable": True,
            "file": None,
        }
        return template

    raise TemplateNotFoundError("Template '%s' was not found." % cleaned)


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def create_template(
    data: Any,
    *,
    base_dir: Optional[str] = None,
    shape: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate, then atomically write a new canonical template.

    ``shape`` selects the storage form: the default ``"file"`` writes
    ``agent_templates/<id>.json``; ``"dir"`` opts into the directory form
    (``<id>/meta.json`` + prompt + ``kb/**``).  Refuses to overwrite an existing
    canonical template in either shape.
    """
    if not isinstance(data, Mapping):
        raise TemplateValidationError("Template must be a JSON object.")
    requested_shape = shape
    if requested_shape is None:
        requested_shape = SHAPE_FILE
    if not isinstance(requested_shape, str) or requested_shape not in (SHAPE_FILE, SHAPE_DIR):
        raise TemplateValidationError(
            "Template shape must be '%s' or '%s'." % (SHAPE_FILE, SHAPE_DIR)
        )
    cleaned = _validate_template_id(_strip_meta(data).get("id"))
    template = validate_template({**_strip_meta(data), "id": cleaned})

    canonical_root = templates_dir(base_dir)
    path = _template_path(canonical_root, cleaned)
    dir_path = _template_dir_path(canonical_root, cleaned)
    if os.path.lexists(path) or os.path.lexists(dir_path):
        raise TemplateExistsError(
            "A template with id '%s' already exists in agent_templates/." % cleaned
        )

    if requested_shape == SHAPE_DIR:
        _write_dir_template(canonical_root, cleaned, template)
        logger.info("agent_templates: created directory template '%s'", cleaned)
    else:
        _atomic_write_template(path, template)
        logger.info("agent_templates: created template '%s'", cleaned)
    return get_template(cleaned, base_dir=base_dir)


def update_template(
    template_id: str,
    data: Any,
    *,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a canonical template (top-level shallow merge) and rewrite it.

    The payload is persisted into whichever shape the template already has.  A
    dropped knowledge-base file is only pruned when the payload actually carries
    ``kb_files``.
    """
    cleaned = _validate_template_id(template_id)
    if not isinstance(data, Mapping):
        raise TemplateValidationError("Template must be a JSON object.")
    payload = _strip_meta(data)

    canonical_root = templates_dir(base_dir)
    path = _template_path(canonical_root, cleaned)
    dir_path = _template_dir_path(canonical_root, cleaned)
    is_file = os.path.isfile(path)
    is_dir = os.path.isdir(dir_path) and not os.path.islink(dir_path)
    if is_file and is_dir:
        raise TemplateValidationError(
            "Template id '%s' exists both as '%s.json' and as a directory '%s/'; "
            "remove one of them before editing." % (cleaned, cleaned, cleaned)
        )
    if not is_file and not is_dir:
        raise TemplateNotFoundError("Template '%s' was not found." % cleaned)

    declared_id = payload.get("id")
    if declared_id is not None and declared_id != cleaned:
        raise TemplateValidationError(
            "The id of an existing template cannot be changed ('%s' -> '%s')."
            % (cleaned, declared_id)
        )

    if is_file:
        existing = _strip_meta(_read_json_object(path))
        merged = {**existing, **payload}
        merged["id"] = cleaned
        template = validate_template(merged)
        _atomic_write_template(path, template)
        logger.info("agent_templates: updated template '%s'", cleaned)
        return get_template(cleaned, base_dir=base_dir)

    existing_meta = _read_json_object(os.path.join(dir_path, DIR_TEMPLATE_META))
    prompt_file = existing_meta.get("prompt_file") or DIR_TEMPLATE_PROMPT
    existing = _strip_meta(_read_dir_template(dir_path, cleaned))
    merged = {**existing, **payload}
    merged["id"] = cleaned
    template = validate_template(merged)
    _write_dir_template(canonical_root, cleaned, template, prompt_file=prompt_file)
    if "kb_files" in payload:
        _prune_dir_kb(dir_path, sorted(template["kb_files"]))
    logger.info("agent_templates: updated directory template '%s'", cleaned)
    return get_template(cleaned, base_dir=base_dir)


def delete_template(template_id: str, *, base_dir: Optional[str] = None) -> bool:
    """Delete a canonical template (either shape)."""
    cleaned = _validate_template_id(template_id)
    canonical_root = templates_dir(base_dir)
    path = _template_path(canonical_root, cleaned)
    if os.path.isfile(path):
        os.unlink(path)
        logger.info("agent_templates: deleted template '%s'", cleaned)
        return True
    dir_path = _template_dir_path(canonical_root, cleaned)
    if os.path.isdir(dir_path) and not os.path.islink(dir_path):
        shutil.rmtree(dir_path)
        logger.info("agent_templates: deleted directory template '%s'", cleaned)
        return True
    raise TemplateNotFoundError("Template '%s' was not found." % cleaned)


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------

def _available_tool_ids() -> Optional[set]:
    """Return every assignable tool id, or ``None`` when it cannot be checked.

    Returns ``None`` (never a false "missing" report) when the tool registry is
    unavailable in this process.
    """
    try:
        from backend.tools import tool_registry
        from backend.tools.registry import BUILTIN_TOOL_IDS

        names = set(BUILTIN_TOOL_IDS)
        for definition in tool_registry.get_all_tool_defs():
            function = (definition or {}).get("function") or {}
            name = function.get("name")
            if name:
                names.add(name)
        for definition in tool_registry.get_builtin_tool_defs():
            name = (definition or {}).get("name")
            if name:
                names.add(name)
        return names
    except Exception:  # noqa: BLE001 - availability check must never be fatal
        logger.warning(
            "agent_templates: tool registry unavailable; skipping tool dependency checks",
            exc_info=True,
        )
        return None


def _available_skill_ids(base_dir: str) -> Optional[set]:
    """Return installed skill ids (manifest scan), or ``None`` if unknowable."""
    skills_root = os.path.join(base_dir, "skills")
    if not os.path.isdir(skills_root):
        return None
    return {
        name
        for name in os.listdir(skills_root)
        if os.path.isfile(os.path.join(skills_root, name, "skill.json"))
    }


def _resolution_report(template: Mapping[str, Any], *, base_dir: str) -> Dict[str, Any]:
    """Build the editor-facing resolution report for a canonical template."""
    declared_tools = list(template.get("tools") or [])
    declared_skills = list(template.get("skills") or [])
    parameters = _declared_parameters(template)

    available_tools = _available_tool_ids()
    tools_checked = available_tools is not None
    missing_tools = (
        [name for name in declared_tools if name not in available_tools]
        if tools_checked else []
    )

    available_skills = _available_skill_ids(base_dir)
    skills_checked = available_skills is not None
    missing_skills = (
        [name for name in declared_skills if name not in available_skills]
        if skills_checked else []
    )

    referenced: List[str] = list(placeholder_names(template.get("system_prompt") or ""))
    for key in ("name", "description"):
        value = (template.get("defaults") or {}).get(key)
        if isinstance(value, str):
            referenced.extend(placeholder_names(value))

    declared_variables = list(template.get("variables") or [])
    required_variables = [
        declaration["key"] for declaration in declared_variables if declaration.get("required")
    ]
    unresolved_variables = [
        declaration["key"]
        for declaration in declared_variables
        if declaration.get("required") and not declaration.get("default")
    ]
    required_parameters = [name for name, param in parameters.items() if param.get("required")]
    parameters_without_value = [
        name for name in required_parameters if parameters[name].get("default") in (None, "")
    ]

    warnings: List[str] = []
    errors: List[str] = []
    if not tools_checked:
        warnings.append("Tool availability could not be checked (registry unavailable).")
    if not skills_checked:
        warnings.append(
            "Skill availability could not be checked (no skills/ directory under base_dir)."
        )
    if missing_tools:
        errors.append("Missing tool(s): %s." % ", ".join(missing_tools))
    if missing_skills:
        errors.append("Missing skill(s): %s." % ", ".join(missing_skills))
    unused = [name for name in parameters if name not in referenced]
    if unused:
        warnings.append("Declared parameter(s) not referenced by the prompt: %s." % ", ".join(unused))
    if unresolved_variables:
        warnings.append(
            "Required variable(s) need a value at instantiate time: %s."
            % ", ".join(unresolved_variables)
        )
    if parameters_without_value:
        warnings.append(
            "Required parameter(s) without a default need a value at instantiate time: %s."
            % ", ".join(parameters_without_value)
        )

    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "source": (template.get(_META_KEY) or {}).get("source"),
        "legacy": bool((template.get(_META_KEY) or {}).get("legacy")),
        "writable": bool((template.get(_META_KEY) or {}).get("writable")),
        "ok": not errors,
        "deps_checked": {"tools": tools_checked, "skills": skills_checked},
        "tools": {
            "declared": declared_tools,
            "resolved": [name for name in declared_tools if name not in missing_tools],
            "missing": missing_tools,
        },
        "skills": {
            "declared": declared_skills,
            "resolved": [name for name in declared_skills if name not in missing_skills],
            "missing": missing_skills,
        },
        "parameters": {
            "declared": list(parameters),
            "required": required_parameters,
            "required_without_value": parameters_without_value,
            "unused": unused,
        },
        "variables": {
            "declared": [declaration["key"] for declaration in declared_variables],
            "required": required_variables,
            "unresolved": unresolved_variables,
        },
        "warnings": warnings,
        "errors": errors,
    }


def resolve_template(template_id: str, *, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return the full editor report for a template (never raises for deps).

    Tools and skills are resolved at *inspect* time only — nothing is cached
    back into the template file.
    """
    template = get_template(template_id, base_dir=base_dir)
    root = base_dir if base_dir is not None else _default_base_dir()
    report = _resolution_report(template, base_dir=root)
    report["template"] = template
    return report


# ---------------------------------------------------------------------------
# Preview + instantiation
# ---------------------------------------------------------------------------

def preview_template(
    template_id: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    base_dir: Optional[str] = None,
    strict: bool = False,
) -> Dict[str, Any]:
    """Render a template without creating anything (editor preview).

    ``strict=False`` (default) tolerates required parameters that still need a
    value, and reports them as warnings.
    """
    template = get_template(template_id, base_dir=base_dir)
    resolved = resolve_parameters(template, params, strict=False)
    declared = _declared_parameters(template)
    prompt = _render_source(
        template.get("system_prompt") or "", declared, resolved
    )
    if len(prompt) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateRenderError(
            "Rendered system prompt is too long (%d characters; max %d)."
            % (len(prompt), MAX_SYSTEM_PROMPT_LENGTH)
        )

    defaults = dict(template.get("defaults") or {})
    if not defaults.get("name"):
        defaults["name"] = template.get("name") or template.get("id")
    if "description" not in defaults:
        defaults["description"] = template.get("description") or ""
    rendered_identity = {
        key: _render_source(value, declared, resolved) if isinstance(value, str) else value
        for key, value in defaults.items()
    }

    warnings: List[str] = []
    if not strict:
        empty_required = [
            name
            for name, param in declared.items()
            if param.get("required") and _format_value(resolved.get(name, "")) == ""
        ]
        if empty_required:
            warnings.append(
                "Required parameter(s) without a value yet: %s." % ", ".join(empty_required)
            )

    return {
        "id": template["id"],
        "name": template["name"],
        "description": template["description"],
        "source": (template.get(_META_KEY) or {}).get("source"),
        "writable": bool((template.get(_META_KEY) or {}).get("writable")),
        "system_prompt": prompt,
        "system_prompt_length": len(prompt),
        "values": resolved,
        "spec_preview": rendered_identity,
        "warnings": warnings,
    }


def _normalize_overrides(overrides: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise TemplateValidationError("Template overrides must be provided as an object.")
    unknown = sorted(key for key in overrides if key not in OVERRIDE_KEYS)
    if unknown:
        raise TemplateValidationError(
            "Template override(s) not allowed: %s." % ", ".join(unknown)
        )
    return dict(overrides)


def _coerce_variable_value(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return _format_value(value)
    if isinstance(value, str):
        return value
    raise TemplateValidationError(
        "Variable '%s' must have a string value." % key
    )


def _merge_variables(
    template: Mapping[str, Any],
    provided: Any,
    *,
    allow_unresolved: bool,
) -> List[Dict[str, Any]]:
    """Merge template variable declarations with values supplied at instantiate.

    Declarations own secrecy; values are never logged or written to the
    template file.
    """
    declarations = list(template.get("variables") or [])
    declared = {declaration["key"]: declaration for declaration in declarations}

    values: Dict[str, Any] = {}
    if provided is None:
        pass
    elif isinstance(provided, Mapping):
        values = dict(provided)
    elif isinstance(provided, (list, tuple)):
        for item in provided:
            if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
                raise TemplateValidationError(
                    "Each variable override must be an object with a 'key'."
                )
            values[item["key"]] = item.get("value", "")
    else:
        raise TemplateValidationError(
            "Variable overrides must be an object or a list of objects."
        )

    unknown = sorted(key for key in values if key not in declared)
    if unknown:
        raise TemplateValidationError(
            "Undeclared variable(s): %s. The template declares: %s."
            % (", ".join(unknown), ", ".join(declared) or "(none)")
        )

    missing: List[str] = []
    merged: List[Dict[str, Any]] = []
    for key, declaration in declared.items():
        if key in values and values[key] is not None:
            value = _coerce_variable_value(key, values[key])
        elif declaration.get("default") is not None:
            value = declaration["default"]
        else:
            value = ""
        if declaration.get("required") and not value:
            missing.append(key)
        merged.append({
            "key": key,
            "value": value,
            "is_secret": 1 if declaration.get("is_secret") else 0,
        })

    if missing and not allow_unresolved:
        raise TemplateValidationError(
            "Missing value(s) for required variable(s): %s." % ", ".join(missing)
        )
    return merged


def create_agent_from_template(
    template_id: str,
    params: Optional[Mapping[str, Any]] = None,
    overrides: Optional[Mapping[str, Any]] = None,
    *,
    db: Any = None,
    base_dir: Optional[str] = None,
    if_exists: Optional[str] = None,
    allow_missing_deps: bool = False,
    allow_unresolved_variables: bool = False,
) -> str:
    """Instantiate a real agent from a template and return its agent id.

    Sequence: load + schema-validate → validate params and overrides →
    resolve tool/skill dependencies (unless ``allow_missing_deps``) → render
    the prompt and merge defaults (``overrides`` > ``defaults``) → hand the
    spec to :func:`backend.agent_factory.create_agent`, which performs the
    atomic DB/filesystem creation.

    ``base_dir`` is the repository root used both for template lookup and for
    the new agent's files.
    """
    template = get_template(template_id, base_dir=base_dir)
    resolved_params = resolve_parameters(template, params, strict=True)
    override_values = _normalize_overrides(overrides)

    root = base_dir if base_dir is not None else _default_base_dir()
    report = _resolution_report(template, base_dir=root)
    if report["errors"] and not allow_missing_deps:
        raise TemplateResolveError(
            "Template '%s' has unresolved dependencies: %s"
            % (template["id"], " ".join(report["errors"])),
            report,
        )

    variables = _merge_variables(
        template,
        override_values.get("variables"),
        allow_unresolved=allow_unresolved_variables,
    )

    declared = _declared_parameters(template)
    prompt = _render_source(
        template.get("system_prompt") or "", declared, resolved_params
    )
    if len(prompt) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateRenderError(
            "Rendered system prompt is too long (%d characters; max %d)."
            % (len(prompt), MAX_SYSTEM_PROMPT_LENGTH)
        )

    spec: Dict[str, Any] = {}
    for key, value in (template.get("defaults") or {}).items():
        spec[key] = value
    for key, value in override_values.items():
        if key in SPEC_FIELD_ALLOWLIST:
            spec[key] = value

    if not spec.get("name"):
        spec["name"] = template.get("name") or template["id"]
    if "description" not in spec:
        spec["description"] = template.get("description") or ""
    for key in ("name", "description"):
        if isinstance(spec.get(key), str) and "{{" in spec[key]:
            spec[key] = _render_source(spec[key], declared, resolved_params)

    spec["system_prompt"] = prompt
    spec["tools"] = (
        _string_list(override_values["tools"], "tools")
        if "tools" in override_values else list(template.get("tools") or [])
    )
    spec["skills"] = (
        _string_list(override_values["skills"], "skills")
        if "skills" in override_values else list(template.get("skills") or [])
    )
    spec["knowledge_base"] = (
        override_values["knowledge_base"]
        if "knowledge_base" in override_values
        else [
            {"path": path, "content": content}
            for path, content in (template.get("kb_files") or {}).items()
        ]
    )
    spec["variables"] = variables
    if "id" in override_values:
        spec["id"] = override_values["id"]

    agent_id = agent_factory.create_agent(
        spec, db=db, base_dir=base_dir, if_exists=if_exists
    )
    logger.info(
        "agent_templates: created agent '%s' from template '%s'", agent_id, template["id"]
    )
    return agent_id

