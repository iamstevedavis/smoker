import pytest

from pitboss_bridge import auth
from pitboss_bridge.api import create_app
from pitboss_bridge.config import Settings
from pitboss_bridge.store import Database


def finish_setup(settings: Settings, password: str = "brisket", required: bool = True) -> None:
    """Store a dashboard password, as first-run setup would."""
    store = Database(settings.db_path)
    store.initialize()
    store.save_auth(auth.hash_password(password), required=required)


@pytest.fixture
def make_app():
    """create_app() past first-run setup with the password turned off, for
    tests that aren't about logging in."""

    def make(settings: Settings):
        finish_setup(settings, required=False)
        return create_app(settings)

    return make
