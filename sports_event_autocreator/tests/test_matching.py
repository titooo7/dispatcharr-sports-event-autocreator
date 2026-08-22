"""
Unit tests for engine.py's shared matching dialect (fold_text, term_matches,
contains_normalized, prefix_matches) and record_matches's exclude-wins
precedence.

engine.py has no Django/network dependencies, but the plugin package's other
modules (plugin.py, tasks.py, runner.py) do. To keep these tests runnable with
plain `python3 -m pytest`, we load engine.py directly by file path via
importlib instead of importing the package -- same pattern as test_engine.py.

Run:  python3 -m pytest sports_event_autocreator/tests/test_matching.py
      (from the repo root, ideally inside the .venv set up for this task:
       python3 -m venv .venv && .venv/bin/pip install pytest
       .venv/bin/python -m pytest sports_event_autocreator/tests/ -q)
"""

import importlib.util
import os

_ENGINE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine.py")
_spec = importlib.util.spec_from_file_location("sea_engine_matching", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


# ------------------------------- fold_text -------------------------------

def test_fold_text_collapses_nbsp():
    assert engine.fold_text("Real Madrid") == "Real Madrid"


def test_fold_text_collapses_narrow_nbsp():
    assert engine.fold_text("Real Madrid") == "Real Madrid"


def test_fold_text_collapses_zero_width_space():
    # The zero-width space is one of the folded whitespace variants -- it
    # collapses to a single ASCII space, same as any other run of whitespace.
    assert engine.fold_text("Real​Madrid") == "Real Madrid"


def test_fold_text_collapses_tab_and_multi_space():
    assert engine.fold_text("Real \t  Madrid") == "Real Madrid"


def test_fold_text_strips_combining_marks():
    assert engine.fold_text("á") == "a"
    assert engine.fold_text("Fútbol") == "Futbol"


def test_fold_text_idempotent():
    s = "Fútbol Sala  Atlético"
    once = engine.fold_text(s)
    twice = engine.fold_text(once)
    assert once == twice


def test_fold_text_empty_and_none():
    assert engine.fold_text("") == ""
    assert engine.fold_text(None) == ""


# ---------------------------- accent-fold matching ------------------------

def test_term_matches_futbol_vs_accented_text():
    assert engine.term_matches("Futbol", "Fútbol Sala")


def test_term_matches_accented_term_vs_plain_text():
    assert engine.term_matches("Fútbol", "Futbol Sala")


def test_term_matches_atletico():
    assert engine.term_matches("Atletico", "Atlético Madrid vs Real Madrid")
    assert engine.term_matches("Atlético", "Atletico Madrid vs Real Madrid")


def test_term_matches_maraton():
    assert engine.term_matches("Maraton", "Maratón de Nueva York")
    assert engine.term_matches("Maratón", "Maraton de Nueva York")


def test_term_matches_sao_paulo():
    assert engine.term_matches("Sao Paulo", "São Paulo vs Flamengo")
    assert engine.term_matches("São Paulo", "Sao Paulo vs Flamengo")


def test_term_matches_messy_scraped_xmltv_title():
    # NBSP after the round number, extra spacing before the team name --
    # typical of scraped XMLTV feeds.
    messy = "Round 16:   Real Madrid vs Barcelona"
    assert engine.term_matches("Real Madrid", messy)


# --------------------- lookaround-boundary regression ---------------------

def test_term_matches_hash_hash_regression():
    # This is the specific regression this task fixes: the lookaround
    # dialect (?<!\w)...(?!\w) matches "##" against "## Final" because '#'
    # is not a \w character, so neither side is a word-boundary violation.
    # The OLD \b...\b dialect would FAIL this (no word boundary around '#').
    assert engine.term_matches("##", "## Final")


def test_term_matches_trailing_punctuation_pattern():
    assert engine.term_matches("Final!", "The Grand Final! starts soon")


def test_term_matches_ampersand_term():
    assert engine.term_matches("Track & Field", "Track & Field Championships")


# ------------------------------ record_matches -----------------------------

def test_record_matches_exclude_wins_over_search():
    assert engine.record_matches(["Boxing"], ["Undercard"],
                                 "Boxing Undercard", "") is False


def test_record_matches_search_hit_no_exclude():
    assert engine.record_matches(["Boxing"], ["Undercard"],
                                 "Boxing Main Event", "") is True


def test_record_matches_empty_patterns_matches_nothing():
    assert engine.record_matches([], [], "Anything at all", "") is False


# --------------------------- contains_normalized ---------------------------

def test_contains_normalized_substring_not_word_boundary():
    assert engine.contains_normalized("box", "Boxing Undercard")


def test_term_matches_rejects_the_same_substring_case():
    # term_matches is word-boundary: "box" must NOT match inside "Boxing".
    assert engine.term_matches("box", "Boxing Undercard") is False


def test_contains_normalized_accent_folded():
    assert engine.contains_normalized("futbol", "Canal Fútbol HD")


def test_contains_normalized_empty_term():
    assert engine.contains_normalized("", "anything") is False


# ----------------------------- prefix_matches ------------------------------

def test_prefix_matches_basic():
    assert engine.prefix_matches("SKY:", "SKY: Premier League")


def test_prefix_matches_accent_folded():
    assert engine.prefix_matches("Espana", "España: La Liga")


def test_prefix_matches_not_a_prefix():
    assert engine.prefix_matches("SKY:", "PL: SKY: Premier League") is False
