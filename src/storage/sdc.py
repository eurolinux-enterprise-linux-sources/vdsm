#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#
"""
Cache module provides general purpose (more or less) cache infrastructure
for keeping storage related data that is expensive to harvest, but needed often
"""
import logging

import multipath
import lvm
import misc

# Default cache age until forcibly refreshed
DEFAULT_REFRESH_INTERVAL = 300

class StorageDomainCache:
    """
    Storage Domain List keeps track of all the storage domains accessible by the
    current system.
    """

    log = logging.getLogger('Storage.StorageDomainCache')
    def __init__(self, storage_repo):
        self.__cache = {}
        self.__isDirty = True
        self.storage_repo = storage_repo
        self.storageStale = True


    def invalidateStorage(self):
        self.storageStale = True
        self.invalidate()


    @misc.samplingmethod
    def refreshStorage(self):
        multipath.rescan()
        lvm.updateLvmConf()
        self.storageStale = False
        self.invalidate()


    def _refreshIfDirty(self):
        """
        Check whether the list content is expired
        """
        if self.__isDirty == True:
            self.log.debug("Cache is cold-expired")
            self._refreshDomains()
            return True

        return False


    def invalidate(self):
        """
        Invalidate the cache data (mark it stale/expired),
        do not, however, actually purge any cached data
        """
        #
        # We do not keep the separate "expired" or "stale" flag,
        # since our isExpire() method checks __isDirty first thing.
        # If that is the case the cache is considered to be cold.
        # The eventual refresh(), however, will take every precaution
        # not to destroy any existing objects in the cache.
        # It rather makes sure that everything is up to date.
        #
        # That is exactly the effect we are aiming at.
        #
        self.__isDirty = True

    def flush_deprecated(self):
        """
        Flush the storage domain list
        """
        self.__cache.clear()
        self.__isDirty = True

    def lookup(self, domainid):
        # In several situations we should refresh the cache before the lookup
        # First, if cache was expired
        # Second, to prevent cache miss when VDC got out of sync
        # For example, VDC gets out of sync if it try to perform refresh when
        # one of domains lost its underlying storage device.
        # In this case we will drop this domain from the cache
        # and we will not be able to add it again if its underlying storage device will come back
        if self._refreshIfDirty():
            return self.__cache.get(domainid)

        dom = self.__cache.get(domainid)
        if not dom:
            self._refreshDomains()

        return self.__cache.get(domainid)

    def getall(self):
        self._refreshIfDirty()
        return self.__cache.values()


    def getUUIDs(self):
        self._refreshIfDirty()
        return self.__cache.keys()


    def refresh(self):
        self.refreshStorage()
        self._refreshDomains()

    def manuallyAddDomain(self, dom):
        self.__cache[dom.sdUUID] = dom

    def manuallyRemoveDomain(self, sdUUID):
        # I know this is race prone and we might
        # find ourselves with a domain in the cache
        # that is invalid. But nothing bad can really
        # come of it so I'm leaving the race in
        del self.__cache[sdUUID]

    @misc.samplingmethod
    def _refreshDomains(self):
        """
        Refresh the domains list
        """
        import blockSD
        import nfsSD
        import localFsSD

        if self.storageStale:
            self.refreshStorage()

        # Get the list of all the domains that are visible *now*
        allDomains = (blockSD.getBlockStorageDomainList() +
                   nfsSD.getFileStorageDomainList() +
                   localFsSD.getFileStorageDomainList())

        uuidList = [i.sdUUID for i in allDomains]
        allUUIDs = set(uuidList)
        if len(allUUIDs) != len(allDomains):
            # If you reached here it means we found multiple domains with the same UUID.
            # We don't know what to do so print warning to log
            for sdUUID in allUUIDs:
                uuidList.remove(sdUUID)
            for sdUUID in uuidList:
                self.log.warn("Found multiple domains claiming to be `%s`. This is not supported.", sdUUID)

        # Get the list of cached domain's UUIDs
        newCache = self.__cache.copy()
        cachedIDs = set(newCache.keys())

        # Generate a list of domains to add and to delete
        toadd = allUUIDs.difference(cachedIDs)
        todel = cachedIDs.difference(allUUIDs)

        # Delete all the unavailable domains
        for d in todel:
            try:
                dom = newCache[d]
                del newCache[d]
                dom.invalidate()
            except Exception:
                self.log.error("Unexpected error", exc_info=True)

        # Add all the new domains
        for d in toadd:
            for dom in allDomains:
                if d == dom.sdUUID:
                    try:
                        newCache[dom.sdUUID] = dom
                        break
                    except Exception:
                        self.log.error("Unexpected error", exc_info=True)

        self.__cache = newCache
        self.__isDirty = False

        for sdUUID in allUUIDs:
            newCache[sdUUID].invalidateMetadata()

