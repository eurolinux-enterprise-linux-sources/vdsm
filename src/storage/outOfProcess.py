import os as mod_os
import glob as mod_glob
import types
from config import config

from fileUtils import open_ex
import fileUtils as mod_fileUtils

from processPool import ProcessPool

MAX_HELPERS = config.getint("irs", "process_pool_size")
GRACE_PERIOD = config.getint("irs", "process_pool_grace_period")
DEFAULT_TIMEOUT = config.getint("irs", "process_pool_timeout")

_globalPool = ProcessPool(MAX_HELPERS, GRACE_PERIOD, DEFAULT_TIMEOUT)

def _directReadLines(path):
    with open_ex(path, "dr") as f:
        return f.readlines()
directReadLines = _globalPool.wrapFunction(_directReadLines)

def _directWriteLines(path, lines):
    with open_ex(path, "dw") as f:
        return f.writelines(lines)
directWriteLines = _globalPool.wrapFunction(_directWriteLines)

def _createSparseFile(path, size):
    with open(path, "w") as f:
        f.truncate(size)
createSparseFile = _globalPool.wrapFunction(_createSparseFile)

def _readLines(path):
    with open(path, "r") as f:
        return f.readlines()
readLines = _globalPool.wrapFunction(_readLines)

def _writeLines(path, lines):
    with open(path, "w") as f:
        return f.writelines(lines)
writeLines = _globalPool.wrapFunction(_writeLines)

class _ModuleWrapper(types.ModuleType):
    def __init__(self, wrappedModule):
        self._wrappedModule = wrappedModule

    def __getattr__(self, name):
        return _globalPool.wrapFunction(getattr(self._wrappedModule, name))

glob = _ModuleWrapper(mod_glob)

os = _ModuleWrapper(mod_os)
setattr(os, 'path', _ModuleWrapper(mod_os.path))

fileUtils = _ModuleWrapper(mod_fileUtils)
