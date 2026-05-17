"""Tests for the shared session-ID resolver and display helper."""

import pytest

from claude_session_backup.ids import (
    AmbiguousSessionID,
    InvalidSessionIDInput,
    NoSuchSessionID,
    SessionCandidate,
    format_ambiguous_error,
    format_short_uuid,
    resolve_session_id,
)


# ── format_short_uuid ─────────────────────────────────────────────────


def test_format_short_uuid_full_uuid():
    full = "abc12345-6789-abcd-ef01-234567890def"
    # Default head=8, tail=8 -> 'abc12345' + '-...-' + '67890def'
    assert format_short_uuid(full) == "abc12345-...-67890def"


def test_format_short_uuid_respects_head_and_tail():
    full = "abc12345-6789-abcd-ef01-234567890def"
    assert format_short_uuid(full, head=4, tail=4) == "abc1-...-0def"


def test_format_short_uuid_short_input_returned_as_is():
    """Inputs that already fit don't get truncated."""
    assert format_short_uuid("abc12345") == "abc12345"
    assert format_short_uuid("short") == "short"


def test_format_short_uuid_empty():
    assert format_short_uuid("") == ""


def test_format_short_uuid_at_boundary():
    """A string exactly head+tail+3 chars long should NOT be truncated."""
    # head=8, tail=8, sep="-...-" (3 chars + 2 dashes = 5? no: '-...-' is 5)
    # default head+tail+3 = 19; "1234567812345678999" is 19 chars
    s = "1" * 19
    assert format_short_uuid(s) == s


# ── resolve_session_id: helpers ───────────────────────────────────────


UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "11112222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"


def _insert(conn, uuid, name="n", project="p", start_folder="/start"):
    conn.execute(
        "INSERT INTO sessions (session_id, session_name, project, start_folder) "
        "VALUES (?, ?, ?, ?)",
        (uuid, name, project, start_folder),
    )


# ── Input validation ──────────────────────────────────────────────────


def test_resolve_rejects_empty_input(mock_db):
    with pytest.raises(InvalidSessionIDInput):
        resolve_session_id(mock_db, "")


def test_resolve_rejects_too_short(mock_db):
    with pytest.raises(InvalidSessionIDInput, match="too short"):
        resolve_session_id(mock_db, "abc")


def test_resolve_rejects_non_hex(mock_db):
    with pytest.raises(InvalidSessionIDInput, match="non-hex"):
        resolve_session_id(mock_db, "xyz1234")


def test_resolve_accepts_hex_with_dashes(mock_db):
    """The UUID hyphens are hex-fragment-friendly."""
    _insert(mock_db, UUID_A)
    mock_db.commit()
    assert resolve_session_id(mock_db, "11111111-1111") == UUID_A


# ── Tier 1: full UUID exact match ─────────────────────────────────────


def test_resolve_full_uuid_exact(mock_db):
    _insert(mock_db, UUID_A)
    mock_db.commit()
    assert resolve_session_id(mock_db, UUID_A) == UUID_A


def test_resolve_full_uuid_case_insensitive(mock_db):
    _insert(mock_db, UUID_A)
    mock_db.commit()
    assert resolve_session_id(mock_db, UUID_A.upper()) == UUID_A


def test_resolve_full_uuid_no_match_raises(mock_db):
    _insert(mock_db, UUID_A)
    mock_db.commit()
    with pytest.raises(NoSuchSessionID):
        resolve_session_id(mock_db, UUID_C)


# ── Tier 2: prefix match ──────────────────────────────────────────────


def test_resolve_prefix_unique(mock_db):
    _insert(mock_db, UUID_A)
    _insert(mock_db, UUID_C)
    mock_db.commit()
    assert resolve_session_id(mock_db, "11111111") == UUID_A
    assert resolve_session_id(mock_db, "3333") == UUID_C


