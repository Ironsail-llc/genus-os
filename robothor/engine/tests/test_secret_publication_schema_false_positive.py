"""A field NAMED after a credential is not a credential.

`no_secret_publication` scans prior tool output, which is the right evidence:
the realistic shape is "read the repo, then commit it". But the assignment
detector behind it treats `"<credential-name>": { ... }` as an assignment and
then, backtracking through the optional type-annotation branch, adopts a
quoted token from deep inside the nested object as the "value".

The result is that ordinary CRM work trips it. `list_tasks` / `list_my_tasks`
return task descriptions, and a task about a connector's auth schema contains
the literal field name `access-token` followed by its JSON schema — no
credential anywhere. Measured: 14 hard blocks in 48 hours on runs whose only
"secret" was a schema.

The two properties held here pull in opposite directions, which is why both
are pinned: a schema DECLARATION must not block, and a real key in a file the
agent just read must still block. Loosening the detector until the first
passes is only correct if the second still fails.
"""

from __future__ import annotations

from robothor.engine.guardrails import (
    GuardrailEngine,
    _first_assigned_credential,
    redact_secrets,
)

# Fake, but shaped like the real thing so the production patterns match.
FAKE_OPENAI_KEY = "sk-" + "a1b2c3d4e5" * 4


class _Step:
    """Minimal stand-in for an agent_run step."""

    def __init__(self, tool_name: str, tool_output: object) -> None:
        self.tool_name = tool_name
        self.tool_output = tool_output


def _engine() -> GuardrailEngine:
    return GuardrailEngine(enabled_policies=["no_secret_publication"])


def _task_list_with_schema() -> list[_Step]:
    """What `list_tasks` actually returned on the blocked runs."""
    return [
        _Step(
            "list_tasks",
            {
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "Document the connector auth schema",
                        "description": (
                            'Fields the bridge accepts: {"access-token": '
                            '{"type": "string", "format": "opaque-bearer"}, '
                            '"region": {"type": "string"}}'
                        ),
                        "status": "open",
                    }
                ]
            },
        )
    ]


class TestASchemaFieldNameDoesNotBlock:
    def test_list_tasks_schema_output_does_not_block_a_commit(self) -> None:
        r = _engine().check_pre_execution(
            "exec",
            {"command": "git commit -am 'update docs'"},
            agent_id="a",
            prior_steps=_task_list_with_schema(),
        )
        assert r.allowed is True, r.reason

    def test_list_my_tasks_schema_output_does_not_block_a_push(self) -> None:
        steps = _task_list_with_schema()
        steps[0].tool_name = "list_my_tasks"
        r = _engine().check_pre_execution("git_push", {}, agent_id="a", prior_steps=steps)
        assert r.allowed is True, r.reason

    def test_the_detector_reports_no_credential_for_a_schema(self) -> None:
        """The unit underneath, so a fix higher up cannot fake this."""
        assert (
            _first_assigned_credential(
                '{"access-token": {"type": "string", "format": "opaque-bearer"}}'
            )
            is None
        )
        assert _first_assigned_credential('"api_key": {"type": "string"}') is None
        assert (
            _first_assigned_credential("{'auth_token': {'type': 'string', 'nullable': True}}")
            is None
        )


class TestRealCredentialsStillBlock:
    def test_an_openai_key_in_read_file_output_still_blocks(self) -> None:
        r = _engine().check_pre_execution(
            "exec",
            {"command": "git push origin main"},
            agent_id="a",
            prior_steps=[_Step("read_file", {"content": f'API_KEY = "{FAKE_OPENAI_KEY}"\n'})],
        )
        assert r.allowed is False
        assert r.guardrail_name == "no_secret_publication"
        assert FAKE_OPENAI_KEY not in r.reason

    def test_a_json_credential_assignment_still_blocks(self) -> None:
        """`"password": "<literal>"` is an assignment, not a schema."""
        r = _engine().check_pre_execution(
            "exec",
            {"command": "git commit -am wip"},
            agent_id="a",
            prior_steps=[_Step("read_file", {"content": '{"db_password": "s3cr3t-staging-99"}'})],
        )
        assert r.allowed is False

    def test_a_typed_python_assignment_still_blocks(self) -> None:
        """The annotation branch exists for this shape and must keep working."""
        assert (
            _first_assigned_credential('client_password: str = "s3cr3t-staging-99"')
            == "client_password"
        )


class TestAQuotedAnnotationDoesNotShieldTheValue:
    """Excluding quotes from the annotation was too much medicine.

    The annotation branch stops at the first quote, so for a quoted or
    subscripted type the branch matches nothing, the `:` is read as the
    assignment, and the TYPE NAME is adopted as the value. Two ways to fail,
    both worse than the false positive being fixed:

    * `password: 'str' = 'hunter2hunter2'` — the type is 3 characters, below
      the 8-character floor, so nothing matches at all and a real credential
      sails through the publication gate; and
    * `password: "SecretStr" = "hunter2hunter2ab"` — the type is 9 characters,
      so `SecretStr` is redacted and the credential is left in the text.

    Only braces need excluding. A nested JSON object is what the annotation
    must not cross; a quote inside a type name is not.
    """

    ROWS = (
        ("password: 'str' = 'hunter2hunter2'", "password", "hunter2hunter2"),
        ('password: "SecretStr" = "hunter2hunter2ab"', "password", "hunter2hunter2ab"),
        ('api_key: Literal["prod"] = "s3cr3t-staging-99"', "api_key", "s3cr3t-staging-99"),
    )

    def test_each_row_is_detected_as_a_credential(self) -> None:
        for text, name, _secret in self.ROWS:
            assert _first_assigned_credential(text) == name, text

    def test_each_row_redacts_the_value_not_the_annotation(self) -> None:
        for text, _name, secret in self.ROWS:
            out = redact_secrets(text)
            assert secret not in out, f"the credential survived redaction: {out}"
            assert "[REDACTED" in out, out

    def test_a_typed_assignment_still_blocks_a_publish(self) -> None:
        r = _engine().check_pre_execution(
            "exec",
            {"command": "git push origin main"},
            agent_id="a",
            prior_steps=[
                _Step("read_file", {"content": 'password: "SecretStr" = "hunter2hunter2ab"'})
            ],
        )
        assert r.allowed is False
        assert "hunter2hunter2ab" not in r.reason
