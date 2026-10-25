"""Unit tests for :mod:`backend.agent_templates`.

The template engine is the user-facing layer that turns a JSON blueprint
(tools, skills, variables, KB files, parameterized system prompt) into a real
agent through :mod:`backend.agent_factory`.  These tests pin the contract that:

* storage is a single writable root (``agent_templates/``) holding each
  template in one of **two equivalent shapes**: a single ``<id>.json`` file or a
  directory ``<id>/`` (``meta.json`` + prompt file + ``kb/**``).  Both load to
  the same canonical mapping and a same-id clash between the two shapes is a
  hard error, never silent precedence,
* every id / KB filename that becomes a filesystem path is traversal-guarded,
* the renderer is a hardened single pass: unknown or malformed placeholders are
  hard errors, ``\\{{`` escapes a literal brace, and substituted values are
  never re-scanned (no recursive injection),
* parameter values *and* defaults are type/range/length validated,
* instantiation merges ``overrides > params > defaults`` and never writes a
  secret value into the template file.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

from models.db import db

from backend import agent_templates
from backend.agent_factory import (
    ARTIFACT_TOOLS,
    MAX_SYSTEM_PROMPT_LENGTH,
    AgentAlreadyExistsError,
    SpecValidationError,
)
from backend.agent_portability import MAX_KB_FILE_LENGTH
from backend.agent_templates import (
    MAX_PARAM_VALUE_LENGTH,
    OVERRIDE_KEYS,
    PARAM_NAME_RE,
    SLUG_RE,
    TEMPLATE_SCHEMA_VERSION,
    TemplateExistsError,
    TemplateNotFoundError,
    TemplateRenderError,
    TemplateResolveError,
    TemplateValidationError,
    create_agent_from_template,
    create_template,
    delete_template,
    get_template,
    has_template,
    list_collisions,
    list_templates,
    placeholder_names,
    preview_template,
    render_system_prompt,
    resolve_parameters,
    resolve_template,
    update_template,
    validate_template,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def template_payload(**overrides):
    """Return a complete, valid template payload (overridable per test)."""
    payload = {
        "id": "support_bot",
        "name": "Support Bot",
        "description": "Answers customer questions.",
        "category": "support",
        "icon": "headset",
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "parameters": [
            {"name": "company", "label": "Company", "type": "text", "required": True, "default": "Acme"},
            {"name": "tone", "type": "select", "options": ["formal", "friendly"], "default": "friendly"},
            {"name": "retries", "type": "number", "default": 3, "min": 1, "max": 10, "integer": True},
            {"name": "verbose", "type": "boolean", "default": False},
        ],
        "system_prompt": "You support {{company}} in a {{tone}} tone (retries={{retries}}, verbose={{verbose}}).",
        "defaults": {"sandbox_enabled": 1, "artifacts_enabled": 0},
        "tools": ["read_file", "calculator"],
        "skills": ["github"],
        "variables": [{"key": "API_TOKEN", "is_secret": True, "description": "API token."}],
        "kb_files": {"guide/start.md": "# Start\n"},
    }
    payload.update(overrides)
    return payload


class TemplateTestCase(unittest.TestCase):
    """Shared temp workspace mimicking the repository root layout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent_templates_test_")
        self.templates_dir = os.path.join(self.tmp, "agent_templates")
        os.makedirs(self.templates_dir, exist_ok=True)
        self.write_skill("github")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def write_skill(self, skill_id):
        directory = os.path.join(self.tmp, "skills", skill_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "skill.json"), "w", encoding="utf-8") as handle:
            json.dump({"id": skill_id, "name": skill_id.title()}, handle)

    def canonical_path(self, template_id):
        return os.path.join(self.templates_dir, template_id + ".json")

    def write_canonical(self, payload, filename=None):
        path = self.canonical_path(filename or payload.get("id", "unnamed"))
        self._write_json(path, payload)
        return path

    @staticmethod
    def _write_json(path, payload):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return path

    @staticmethod
    def read_text(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def template_files(self):
        return sorted(os.listdir(self.templates_dir))

    def create(self, payload=None, **kwargs):
        return create_template(payload or template_payload(), base_dir=self.tmp, **kwargs)


# ----------------------------------------------------------------------
# Renderer
# ----------------------------------------------------------------------


class RendererTests(TemplateTestCase):
    def canonical(self, **overrides):
        return validate_template(template_payload(**overrides))

    def test_defaults_are_rendered(self):
        rendered = render_system_prompt(self.canonical())
        self.assertEqual(
            rendered,
            "You support Acme in a friendly tone (retries=3, verbose=false).",
        )

    def test_resolution_order_value_then_default_then_empty(self):
        template = self.canonical()
        resolved = resolve_parameters(template, {"company": "Globex"})
        self.assertEqual(resolved["company"], "Globex")   # caller value wins
        self.assertEqual(resolved["tone"], "friendly")    # declared default
        self.assertEqual(resolved["verbose"], False)      # declared default
        bare = validate_template(
            template_payload(parameters=[{"name": "only"}], system_prompt="x{{only}}y")
        )
        self.assertEqual(resolve_parameters(bare), {"only": ""})

    def test_unknown_placeholder_is_a_hard_error_at_save(self):
        with self.assertRaises(TemplateRenderError) as ctx:
            validate_template(template_payload(system_prompt="Hello {{nope}} and {{other}}."))
        message = str(ctx.exception)
        self.assertIn("nope", message)
        self.assertIn("other", message)

    def test_unknown_placeholder_is_a_hard_error_at_render(self):
        template = self.canonical()
        template["system_prompt"] = "Hello {{nope}}."
        with self.assertRaises(TemplateRenderError) as ctx:
            render_system_prompt(template)
        self.assertIn("nope", str(ctx.exception))

    def test_malformed_placeholder_is_a_hard_error(self):
        for prompt in ("{{ 1bad }}", "{{unclosed", "{{}}", "{{bad-name}}"):
            with self.assertRaises(TemplateRenderError, msg=prompt):
                validate_template(template_payload(system_prompt="x " + prompt + " y"))

    def test_escaped_braces_render_literally(self):
        template = validate_template(
            template_payload(system_prompt="literal \\{{company}} then {{company}}")
        )
        self.assertEqual(
            render_system_prompt(template, {"company": "Acme"}),
            "literal {{company}} then Acme",
        )

    def test_escaped_braces_are_not_placeholders(self):
        template = validate_template(
            template_payload(parameters=[], system_prompt="only \\{{braces}} here")
        )
        self.assertEqual(placeholder_names(template["system_prompt"]), [])
        self.assertEqual(render_system_prompt(template), "only {{braces}} here")

    def test_substituted_values_are_never_re_scanned(self):
        rendered = render_system_prompt(
            self.canonical(), {"company": "{{verbose}} {{retries}}"}
        )
        # The injected placeholder survives verbatim: no recursive rendering.
        self.assertEqual(
            rendered,
            "You support {{verbose}} {{retries}} in a friendly tone (retries=3, verbose=false).",
        )

    def test_boolean_values_are_coerced_not_stringified_naively(self):
        template = self.canonical()
        self.assertIn("verbose=true", render_system_prompt(template, {"verbose": True}))
        self.assertIn("verbose=true", render_system_prompt(template, {"verbose": "true"}))
        self.assertIn("verbose=true", render_system_prompt(template, {"verbose": "YES"}))
        self.assertIn("verbose=true", render_system_prompt(template, {"verbose": 1}))
        self.assertIn("verbose=false", render_system_prompt(template, {"verbose": "false"}))
        self.assertIn("verbose=false", render_system_prompt(template, {"verbose": 0}))
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(template, {"verbose": "maybe"})
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(template, {"verbose": 2})

    def test_number_values_are_coerced_and_range_checked(self):
        template = self.canonical()
        self.assertIn("retries=7", render_system_prompt(template, {"retries": "7"}))
        self.assertIn("retries=7", render_system_prompt(template, {"retries": 7}))
        with self.assertRaises(TemplateValidationError) as ctx:
            render_system_prompt(template, {"retries": 11})
        self.assertIn("less than or equal to 10", str(ctx.exception))
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(template, {"retries": 0})
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(template, {"retries": "seven"})
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(template, {"retries": True})

    def test_select_values_must_be_declared_options(self):
        template = self.canonical()
        self.assertIn("formal", render_system_prompt(template, {"tone": "formal"}))
        with self.assertRaises(TemplateValidationError) as ctx:
            render_system_prompt(template, {"tone": "shouty"})
        self.assertIn("tone", str(ctx.exception))

    def test_undeclared_caller_parameter_is_rejected(self):
        template = self.canonical()
        with self.assertRaises(TemplateValidationError) as ctx:
            render_system_prompt(template, {"ghost": "x", "phantom": "y"})
        message = str(ctx.exception)
        self.assertIn("ghost", message)
        self.assertIn("phantom", message)

    def test_required_parameter_must_resolve_non_empty(self):
        template = self.canonical()
        with self.assertRaises(TemplateValidationError) as ctx:
            render_system_prompt(template, {"company": "   "})
        self.assertIn("company", str(ctx.exception))
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(template, {"company": ""})
        # strict=False is the editor path: no error, caller reports the warning.
        self.assertEqual(
            resolve_parameters(template, {"company": ""}, strict=False)["company"], ""
        )

    def test_parameter_value_length_is_capped(self):
        template = self.canonical()
        oversized = "x" * (MAX_PARAM_VALUE_LENGTH + 1)
        with self.assertRaises(TemplateValidationError) as ctx:
            render_system_prompt(template, {"company": oversized})
        self.assertIn("company", str(ctx.exception))
        # Exactly at the cap is fine.
        accepted = "y" * MAX_PARAM_VALUE_LENGTH
        self.assertIn(accepted, render_system_prompt(template, {"company": accepted}))

    def test_post_render_prompt_length_is_capped(self):
        template = validate_template(
            template_payload(
                parameters=[{"name": "who", "default": "x" * 100}],
                system_prompt="z" * (MAX_SYSTEM_PROMPT_LENGTH - 10) + "{{who}}",
            )
        )
        with self.assertRaises(TemplateRenderError) as ctx:
            render_system_prompt(template)
        self.assertIn(str(MAX_SYSTEM_PROMPT_LENGTH), str(ctx.exception))

    def test_render_requires_a_template_mapping_and_string_prompt(self):
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(["not", "a", "mapping"])
        with self.assertRaises(TemplateValidationError):
            render_system_prompt({"system_prompt": 5, "parameters": []})


# ----------------------------------------------------------------------
# Schema / parameters / defaults / variables / KB
# ----------------------------------------------------------------------


class SchemaTests(TemplateTestCase):
    def test_valid_template_is_normalised(self):
        canonical = validate_template(template_payload())
        self.assertEqual(canonical["id"], "support_bot")
        self.assertEqual(canonical["schema_version"], TEMPLATE_SCHEMA_VERSION)
        self.assertEqual(
            set(canonical),
            {
                "id", "name", "description", "category", "icon", "schema_version",
                "parameters", "system_prompt", "defaults", "tools", "skills",
                "variables", "kb_files",
            },
        )
        company = canonical["parameters"][0]
        self.assertEqual(
            sorted(company),
            ["default", "description", "integer", "label", "max", "min", "name",
             "options", "placeholder", "required", "type"],
        )
        self.assertTrue(company["required"])
        self.assertEqual(canonical["defaults"], {"sandbox_enabled": 1, "artifacts_enabled": 0})

    def test_category_icon_and_schema_version_default(self):
        payload = template_payload()
        payload.pop("category")
        payload.pop("icon")
        payload.pop("schema_version")
        canonical = validate_template(payload)
        self.assertEqual(canonical["category"], "general")
        self.assertEqual(canonical["icon"], "")
        self.assertEqual(canonical["schema_version"], 1)

    def test_unknown_top_level_key_is_rejected(self):
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(bogus=1))
        self.assertIn("bogus", str(ctx.exception))

    def test_required_top_level_fields(self):
        for field in ("id", "name", "system_prompt"):
            payload = template_payload()
            payload.pop(field)
            with self.assertRaises(TemplateValidationError, msg=field):
                validate_template(payload)

    def test_invalid_id_is_rejected(self):
        for bad_id in ("", "Bad", "bad id", "bad/id", "../evil", "..", "-leading"):
            with self.assertRaises(TemplateValidationError, msg=bad_id):
                validate_template(template_payload(id=bad_id))

    def test_meta_block_is_stripped(self):
        canonical = validate_template(template_payload(_meta={"source": "x"}))
        self.assertNotIn("_meta", canonical)

    def test_duplicate_parameter_names_are_rejected(self):
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(parameters=[
                {"name": "a"}, {"name": "a"},
            ]))
        self.assertIn("Duplicate", str(ctx.exception))

    def test_parameter_name_charset_is_enforced(self):
        for bad_name in ("bad-name", "1leading", "bad name", "bad.name", "café"):
            self.assertIsNone(PARAM_NAME_RE.fullmatch(bad_name))
            with self.assertRaises(TemplateValidationError, msg=bad_name):
                validate_template(template_payload(parameters=[{"name": bad_name}]))

    def test_parameters_must_be_a_list_of_objects(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters={"name": "a"}))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=["a"]))

    def test_parameter_types_are_validated(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[{"name": "a", "type": "color"}]))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[{"name": "a", "unknown": 1}]))

    def test_select_requires_options(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[{"name": "a", "type": "select"}]))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[
                {"name": "a", "type": "select", "options": []},
            ]))
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(parameters=[
                {"name": "a", "type": "select", "options": ["x"], "default": "y"},
            ]))
        self.assertIn("default", str(ctx.exception))

    def test_options_are_rejected_for_non_select_parameters(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[
                {"name": "a", "type": "text", "options": ["x"]},
            ]))

    def test_numeric_bounds_are_validated(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[
                {"name": "n", "type": "number", "min": 5, "max": 1},
            ]))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[{"name": "n", "type": "text", "min": 1}]))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(parameters=[{"name": "n", "type": "text", "integer": True}]))

    def test_invalid_defaults_are_rejected(self):
        cases = [
            [{"name": "n", "type": "number", "default": 99, "max": 10}],
            [{"name": "n", "type": "number", "integer": True, "default": 1.5}],
            [{"name": "n", "type": "number", "default": "seven"}],
            [{"name": "b", "type": "boolean", "default": "maybe"}],
            [{"name": "t", "type": "text", "default": "x" * (MAX_PARAM_VALUE_LENGTH + 1)}],
        ]
        for parameters in cases:
            with self.assertRaises(TemplateValidationError, msg=repr(parameters)) as ctx:
                validate_template(template_payload(
                    parameters=parameters, system_prompt="{{%s}}" % parameters[0]["name"],
                ))
            self.assertIn("invalid default", str(ctx.exception))

    def test_valid_boolean_and_number_defaults_round_trip(self):
        canonical = validate_template(template_payload(parameters=[
            {"name": "b", "type": "boolean", "default": "true"},
            {"name": "n", "type": "number", "default": "4.5"},
            {"name": "i", "type": "number", "integer": True, "default": 7},
        ], system_prompt="{{b}} {{n}} {{i}}"))
        self.assertIs(canonical["parameters"][0]["default"], True)
        self.assertEqual(canonical["parameters"][1]["default"], 4.5)
        self.assertEqual(canonical["parameters"][2]["default"], 7)
        self.assertEqual(render_system_prompt(canonical), "true 4.5 7")

    def test_defaults_allowlist_is_enforced(self):
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(defaults={"workspace": "/tmp/elsewhere"}))
        self.assertIn("workspace", str(ctx.exception))
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(defaults={"is_super": True}))
        self.assertIn("is_super", str(ctx.exception))

    def test_defaults_values_are_coerced_by_the_factory(self):
        canonical = validate_template(template_payload(defaults={
            "enabled": "false",
            "summarize_threshold": "8",
            "kb_organizer_mode": "on",
            "name": "Named Agent",
        }))
        self.assertEqual(canonical["defaults"]["enabled"], 0)
        self.assertEqual(canonical["defaults"]["summarize_threshold"], 8)
        self.assertEqual(canonical["defaults"]["kb_organizer_mode"], "agentic")
        self.assertEqual(canonical["defaults"]["name"], "Named Agent")
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(defaults={"summarize_threshold": "abc"}))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(defaults={"memory_engine": "sqlite"}))

    def test_variable_declarations(self):
        canonical = validate_template(template_payload(variables=[
            {"key": "API_TOKEN", "is_secret": True, "required": True, "description": "d"},
            {"key": "GREETING", "default": "hi"},
        ]))
        self.assertEqual(canonical["variables"][0]["key"], "API_TOKEN")
        self.assertTrue(canonical["variables"][0]["is_secret"])
        self.assertEqual(canonical["variables"][1]["default"], "hi")

    def test_secret_variable_must_not_embed_a_value(self):
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(variables=[
                {"key": "S", "is_secret": True, "default": "leaked"},
            ]))
        self.assertIn("declarations only", str(ctx.exception))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(variables=[
                {"key": "S", "is_secret": True, "value": "leaked"},
            ]))

    def test_variable_keys_and_uniqueness(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(variables=[{"key": "not a key"}]))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(variables=[{"key": "A"}, {"key": "A"}]))

    def test_kb_file_paths_are_traversal_guarded(self):
        for bad_path in ("../evil.md", "/etc/passwd", "a\\b.md", "..", "."):
            with self.assertRaises(TemplateValidationError, msg=bad_path) as ctx:
                validate_template(template_payload(kb_files={bad_path: "x"}))
            self.assertIn("Knowledge-base path", str(ctx.exception))

    def test_kb_file_paths_are_normalised_and_deduplicated(self):
        canonical = validate_template(template_payload(kb_files={"a/../b.md": "content"}))
        self.assertEqual(canonical["kb_files"], {"b.md": "content"})
        with self.assertRaises(TemplateValidationError) as ctx:
            validate_template(template_payload(kb_files={"a/../b.md": "1", "b.md": "2"}))
        self.assertIn("Duplicate", str(ctx.exception))

    def test_kb_file_content_is_bounded_and_textual(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(kb_files={"a.md": 5}))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(
                kb_files={"a.md": "x" * (MAX_KB_FILE_LENGTH + 1)}
            ))

    def test_tools_and_skills_must_be_unique_non_empty_strings(self):
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(tools="read_file"))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(tools=[""]))
        with self.assertRaises(TemplateValidationError):
            validate_template(template_payload(skills=[5]))
        canonical = validate_template(template_payload(tools=["read_file", "read_file"]))
        self.assertEqual(canonical["tools"], ["read_file"])

    def test_placeholder_must_be_declared_inside_identity_defaults_too(self):
        with self.assertRaises(TemplateRenderError) as ctx:
            validate_template(template_payload(defaults={"name": "{{ghost}}"}))
        self.assertIn("ghost", str(ctx.exception))

    def test_placeholder_audit_covers_only_system_prompt_and_identity(self):
        # A placeholder in ``description`` is NOT rendered (only name/description
        # of *defaults* are), so the template's own description is literal text.
        canonical = validate_template(template_payload(description="{{literal}}"))
        self.assertEqual(canonical["description"], "{{literal}}")


