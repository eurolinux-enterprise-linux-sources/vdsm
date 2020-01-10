#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import os
import errno

import sd
import fileUtils
import storage_exception as se
import fileVolume
import image
import misc
import outOfProcess as oop
from processPool import Timeout
from persistentDict import PersistentDict, DictValidator
import constants

REMOTE_PATH = "REMOTE_PATH"

FILE_SD_MD_FIELDS = sd.SD_MD_FIELDS.copy()
# TBD: Do we really need this key?
FILE_SD_MD_FIELDS[REMOTE_PATH] = (str, str)

class FileMetadataRW(object):
    """
    FileSDMetadata implements metadata extractor/committer over a simple file
    """

    def __init__(self, metafile):
        # FileSDMetadata is kept in the file
        self._metafile = metafile

    def readlines(self):
        if not oop.fileUtils.pathExists(self._metafile):
                return []
        return misc.stripNewLines(oop.directReadLines(self._metafile))

    def writelines(self, metadata):
        metadata = [i + '\n' for i in metadata]
        tmpFilePath = self._metafile + ".new"
        try:
            oop.writeLines(tmpFilePath, metadata)
        except IOError, e:
            if e.errno != errno.ESTALE:
                raise
            oop.writeLines(tmpFilePath, metadata)
        oop.os.rename(tmpFilePath, self._metafile)

FileSDMetadata = lambda metafile : DictValidator(PersistentDict(FileMetadataRW(metafile)), FILE_SD_MD_FIELDS)

def createmetafile(path, size_str):
    try:
        size = sd.sizeStr2Int(size_str)
        oop.createSparseFile(path, size)
    except Exception, e:
        raise se.StorageDomainMetadataCreationError("create meta file failed: %s: %s" % (path, str(e)))

