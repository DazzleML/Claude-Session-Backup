# Triaged mutation survivors

Survivors judged **equivalent** (no test can kill them) or **don't-care** (behavior changed, but only outside the tested contract) are recorded here so future sweeps do not re-litigate them.

**Expiry is the hash.** Each heading carries the target file's `git hash-object` prefix at the time of triage. When the file's current hash no longer matches, every entry beneath it is STALE -- re-triage before reuse, and never feed stale entries to a generator.

Only separated-generation runs (a fresh-context generator with no access to the tests) may write here. A self-check run reads but never writes: an equivalence claim is an authority claim.

---

## claude_session_backup/commands.py @ 7775a8f5cf63

*Triaged 2026-08-15, generation mode 1 (fresh subagent, zero tool uses), sweep `v0.9.11__report__plugin-drift.json`.*

### M1 -- `equivalent`

```
    if key_installed < key_base:     ->     if key_installed <= key_base:
        return "behind", ...                    return "behind", ...
```

`plugin_drift` gained an explicit `if key_installed == key_base: return "same", None` branch immediately above this line (the zero-padding fix). Equal keys are therefore consumed before the comparison is reached, and `<` and `<=` cannot differ for any input. Provably unkillable **at this hash** -- if that equality branch is ever removed, this entry expires with the hash and M1 becomes killable again.

Note the history: M1 was killable before the padding fix landed. The fix that closed a real bug also made this mutant equivalent, which is why the hash guard matters more than the verdict.

### M5 -- `don't-care`

```
    if not text:          ->      if text is None:
        return None                   return None
```

In `_version_key`. For any **string** input the two are identical: `""` is the only falsy string, and both paths return `None` for it (the mutant reaches `int("")`, which raises `ValueError` and is caught by the existing handler). They diverge only for falsy non-strings -- `0`, `[]`, `()` -- which no caller can produce: `active_plugin_version` returns `str(...)`, and the cache scan yields directory names. Pinning behavior for an input outside the function's contract would be noise.

---

## Killed, for the record

Nine of twelve mutants were killed by `tests/test_setup_update.py` alone: M2 (or/and in the unordered guard), M3 (swapped ranking arguments), M6 (and/or in the registry filter), M7 (removed cache fallback), M8 (cache-scan comparison flip), M9 (checklist drops the `ahead` case), M10 (status reports drift as green), M11 (exit code 3 -> 0), M12 (post-update comparison flip).

M4 (`installed is None` -> `not installed`) survived, was triaged **killable**, and is now killed by `TestVersionRanking::test_empty_version_is_unordered_not_absent` -- verified red under the mutant and green on real code.

**A clean sweep is evidence about the mutations tried, not proof of suite completeness.**
