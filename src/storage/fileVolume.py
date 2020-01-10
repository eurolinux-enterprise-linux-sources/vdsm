#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#


import os
import uuid
import constants
from config import config

import storage_exception as se
from sdf import StorageDomainFactory as SDF
import volume
import image
import sd
import misc
import fileUtils
import task
from threadLocal import vars
import outOfProcess as oop

class FileVolume(volume.Volume):
    """ Actually represents a single volume (i.e. part of virtual disk).
    """
    def __init__(self, repoPath, sdUUID, imgUUID, volUUID):
        volume.Volume.__init__(self, repoPath, sdUUID, imgUUID, volUUID)

    @staticmethod
    def file_setrw(path, rw):
        mode = 0440
        if rw:
            mode |= 0220
        if os.path.isdir(path):
            mode |= 0110
        oop.os.chmod(path, mode)

    @classmethod
    def halfbakedVolumeRollback(cls, taskObj, volPath):
        cls.log.info("halfbakedVolumeRollback: volPath=%s" % (volPath))
        if oop.fileUtils.pathExists(volPath):
            oop.os.unlink(volPath)

    @classmethod
    def validateCreateVolumeParams(cls, volFormat, preallocate, srcVolUUID):
        """
        Validate create volume parameters.
        'srcVolUUID' - backing volume UUID
        'volFormat' - volume format RAW/QCOW2
        'preallocate' - sparse/preallocate
        """
        volume.Volume.validateCreateVolumeParams(volFormat, preallocate, srcVolUUID)

        # Snapshot should be COW volume
        if srcVolUUID != volume.BLANK_UUID and volFormat != volume.COW_FORMAT:
            raise se.IncorrectFormat(srcVolUUID)


    @classmethod
    def createVolumeMetadataRollback(cls, taskObj, volPath):
        cls.log.info("createVolumeMetadataRollback: volPath=%s" % (volPath))
        metaPath = cls.__metaVolumePath(volPath)
        if oop.os.path.lexists(metaPath):
            oop.os.unlink(metaPath)


    @classmethod
    def create(cls, repoPath, sdUUID, imgUUID, size, volFormat, preallocate, diskType, volUUID, desc, srcImgUUID, srcVolUUID):
        """
        Create a new volume with given size or snapshot
            'size' - in sectors
            'volFormat' - volume format COW / RAW
            'preallocate' - Prealocate / Sparse
            'diskType' - string that describes disk type System|Data|Shared|Swap|Temp
            'srcImgUUID' - source image UUID
            'srcVolUUID' - source volume UUID
        """
        if not volUUID:
            volUUID = str(uuid.uuid4())
        if volUUID == volume.BLANK_UUID:
            raise se.InvalidParameterException("volUUID", volUUID)

        # Validate volume parameters should be checked here for all
        # internal flows using volume creation.
        cls.validateCreateVolumeParams(volFormat, preallocate, srcVolUUID)

        imageDir = image.Image(repoPath).create(sdUUID, imgUUID)
        vol_path = os.path.join(imageDir, volUUID)
        voltype = "LEAF"
        pvol = None
        # Check if volume already exists
        if oop.fileUtils.pathExists(vol_path):
            raise se.VolumeAlreadyExists(vol_path)
        # Check if snapshot creation required
        if srcVolUUID != volume.BLANK_UUID:
            if srcImgUUID == volume.BLANK_UUID:
                srcImgUUID = imgUUID
            pvol = FileVolume(repoPath, sdUUID, srcImgUUID, srcVolUUID)
            # Cannot create snapshot for ILLEGAL volume
            if not pvol.isLegal():
                raise se.createIllegalVolumeSnapshotError(pvol.volUUID)

        # create volume rollback
        vars.task.pushRecovery(task.Recovery("halfbaked volume rollback", "fileVolume", "FileVolume", "halfbakedVolumeRollback",
                                             [vol_path]))
        if preallocate == volume.PREALLOCATED_VOL:
            idle = config.getfloat('irs', 'idle')
            try:
                # ddWatchCopy expects size to be in bytes
                misc.ddWatchCopy("/dev/zero", vol_path, vars.task.aborting, idle, (int(size) * 512))
            except se.ActionStopped, e:
                raise e
            except Exception, e:
                cls.log.error("Unexpected error", exc_info=True)
                raise se.VolumesZeroingError(vol_path)
        else:
            # Sparse = Normal file
            oop.createSparseFile(vol_path, 0)

        cls.log.info("fileVolume: create: volUUID %s srcImg %s srvVol %s" % (volUUID, srcImgUUID, srcVolUUID))
        if not pvol:
            cls.log.info("Request to create %s volume %s with size = %s sectors",
                     volume.type2name(volFormat), vol_path, size)
            volume.createVolume(None, None, vol_path, size, volFormat, preallocate)
        else:
            # Create hardlink to template and its meta file
            if imgUUID != srcImgUUID:
                pvol.share(imageDir, hard=True)
                # Make clone to link the new volume against the local shared volume
                pvol = FileVolume(repoPath, sdUUID, imgUUID, srcVolUUID)
            pvol.clone(imageDir, volUUID, volFormat, preallocate)
            size = pvol.getMetaParam(volume.SIZE)

        try:
            vars.task.pushRecovery(task.Recovery("create block volume metadata rollback", "fileVolume", "FileVolume", "createVolumeMetadataRollback",
                                                 [vol_path]))
            # By definition volume is now a leaf
            cls.file_setrw(vol_path, rw=True)
            # Set metadata and mark volume as legal.\
            cls.newMetadata(vol_path, sdUUID, imgUUID, srcVolUUID, size, volume.type2name(volFormat),
                            volume.type2name(preallocate), voltype, diskType, desc, volume.LEGAL_VOL)
        except Exception, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise se.VolumeMetadataWriteError(vol_path + ":" + str(e))

        # Remove all previous rollbacks for 'halfbaked' volume and add rollback for 'real' volume creation
        vars.task.replaceRecoveries(task.Recovery("create file volume rollback", "fileVolume", "FileVolume", "createVolumeRollback",
                                             [repoPath, sdUUID, imgUUID, volUUID, imageDir]))
        return volUUID


    def delete(self, postZero, force):
        """
        Delete volume.
            'postZero' - zeroing file before deletion
            'force' - required to remove shared and internal volumes
        """
        self.log.info("Request to delete volume %s", self.volUUID)

        vol_path = self.getVolumePath()
        size = self.getVolumeSize(bs=1)

        if not force:
            self.validateDelete()

        # Mark volume as illegal before deleting
        self.setLegality(volume.ILLEGAL_VOL)

        # try to cleanup as much as possible
        eFound = se.CannotDeleteVolume(self.volUUID)
        puuid = None
        try:
            # We need to blank parent record in our metadata
            # for parent to become leaf successfully.
            puuid = self.getParent()
            self.setParent(volume.BLANK_UUID)
            if puuid and puuid != volume.BLANK_UUID:
                pvol = FileVolume(self.repoPath, self.sdUUID, self.imgUUID, puuid)
                pvol.recheckIfLeaf()
        except Exception, e:
            eFound = e
            self.log.warning("cannot finalize parent volume %s", puuid, exc_info=True)

        try:
            oop.fileUtils.cleanupfiles([vol_path])
        except Exception, e:
            eFound = e
            self.log.error("cannot delete volume %s at path: %s", self.volUUID,
                            vol_path, exc_info=True)

        try:
            self.removeMetadata()
            return True
        except Exception, e:
            eFound = e
            self.log.error("cannot remove volume's %s metadata", self.volUUID, exc_info=True)

        raise eFound

    def getDevPath(self):
        """
        Return the underlying device (for sharing)
        """
        return self.getVolumePath()

    def share(self, dst_image_dir, hard=True):
        """
        Share this volume to dst_image_dir, including the meta file
        """
        volume.Volume.share(self, dst_image_dir, hard=hard)

        self.log.debug("share  meta of %s to %s hard %s" % (self.volUUID, dst_image_dir, hard))
        src = self._getMetaVolumePath()
        dst = self._getMetaVolumePath(os.path.join(dst_image_dir, self.volUUID))
        if oop.fileUtils.pathExists(dst):
            oop.os.unlink(dst)
        if hard:
            oop.os.link(src, dst)
        else:
            oop.os.symlink(src, dst)

    def setrw(self, rw):
        """
        Set the read/write permission on the volume
        """
        self.file_setrw(self.getVolumePath(), rw=rw)

    def llPrepare(self, rw=False, setrw=False):
        """
        Make volume accessible as readonly (internal) or readwrite (leaf)
        """
        volPath = self.getVolumePath()

        if setrw:
            self.setrw(rw=rw)
        if rw:
            if not oop.os.access(volPath, os.R_OK | os.W_OK):
                raise se.VolumeAccessError(volPath)
        else:
            if not oop.os.access(volPath, os.R_OK):
                raise se.VolumeAccessError(volPath)

    def llTeardown(self, rw=False, setrw=False):
        """
        Chmod the volume to be read only
        """
        if setrw:
            self.setrw(rw=rw)

    def removeMetadata(self):
        """
        Remove the meta file
        """
        metaPath = self._getMetaVolumePath()
        if oop.os.path.lexists(metaPath):
            oop.os.unlink(metaPath)


    def getMetadata(self, vol_path = None, nocache=False):
        """
        Get Meta data array of key,values lines
        """
        if nocache:
            out = self.metaCache()
            if out:
                return out
        meta = self._getMetaVolumePath(vol_path)
        try:
            f = oop.directReadLines(meta)
            out = {}
            for l in f:
                if l.startswith("EOF"):
                    return out
                if l.find("=") < 0:
                    continue
                key, value = l.split("=")
                out[key.strip()] = value.strip()
        except Exception, e:
            self.log.error(e, exc_info=True)
            raise se.VolumeMetadataReadError(meta + str(e))
        self.putMetaCache(out)
        return out

    @classmethod
    def __putMetadata(cls, metaarr, vol_path):
        meta = cls.__metaVolumePath(vol_path)
        f = None
        try:
            f = open(meta + ".new", "w")
            for key, value in metaarr.iteritems():
                f.write("%s=%s\n" % (key.strip(), str(value).strip()))
            f.write("EOF\n")
        finally:
            if f:
                f.close()
        oop.os.rename(meta + ".new", meta)


    @classmethod
    def createMetadata(cls, metaarr, vol_path):
        cls.__putMetadata(metaarr, vol_path)

    def setMetadata(self, metaarr, vol_path = None, nocache=False):
        """
        Set the meta data hash as the new meta data of the Volume
        """
        if not vol_path:
            vol_path = self.getVolumePath()
        try:
            self.__putMetadata(metaarr, vol_path)
            if not nocache:
                self.putMetaCache(metaarr)
        except Exception, e:
            self.log.error(e, exc_info=True)
            raise se.VolumeMetadataWriteError(vol_path + ":" + str(e))

    @classmethod
    def getImageVolumes(cls, repoPath, sdUUID, imgUUID):
        """
        Fetch the list of the Volumes UUIDs, not including the shared base (template)
        """
        # Get Volumes of an image
        pattern = os.path.join(os.path.join(repoPath, sdUUID, sd.DOMAIN_IMAGES, imgUUID, "*.meta"))
        files = oop.glob.glob(pattern)
        volList = []
        for i in files:
            volid = os.path.splitext(os.path.basename(i))[0]
            if SDF.produce(sdUUID).produceVolume(imgUUID, volid).getImage() == imgUUID:
                volList.append(volid)
        return volList

    @classmethod
    def getAllChildrenList(cls, repoPath, sdUUID, imgUUID, pvolUUID):
        """
        Fetch the list of children volumes (across the all images in domain)
        """
        volList = []
        # FIXME!!! We cannot check hardlinks in 'backup' domain, because of possibility of overwriting
        #  'fake' volumes that have hardlinks with 'legal' volumes with same uuid and without hardlinks
        # First, check number of hardlinks
     ## volPath = os.path.join(cls.storage_repository, spUUID, sdUUID, sd.DOMAIN_IMAGES, imgUUID, pvolUUID)
     ## if os.path.exists(volPath):
     ##     if os.stat(volPath).st_nlink == 1:
     ##         return volList
     ## else:
     ##     cls.log.info("Volume %s does not exist", volPath)
     ##     return volList
        # scan whole domain
        pattern = os.path.join(repoPath, sdUUID, sd.DOMAIN_IMAGES, "*", "*.meta")
        files = oop.glob.glob(pattern)
        for i in files:
            volid = os.path.splitext(os.path.basename(i))[0]
            imgUUID = os.path.basename(os.path.dirname(i))
            if SDF.produce(sdUUID).produceVolume(imgUUID, volid).getParent() == pvolUUID:
                volList.append({'imgUUID':imgUUID, 'volUUID':volid})

        return volList

    def findImagesByVolume(self, legal=False):
        """
        Find the image(s) UUID by one of its volume UUID.
        Templated and shared disks volumes may result more then one image.
        """
        try:
            vollist = oop.glob.glob(os.path.join(self.repoPath, self.sdUUID, sd.DOMAIN_IMAGES, "*", self.volUUID))
            for vol in vollist[:]:
                img = os.path.basename(os.path.dirname(vol))
                if img.startswith(image.REMOVED_IMAGE_PREFIX):
                    vollist.remove(vol)
        except Exception, e:
            self.log.info("Volume %s does not exists." % (self.volUUID))
            raise se.VolumeDoesNotExist("%s: %s:" % (self.volUUID, e))

        imglist = [ os.path.basename(os.path.dirname(vol)) for vol in vollist ]

        # Check image legallity, if needed
        if legal:
            for img in imglist[:]:
                if not image.Image(self.repoPath).isLegal(self.sdUUID, img):
                    imglist.remove(img)

        return imglist

    def getParent(self):
        """
        Return parent volume UUID
        """
        return self.getMetaParam(volume.PUUID)

    def getImage(self):
        """
        Return image UUID
        """
        return self.getMetaParam(volume.IMAGE)

    def setParent(self, puuid):
        """
        Set parent volume UUID
        """
        self.setMetaParam(volume.PUUID, puuid)

    def setImage(self, imgUUID):
        """
        Set image UUID
        """
        self.setMetaParam(volume.IMAGE, imgUUID)

    @classmethod
    def changeModifyTime(cls, src_name, dst_name):
        """
        Change last modify time of 'dst' to last modify time of 'src'
        """
        cmd = [constants.EXT_TOUCH, '-r', src_name, dst_name]
        (rc, out, err) = misc.execCmd(cmd, sudo=False)
        if rc:
            raise se.CannotModifyVolumeTime("src=%s dst=%s" % (src_name, dst_name))
        cmd = [constants.EXT_TOUCH, '-r', cls.__metaVolumePath(src_name), cls.__metaVolumePath(dst_name)]
        (rc, out, err) = misc.execCmd(cmd, sudo=False)
        if rc:
            raise se.CannotModifyVolumeTime("src=%s dst=%s" % (cls.__metaVolumePath(src_name), cls.__metaVolumePath(dst_name)))

    @classmethod
    def getVSize(cls, sdobj, imgUUID, volUUID, bs=512):
        return sdobj.produceVolume(imgUUID, volUUID).getVolumeSize(bs)


    @classmethod
    def getVTrueSize(cls, sdobj, imgUUID, volUUID, bs=512):
        return sdobj.produceVolume(imgUUID, volUUID).getVolumeTrueSize(bs)

    @classmethod
    def renameVolumeRollback(cls, taskObj, oldPath, newPath):
        try:
            cls.log.info("oldPath=%s newPath=%s", oldPath, newPath)
            oop.os.rename(oldPath, newPath)
        except Exception:
            cls.log.error("Could not rollback volume rename (oldPath=%s newPath=%s)", oldPath, newPath, exc_info=True)

    def rename(self, newUUID, recovery=True):
        """
        Rename volume
        """
        self.log.info("Rename volume %s as %s ", self.volUUID, newUUID)
        if not self.imagePath:
            self.validateImagePath()
        volPath = os.path.join(self.imagePath, newUUID)
        metaPath = self._getMetaVolumePath(volPath)
        prevMetaPath = self._getMetaVolumePath()

        if recovery:
            name = "Rename volume rollback: " + volPath
            vars.task.pushRecovery(task.Recovery(name, "fileVolume", "FileVolume", "renameVolumeRollback",
                                                 [volPath, self.volumePath]))
        oop.os.rename(self.volumePath, volPath)
        if recovery:
            name = "Rename meta-volume rollback: " + metaPath
            vars.task.pushRecovery(task.Recovery(name, "fileVolume", "FileVolume", "renameVolumeRollback",
                                                 [metaPath, prevMetaPath]))
        oop.os.rename(prevMetaPath, metaPath)
        self.volUUID = newUUID
        self.volumePath = volPath

    def validateImagePath(self):
        """
        Validate that the image dir exists and valid. In the file volume repositories,
        the image dir must exists after creation its first volume.
        """
        imageDir = image.Image(self.repoPath).getImageDir(self.sdUUID, self.imgUUID)
        if not oop.os.path.isdir(imageDir):
            raise se.ImagePathError(imageDir)
        if not oop.os.access(imageDir, os.R_OK | os.W_OK | os.X_OK):
            raise se.ImagePathError(imageDir)
        self.imagePath = imageDir

    @classmethod
    def __metaVolumePath(cls, vol_path):
        if vol_path:
            return vol_path + '.meta'
        else:
            return None

    def _getMetaVolumePath(self, vol_path=None):
        """
        Get/Set the path of the metadata volume file/link
        """
        if not vol_path:
            vol_path = self.getVolumePath()
        return self.__metaVolumePath(vol_path)

    def validateVolumePath(self):
        """
        In file volume repositories,
        the volume file and the volume md must exists after the image/volume is created.
        """
        self.log.debug("validate path for %s" % self.volUUID)
        if not self.imagePath:
            self.validateImagePath()
        volPath = os.path.join(self.imagePath, self.volUUID)
        if not oop.fileUtils.pathExists(volPath):
            raise se.VolumeDoesNotExist(self.volUUID)

        self.volumePath = volPath
        if not SDF.produce(self.sdUUID).isISO():
            self.validateMetaVolumePath()

    def validateMetaVolumePath(self):
        """
        In file volume repositories,
        the volume metadata must exists after the image/volume is created.
        """
        metaVolumePath = self._getMetaVolumePath()
        if not oop.fileUtils.pathExists(metaVolumePath):
            raise se.VolumeDoesNotExist(self.volUUID)

    def getVolumeSize(self, bs=512):
        """
        Return the volume size in blocks
        """
        volPath = self.getVolumePath()
        return int(int(oop.os.stat(volPath).st_size) / bs)


    def getVolumeTrueSize(self, bs=512):
        """
        Return the size of the storage allocated for this volume
        on underlying storage
        """
        volPath = self.getVolumePath()
        return int(int(oop.os.stat(volPath).st_blocks) * 512 / bs)


    def getVolumeMtime(self):
        """
        Return the volume mtime in msec epoch
        """
        volPath = self.getVolumePath()
        try:
            return self.getMetaParam(volume.MTIME)
        except se.MetaDataKeyNotFoundError:
            return oop.os.stat(volPath).st_mtime


