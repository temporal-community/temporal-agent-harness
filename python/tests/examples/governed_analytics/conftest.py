from collections.abc import Iterator
from pathlib import Path

import pytest

from examples.governed_analytics.generate_data import generate


@pytest.fixture(scope="session", autouse=True)
def bank_data(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The three databases, generated once, where ``databases.connect`` looks for them."""
    directory = tmp_path_factory.mktemp("bank")
    generate(directory)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GOVERNED_ANALYTICS_DATA_DIR", str(directory))
        yield directory
