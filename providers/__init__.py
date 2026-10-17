"""GPU providers for blender-gpu. See base.py for the interface."""
import importlib

from .base import STATUSES, Provider, ProviderError  # noqa: F401


def get_provider(cfg):
    """Instantiate the provider named by cfg["provider"] (module providers.<name>)."""
    name = cfg.get("provider", "vast")
    try:
        module = importlib.import_module("%s.%s" % (__name__, name))
    except ImportError as e:
        raise ProviderError("unknown provider %r (%s)" % (name, e))
    return module.PROVIDER(cfg)