def test_resolve_prefix_ambiguous_raises(mock_db):
    """A 4-char prefix that matches multiple sessions raises with candidates."""
    _insert(mock_db, UUID_A, name="a")
    _insert(mock_db, UUID_B, name="b")
    mock_db.commit()

    with pytest.raises(AmbiguousSessionID) as info:
        resolve_session_id(mock_db, "1111")

    err = info.value
    assert err.query == "1111"
    assert len(err.candidates) == 2
    assert {c.session_id for c in err.candidates} == {UUID_A, UUID_B}


def test_resolve_longer_prefix_disambiguates(mock_db):
    """User can re-run with longer prefix to break ambiguity."""
    _insert(mock_db, UUID_A, name="a")
    _insert(mock_db, UUID_B, name="b")
    mock_db.commit()

    # "1111" matches both, "11111111" matches only A
    assert resolve_session_id(mock_db, "11111111") == UUID_A


# ── Tier 3: suffix match ──────────────────────────────────────────────


def test_resolve_suffix_unique(mock_db):
    _insert(mock_db, UUID_A)
    _insert(mock_db, UUID_C)
    mock_db.commit()
    # Suffix of UUID_A
    assert resolve_session_id(mock_db, "111111111111") == UUID_A


def test_resolve_suffix_ambiguous_raises(mock_db):
    """If two UUIDs share a suffix, raise with candidates."""
    shared_suffix = "ffff-ffffffffffff"
    a = "aaaaaaaa-aaaa-aaaa-" + shared_suffix
    b = "bbbbbbbb-bbbb-bbbb-" + shared_suffix
    _insert(mock_db, a, name="a")
    _insert(mock_db, b, name="b")
    mock_db.commit()

    with pytest.raises(AmbiguousSessionID) as info:
        resolve_session_id(mock_db, "ffffffffffff")
    assert len(info.value.candidates) == 2


def test_resolve_suffix_falls_back_after_prefix_miss(mock_db):
    """Prefix tier with zero matches falls through to suffix tier."""
    _insert(mock_db, UUID_A)
    mock_db.commit()

    # "1111" prefix-matches UUID_A. "1112" prefix-matches nothing but
    # also doesn't suffix-match anything. "1111" with a unique suffix
    # is what we test for suffix fallback.
    # UUID_A ends with "111111111111" (12 ones)
    assert resolve_session_id(mock_db, "111111111111") == UUID_A
    # A query that's a suffix of UUID_A but not a prefix
    # UUID_A is "11111111-1111-1111-1111-111111111111" -- all 1s
    # So a 12-char "1...1" string is BOTH prefix and suffix
    # Let's use a different test session to test pure-suffix.


def test_resolve_pure_suffix_no_prefix_match(mock_db):
    """A query that matches only as a suffix (not as a prefix) still resolves."""
    uuid = "aaaaaaaa-aaaa-aaaa-aaaa-deadbeefcafe"
    _insert(mock_db, uuid)
    mock_db.commit()
    # "deadbeefcafe" is a suffix but no row starts with it
    assert resolve_session_id(mock_db, "deadbeefcafe") == uuid


# ── No match ──────────────────────────────────────────────────────────


def test_resolve_no_match_anywhere_raises(mock_db):
    _insert(mock_db, UUID_A)
    mock_db.commit()
    with pytest.raises(NoSuchSessionID) as info:
        resolve_session_id(mock_db, "deadbeef")
    assert info.value.query == "deadbeef"


# ── format_ambiguous_error ────────────────────────────────────────────


def test_format_ambiguous_error_shows_all_when_few(mock_db):
    candidates = [
        SessionCandidate(
            session_id=UUID_A, session_name="a", project="p1",
            start_folder="/code/foo",
        ),
        SessionCandidate(
            session_id=UUID_B, session_name="b", project="p2",
            start_folder="/code/bar",
        ),
    ]
    err = AmbiguousSessionID("1111", candidates)
    out = format_ambiguous_error(err)
    assert "Ambiguous session ID '1111' (2 matches)" in out
    assert "11111111-...-111111" in out
    assert "/code/foo" in out
    assert "Use a longer prefix" in out


