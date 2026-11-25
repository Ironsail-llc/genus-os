"""Seed linking and the activation module's contract, without a database."""

from __future__ import annotations

import pytest

from robothor.memory import activation


class TestPhrases:
    def test_a_single_token_is_never_a_candidate(self) -> None:
        assert activation.candidate_phrases("Tell Alice about it") == [
            "tell alice",
            "alice about",
            "about it",
            "tell alice about",
            "alice about it",
            "tell alice about it",
        ]
        assert activation.candidate_phrases("Alice") == []
        assert activation.candidate_phrases("Alice?!") == []

    def test_phrases_are_lowercased_and_stripped_of_punctuation(self) -> None:
        phrases = activation.candidate_phrases("Did Alice Smith call, ACME Logistics?")
        assert "alice smith" in phrases
        assert "acme logistics" in phrases
        assert all(p == p.lower() for p in phrases)

    def test_a_possessive_names_the_owner(self) -> None:
        """ "Dave Larkwick's manager" is about Dave Larkwick: the possessive must
        not glue itself to the surname (the world eval's multi-hop questions
        missed every such seed)."""
        for text in ("Who does Dave Larkwick's manager report to?", "Who is Dave Larkwick’s lead?"):
            assert "dave larkwick" in activation.candidate_phrases(text)

    def test_phrases_are_bounded(self) -> None:
        long = " ".join(f"word{i}" for i in range(500))
        phrases = activation.candidate_phrases(long)
        assert len(phrases) <= activation.MAX_PHRASES
        assert all(2 <= len(p.split()) <= activation.MAX_NAME_TOKENS for p in phrases)


class TestStrongKeys:
    def test_emails_are_extracted_lowercased(self) -> None:
        assert activation.strong_keys("mail Alice@Example.com or bob@example.com.") == [
            "alice@example.com",
            "bob@example.com",
        ]

    def test_handles_are_extracted(self) -> None:
        assert "@alice_smith" in activation.strong_keys("ping @alice_smith on chat")

    def test_plain_words_are_not_strong_keys(self) -> None:
        assert activation.strong_keys("Alice Smith said hello") == []


class TestConstants:
    def test_the_tunable_bounds_are_named(self) -> None:
        assert activation.MAX_HOPS == 2
        assert activation.HARD_MAX_HOPS == 3
        assert activation.FANOUT >= 1
        assert 0 < activation.DAMPING < 1
        assert activation.FACTS_PER_ENTITY >= 1


class TestNoSeeds:
    def test_activate_with_no_seeds_returns_nothing(self) -> None:
        def _no_db(*_a: object, **_k: object) -> object:
            raise AssertionError("no seeds must not reach the database")

        assert activation.activate("test-tenant", [], trusted_only=True, connect=_no_db) == []

    def test_seeds_from_empty_text_returns_nothing(self) -> None:
        def _no_db(*_a: object, **_k: object) -> object:
            raise AssertionError("nothing to link must not reach the database")

        assert activation.seeds_from_text("test-tenant", "  ", connect=_no_db) == []


@pytest.mark.parametrize("hops", [-1, 0, 1, 2, 3, 10])
def test_hops_are_clamped_before_the_call(hops: int) -> None:
    assert 0 <= activation.clamp_hops(hops) <= activation.HARD_MAX_HOPS


class TestOperatorLookup:
    """Without the operator's identity the walk could run through the
    operator's own node, so it does not run."""

    def _connect(self, statements: list[str]):
        from contextlib import contextmanager

        class _Cur:
            def __init__(self, conn: object) -> None:
                self.connection = conn  # as a psycopg2 cursor names its connection

            def execute(self, sql: str, _params: object = None) -> None:
                statements.append(sql)

            def fetchall(self) -> list[dict[str, object]]:
                if "memory_entities" in statements[-1]:
                    # The identity lookup's row for "Alice Smith".
                    return [
                        {
                            "id": 1,
                            "name": "Alice Smith",
                            "entity_type": "person",
                            "aliases": [],
                            "crm_person_id": None,
                            "crm_company_id": None,
                            "scope_kind": "shared",
                            "scope_id": None,
                            "norm": "alice smith",
                            "alias_norms": [],
                        }
                    ]
                return []

            def fetchone(self) -> None:
                return None

            def close(self) -> None:
                pass

        class _Conn:
            def cursor(self, cursor_factory: object = None) -> _Cur:
                return _Cur(self)

        @contextmanager
        def _connect(*_a: object, **_k: object):
            yield _Conn()

        return _connect

    def test_a_failed_lookup_raises_and_never_walks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import robothor.crm.dal

        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("pool exhausted")

        monkeypatch.setattr(robothor.crm.dal, "get_owner_person", _boom)
        statements: list[str] = []
        with pytest.raises(activation.OperatorUnknownError):
            activation.graph_candidates_for_query(
                "test-tenant", "Alice Smith", connect=self._connect(statements)
            )
        assert not any("memory_activate" in s for s in statements)

    def test_the_lookup_runs_on_the_held_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import robothor.crm.dal

        seen: dict[str, object] = {}

        def _owner(tenant_id: str, *, cur: object = None) -> dict[str, object]:
            seen["cur"] = cur
            return {"id": None}

        monkeypatch.setattr(robothor.crm.dal, "get_owner_person", _owner)
        statements: list[str] = []
        activation.graph_candidates_for_query(
            "test-tenant", "Alice Smith", connect=self._connect(statements)
        )
        assert seen["cur"] is not None, "the operator lookup opened its own connection"
        assert any("memory_activate" in s for s in statements)


class TestSlowCheckout:
    """The time limit is the time left once the connection is in hand: a
    checkout that waits (a new connection, its session settings) is not added
    on top of the budget."""

    def _run(self, monkeypatch: pytest.MonkeyPatch, checkout_seconds: float) -> list[str]:
        from contextlib import contextmanager

        import robothor.crm.dal

        clock = {"now": 0.0}  # binary-exact values: no rounding in the check

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return clock["now"]

        statements: list[str] = []

        class _Cur:
            def execute(self, sql: str, params: object = None) -> None:
                statements.append(sql % params if params and "%s" in sql else sql)

            def fetchall(self) -> list[object]:
                return []

            def fetchone(self) -> None:
                return None

        class _Conn:
            def cursor(self, cursor_factory: object = None) -> _Cur:
                return _Cur()

        @contextmanager
        def _slow_checkout(*_a: object, **_k: object):
            clock["now"] += checkout_seconds  # the pool made us wait
            yield _Conn()

        monkeypatch.setattr(activation, "time", _Clock)
        monkeypatch.setattr(robothor.crm.dal, "get_owner_person", lambda *_a, **_k: {"id": None})
        activation.recall_for_message(
            "test-tenant",
            "Did Alice Smith sign it?",
            deadline=clock["now"] + 1.0,
            connect=_slow_checkout,
        )
        return statements

    def test_the_armed_limit_is_the_time_left_after_the_checkout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        statements = self._run(monkeypatch, checkout_seconds=0.25)
        assert statements[0] == "SELECT set_config('statement_timeout', 750ms, false)", statements

    def test_a_checkout_that_outlasts_the_deadline_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._run(monkeypatch, checkout_seconds=1.5) == []
