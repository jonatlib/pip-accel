# Accelerator for pip, the Python package manager.
#
# Author: Peter Odding <peter.odding@paylogic.com>
# Last Change: October 31, 2015
# URL: https://github.com/paylogic/pip-accel

"""
Support for multiple cache backends.

This module defines an abstract base class (:class:`AbstractCacheBackend`)
to be inherited by custom cache backends in order to easily integrate them in
pip-accel. The cache backends included in pip-accel are built on top of the
same mechanism.

Additionally this module defines :class:`CacheManager` which makes it
possible to merge the available cache backends into a single logical cache
which automatically disables backends that report errors.
"""

# Standard library modules.
import logging

# Modules included in our package.
from pip_accel.compat import WINDOWS
from pip_accel.exceptions import CacheBackendDisabledError
from pip_accel.utils import get_python_version

# External dependencies.
from humanfriendly import concatenate, pluralize
from pkg_resources import get_entry_map

# Initialize a logger for this module.
logger = logging.getLogger(__name__)

# Initialize the registry of cache backends.
registered_backends = set()

# On Windows it is not allowed to have colons in filenames so we use a dollar sign instead.
FILENAME_PATTERN = 'v%i\\%s$%s$%s.tar.gz' if WINDOWS else 'v%i/%s:%s:%s.tar.gz'


class CacheBackendMeta(type):

    """Metaclass to intercept cache backend definitions."""

    def __init__(cls, name, bases, dict):
        """Intercept cache backend definitions."""
        type.__init__(cls, name, bases, dict)
        registered_backends.add(cls)


class AbstractCacheBackend(object):

    """
    Abstract base class for implementations of pip-accel cache backends.

    Subclasses of this class are used by pip-accel to store Python distribution
    archives in order to accelerate performance and gain independence of
    external systems like PyPI and distribution sites.

    .. note:: This base class automatically registers subclasses at definition
              time, providing a simple and elegant registration mechanism for
              custom backends. This technique uses metaclasses and was
              originally based on the article `Using Metaclasses to Create
              Self-Registering Plugins
              <http://effbot.org/zone/metaclass-plugins.htm>`_.

              I've since had to introduce some additional magic to make this
              mechanism compatible with both Python 2.x and Python 3.x because
              the syntax for metaclasses is very much incompatible and I refuse
              to write separate implementations for both :-).
    """

    PRIORITY = 0

    def __init__(self, config):
        """
        Initialize a cache backend.

        :param config: The pip-accel configuration (a :class:`.Config`
                       object).
        """
        self.config = config

    def get(self, filename):
        """
        Get a previously cached distribution archive from the cache.

        :param filename: The expected filename of the distribution archive (a
                         string).
        :returns: The absolute pathname of a local file or :data:`None` when the
                  distribution archive hasn't been cached.

        This method is called by `pip-accel` before fetching or building a
        distribution archive, in order to check whether a previously cached
        distribution archive is available for re-use.
        """
        raise NotImplementedError()

    def put(self, filename, handle):
        """
        Store a newly built distribution archive in the cache.

        :param filename: The filename of the distribution archive (a string).
        :param handle: A file-like object that provides access to the
                       distribution archive.

        This method is called by `pip-accel` after fetching or building a
        distribution archive, in order to cache the distribution archive.
        """
        raise NotImplementedError()

    def __repr__(self):
        """Generate a textual representation of the cache backend."""
        return self.__class__.__name__


# Obscure syntax gymnastics to define a class with a metaclass whose
# definition is compatible with Python 2.x as well as Python 3.x.
# See also: https://wiki.python.org/moin/PortingToPy3k/BilingualQuickRef#metaclasses
AbstractCacheBackend = CacheBackendMeta('AbstractCacheBackend',
                                        AbstractCacheBackend.__bases__,
                                        dict(AbstractCacheBackend.__dict__))


class CacheManager(object):

    """
    Interface to treat multiple cache backends as a single one.

    The cache manager automatically disables cache backends that raise
    exceptions on ``get()`` and ``put()`` operations.
    """

    def __init__(self, config):
        """
        Initialize a cache manager.

        Automatically initializes instances of all registered cache backends
        based on setuptools' support for entry points which makes it possible
        for external Python packages to register additional cache backends
        without any modifications to pip-accel.

        :param config: The pip-accel configuration (a :class:`.Config`
                       object).
        """
        self.config = config
        for entry_point in get_entry_map('pip-accel', 'pip_accel.cache_backends').values():
            logger.debug("Importing cache backend: %s", entry_point.module_name)
            __import__(entry_point.module_name)
        # Initialize instances of all registered cache backends (sorted by
        # priority so that e.g. the local file system is checked before S3).
        self.backends = sorted((b(self.config) for b in registered_backends if b != AbstractCacheBackend),
                               key=lambda b: b.PRIORITY)
        logger.debug("Initialized %s: %s",
                     pluralize(len(self.backends), "cache backend"),
                     concatenate(map(repr, self.backends)))

    def get(self, requirement):
        """
        Get a distribution archive from any of the available caches.

        :param requirement: A :class:`.Requirement` object.
        :returns: The absolute pathname of a local file or :data:`None` when the
                  distribution archive is missing from all available caches.
        """
        filename = self.generate_filename(requirement)
        for backend in list(self.backends):
            try:
                pathname = backend.get(filename)
                if pathname is not None:
                    return pathname
            except CacheBackendDisabledError as e:
                logger.debug("Disabling %s because it requires configuration: %s", backend, e)
                self.backends.remove(backend)
            except Exception as e:
                logger.exception("Disabling %s because it failed: %s", backend, e)
                self.backends.remove(backend)

    def put(self, requirement, handle):
        """
        Store a distribution archive in all of the available caches.

        :param requirement: A :class:`.Requirement` object.
        :param handle: A file-like object that provides access to the
                       distribution archive.
        """
        filename = self.generate_filename(requirement)
        for backend in list(self.backends):
            handle.seek(0)
            try:
                backend.put(filename, handle)
            except CacheBackendDisabledError as e:
                logger.debug("Disabling %s because it requires configuration: %s", backend, e)
                self.backends.remove(backend)
            except Exception as e:
                logger.exception("Disabling %s because it failed: %s", backend, e)
                self.backends.remove(backend)

    def generate_filename(self, requirement):
        """
        Generate a distribution archive filename for a package.

        :param requirement: A :class:`.Requirement` object.
        :returns: The filename of the distribution archive (a string)
                  including a single leading directory component to indicate
                  the cache format revision.
        """
        return FILENAME_PATTERN % (self.config.cache_format_revision,
                                   requirement.name, requirement.version,
                                   get_python_version())
