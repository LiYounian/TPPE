"""锁住词库规则：词≤20字符、候选至多3个且不重复、玩家输入一律入库、去重、预置词幂等。"""

import pytest

from tppe import config
from tppe.db import Database
from tppe.wordbank import WordBank, load_seed, normalize, validate


def test_normalize_trims_and_collapses_spaces():
    assert normalize("  春天\n 的  早上 ") == "春天 的 早上"


@pytest.mark.parametrize("text,ok", [("", False), ("春", True), ("a" * 20, True), ("a" * 21, False),
                                     ("二" * 20, True), ("二" * 21, False)])
def test_validate_length_limit_is_1_to_20_chars(text, ok):
    assert validate(text)[0] is ok


def test_empty_bank_suggests_nothing(bank):
    assert bank.suggest("time") == []


def test_suggest_returns_what_exists_when_fewer_than_3(bank):
    bank.submit("place", "火星")
    bank.submit("place", "食堂")
    assert sorted(bank.suggest("place")) == ["火星", "食堂"]


def test_suggest_at_most_3_unique(bank):
    for i in range(10):
        bank.submit("person", f"人{i}")
    for _ in range(20):
        s = bank.suggest("person")
        assert len(s) == config.SUGGEST_COUNT
        assert len(set(s)) == len(s)


def test_suggest_only_from_same_element(bank):
    bank.submit("time", "夏天")
    assert bank.suggest("place") == []


def test_player_input_is_stored_and_can_be_suggested_later(bank, db):
    bank.submit("event", "  偷吃了月饼 ")
    assert db.count_words("event") == 1
    assert bank.suggest("event") == ["偷吃了月饼"]


def test_duplicate_word_not_stored_twice(bank, db):
    bank.submit("time", "春天")
    bank.submit("time", "春天")
    assert db.count_words("time") == 1


def test_same_text_different_element_both_stored(bank, db):
    bank.submit("time", "火星时间")
    bank.submit("place", "火星时间")
    assert db.count_words() == 2


@pytest.mark.parametrize("bad", ["", "   ", "x" * 21])
def test_invalid_input_rejected_and_not_stored(bank, db, bad):
    with pytest.raises(ValueError):
        bank.submit("time", bad)
    assert db.count_words() == 0


def test_unknown_element_rejected(bank):
    with pytest.raises(ValueError):
        bank.submit("weather", "晴")


def test_seed_file_valid_and_10_to_20_per_element():
    seed = load_seed()
    assert set(seed) == set(config.ELEMENTS)
    for words in seed.values():
        assert 10 <= len(words) <= 20
        assert len(set(words)) == len(words)


def test_seed_insert_is_idempotent(tmp_path):
    d = Database(tmp_path / "s.db")
    seed = load_seed()
    d.init_db(seed)
    d.init_db(seed)
    assert d.count_words() == sum(len(v) for v in seed.values())
    assert len(WordBank(d).suggest("time")) == config.SUGGEST_COUNT
