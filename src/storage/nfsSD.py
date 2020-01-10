# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#


import os
import glob

import sd
import fileSD
import fileUtils
import storage_exception as se
import outOfProcess as oop
from processPool import Timeout
import misc
import constants

class NfsStorageDomain(fileSD.FileStorageDomain):

    @classmethod
    def _preCreateValidation(cls, domPath, typeSpecificArg, version):
        # Some trivial resource validation
        if ":" not in typeSpecificArg:
            raise se.StorageDomainIllegalRemotePath(typeSpecificArg)

        sd.validateDomainVersion(version)

        # Make sure the underlying file system is mounted
        if not fileUtils.isMounted(mountPoint=domPath, mountType=fileUtils.FSTYPE_NFS):
            raise se.StorageDomainFSNotMounted(typeSpecificArg)

        oop.fileUtils.validateAccess(domPath)

        # Make sure there are no remnants of other domain
        mdpat = os.path.join(domPath, "*", sd.DOMAIN_META_DATA)
        if len(oop.glob.glob(mdpat)) > 0:
            raise se.StorageDomainNotEmpty(typeSpecificArg)

    @classmethod
    def create(cls, sdUUID, domainName, domClass, remotePath, storageType, version):
        """
        Create new storage domain.
            'sdUUID' - Storage Domain UUID
            'domainName' - storage domain name ("iso" or "data domain name")
            'remotePath' - server:/export_path
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
        oop.fileUtils.createdir(imagesDir)

        # create special imageUUID for ISO/Floppy volumes
        if domClass is sd.ISO_DOMAIN:
            isoDir = os.path.join(imagesDir, sd.ISO_IMAGE_UUID)
            oop.fileUtils.createdir(isoDir)

        fsd = NfsStorageDomain(sdUUID)
        fsd.initSPMlease()

        return fsd

    @classmethod
    def getDomainPath(cls, sdUUID):
        """
        Check whether it's a NFS domain.
        Return domain's path or ERROR for non-NFS domains
        """
        # Check whether it's NFS domain
        DOM_PATTERN = os.path.join(sd.StorageDomain.storage_repository,
            sd.DOMAIN_MNT_POINT, '[!_]*:*', sdUUID)
        dom = oop.glob.glob(DOM_PATTERN)
        if len(dom):
            # Return full domain path
            return dom[0]

        raise se.StorageDomainDoesNotExist(sdUUID)

    def getIsoList(self, extension):
        """
        Get list of all ISO/Floppy images
            'extension' - 'iso'/'floppy' for ISO/Floppy images
        """
        self.log.debug("list iso domain %s ext %s" % (self.sdUUID, extension))
        # isoDict - {'iso_image_path1' : error_code1, 'iso_image_path2' : error_code2}
        isoDict = {}
        isoPat = os.path.join(self.domaindir, sd.DOMAIN_IMAGES,
                              sd.ISO_IMAGE_UUID, '*.*')
        files = oop.glob.glob(isoPat)
        extension = "." + extension.lower()
        list = [ f for f in files if f[-4:].lower() == extension]
        for entry in list:
            try:
                oop.fileUtils.validateQemuReadable(entry)
                isoDict[os.path.basename(entry)] = 0  # Status OK
            except se.StorageServerAccessPermissionError:
                isoDict[os.path.basename(entry)] = se.StorageServerAccessPermissionError.code

        return isoDict

    def selftest(self):
        """
        Run internal self test
        """
        if not fileUtils.isMounted(mountPoint=self.mountpoint, mountType=fileUtils.FSTYPE_NFS):
            raise se.StorageDomainFSNotMounted

        # Run general part of selftest
        return fileSD.FileStorageDomain.selftest(self)


def getFileStorageDomainList():
    domlist = glob.glob(os.path.join(sd.StorageDomain.storage_repository,
                sd.DOMAIN_MNT_POINT, '[!_]*:*'))
    files = []
    def collectMetaFiles(possibleDomain):
        try:
            metaFiles = oop.glob.glob(os.path.join(possibleDomain, constants.UUID_GLOB_PATTERN, sd.DOMAIN_META_DATA))
            files.extend(metaFiles)
        except Timeout:
            pass

    misc.tmap(collectMetaFiles, domlist)

    dl = []
    for f in files:
        if os.path.basename(os.path.dirname(f)) != sd.MASTER_FS_DIR:
            dl.append(NfsStorageDomain(os.path.basename(os.path.dirname(f))))

    return dl

