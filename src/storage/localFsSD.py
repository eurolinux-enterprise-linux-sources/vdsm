#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#


import os
from glob import glob

import sd
import fileSD
import fileUtils
import storage_exception as se
import constants


class LocalFsStorageDomain(fileSD.FileStorageDomain):

    @classmethod
    def _preCreateValidation(cls, domPath, typeSpecificArg, version):
        # Some trivial resource validation
        if os.path.abspath(typeSpecificArg) != typeSpecificArg:
            raise se.StorageDomainIllegalRemotePath(typeSpecificArg)

        fileUtils.validateAccess(domPath)

        sd.validateDomainVersion(version)

        # Make sure there are no remnants of other domain
        mdpat = os.path.join(domPath, "*", sd.DOMAIN_META_DATA)
        if len(glob(mdpat)) > 0:
            raise se.StorageDomainNotEmpty(typeSpecificArg)

    @classmethod
    def create(cls, sdUUID, domainName, domClass, remotePath, storageType, version):
        """
        Create new storage domain.
            'sdUUID' - Storage Domain UUID
            'domainName' - storage domain name ("iso" or "data domain name")
            'remotePath' - /data2
            'domClass' - Data/Iso
        """
        cls.log.info("sdUUID=%s domainName=%s remotePath=%s "
            "domClass=%s", sdUUID, domainName, remotePath, domClass)

        # Create local path
        mntPath = fileUtils.transformPath(remotePath)

        mntPoint = os.path.join(cls.storage_repository,
            sd.DOMAIN_MNT_POINT, mntPath)

        cls._preCreateValidation(mntPoint, remotePath, version)

        domainDir = os.path.join(mntPoint, sdUUID)
        cls._prepareMetadata(domainDir, sdUUID, domainName, domClass,
                            remotePath, storageType, version)

        # create domain images folder
        imagesDir = os.path.join(domainDir, sd.DOMAIN_IMAGES)
        fileUtils.createdir(imagesDir)

        # create special imageUUID for ISO/Floppy volumes
        # Actually the local domain shouldn't be ISO, but
        # we can allow it for systems without NFS at all
        if domClass is sd.ISO_DOMAIN:
            isoDir = os.path.join(imagesDir, sd.ISO_IMAGE_UUID)
            fileUtils.createdir(isoDir)

        fsd = LocalFsStorageDomain(sdUUID)
        fsd.initSPMlease()

        return fsd

    @classmethod
    def getDomainPath(cls, sdUUID):
        """
        Check whether it's a LocalFS domain.
        Return domain's path or ERROR for non-local domains
        """
        DOM_PATTERN = os.path.join(sd.StorageDomain.storage_repository,
            sd.DOMAIN_MNT_POINT, '_*', sdUUID)
        dom = glob(DOM_PATTERN)
        if len(dom):
            # Return full domain path
            return dom[0]

        raise se.StorageDomainDoesNotExist(sdUUID)

    def getIsoList(self, extension):
        """
        Get list of all ISO/Floppy images
            'extension' - 'iso'/'floppy' for ISO/Floppy images
        """
        isoDict = {}
        return isoDict


def getFileStorageDomainList():
    dl = []
    DOM_METAPATTERN = os.path.join(sd.StorageDomain.storage_repository,
        sd.DOMAIN_MNT_POINT, '_*', constants.UUID_GLOB_PATTERN, sd.DOMAIN_META_DATA)
    files = glob(DOM_METAPATTERN)

    for f in files:
        if os.path.basename(os.path.dirname(f)) != sd.MASTER_FS_DIR:
            dl.append(LocalFsStorageDomain(os.path.basename(os.path.dirname(f))))

    return dl
