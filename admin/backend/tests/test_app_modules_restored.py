"""A test that evicts the app's modules from sys.modules doesn't leave a
second copy of the app behind for the tests after it."""
import importlib
import sys

from tests.conftest import one_copy_of_the_app_modules

NAME = "app.utils.service_state"


def test_evicted_app_modules_are_put_back():
    import app.utils as package

    original = importlib.import_module(NAME)
    with one_copy_of_the_app_modules():
        del sys.modules[NAME]
        copy = importlib.import_module(NAME)
        assert copy is not original, "the eviction really produced a second copy"
        assert package.service_state is copy
        # Imported for the first time inside the block: part of the copy.
        sys.modules["app.zz_only_in_the_copy"] = copy
    assert sys.modules[NAME] is original
    assert package.service_state is original, "the package attribute is restored too"
    assert importlib.import_module(NAME) is original
    assert "app.zz_only_in_the_copy" not in sys.modules


def test_a_block_that_evicts_nothing_keeps_its_imports():
    """Only a replaced module triggers the restore: a lazy first import
    inside an ordinary test stays imported."""
    planted = "app.zz_lazy_import"
    try:
        with one_copy_of_the_app_modules():
            sys.modules[planted] = importlib.import_module(NAME)
        assert planted in sys.modules
    finally:
        sys.modules.pop(planted, None)