# ----------------------------------------------------------------------
# Storage: CRUD, precedence, collisions, traversal
# ----------------------------------------------------------------------


class StorageTests(TemplateTestCase):
    def test_create_and_get_round_trip(self):
        created = self.create()
        self.assertTrue(os.path.isfile(self.canonical_path("support_bot")))
        self.assertEqual(created["id"], "support_bot")
        self.assertEqual(created["_meta"], {
            "source": "agent_templates",
            "legacy": False,
            "writable": True,
            "file": "support_bot.json",
        })
        stored = json.loads(self.read_text(self.canonical_path("support_bot")))
        self.assertNotIn("_meta", stored)
        self.assertEqual(stored, validate_template(template_payload()))

    def test_create_writes_no_partial_or_temp_files(self):
        self.create()
        self.assertEqual(self.template_files(), ["support_bot.json"])

    def test_create_refuses_to_overwrite(self):
        self.create()
        with self.assertRaises(TemplateExistsError):
            self.create(template_payload(description="changed"))
        self.assertEqual(
            get_template("support_bot", base_dir=self.tmp)["description"],
            "Answers customer questions.",
        )

    def test_update_merges_top_level_fields(self):
        self.create()
        updated = update_template(
            "support_bot", {"description": "New description"}, base_dir=self.tmp
        )
        self.assertEqual(updated["description"], "New description")
        self.assertEqual(updated["name"], "Support Bot")
        self.assertEqual(updated["tools"], ["read_file", "calculator"])
        reloaded = get_template("support_bot", base_dir=self.tmp)
        self.assertEqual(reloaded["description"], "New description")

    def test_failed_update_leaves_the_file_byte_identical(self):
        self.create()
        path = self.canonical_path("support_bot")
        before = self.read_text(path)
        with self.assertRaises(TemplateRenderError):
            update_template("support_bot", {"system_prompt": "Broken {{ghost}}"}, base_dir=self.tmp)
        self.assertEqual(self.read_text(path), before)
        self.assertEqual(self.template_files(), ["support_bot.json"])

    def test_update_cannot_rename_the_id(self):
        self.create()
        with self.assertRaises(TemplateValidationError):
            update_template("support_bot", {"id": "renamed"}, base_dir=self.tmp)

    def test_update_and_delete_missing_template(self):
        with self.assertRaises(TemplateNotFoundError):
            update_template("ghost", {"name": "x"}, base_dir=self.tmp)
        with self.assertRaises(TemplateNotFoundError):
            delete_template("ghost", base_dir=self.tmp)

    def test_delete_removes_only_the_canonical_file(self):
        self.create()
        self.assertTrue(delete_template("support_bot", base_dir=self.tmp))
        self.assertFalse(has_template("support_bot", base_dir=self.tmp))
        with self.assertRaises(TemplateNotFoundError):
            delete_template("support_bot", base_dir=self.tmp)

    def test_has_template_and_get_raise_for_unknown_ids(self):
        self.assertFalse(has_template("nope", base_dir=self.tmp))
        with self.assertRaises(TemplateNotFoundError):
            get_template("nope", base_dir=self.tmp)

    def test_id_path_traversal_is_blocked(self):
        for bad_id in ("../evil", "..", "../../etc/passwd", "a/b", "a\\b", "/etc/passwd", ""):
            with self.assertRaises(TemplateValidationError, msg=bad_id):
                get_template(bad_id, base_dir=self.tmp)
            with self.assertRaises(TemplateValidationError, msg=bad_id):
                create_template(template_payload(id=bad_id), base_dir=self.tmp)
            with self.assertRaises(TemplateValidationError, msg=bad_id):
                delete_template(bad_id, base_dir=self.tmp)
        self.assertEqual(self.template_files(), [])
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "evil.json")))

    def test_slug_re_matches_legacy_skillset_ids(self):
        for legacass_id in ("coder", "customer_service", "fullstack_dev", "data_analyst"):
            self.assertIsNotNone(SLUG_RE.fullmatch(legacass_id))

    def test_symlinked_template_file_is_refused(self):
        outside = os.path.join(self.tmp, "outside.json")
        self._write_json(outside, template_payload(id="linked"))
        os.symlink(outside, self.canonical_path("linked"))
        self.assertFalse(has_template("linked", base_dir=self.tmp))
        with self.assertRaises(TemplateValidationError):
            get_template("linked", base_dir=self.tmp)
        self.assertEqual([t["id"] for t in list_templates(base_dir=self.tmp)], [])

    def test_invalid_files_are_reported_not_fatal(self):
        with open(self.canonical_path("broken"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.write_canonical(template_payload(id="mismatch"), filename="other_name")
        self.write_canonical(template_payload(id="good"))
        entries = {entry["id"]: entry for entry in list_templates(base_dir=self.tmp)}
        self.assertEqual(sorted(entries), ["broken", "good", "other_name"])
        self.assertFalse(entries["broken"]["valid"])
        self.assertIn("JSON", entries["broken"]["error"])
        self.assertFalse(entries["other_name"]["valid"])
        self.assertIn("declares id", entries["other_name"]["error"])
        self.assertTrue(entries["good"]["valid"])
        # The valid entry still loads, the broken ones raise a clear error.
        self.assertEqual(get_template("good", base_dir=self.tmp)["id"], "good")
        with self.assertRaises(TemplateValidationError):
            get_template("other_name", base_dir=self.tmp)

    def test_list_templates_sorts_by_id(self):
        self.create(template_payload(id="zeta"))
        self.create(template_payload(id="alpha"))
        everything = list_templates(base_dir=self.tmp)
        self.assertEqual([t["id"] for t in everything], ["alpha", "zeta"])

    def test_missing_roots_are_treated_as_empty(self):
        empty = tempfile.mkdtemp(prefix="agent_templates_empty_")
        try:
            self.assertEqual(list_templates(base_dir=empty), [])
            self.assertEqual(list_collisions(base_dir=empty), [])
            self.assertFalse(has_template("anything", base_dir=empty))
            with self.assertRaises(TemplateNotFoundError):
                get_template("anything", base_dir=empty)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_default_base_dir_is_the_repository_root(self):
        self.assertEqual(
            os.path.realpath(agent_templates.templates_dir()),
            os.path.realpath(os.path.join(REPO_ROOT, "agent_templates")),
        )


# ----------------------------------------------------------------------
# Resolution report / dependency checking
# ----------------------------------------------------------------------


class ResolutionTests(TemplateTestCase):
    def test_report_lists_resolved_and_missing_dependencies(self):
        self.create(template_payload(
            tools=["read_file", "ghost_tool"], skills=["github", "ghost_skill"]
        ))
        report = resolve_template("support_bot", base_dir=self.tmp)
        self.assertTrue(report["deps_checked"]["tools"])
        self.assertTrue(report["deps_checked"]["skills"])
        self.assertEqual(report["tools"]["resolved"], ["read_file"])
        self.assertEqual(report["tools"]["missing"], ["ghost_tool"])
        self.assertEqual(report["skills"]["resolved"], ["github"])
        self.assertEqual(report["skills"]["missing"], ["ghost_skill"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["template"]["id"], "support_bot")

    def test_report_is_ok_for_available_dependencies(self):
        self.create()
        report = resolve_template("support_bot", base_dir=self.tmp)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["tools"]["missing"], [])
        self.assertEqual(report["skills"]["missing"], [])

    def test_report_flags_unused_and_required_parameters(self):
        self.create(template_payload(
            parameters=[
                {"name": "company", "label": "C", "type": "text", "required": True},
                {"name": "ghost", "type": "text"},
            ],
            system_prompt="Hello {{company}}.",
        ))
        report = resolve_template("support_bot", base_dir=self.tmp)
        self.assertEqual(report["parameters"]["declared"], ["company", "ghost"])
        self.assertEqual(report["parameters"]["unused"], ["ghost"])
        self.assertEqual(report["parameters"]["required"], ["company"])
        self.assertEqual(report["parameters"]["required_without_value"], ["company"])

    def test_report_flags_unresolved_required_variables(self):
        self.create(template_payload(variables=[
            {"key": "API_TOKEN", "is_secret": True, "required": True,
             "description": "Token used to authenticate."},
            {"key": "REGION", "description": "Optional region override."},
        ]))
        report = resolve_template("support_bot", base_dir=self.tmp)
        self.assertEqual(report["variables"]["declared"], ["API_TOKEN", "REGION"])
        self.assertEqual(report["variables"]["required"], ["API_TOKEN"])
        self.assertEqual(report["variables"]["unresolved"], ["API_TOKEN"])

    def test_report_for_unavailable_registry_degrades_gracefully(self):
        self.create()
        with unittest.mock.patch.object(
            agent_templates, "_available_tool_ids", return_value=None
        ), unittest.mock.patch.object(
            agent_templates, "_available_skill_ids", return_value=None
        ):
            report = resolve_template("support_bot", base_dir=self.tmp)
        self.assertFalse(report["deps_checked"]["tools"])
        self.assertFalse(report["deps_checked"]["skills"])
        self.assertEqual(report["tools"]["missing"], [])
        self.assertTrue(report["ok"])

    def test_undeclared_override_keys_are_rejected(self):
        self.create()
        with self.assertRaises(TemplateValidationError) as ctx:
            create_agent_from_template(
                "support_bot", base_dir=self.tmp, overrides={"bogus": 1}
            )
        self.assertIn("bogus", str(ctx.exception))
        self.assertNotIn("bogus", OVERRIDE_KEYS)


# ----------------------------------------------------------------------
# Instantiation (end to end, through backend.agent_factory)
# ----------------------------------------------------------------------


class InstantiationTests(TemplateTestCase):
    def kb_content(self, agent_id, relative):
        path = os.path.join(self.tmp, "agents", agent_id, "kb", relative)
        return self.read_text(path)

    def system_prompt(self, agent_id):
        return self.read_text(os.path.join(self.tmp, "agents", agent_id, "SYSTEM.md"))

    def test_instantiate_with_params_and_secret_variables(self):
        self.create()
        agent_id = create_agent_from_template(
            "support_bot",
            params={"company": "Globex", "tone": "formal", "retries": 5, "verbose": True},
            overrides={"variables": {"API_TOKEN": "s3cr3t-value"}},
            db=db,
            base_dir=self.tmp,
        )
        self.assertEqual(agent_id, "support_bot")
        agent = db.get_agent(agent_id)
        self.assertEqual(agent["name"], "Support Bot")
        self.assertEqual(agent["description"], "Answers customer questions.")
        self.assertEqual(agent["sandbox_enabled"], 1)
        self.assertEqual(agent["artifacts_enabled"], 0)
        self.assertEqual(
            self.system_prompt(agent_id),
            "You support Globex in a formal tone (retries=5, verbose=true).",
        )
        self.assertEqual(self.kb_content(agent_id, "guide/start.md"), "# Start\n")
        variables = {v["key"]: v for v in db.get_agent_variables(agent_id)}
        self.assertEqual(variables["API_TOKEN"]["value"], "s3cr3t-value")
        self.assertTrue(variables["API_TOKEN"]["is_secret"])
        tools = set(db.get_agent_tools(agent_id))
        self.assertIn("read_file", tools)
        self.assertIn("calculator", tools)
        # artifacts_enabled=0 prunes the managed artifact tools.
        self.assertEqual(tools & ARTIFACT_TOOLS, set())
        self.assertEqual(db.get_agent_skills(agent_id), ["github"])

    def test_secret_values_never_reach_the_template_file(self):
        self.create()
        create_agent_from_template(
            "support_bot",
            overrides={"variables": {"API_TOKEN": "super-secret"}},
            db=db,
            base_dir=self.tmp,
        )
        stored = self.read_text(self.canonical_path("support_bot"))
        self.assertNotIn("super-secret", stored)
        self.assertNotIn("super-secret", self.system_prompt("support_bot"))

    def test_identity_defaults_are_rendered_and_defaults_flow_through(self):
        self.create(template_payload(
            parameters=[{"name": "who", "type": "text", "required": True, "default": "Acme"}],
            system_prompt="Hi {{who}}",
            defaults={"name": "{{who}} Helper", "description": "Helper for {{who}}", "enabled": 0},
        ))
        agent_id = create_agent_from_template(
            "support_bot", params={"who": "Globex"}, db=db, base_dir=self.tmp
        )
        self.assertEqual(agent_id, "globex_helper")
        agent = db.get_agent(agent_id)
        self.assertEqual(agent["name"], "Globex Helper")
        self.assertEqual(agent["description"], "Helper for Globex")
        self.assertEqual(agent["enabled"], 0)

    def test_callers_override_defaults(self):
        self.create(template_payload(defaults={"name": "Template Name", "enabled": 0}))
        agent_id = create_agent_from_template(
            "support_bot",
            overrides={"name": "Override Name", "enabled": 1, "tools": ["calculator"]},
            db=db,
            base_dir=self.tmp,
        )
        self.assertEqual(agent_id, "override_name")
        agent = db.get_agent(agent_id)
        self.assertEqual(agent["name"], "Override Name")
        self.assertEqual(agent["enabled"], 1)
        tools = set(db.get_agent_tools(agent_id))
        self.assertIn("calculator", tools)
        self.assertNotIn("read_file", tools)

    def test_explicit_id_override_and_if_exists(self):
        self.create()
        first = create_agent_from_template(
            "support_bot", overrides={"id": "fixed_id"}, db=db, base_dir=self.tmp
        )
        self.assertEqual(first, "fixed_id")
        with self.assertRaises(AgentAlreadyExistsError):
            create_agent_from_template(
                "support_bot",
                overrides={"id": "fixed_id"},
                db=db,
                base_dir=self.tmp,
                if_exists="error",
            )

    def test_required_variable_blocks_instantiation_unless_allowed(self):
        self.create(template_payload(variables=[
            {"key": "API_TOKEN", "is_secret": True, "required": True},
        ]))
        with self.assertRaises(TemplateValidationError) as ctx:
            create_agent_from_template("support_bot", db=db, base_dir=self.tmp)
        self.assertIn("API_TOKEN", str(ctx.exception))
        agent_id = create_agent_from_template(
            "support_bot",
            db=db,
            base_dir=self.tmp,
            allow_unresolved_variables=True,
        )
        variables = {v["key"]: v for v in db.get_agent_variables(agent_id)}
        self.assertEqual(variables["API_TOKEN"]["value"], "")

    def test_missing_dependency_blocks_instantiation_unless_allowed(self):
        self.create(template_payload(tools=["ghost_tool"]))
        with self.assertRaises(TemplateResolveError) as ctx:
            create_agent_from_template("support_bot", db=db, base_dir=self.tmp)
        self.assertIn("ghost_tool", str(ctx.exception))
        self.assertEqual(ctx.exception.report["tools"]["missing"], ["ghost_tool"])
        agent_id = create_agent_from_template(
            "support_bot", db=db, base_dir=self.tmp, allow_missing_deps=True
        )
        self.assertTrue(db.get_agent(agent_id))

    def test_undeclared_variable_and_param_are_rejected(self):
        self.create()
        with self.assertRaises(TemplateValidationError) as ctx:
            create_agent_from_template(
                "support_bot", db=db, base_dir=self.tmp,
                overrides={"variables": {"NOPE": "x"}},
            )
        self.assertIn("NOPE", str(ctx.exception))
        with self.assertRaises(TemplateValidationError):
            create_agent_from_template(
                "support_bot", db=db, base_dir=self.tmp, params={"ghost": "x"}
            )

    def test_variable_list_overrides_are_supported(self):
        self.create()
        agent_id = create_agent_from_template(
            "support_bot",
            overrides={"variables": [{"key": "API_TOKEN", "value": "tok"}]},
            db=db,
            base_dir=self.tmp,
        )
        variables = {v["key"]: v for v in db.get_agent_variables(agent_id)}
        self.assertEqual(variables["API_TOKEN"]["value"], "tok")
        self.assertTrue(variables["API_TOKEN"]["is_secret"])

    def test_invalid_override_values_are_rejected_by_the_factory(self):
        self.create()
        with self.assertRaises(SpecValidationError):
            create_agent_from_template(
                "support_bot", db=db, base_dir=self.tmp, overrides={"memory_engine": "sqlite"}
            )




class PreviewTests(TemplateTestCase):
    def test_preview_renders_without_creating_anything(self):
        self.create(template_payload(defaults={"name": "{{company}} Bot"}))
        preview = preview_template("support_bot", {"company": "Globex"}, base_dir=self.tmp)
        self.assertEqual(preview["system_prompt"], "You support Globex in a friendly tone (retries=3, verbose=false).")
        self.assertEqual(preview["spec_preview"]["name"], "Globex Bot")
        self.assertEqual(preview["warnings"], [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "agents")))

    def test_preview_warns_for_pending_required_parameters(self):
        self.create(template_payload(
            parameters=[{"name": "company", "type": "text", "required": True}],
            system_prompt="Hello {{company}}",
        ))
        preview = preview_template("support_bot", base_dir=self.tmp)
        self.assertEqual(preview["system_prompt"], "Hello ")
        self.assertEqual(len(preview["warnings"]), 1)
        self.assertIn("company", preview["warnings"][0])
        with self.assertRaises(TemplateValidationError):
            render_system_prompt(
                get_template("support_bot", base_dir=self.tmp), {}, strict=True
            )


# ----------------------------------------------------------------------
# Directory-form templates (additive: meta.json + system.md + kb/**)
# ----------------------------------------------------------------------


class DirectoryTemplateTests(TemplateTestCase):
    """The additive directory form: ``<id>/meta.json`` + ``<id>/system.md`` + ``kb/**``.

    The single-file ``<id>.json`` form stays valid forever; the directory form
    only changes *where* the prompt and the knowledge base live on disk.  Every
    test here proves the loader re-inlines a directory template into the exact
    same canonical mapping the JSON loader returns, and that the writers keep the
    two shapes independent.
    """

    def dir_path(self, template_id):
        return os.path.join(self.templates_dir, template_id)

    def write_dir_template(self, template_id, *, meta=None, prompt=None,
                           kb=None, prompt_name="system.md"):
        base = self.dir_path(template_id)
        os.makedirs(os.path.join(base, "kb"), exist_ok=True)
        if prompt is not None:
            path = os.path.join(base, *prompt_name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(prompt)
        for relative, content in (kb or {}).items():
            path = os.path.join(base, "kb", *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        if meta is not None:
            self._write_json(os.path.join(base, "meta.json"), meta)
        return base

    def dir_meta(self, template_id="support_bot", **overrides):
        """The ``meta.json`` body for *template_id* (never inline prompt/kb)."""
        payload = template_payload(id=template_id)
        payload.pop("system_prompt")
        payload.pop("kb_files")
        payload.update(overrides)
        return payload

    def snapshot_dir(self, template_id):
        """Content hash of every file in a directory template."""
        base = self.dir_path(template_id)
        hashes = {}
        for dirpath, _dirnames, names in os.walk(base):
            for name in names:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as handle:
                    hashes[os.path.relpath(full, base)] = hashlib.sha256(
                        handle.read()).hexdigest()
        return hashes

    def summary(self, template_id):
        return {s["id"]: s for s in list_templates(base_dir=self.tmp)}[template_id]

    # -- discovery ----------------------------------------------------

    def test_directory_template_is_discovered_and_loads_like_the_file_form(self):
        self.write_dir_template(
            "support_bot",
            meta=self.dir_meta(),
            prompt="You support {{company}} in a {{tone}} tone.",
            kb={"guide/start.md": "# Start\n"},
        )
        loaded = get_template("support_bot", base_dir=self.tmp)
        self.assertEqual(loaded["id"], "support_bot")
        self.assertEqual(loaded["system_prompt"],
                         "You support {{company}} in a {{tone}} tone.")
        self.assertEqual(loaded["kb_files"], {"guide/start.md": "# Start\n"})
        self.assertEqual(loaded["_meta"]["file"], None)
        self.assertTrue(loaded["_meta"]["writable"])
        self.assertFalse(loaded["_meta"]["legacy"])
        # No ``<id>.json`` was created: the directory is the only artifact.
        self.assertEqual(self.template_files(), ["support_bot"])
        self.assertFalse(os.path.exists(self.canonical_path("support_bot")))

    def test_list_templates_reports_the_shape_and_keeps_file_for_compat(self):
        self.create()                       # support_bot.json  -> shape "file"
        self.write_dir_template(
            "dir_bot", meta=self.dir_meta("dir_bot"), prompt="Hi", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertEqual(entries["support_bot"]["shape"], "file")
        self.assertEqual(entries["support_bot"]["file"], "support_bot.json")
        self.assertEqual(entries["dir_bot"]["shape"], "dir")
        self.assertIsNone(entries["dir_bot"]["file"])
        self.assertTrue(entries["dir_bot"]["valid"])
        self.assertTrue(has_template("dir_bot", base_dir=self.tmp))

    def test_directory_without_meta_json_is_invalid_not_fatal(self):
        os.makedirs(os.path.join(self.dir_path("half_built"), "kb"), exist_ok=True)
        with open(os.path.join(self.dir_path("half_built"), "kb", "x.md"),
                  "w", encoding="utf-8") as handle:
            handle.write("content")
        self.create()
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertEqual(sorted(entries), ["half_built", "support_bot"])
        self.assertFalse(entries["half_built"]["valid"])
        self.assertIn("meta.json", entries["half_built"]["error"])
        # The healthy template is unaffected.
        self.assertTrue(entries["support_bot"]["valid"])

    # -- meta.json contract -------------------------------------------

    def test_declared_id_must_match_the_directory_name(self):
        self.write_dir_template(
            "other_name", meta=self.dir_meta("support_bot"), prompt="Hi", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["other_name"]["valid"])
        self.assertIn("does not match", entries["other_name"]["error"])
        with self.assertRaises(TemplateValidationError):
            get_template("other_name", base_dir=self.tmp)

    def test_inline_prompt_and_kb_files_are_forbidden_in_meta_json(self):
        meta = self.dir_meta()
        meta["system_prompt"] = "inline"
        self.write_dir_template("support_bot", meta=meta,
                                prompt="from file", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("system_prompt", entries["support_bot"]["error"])

        shutil.rmtree(self.dir_path("support_bot"))
        meta = self.dir_meta()
        meta["kb_files"] = {"a.md": "x"}
        self.write_dir_template("support_bot", meta=meta, prompt="p", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("kb_files", entries["support_bot"]["error"])

    def test_meta_json_rejects_unsupported_keys(self):
        meta = self.dir_meta()
        meta["surprise"] = "x"
        self.write_dir_template("support_bot", meta=meta, prompt="p", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("surprise", entries["support_bot"]["error"])

    def test_missing_prompt_file_is_reported(self):
        self.write_dir_template("support_bot", meta=self.dir_meta(),
                                prompt=None, kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("system.md", entries["support_bot"]["error"])

    # -- safety: traversal, symlinks, extensions, caps -----------------

    def test_prompt_file_traversal_is_refused(self):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret")
        meta = self.dir_meta()
        meta["prompt_file"] = "../outside.md"
        self.write_dir_template("support_bot", meta=meta, prompt="p", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("prompt_file", entries["support_bot"]["error"])

    def test_prompt_file_override_is_honoured(self):
        meta = self.dir_meta()
        meta["prompt_file"] = "prompt/agent.md"
        self.write_dir_template("support_bot", meta=meta,
                                prompt="Custom prompt.", kb={},
                                prompt_name="prompt/agent.md")
        loaded = get_template("support_bot", base_dir=self.tmp)
        self.assertEqual(loaded["system_prompt"], "Custom prompt.")

    def test_symlinked_prompt_file_is_refused(self):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret")
        base = self.dir_path("support_bot")
        os.makedirs(base, exist_ok=True)
        os.symlink(outside, os.path.join(base, "system.md"))
        self.write_dir_template("support_bot", meta=self.dir_meta(), kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("symbolic link", entries["support_bot"]["error"])

    def test_symlinked_template_directory_is_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        os.symlink(outside, self.dir_path("linked"))
        self.assertFalse(has_template("linked", base_dir=self.tmp))
        with self.assertRaises(TemplateValidationError):
            get_template("linked", base_dir=self.tmp)

    def test_symlinked_kb_entry_is_refused(self):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret")
        self.write_dir_template("support_bot", meta=self.dir_meta(),
                                prompt="p", kb={})
        os.symlink(outside, os.path.join(self.dir_path("support_bot"), "kb",
                                         "leak.md"))
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("symbolic link", entries["support_bot"]["error"])

    def test_symlinked_kb_directory_is_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        self.write_dir_template("support_bot", meta=self.dir_meta(),
                                prompt="p", kb={})
        os.symlink(outside, os.path.join(self.dir_path("support_bot"), "kb",
                                         "linked_dir"))
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("symbolic link", entries["support_bot"]["error"])

    def test_disallowed_kb_extension_is_refused(self):
        self.write_dir_template("support_bot", meta=self.dir_meta(),
                                prompt="p", kb={"config.json": "{}"})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("config.json", entries["support_bot"]["error"])

    def test_dotfiles_and_dot_directories_are_skipped(self):
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(), prompt="p",
            kb={".gitkeep": "", "guide/start.md": "ok"},
        )
        os.makedirs(os.path.join(self.dir_path("support_bot"), "kb", ".hidden"),
                    exist_ok=True)
        with open(os.path.join(self.dir_path("support_bot"), "kb", ".hidden", "x.md"),
                  "w", encoding="utf-8") as handle:
            handle.write("nope")
        loaded = get_template("support_bot", base_dir=self.tmp)
        self.assertEqual(loaded["kb_files"], {"guide/start.md": "ok"})

    def test_oversized_kb_file_is_refused(self):
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(), prompt="p",
            kb={"big.md": "x" * (MAX_KB_FILE_LENGTH + 1)},
        )
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("too large", entries["support_bot"]["error"])

    def test_oversized_prompt_file_is_refused(self):
        from backend.agent_templates import MAX_TEMPLATE_BYTES
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(),
            prompt="x" * (MAX_TEMPLATE_BYTES + 1), kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("too large", entries["support_bot"]["error"])

    def test_too_many_kb_files_is_refused(self):
        from backend.agent_templates import MAX_TEMPLATE_ITEMS
        kb = {"file-%03d.md" % index: "x" for index in range(MAX_TEMPLATE_ITEMS + 1)}
        self.write_dir_template("support_bot", meta=self.dir_meta(),
                                prompt="p", kb=kb)
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("too many", entries["support_bot"]["error"])

    # -- content semantics --------------------------------------------

    def test_kb_content_is_copied_verbatim(self):
        """KB placeholders stay literal: no rendering and no audit."""
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(),
            prompt="You support {{company}}.",
            kb={"notes.md": "Tone for {{company}} is {{tone}} and {{unknown}} stays."},
        )
        loaded = get_template("support_bot", base_dir=self.tmp)
        self.assertEqual(
            loaded["kb_files"]["notes.md"],
            "Tone for {{company}} is {{tone}} and {{unknown}} stays.",
        )
        # ``{{unknown}}`` is undeclared, yet the template is valid: the audit
        # never looks at knowledge-base content.
        self.assertTrue(self.summary("support_bot")["valid"])
        self.assertEqual(loaded["system_prompt"], "You support {{company}}.")

    def test_prompt_placeholders_are_still_rendered(self):
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(),
            prompt="You support {{company}} in a {{tone}} tone.",
            kb={},
        )
        rendered = render_system_prompt(
            get_template("support_bot", base_dir=self.tmp), {}, strict=True)
        self.assertEqual(rendered, "You support Acme in a friendly tone.")

    def test_undeclared_prompt_placeholder_is_still_a_hard_error(self):
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(),
            prompt="Hello {{ghost}}.", kb={})
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("ghost", entries["support_bot"]["error"])

    # -- both shapes at once ------------------------------------------

    def test_both_shapes_for_one_id_is_a_hard_error(self):
        self.write_canonical(template_payload(id="support_bot"))
        self.write_dir_template(
            "support_bot", meta=self.dir_meta(), prompt="from dir", kb={})
        collisions = list_collisions(base_dir=self.tmp)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["id"], "support_bot")
        self.assertEqual(collisions[0]["kind"], "canonical_shape")
        self.assertEqual(collisions[0]["canonical_file"], "support_bot.json")
        self.assertEqual(collisions[0]["canonical_dir"], "support_bot")
        self.assertIn("both", collisions[0]["message"])

        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertFalse(entries["support_bot"]["valid"])
        self.assertIn("both", entries["support_bot"]["error"])
        with self.assertRaises(TemplateValidationError):
            get_template("support_bot", base_dir=self.tmp)
        with self.assertRaises(TemplateExistsError):
            create_template(template_payload(id="support_bot"), base_dir=self.tmp)
        with self.assertRaises(TemplateValidationError):
            update_template("support_bot", {"name": "Nope"}, base_dir=self.tmp)

    def test_only_the_shape_collision_is_reported(self):
        self.create()
        self.write_dir_template("dir_bot", meta=self.dir_meta("dir_bot"),
                                prompt="Hi", kb={})
        self.assertEqual(list_collisions(base_dir=self.tmp), [])

    # -- writers ------------------------------------------------------

    def test_create_update_delete_round_trip_on_the_directory_shape(self):
        created = create_template(
            template_payload(id="dir_bot", system_prompt="Hi {{company}}",
                             kb_files={"guide/start.md": "# Start\n"}),
            base_dir=self.tmp, shape="dir",
        )
        self.assertEqual(created["id"], "dir_bot")
        self.assertEqual(created["_meta"]["file"], None)
        # meta.json + system.md + kb/guide/start.md, and no <id>.json.
        self.assertEqual(sorted(os.listdir(self.dir_path("dir_bot"))),
                         ["kb", "meta.json", "system.md"])
        self.assertEqual(self.template_files(), ["dir_bot"])
        stored = json.loads(self.read_text(
            os.path.join(self.dir_path("dir_bot"), "meta.json")))
        self.assertNotIn("system_prompt", stored)
        self.assertNotIn("kb_files", stored)
        self.assertEqual(stored["id"], "dir_bot")
        # Clean staging: nothing left behind that discovery could pick up.
        self.assertEqual(self.template_files(), ["dir_bot"])

        updated = update_template(
            "dir_bot", {"description": "Now described."}, base_dir=self.tmp)
        self.assertEqual(updated["description"], "Now described.")
        self.assertEqual(updated["system_prompt"], "Hi {{company}}")
        self.assertEqual(updated["kb_files"], {"guide/start.md": "# Start\n"})
        self.assertEqual(updated["_meta"]["file"], None)

        self.assertTrue(delete_template("dir_bot", base_dir=self.tmp))
        self.assertFalse(os.path.exists(self.dir_path("dir_bot")))
        self.assertEqual(self.template_files(), [])

    def test_create_defaults_to_the_single_file_shape(self):
        created = self.create()
        self.assertEqual(created["_meta"]["file"], "support_bot.json")
        self.assertTrue(os.path.isfile(self.canonical_path("support_bot")))
        self.assertFalse(os.path.isdir(self.dir_path("support_bot")))

    def test_create_rejects_an_unknown_shape(self):
        with self.assertRaises(TemplateValidationError):
            create_template(template_payload(), base_dir=self.tmp, shape="zip")
        self.assertEqual(self.template_files(), [])

    def test_update_prunes_dropped_kb_files(self):
        create_template(
            template_payload(id="dir_bot", kb_files={"a.md": "a", "b.md": "b"}),
            base_dir=self.tmp, shape="dir",
        )
        update_template(
            "dir_bot", {"kb_files": {"a.md": "a2"}}, base_dir=self.tmp)
        loaded = get_template("dir_bot", base_dir=self.tmp)
        self.assertEqual(loaded["kb_files"], {"a.md": "a2"})
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.dir_path("dir_bot"), "kb"))),
            ["a.md"])

    def test_update_without_kb_files_never_prunes(self):
        create_template(
            template_payload(id="dir_bot", kb_files={"a.md": "a", "b.md": "b"}),
            base_dir=self.tmp, shape="dir",
        )
        update_template("dir_bot", {"name": "Renamed"}, base_dir=self.tmp)
        loaded = get_template("dir_bot", base_dir=self.tmp)
        self.assertEqual(sorted(loaded["kb_files"]), ["a.md", "b.md"])

    def test_failed_update_leaves_the_directory_byte_identical(self):
        create_template(
            template_payload(id="dir_bot", system_prompt="Hi",
                             kb_files={"a.md": "a"}),
            base_dir=self.tmp, shape="dir",
        )
        before = self.snapshot_dir("dir_bot")
        with self.assertRaises(TemplateValidationError):
            update_template("dir_bot", {"kb_files": {"a.md": "a", "pickle.json": "{}"}},
                            base_dir=self.tmp)
        with self.assertRaises(TemplateRenderError):
            update_template("dir_bot", {"system_prompt": "Hello {{ghost}}."},
                            base_dir=self.tmp)
        self.assertEqual(self.snapshot_dir("dir_bot"), before)
        self.assertEqual(get_template("dir_bot", base_dir=self.tmp)["kb_files"],
                         {"a.md": "a"})

    def test_delete_uses_rmtree_for_nested_kb_directories(self):
        create_template(
            template_payload(id="dir_bot", kb_files={"deep/nested/leaf.md": "x"}),
            base_dir=self.tmp, shape="dir",
        )
        self.assertTrue(os.path.isfile(
            os.path.join(self.dir_path("dir_bot"), "kb", "deep", "nested", "leaf.md")))
        self.assertTrue(delete_template("dir_bot", base_dir=self.tmp))
        self.assertFalse(os.path.exists(self.dir_path("dir_bot")))
        self.assertEqual(list_collisions(base_dir=self.tmp), [])

    def test_the_two_shapes_coexist_for_different_ids(self):
        """A directory template never disturbs a single-file one."""
        self.create()
        create_template(template_payload(id="dir_bot"), base_dir=self.tmp,
                        shape="dir")
        entries = {e["id"]: e for e in list_templates(base_dir=self.tmp)}
        self.assertEqual(sorted(entries), ["dir_bot", "support_bot"])
        self.assertTrue(entries["dir_bot"]["valid"])
        self.assertTrue(entries["support_bot"]["valid"])
        self.assertEqual(entries["dir_bot"]["shape"], "dir")
        self.assertEqual(entries["support_bot"]["shape"], "file")
        self.assertEqual(list_collisions(base_dir=self.tmp), [])
        self.assertEqual(self.template_files(), ["dir_bot", "support_bot.json"])

    def test_create_dir_rejects_a_disallowed_kb_extension(self):
        """The writer enforces the same allowlist as the loader."""
        with self.assertRaises(TemplateValidationError):
            create_template(
                template_payload(id="dir_bot", kb_files={"config.json": "{}"}),
                base_dir=self.tmp, shape="dir")
        self.assertEqual(self.template_files(), [])

    def test_create_dir_never_leaves_a_staging_directory(self):
        create_template(template_payload(id="dir_bot", kb_files={"a.md": "a"}),
                        base_dir=self.tmp, shape="dir")
        # Only the template directory is left; no ``.stage-*`` temp remains.
        self.assertEqual(self.template_files(), ["dir_bot"])

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
