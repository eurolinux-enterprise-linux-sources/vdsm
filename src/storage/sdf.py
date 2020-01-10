#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

from config import config
import logging

import sdc
import sd
import storage_exception as se


class StorageDomainFactory:
    log = logging.getLogger("Storage.StorageDomainFactory")
    storage_repository = config.get('irs', 'repository')
    __sdc = sdc.StorageDomainCache(storage_repository)


    @classmethod
    def produce(cls, sdUUID):
        """
        Produce a new Storage domain
        """

        newSD = cls.__sdc.lookup(sdUUID)
        if not newSD:
            raise se.StorageDomainDoesNotExist(sdUUID)
        return newSD


    @classmethod
    def create(cls, sdUUID, storageType, domainName, domClass, typeSpecificArg, version):
        """
        Create a new Storage domain
        """
        import nfsSD
        import localFsSD
        import blockSD

        newSD = None
        if storageType in [sd.NFS_DOMAIN]:
            newSD = nfsSD.NfsStorageDomain.create(sdUUID=sdUUID,
                domainName=domainName, domClass=domClass,
                remotePath=typeSpecificArg, storageType=storageType,
                version=version)
        elif storageType in [sd.LOCALFS_DOMAIN]:
            newSD = localFsSD.LocalFsStorageDomain.create(sdUUID=sdUUID,
                domainName=domainName, domClass=domClass,
                remotePath=typeSpecificArg, storageType=storageType,
                version=version)
        elif storageType in [sd.ISCSI_DOMAIN, sd.FCP_DOMAIN]:
            newSD = blockSD.BlockStorageDomain.create(sdUUID=sdUUID,
                domainName=domainName, domClass=domClass,
                vgUUID=typeSpecificArg, storageType=storageType,
                version=version)
        else:
            raise se.StorageDomainTypeError(storageType)

        cls.__sdc.manuallyAddDomain(newSD)
        return newSD


    @classmethod
    def recycle(cls, sdUUID):
        """
        Cleanly destroys the domain
        """
        import nfsSD
        import localFsSD
        import blockSD

        try:
            cls.__sdc.manuallyRemoveDomain(sdUUID)
        except Exception:
            cls.log.warn("Storage domain %s doesn't exist. Trying recycle leftovers ...", sdUUID)

        # Check whether it's a block domain
        try:
            domaindir = blockSD.BlockStorageDomain.getDomainPath(sdUUID)
        except (se.StorageDomainDoesNotExist, se.VolumeGroupDoesNotExist):
            pass
        except Exception:
            cls.log.error("Can't find out block domain %s", sdUUID, exc_info=True)
        else:
            return blockSD.BlockStorageDomain.format(sdUUID, domaindir)

        # Check whether it's a NFS domain
        try:
            domaindir = nfsSD.NfsStorageDomain.getDomainPath(sdUUID)
        except se.StorageDomainDoesNotExist:
            pass
        except Exception:
            cls.log.error("Can't find out NFS domain %s", sdUUID, exc_info=True)
        else:
            return nfsSD.NfsStorageDomain.format(sdUUID, domaindir)

        # Check whether it's a local domain
        try:
            domaindir = localFsSD.LocalFsStorageDomain.getDomainPath(sdUUID)
        except se.StorageDomainDoesNotExist:
            pass
        except Exception:
            cls.log.error("Can't find out local domain %s", sdUUID, exc_info=True)
        else:
            return localFsSD.LocalFsStorageDomain.format(sdUUID, domaindir)

        raise se.StorageDomainTypeError(sdUUID)


    @classmethod
    def getAllDomains(cls):
        return cls.__sdc.getall()


    @classmethod
    def getAllUUIDs(cls):
        return cls.__sdc.getUUIDs()


    @classmethod
    def flushCache_deprecated(cls):
        cls.__sdc.flush_deprecated()


    @classmethod
    def refresh(cls):
        cls.__sdc.refresh()


    @classmethod
    def invalidateStorage(cls):
        cls.__sdc.invalidateStorage()


    @classmethod
    def refreshStorage(cls):
        cls.__sdc.refreshStorage()

