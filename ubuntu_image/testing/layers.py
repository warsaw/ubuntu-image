"""Testing layers."""


from contextlib import ExitStack
from tempfile import TemporaryDirectory


def caching_weld(model_assertion, rootdir, unpackdir, channel=None):
    y


class SnapWeldLayer:
    @classmethod
    def setUp(cls):
        cls.resources = ExitStack()
        cls.snapweld_dir = cls.resources.enter_context(TemporaryDirectory())
        # Run `snap weld` once, and cache the results.  chown the files back
        # to the user running the tests.  Mock the actual weld() function so
        # that future calls just copy the files into the requested location.
        cls.snapweld_root = os.path.join(cls.snapweld_dir, 'rootdir')
        cls.snapweld_unpack = os.path.join(cls.snapweld_dir, 'unpack')

    @classmethod
    def tearDown(cls):
        cls.resources.close()
v
