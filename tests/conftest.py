import pytest

from tppe.db import Database
from tppe.wordbank import WordBank


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    d.init_db()
    return d


@pytest.fixture
def bank(db):
    return WordBank(db)
