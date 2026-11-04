"""Shared fixtures. The stock image is never committed; point CHARMERA_STOCK_IMAGE at a local copy."""
import os
from pathlib import Path

import pytest

DEFAULT_STOCK = Path(os.environ.get("CHARMERA_STOCK_IMAGE", ""))


@pytest.fixture(scope="session")
def stock_image_path() -> Path:
    if not DEFAULT_STOCK.is_file():
        pytest.skip("set CHARMERA_STOCK_IMAGE to a local 2 MiB stock dump to run image tests")
    return DEFAULT_STOCK


@pytest.fixture(scope="session")
def stock_image(stock_image_path) -> bytes:
    return stock_image_path.read_bytes()