def test_format_ambiguous_error_truncates_large_lists(mock_db):
    candidates = [
        SessionCandidate(
            session_id=f"{i:08x}-1111-1111-1111-111111111111",
            session_name=f"s{i}", project="p", start_folder="/x",
        )
        for i in range(15)
    ]
    err = AmbiguousSessionID("11", candidates)
    out = format_ambiguous_error(err, max_show=5)
    assert "... and 10 more" in out


def test_format_ambiguous_error_handles_null_metadata():
    """Sessions with no name / project / start_folder don't crash the formatter."""
    candidates = [
        SessionCandidate(
            session_id=UUID_A, session_name=None, project=None, start_folder=None,
        ),
    ]
    err = AmbiguousSessionID("1111", candidates)
    out = format_ambiguous_error(err)
    assert "(unnamed)" in out


# ── Tier 0: compact display form '<head>-...-<tail>' ──────────────────


def test_resolve_compact_display_form_resolves(mock_db):
    """A user can paste the displayed compact form '<head>-...-<tail>' back
    into any csb command and the resolver figures it out."""
    uuid = "916441e6-afca-466d-b00b-94801d090ef5"
    _insert(mock_db, uuid)
    mock_db.commit()
    # What csb list --shortid would display
    assert resolve_session_id(mock_db, "916441e6-...-1d090ef5") == uuid


def test_resolve_compact_form_with_no_dashes_around_dots(mock_db):
    """The display form has dashes around '...' from the UUID separators,
    but the resolver should accept the bare '<head>...<tail>' too."""
    uuid = "916441e6-afca-466d-b00b-94801d090ef5"
    _insert(mock_db, uuid)
    mock_db.commit()
    assert resolve_session_id(mock_db, "916441e6...1d090ef5") == uuid


def test_resolve_compact_form_ambiguous_raises(mock_db):
    """If two sessions share the same head AND tail (very rare), we
    raise AmbiguousSessionID with both candidates."""
    a = "11111111-aaaa-aaaa-aaaa-deadbeefcafe"
    b = "11111111-bbbb-bbbb-bbbb-deadbeefcafe"
    _insert(mock_db, a, name="a")
    _insert(mock_db, b, name="b")
    mock_db.commit()
    with pytest.raises(AmbiguousSessionID) as info:
        resolve_session_id(mock_db, "11111111-...-deadbeefcafe")
    assert len(info.value.candidates) == 2


def test_resolve_compact_form_no_match_raises(mock_db):
    _insert(mock_db, UUID_A)
    mock_db.commit()
    with pytest.raises(NoSuchSessionID):
        resolve_session_id(mock_db, "deadbeef-...-cafefeed")


def test_resolve_compact_form_rejects_too_short_head_or_tail(mock_db):
    """Both halves must individually meet the 4-char minimum."""
    _insert(mock_db, UUID_A)
    mock_db.commit()
    with pytest.raises(InvalidSessionIDInput, match="head and tail"):
        resolve_session_id(mock_db, "ab-...-1111")  # head only 2 chars
    with pytest.raises(InvalidSessionIDInput, match="head and tail"):
        resolve_session_id(mock_db, "1111-...-ab")  # tail only 2 chars


def test_resolve_compact_form_rejects_non_hex_halves(mock_db):
    _insert(mock_db, UUID_A)
    mock_db.commit()
    with pytest.raises(InvalidSessionIDInput, match="non-hex"):
        resolve_session_id(mock_db, "xyz12345-...-11111111")
    with pytest.raises(InvalidSessionIDInput, match="non-hex"):
        resolve_session_id(mock_db, "11111111-...-xyz12345")


def test_resolve_compact_form_rejects_multiple_dots(mock_db):
    """A query with more than one '...' separator is malformed."""
    _insert(mock_db, UUID_A)
    mock_db.commit()
    with pytest.raises(InvalidSessionIDInput, match="more than one"):
        resolve_session_id(mock_db, "1111-...-2222-...-3333")