class FileStorageDomain(sd.StorageDomain):
    def __init__(self, sdUUID):
        mountPath = os.path.join(self.storage_repository, sd.DOMAIN_MNT_POINT)
        # Using glob might look like the simplest thing to do but it isn't
        # If one of the mounts is stuck it'll cause the entire glob to fail
        # and you wouldn't be able to access any domain
        def checkPath(mnt):
            possiblePath = os.path.join(mountPath, mnt, sdUUID)
            try:
                if oop.fileUtils.pathExists(possiblePath):
                    return possiblePath
            except Timeout:
                return None

        domainPath = None
        for path in misc.tmap(checkPath, os.listdir(mountPath)):
            if path is not None:
                #NB: We do not support a case where there is more then one result.
                #    having two domains with the same uuid is unsupported and will
                #    cause undefined behaviour
                domainPath = path
                break

        if domainPath is None:
            self.log.error("Underlying storage for domain %s does not exist" % sdUUID)
            raise se.StorageDomainDoesNotExist(sdUUID)

        self.mountpoint = os.path.dirname(domainPath)
        self.remotePath = os.path.basename(self.mountpoint)
        self.metafile = os.path.join(self.mountpoint, sdUUID,
                                sd.DOMAIN_META_DATA, sd.METADATA)

        domaindir = os.path.join(self.mountpoint, sdUUID)
        metadata = FileSDMetadata(self.metafile)
        sd.StorageDomain.__init__(self, sdUUID, domaindir, metadata)

        if not oop.fileUtils.pathExists(self.metafile):
            raise se.StorageDomainMetadataNotFound(sdUUID, self.metafile)
        self.imageGarbageCollector()
        self._registerResourceNamespaces()

    @classmethod
    def _prepareMetadata(cls, domPath, sdUUID, domainName, domClass, remotePath, storageType, version):
        """
        Prepare all domain's special volumes and metadata
        """
        # create domain metadata folder
        metadataDir = os.path.join(domPath, sd.DOMAIN_META_DATA)
        oop.fileUtils.createdir(metadataDir, 0775)

        createmetafile(os.path.join(metadataDir, sd.LEASES), sd.LEASES_SIZE)
        createmetafile(os.path.join(metadataDir, sd.IDS), sd.IDS_SIZE)
        createmetafile(os.path.join(metadataDir, sd.INBOX), sd.INBOX_SIZE)
        createmetafile(os.path.join(metadataDir, sd.OUTBOX), sd.OUTBOX_SIZE)

        metaFile = os.path.join(metadataDir, sd.METADATA)

        md = FileSDMetadata(metaFile)
        # initialize domain metadata content
        # FIXME : This is 99% like the metadata in block SD
        #         Do we really need to keep the EXPORT_PATH?
        #         no one uses it
        md.update({
                sd.DMDK_VERSION : version,
                sd.DMDK_SDUUID : sdUUID,
                sd.DMDK_TYPE : storageType,
                sd.DMDK_CLASS : domClass,
                sd.DMDK_DESCRIPTION : domainName,
                sd.DMDK_ROLE : sd.REGULAR_DOMAIN,
                sd.DMDK_POOLS : [],
                sd.DMDK_LOCK_POLICY : '',
                sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC : sd.DEFAULT_LEASE_PARAMS[sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC],
                sd.DMDK_LEASE_TIME_SEC : sd.DEFAULT_LEASE_PARAMS[sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC],
                sd.DMDK_IO_OP_TIMEOUT_SEC : sd.DEFAULT_LEASE_PARAMS[sd.DMDK_IO_OP_TIMEOUT_SEC],
                sd.DMDK_LEASE_RETRIES : sd.DEFAULT_LEASE_PARAMS[sd.DMDK_LEASE_RETRIES],
                REMOTE_PATH : remotePath
                })

    def produceVolume(self, imgUUID, volUUID):
        """
        Produce a type specific volume object
        """
        repoPath = self._getRepoPath()
        return fileVolume.FileVolume(repoPath, self.sdUUID, imgUUID, volUUID)


    def getVolumeClass(self):
        """
        Return a type specific volume generator object
        """
        return fileVolume.FileVolume


    @classmethod
    def validateCreateVolumeParams(cls, volFormat, preallocate, srcVolUUID):
        """
        Validate create volume parameters.
            'srcVolUUID' - backing volume UUID
            'volFormat' - volume format RAW/QCOW2
            'preallocate' - sparse/preallocate
        """
        fileVolume.FileVolume.validateCreateVolumeParams(volFormat, preallocate, srcVolUUID)


    def createVolume(self, imgUUID, size, volFormat, preallocate, diskType, volUUID, desc, srcImgUUID, srcVolUUID):
        """
        Create a new volume
        """
        repoPath = self._getRepoPath()
        return fileVolume.FileVolume.create(repoPath, self.sdUUID,
                            imgUUID, size, volFormat, preallocate, diskType,
                            volUUID, desc, srcImgUUID, srcVolUUID)


    def validate(self):
        """
        Validate that the storage domain is accessible.
        """
        self.log.info("sdUUID=%s", self.sdUUID)
        # TODO: use something less intensive
        self._metadata.invalidate()
        self._metadata.copy()
        return True

    def getAllImages(self):
        """
        Fetch the list of the Image UUIDs
        """
        # Get Volumes of an image
        pattern = os.path.join(self.storage_repository,
                               # ISO domains don't have images,
                               # we can assume single domain
                               self.getPools()[0],
                               self.sdUUID, sd.DOMAIN_IMAGES)
        pattern = os.path.join(pattern, constants.UUID_GLOB_PATTERN)
        files = oop.glob.glob(pattern)
        imgList = []
        for i in files:
            if oop.os.path.isdir(i):
                imgList.append(os.path.basename(i))
        return imgList

    @classmethod
    def format(cls, sdUUID, domaindir):
        """
        Format detached storage domain.
        This removes all data from the storage domain.
        """
        cls.log.info("Formating domain %s", sdUUID)
        oop.fileUtils.cleanupdir(domaindir, ignoreErrors = False)
        return True

    def getRemotePath(self):
        return self.remotePath

    def getInfo(self):
        """
        Get storage domain info
        """
        ##self.log.info("sdUUID=%s", self.sdUUID)
        # First call parent getInfo() - it fills in all the common details
        info = sd.StorageDomain.getInfo(self)
        # Now add fileSD specific data
        info['remotePath'] = ''
        mounts = fileUtils.getMounts()
        for mount in mounts:
            if self.mountpoint == mount[1]:
                info['remotePath'] = mount[0]
                break

        return info

    def getStats(self):
        """
        Get storage domain statistics
        """
        ##self.log.info("sdUUID=%s", self.sdUUID)
        stats = {'disktotal':'', 'diskfree':''}
        try:
            st = oop.os.statvfs(self.domaindir)
            stats['disktotal'] = str(st.f_frsize * st.f_blocks)
            stats['diskfree'] = str(st.f_frsize * st.f_bavail)
        except OSError, e:
            self.log.info("sdUUID=%s %s", self.sdUUID, str(e))
            if e.errno == errno.ESTALE:
                raise se.FileStorageDomainStaleNFSHandle
            raise se.StorageDomainAccessError(self.sdUUID)
        return stats

    def getIsoList(self, extension):
        """
        Get list of all ISO/Floppy images
            'extension' - 'iso'/'floppy' for ISO/Floppy images
        """
        pass

    def mountMaster(self):
        """
        Mount the master metadata file system. Should be called only by SPM.
        """
        masterdir = os.path.join(self.domaindir, sd.MASTER_FS_DIR)
        if not oop.fileUtils.pathExists(masterdir):
            oop.os.mkdir(masterdir, 0755)

    def unmountMaster(self):
        """
        Unmount the master metadata file system. Should be called only by SPM.
        """
        pass


    def selftest(self):
        """
        Run internal self test
        """
        try:
            oop.os.statvfs(self.domaindir)
        except OSError, e:
            if e.errno == errno.ESTALE:
                # In case it is "Stale NFS handle" we are taking preventive
                # measures and unmounting this NFS resource. Chances are
                # that is the most intelligent thing we can do in this
                # situation anyway.
                self.log.debug("Unmounting stale file system %s", self.mountpoint)
                oop.fileUtils.umount(mountPoint=self.mountpoint)
                raise se.FileStorageDomainStaleNFSHandle
            raise

        return True

    def imageGarbageCollector(self):
        """
        Image Garbage Collector
        remove the remnants of the removed images (they could be left sometimes
        (on NFS mostly) due to lazy file removal
        """
        removedPattern = os.path.join(self.domaindir, sd.DOMAIN_IMAGES,
            image.REMOVED_IMAGE_PREFIX+'*')
        removedImages = oop.glob.glob(removedPattern)
        self.log.debug("Removing remnants of deleted images %s" % removedImages)
        for imageDir in removedImages:
            oop.fileUtils.cleanupdir(imageDir)
