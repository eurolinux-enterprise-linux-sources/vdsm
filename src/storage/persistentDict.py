#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#
"""
persistentDict module provides generic class with common verification and
validation functionality implemented.
"""

import hashlib
import logging
from contextlib import contextmanager

import storage_exception as se
import threading
from copy import deepcopy
from itertools import ifilter

SHA_CKSUM_TAG = "_SHA_CKSUM"

_preprocessLine = lambda line : unicode.encode(unicode(line), 'ascii', 'xmlcharrefreplace')

class DictValidator(object):
    def __init__(self, dictObj, validatorDict):
        self._dict = dictObj
        self._validatorDict = validatorDict

        # Fields to export as is
        self.transaction = self._dict.transaction
        self.invalidate = self._dict.invalidate
        self.flush = self._dict.flush
        self.refresh = self._dict.refresh

    def __len__(self):
        return len(self.keys())

    def __contains__(self, item):
        return (item in self._validatorDict and item in self._dict)

    def getValidator(self, key):
        if key in self._validatorDict:
            return self._validatorDict[key]

        for entry in self._validatorDict:
            if hasattr(entry, "match"):
                if entry.match(key) is not None:
                    return self._validatorDict[entry]

        raise KeyError("%s not in allowed keys list" % key)

    def getEncoder(self, key):
        return self.getValidator(key)[1]

    def getDecoder(self, key):
        return self.getValidator(key)[0]

    def __getitem__(self, key):
        dec = self.getDecoder(key)
        return dec(self._dict[key])

    def get(self, key, default=None):
        dec = self.getDecoder(key)
        try:
            return dec(self._dict[key])
        except KeyError:
            return default

    def __setitem__(self, key, value):
        enc = self.getEncoder(key)
        self._dict.__setitem__(key, enc(value))

    def __delitem__(self, key):
        del self._dict[key]

    def __iter__(self):
        return ifilter(lambda k: k in self._validatorDict, self._dict.__iter__())

    def keys(self):
        return list(self.__iter__())

    def iterkeys(self):
        return self.__iter__()

    def update(self, metadata):
        metadata = metadata.copy()
        for key, value in metadata.iteritems():
            enc = self.getEncoder(key)
            metadata[key] = enc(value)

        self._dict.update(metadata)

    def clear(self):
        for key in self._validatorDict:
            if key in self._dict:
                del self._dict[key]

    def copy(self):
        md = self._dict.copy()
        for key, value in md.iteritems():
            try:
                dec = self.getDecoder(key)
                md[key] = dec(value)
            except KeyError:
                # there is a value in the dict that isn't mine, skipping
                pass

        return md


class PersistentDict(object):
    """
    This class provides interface for a generic set of key=value pairs
    that can be accessed by any consumer
    """
    log = logging.getLogger("Storage.PersistentDict")

    @contextmanager
    def _accessWrapper(self):
        with self._syncRoot:
            if not self._isValid:
                self.refresh()

            try:
                yield
            finally:
                return

    @contextmanager
    def transaction(self):
        with self._syncRoot:
            if self._inTransaction:
                try:
                    self.log.debug("Reusing active transaction")
                    yield
                finally:
                    return
            self._inTransaction = True

            with self._accessWrapper():
                self.log.debug("Starting transaction")
                backup = deepcopy(self._metadata)
                try:
                    yield
                    #TODO : check appropriateness
                    if backup != self._metadata:
                        self.log.debug("Flushing changes")
                        self.flush(self._metadata)
                    self.log.debug("Finished transaction")
                except:
                    self.log.debug("Error in transaction, rolling back changes", exc_info=True)
                    # TBD: Maybe check that the old MD is what I remember?
                    self.flush(backup)
                    raise
                finally:
                    self._inTransaction = False

    def __init__(self, metaReaderWriter):
        self._syncRoot = threading.RLock()
        self._metadata = {}
        self._metaRW = metaReaderWriter
        self._isValid = False
        self._inTransaction = False
        self.log.debug("Created a persistant dict with %s backend", self._metaRW.__class__.__name__)


    def get(self, key, default=None):
        with self._accessWrapper():
            return self._metadata.get(key, default)

    def __getitem__(self, key):
        with self._accessWrapper():
            if key not in self._metadata:
                raise KeyError(key)
            return self._metadata[key]

    def __setitem__(self, key, value):
        with self.transaction():
            self._metadata.__setitem__(key, value)

    def __delitem__(self, key):
        with self.transaction():
            self._metadata.__delitem__(key)

    def update(self, metadata):
        with self.transaction():
            self._metadata.update(metadata)

    def keys(self):
        with self._accessWrapper():
            return self._metadata.keys()

    def iterkeys(self):
        with self._accessWrapper():
            return self._metadata.iterkeys()

    def __iter__(self):
        with self._accessWrapper():
            return self._metadata.__iter__()

    def refresh(self):
        with self._syncRoot:
            lines = self._metaRW.readlines()

            self.log.debug("read lines (%s)=%s", self._metaRW.__class__.__name__, lines)
            newMD = {}
            declaredChecksum = None
            for line in lines:
                try:
                    key, value = line.split("=", 1)
                    value = value.strip()
                except ValueError:
                    self.log.warn("Could not parse line `%s`.", line)
                    continue

                if key == SHA_CKSUM_TAG:
                    declaredChecksum = value
                    continue

                newMD[key] = value

            if declaredChecksum is None:
                # No checksum in the metadata, let it through as is
                # FIXME : This is ugly but necessary, What we need is a class
                # method that creates the initial metadata. Then we can assume
                # that empty metadata is always invalid.
                self.log.warn("data has no embedded checksum - trust it as it is")
                return

            checksumCalculator = hashlib.sha1()
            keys = newMD.keys()
            keys.sort()
            for key in keys:
                value = newMD[key]
                line = "%s=%s" % (key, value)
                checksumCalculator.update(_preprocessLine(line))
            computedChecksum = checksumCalculator.hexdigest()

            if declaredChecksum != computedChecksum:
                self.log.warning("data seal is broken metadata declares `%s` should be `%s` (lines=%s)",
                        declaredChecksum, computedChecksum, newMD)
                raise se.MetaDataSealIsBroken(declaredChecksum, computedChecksum)

            self._isValid = True
            # Atomic replace
            self._metadata = newMD

    def flush(self, overrideMD):
        with self._syncRoot:
            md = overrideMD

            checksumCalculator = hashlib.sha1()
            lines = []
            keys = md.keys()
            keys.sort()
            for key in keys:
                value = md[key]
                line = "=".join([key, str(value)])
                checksumCalculator.update(_preprocessLine(line))
                lines.append(line)

            computedChecksum = checksumCalculator.hexdigest()
            lines.append("=".join([SHA_CKSUM_TAG, computedChecksum]))

            self.log.debug("about to write lines (%s)=%s", self._metaRW.__class__.__name__, lines)
            self._metaRW.writelines(lines)

            self._metadata = md
            self._isValid = True

    def invalidate(self):
        with self._syncRoot:
            self._isValid = False

    def __len__(self):
        with self._accessWrapper():
            return len(self._metadata)

    def __contains__(self, item):
        with self._accessWrapper():
            return self._metadata.__contains__(self, item)

    def copy(self):
        with self._accessWrapper():
            return self._metadata.copy()

    def clear(self):
        with self.transaction():
            self._metadata.clear()

