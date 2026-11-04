"""Keep stubbed test measurements out of the user's live evaluation dataset."""
import pytest


@pytest.fixture(autouse=True)
def isolated_evaluation(monkeypatch,tmp_path):
    monkeypatch.setenv('EVALUATION_FILE',str(tmp_path/'evaluation.jsonl'))
    monkeypatch.setenv('EVALUATION_RUN','test')
