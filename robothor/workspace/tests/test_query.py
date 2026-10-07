"""Gmail search syntax -> a structured MailQuery, losing nothing."""

from __future__ import annotations

import pytest

from robothor.workspace.query import parse_query
from robothor.workspace.types import AllOf, AnyOf, MailQuery, Term


def test_original_string_is_kept_verbatim() -> None:
    text = '  from:bob@example.com   is:unread "Q3 numbers" '
    assert parse_query(text).original == text


def test_empty_query() -> None:
    q = parse_query("")
    assert q == MailQuery(original="", clauses=(), raw=())
    assert q.is_structured


def test_known_operators() -> None:
    q = parse_query(
        "from:bob@example.com to:carol@example.com cc:dave@example.com subject:invoice "
        "is:unread in:inbox label:clients after:2026/09/01 before:2026/10/01 "
        "newer_than:2d older_than:1y has:attachment"
    )
    assert q.clauses == (
        Term("from", "bob@example.com"),
        Term("to", "carol@example.com"),
        Term("cc", "dave@example.com"),
        Term("subject", "invoice"),
        Term("is", "unread"),
        Term("in", "inbox"),
        Term("label", "clients"),
        Term("after", "2026/09/01"),
        Term("before", "2026/10/01"),
        Term("newer_than", "2d"),
        Term("older_than", "1y"),
        Term("has", "attachment"),
    )
    assert q.raw == ()
    assert q.is_structured


def test_operator_names_are_case_insensitive_values_kept() -> None:
    assert parse_query("FROM:Bob@Example.com").clauses == (Term("from", "Bob@Example.com"),)


def test_free_text_and_quoted_phrases() -> None:
    q = parse_query('budget "quarterly review" subject:"board pack"')
    assert q.clauses == (
        Term("text", "budget"),
        Term("phrase", "quarterly review"),
        Term("subject", "board pack"),
    )


def test_negation() -> None:
    q = parse_query("-from:noreply@example.com -label:spam -newsletter")
    assert q.clauses == (
        Term("from", "noreply@example.com", negated=True),
        Term("label", "spam", negated=True),
        Term("text", "newsletter", negated=True),
    )


def test_or_between_terms() -> None:
    q = parse_query("from:bob@example.com OR from:carol@example.com is:unread")
    assert q.clauses == (
        AnyOf((Term("from", "bob@example.com"), Term("from", "carol@example.com"))),
        Term("is", "unread"),
    )


def test_lowercase_or_is_a_word_not_an_operator() -> None:
    q = parse_query("this or that")
    assert q.clauses == (Term("text", "this"), Term("text", "or"), Term("text", "that"))


def test_braces_are_any_of() -> None:
    q = parse_query("{from:bob@example.com from:carol@example.com} subject:hi")
    assert q.clauses == (
        AnyOf((Term("from", "bob@example.com"), Term("from", "carol@example.com"))),
        Term("subject", "hi"),
    )


def test_parentheses_group_terms() -> None:
    q = parse_query("(from:bob@example.com is:unread) OR label:vip")
    assert q.clauses == (
        AnyOf(
            (
                AllOf((Term("from", "bob@example.com"), Term("is", "unread"))),
                Term("label", "vip"),
            )
        ),
    )


def test_unknown_operators_are_kept_raw_not_dropped() -> None:
    q = parse_query("filename:pdf larger:5M from:bob@example.com")
    assert q.clauses == (Term("from", "bob@example.com"),)
    assert q.raw == ("filename:pdf", "larger:5M")
    assert not q.is_structured


def test_unbalanced_input_never_raises_and_is_raw() -> None:
    q = parse_query('from:bob (is:unread "open quote')
    assert q.original == 'from:bob (is:unread "open quote'
    assert not q.is_structured


@pytest.mark.parametrize("text", ["a:b:c", "subject:(a b)", "-(a b)", "OR", "{}", ":"])
def test_awkward_shapes_never_raise(text: str) -> None:
    assert parse_query(text).original == text
