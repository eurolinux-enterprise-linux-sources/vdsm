#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#


import os.path
import uuid

from config import config
import storage_exception as se
import volume
import image
import sd
import misc
from misc import logskip
import task
import lvm
import resourceManager as rm
from threadLocal import vars
from sdf import StorageDomainFactory as SDF
from resourceFactories import LVM_ACTIVATION_NAMESPACE

TAG_PREFIX_MD = "MD_"
TAG_PREFIX_IMAGE =  "IU_"
TAG_PREFIX_PARENT = "PU_"
VOLUME_TAGS = [TAG_PREFIX_PARENT,
               TAG_PREFIX_IMAGE,
               TAG_PREFIX_MD]


# volume meta data block size
VOLUME_METASIZE = 512

rmanager = rm.ResourceManager.getInstance()

class BlockVolume(volume.Volume):
    """ Actually represents a single volume (i.e. part of virtual disk).
    """
    def __init__(self, repoPath, sdUUID, imgUUID, volUUID):
        self.metaoff = None
        volume.Volume.__init__(self, repoPath, sdUUID, imgUUID, volUUID)
        self.lvmActivationNamespace = sd.getNamespace(self.sdUUID, LVM_ACTIVATION_NAMESPACE)

    def validate(self):
        try:
            lvm.getLV(self.sdUUID, self.volUUID)
        except se.LogicalVolumeDoesNotExistError:
            raise se.VolumeDoesNotExist(self.volUUID) #Fix me
        volume.Volume.validate(self)


    def refreshVolume(self):
        lvm.refreshLV(self.sdUUID, self.volUUID)


    @classmethod
    def getVSize(cls, sdobj, imgUUID, volUUID, bs=512):
        return int(int(lvm.getLV(sdobj.sdUUID, volUUID).size) / bs)

    getVTrueSize = getVSize


    @classmethod
    def halfbakedVolumeRollback(cls, taskObj, sdUUID, volUUID, volPath):
        cls.log.info("sdUUID=%s volUUID=%s volPath=%s" % (sdUUID, volUUID, volPath))

        try:
            #Fix me: assert resource lock.
            lvm.getLV(sdUUID, volUUID)
            lvm.removeLV(sdUUID, volUUID)
        except se.LogicalVolumeDoesNotExistError, e:
            pass #It's OK: inexistent LV, don't try to remove.
        except se.CannotRemoveLogicalVolume, e:
            cls.log.warning("Remove logical volume failed %s/%s %s", sdUUID, volUUID, str(e))

        if os.path.lexists(volPath):
            os.unlink(volPath)

    @classmethod
    def validateCreateVolumeParams(cls, volFormat, preallocate, srcVolUUID):
        """
        Validate create volume parameters.
        'srcVolUUID' - backing volume UUID
        'volFormat' - volume format RAW/QCOW2
        'preallocate' - sparse/preallocate
        """
        volume.Volume.validateCreateVolumeParams(volFormat, preallocate, srcVolUUID)

        # Sparse-Raw not supported for block volumes
        if preallocate == volume.SPARSE_VOL and volFormat == volume.RAW_FORMAT:
            raise se.IncorrectFormat(srcVolUUID)

        # Snapshot should be COW volume
        if srcVolUUID != volume.BLANK_UUID and volFormat != volume.COW_FORMAT:
            raise se.IncorrectFormat(srcVolUUID)

    @classmethod
    def createVolumeMetadataRollback(cls, taskObj, sdUUID, offs):
        cls.log.info("createVolumeMetadataRollback: sdUUID=%s offs=%s" % (sdUUID, offs))
        metaid = [sdUUID, int(offs)]
        cls.__putMetadata({ "NONE": "#" * (sd.METASIZE-10) }, metaid)

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

        mysd = SDF.produce(sdUUID=sdUUID)
        try:
            lvm.getLV(sdUUID, volUUID)
        except se.LogicalVolumeDoesNotExistError:
            pass #OK, this is a new volume
        else:
            raise se.VolumeAlreadyExists(volUUID)

        imageDir = image.Image(repoPath).create(sdUUID, imgUUID)
        vol_path = os.path.join(imageDir, volUUID)
        pvol = None
        voltype = "LEAF"

        try:
            if srcVolUUID != volume.BLANK_UUID:
                # We have a parent
                if srcImgUUID == volume.BLANK_UUID:
                    srcImgUUID = imgUUID
                pvol = BlockVolume(repoPath, sdUUID, srcImgUUID, srcVolUUID)
                # Cannot create snapshot for ILLEGAL volume
                if not pvol.isLegal():
                    raise se.createIllegalVolumeSnapshotError(pvol.volUUID)

                if imgUUID != srcImgUUID:
                    pvol.share(imageDir, hard=False)
                    pvol = BlockVolume(repoPath, sdUUID, imgUUID, srcVolUUID)

                # override size param by parent's size
                size = pvol.getSize()
        except se.StorageException, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise
        except Exception, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise se.VolumeCannotGetParent("blockVolume can't get parent %s for volume %s: %s" % (srcVolUUID, volUUID, str(e)))

        try:
            cls.log.info("blockVolume: creating LV: volUUID %s" % (volUUID))
            if preallocate == volume.SPARSE_VOL:
                volsize = "%s" % config.get("irs", "volume_utilization_chunk_mb")
            else:
                # should stay %d and size should be int(size)
                volsize = "%s" % (size / 2 / 1024)
            vars.task.pushRecovery(task.Recovery("halfbaked volume rollback", "blockVolume", "BlockVolume", "halfbakedVolumeRollback",
                                                 [sdUUID, volUUID, vol_path]))
            lvm.createLV(sdUUID, volUUID, volsize, activate=True)
            if os.path.exists(vol_path):
                os.unlink(vol_path)
            os.symlink(lvm.lvPath(sdUUID, volUUID), vol_path)
        except se.StorageException, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise
        except Exception, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise se.VolumeCreationError("blockVolume create/link lv %s failed: %s" % (volUUID, str(e)))

        # By definition volume is now a leaf and should be writeable.
        # Default permission for lvcreate is read and write. No need to set permission.

        try:
            cls.log.info("blockVolume: create: volUUID %s srcImg %s srvVol %s" % (volUUID, srcImgUUID, srcVolUUID))
            if not pvol:
                cls.log.info("Request to create %s volume %s with size = %s sectors", volume.type2name(volFormat), vol_path, size)
                # Create 'raw' volume via qemu-img actually redundant
                if volFormat == volume.COW_FORMAT:
                    volume.createVolume(None, None, vol_path, size, volFormat, preallocate)
            else:
                ## Create hardlink to template and its meta file
                cls.log.info("Request to create snapshot %s/%s of volume %s/%s", imgUUID, volUUID, srcImgUUID, srcVolUUID)
                pvol.clone(imageDir, volUUID, volFormat, preallocate)
        except Exception, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise

        try:
            offs = mysd.mapMetaOffset(volUUID)
            vars.task.pushRecovery(task.Recovery("create block volume metadata rollback", "blockVolume", "BlockVolume", "createVolumeMetadataRollback",
                                                 [sdUUID, str(offs)]))
            lvm.addLVTags(sdUUID, volUUID, ("%s%s" % (TAG_PREFIX_MD, offs),
                            "%s%s" % (TAG_PREFIX_PARENT, srcVolUUID,), "%s%s" % (TAG_PREFIX_IMAGE, imgUUID,)))
            lvm.deactivateLVs(sdUUID, volUUID)
            # Set metadata and mark volume as legal.
            # FIXME: In next version we should remove imgUUID and srcVolUUID, as they are saved on lvm tags
            cls.newMetadata([sdUUID, offs], sdUUID, imgUUID, srcVolUUID,
                            size, volume.type2name(volFormat),
                            volume.type2name(preallocate), voltype,
                            diskType, desc, volume.LEGAL_VOL)
        except se.StorageException, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise
        except Exception, e:
            cls.log.error("Unexpected error", exc_info=True)
            raise se.VolumeMetadataWriteError("tag target volume %s failed: %s" % (volUUID, str(e)))

        # Remove all previous rollbacks for 'halfbaked' volume and add rollback for 'real' volume creation
        vars.task.replaceRecoveries(task.Recovery("create block volume rollback", "blockVolume", "BlockVolume", "createVolumeRollback",
                                             [repoPath, sdUUID, imgUUID, volUUID, imageDir]))

        return volUUID


    def delete(self, postZero, force):
        """ Delete volume
            'postZero' - zeroing file before deletion
            'force' is required to remove shared and internal volumes
        """
        self.log.info("Request to delete LV %s of image %s in VG %s ",
                      self.volUUID, self.imgUUID, self.sdUUID)

        vol_path = self.getVolumePath()
        size = self.getVolumeSize(bs=1)
        offs = self.getMetaOffset()

        if not force:
            self.validateDelete()

        # Mark volume as illegal before deleting
        self.setLegality(volume.ILLEGAL_VOL)

        if postZero:
            self.prepare(justme=True, rw=True, chainrw=force, setrw=True, force=True)
            try:
                # wipe out the whole volume
                idle = config.getfloat('irs', 'idle')
                try:
                    misc.ddWatchCopy("/dev/zero", vol_path, vars.task.aborting, idle, int(size))
                except se.ActionStopped, e:
                    raise e
                except Exception, e:
                    self.log.error("Unexpected error", exc_info=True)
                    raise se.VolumesZeroingError(vol_path)
            finally:
                self.teardown(justme=True)

        # try to cleanup as much as possible
        eFound = se.CannotDeleteVolume(self.volUUID)
        puuid = None
        try:
            # We need to blank parent record in our metadata
            # for parent to become leaf successfully.
            puuid = self.getParent()
            self.setParent(volume.BLANK_UUID)
            if puuid and puuid != volume.BLANK_UUID:
                pvol = BlockVolume(self.repoPath, self.sdUUID, self.imgUUID, puuid)
                pvol.recheckIfLeaf()
        except Exception, e:
            eFound = e
            self.log.warning("cannot finalize parent volume %s", puuid, exc_info=True)

        try:
            try:
                lvm.removeLV(self.sdUUID, self.volUUID)
            except se.CannotRemoveLogicalVolume:
                # At this point LV is already marked as illegal, we will try to cleanup whatever we can...
                pass

            self.removeMetadata([self.sdUUID, offs])
        except Exception, e:
            eFound = e
            self.log.error("cannot remove volume %s/%s", self.sdUUID, self.volUUID, exc_info=True)

        try:
            os.unlink(vol_path)
            return True
        except Exception, e:
            eFound = e
            self.log.error("cannot delete volume's %s/%s link path: %s", self.sdUUID, self.volUUID,
                            vol_path, exc_info=True)

        raise eFound

    def extend(self, newSize):
        """Extend a logical volume
            'newSize' - new size in blocks
        """
        self.log.info("Request to extend LV %s of image %s in VG %s with size = %s",
                      self.volUUID, self.imgUUID, self.sdUUID, newSize)
        # we should return: Success/Failure
        # Backend APIs:
        sizemb = (newSize + 2047) / 2048
        lvm.extendLV(self.sdUUID, self.volUUID, sizemb)

    @classmethod
    def changeModifyTime(cls, src_name, dst_name):
        """
        Change last modify time of 'dst' to last modify time of 'src'
        """
        pass

    @classmethod
    def renameVolumeRollback(cls, taskObj, sdUUID, oldUUID, newUUID):
        try:
            cls.log.info("renameVolumeRollback: sdUUID=%s oldUUID=%s newUUID=%s", sdUUID, oldUUID, newUUID)
            lvm.renameLV(sdUUID, oldUUID, newUUID)
        except Exception:
            cls.log.error("Failure in renameVolumeRollback: sdUUID=%s oldUUID=%s newUUID=%s", sdUUID, oldUUID, newUUID, exc_info=True)

    def rename(self, newUUID, recovery=True):
        """
        Rename volume
        """
        self.log.info("Rename volume %s as %s ", self.volUUID, newUUID)
        if not self.imagePath:
            self.validateImagePath()

        if os.path.lexists(self.getVolumePath()):
            os.unlink(self.getVolumePath())

        if recovery:
            name = "Rename volume rollback: " + newUUID
            vars.task.pushRecovery(task.Recovery(name, "blockVolume", "BlockVolume", "renameVolumeRollback",
                                                 [self.sdUUID, newUUID, self.volUUID]))
        lvm.renameLV(self.sdUUID, self.volUUID, newUUID)
        self.volUUID = newUUID
        self.volumePath = os.path.join(self.imagePath, newUUID)

    def getDevPath(self):
        """
        Return the underlying device (for sharing)
        """
        return lvm.lvPath(self.sdUUID, self.volUUID)

    def setrw(self, rw):
        """
        Set the read/write permission on the volume
        """
        lvm.setrwLV(self.sdUUID, self.volUUID, rw)

    @logskip("ResourceManager")
    def llPrepare(self, rw=False, setrw=False):
        """
        Perform low level volume use preparation

        For the Block Volumes the actual LV activation is wrapped
        into lvmActivation resource. It is being initialized by the
        storage domain sitting on top of the encapsulating VG.
        We just use it here.
        """
        if setrw:
            self.setrw(rw=rw)
        access = rm.LockType.exclusive if rw else rm.LockType.shared
        activation = rmanager.acquireResource(self.lvmActivationNamespace, self.volUUID, access)
        activation.autoRelease = False

    @logskip("ResourceManager")
    def llTeardown(self, rw=False, setrw=False):
        """
        Perform low level volume use teardown

        See also llPrepare() for implementation hints
        """
        if setrw:
            self.setrw(rw=rw)
        rmanager.releaseResource(self.lvmActivationNamespace, self.volUUID)

    def validateImagePath(self):
        """
        Block SD supports lazy image dir creation
        """
        imageDir = image.Image(self.repoPath).getImageDir(self.sdUUID, self.imgUUID)
        if not os.path.isdir(imageDir):
            try:
                os.mkdir(imageDir, 0755)
            except Exception:
                self.log.error("Unexpected error", exc_info=True)
                raise se.ImagePathError(imageDir)
        self.imagePath = imageDir

    def validateVolumePath(self):
        """
        Block SD supports lazy volume link creation. Note that the volume can be still inactive.
        An explicit prepare is required to validate that the volume is active.
        """
        if not self.imagePath:
            self.validateImagePath()
        volPath = os.path.join(self.imagePath, self.volUUID)
        if not os.path.lexists(volPath):
            os.symlink(lvm.lvPath(self.sdUUID, self.volUUID), volPath)
        self.volumePath = volPath

    def findImagesByVolume(self, legal=False):
        """
        Find the image(s) UUID by one of its volume UUID.
        Templated and shared disks volumes may result more then one image.
        """
        vollist = lvm.lvsByTag(self.sdUUID, "PU_%s" % self.volUUID)
        vollist.append(self.volUUID)
        imglist = []
        for vol in vollist:
            for tag in lvm.getLV(self.sdUUID, vol).tags:
                if tag.startswith("IU_"):
                    img = tag[3:]
                    if image.REMOVED_IMAGE_PREFIX not in img:
                        imglist.append(img)

        # Check image legallity, if needed
        if legal:
            for img in imglist[:]:
                if not image.Image(self.repoPath).isLegal(self.sdUUID, img):
                    imglist.remove(img)

        return imglist


    def getVolumeTag(self, tagPrefix):

        for tag in lvm.getLV(self.sdUUID, self.volUUID).tags:
            if tag.startswith(tagPrefix):
                return tag[len(tagPrefix):]

        raise se.MissingTagOnLogicalVolume(self.volUUID, tagPrefix)


    def changeVolumeTag(self, tagPrefix, uuid):

        if tagPrefix not in VOLUME_TAGS:
            raise se.LogicalVolumeWrongTagError(tagPrefix)

        oldTag = ""
        for tag in lvm.getLV(self.sdUUID, self.volUUID).tags:
            if tag.startswith(tagPrefix):
                oldTag = tag
                break

        if not oldTag:
            raise se.MissingTagOnLogicalVolume(self.volUUID, tagPrefix)

        newTag = tagPrefix + uuid
        if oldTag != newTag:
            lvm.replaceLVTag(self.sdUUID, self.volUUID, oldTag, newTag)

    def getParent(self):
        """
        Return parent volume UUID
        """
        return self.getVolumeTag("PU_")

    def getImage(self):
        """
        Return image UUID
        """
        return self.getVolumeTag("IU_")

    def setParent(self, puuid):
        """
        Set parent volume UUID
        """
        self.changeVolumeTag("PU_", puuid)
        #FIXME In next version we should remove PUUID, as it is saved on lvm tags
        self.setMetaParam(volume.PUUID, puuid)

    def setImage(self, imgUUID):
        """
        Set image UUID
        """
        self.changeVolumeTag("IU_", imgUUID)
        #FIXME In next version we should remove imgUUID, as it is saved on lvm tags
        self.setMetaParam(volume.IMAGE, imgUUID)

    @classmethod
    def getImageVolumes(cls, repoPath, sdUUID, imgUUID):
        """
        Fetch the list of the Volumes UUIDs, not including the shared base (template)
        """
        return lvm.lvsByTag(sdUUID, "IU_%s" % imgUUID)

    @classmethod
    def getAllChildrenList(cls, repoPath, sdUUID, imgUUID, pvolUUID):
        """
        Fetch the list of children volumes (across the all images in domain)
        """
        chList = []

        vollist = lvm.lvsByTag(sdUUID, "PU_%s" % pvolUUID)
        for v in vollist:
            for t in lvm.getLV(sdUUID, v).tags:
                if t.startswith("IU_"):
                    chList.append({'imgUUID':t[3:], 'volUUID':v})

        return chList

    def removeMetadata(self, metaid):
        """
        Just wipe meta.
        """
        try:
            self.__putMetadata({ "NONE": "#" * (sd.METASIZE-10) }, metaid)
        except Exception, e:
            self.log.error(e, exc_info=True)
            raise se.VolumeMetadataWriteError(str(metaid) + str(e))

    @classmethod
    def __putMetadata(cls, meta, metaid):
        vgname = metaid[0]
        offs = metaid[1]
        lines = ["%s=%s\n" % (key.strip(), str(value).strip()) for key, value in meta.iteritems()]
        lines.append("EOF\n")
        misc.writeblockSUDO(lvm.lvPath(vgname, sd.METADATA), offs * VOLUME_METASIZE,
            VOLUME_METASIZE, lines)

    @classmethod
    def createMetadata(cls, meta, metaid):
        cls.__putMetadata(meta, metaid)

    def getMetaOffset(self):
        if self.metaoff:
            return self.metaoff
        l = lvm.getLV(self.sdUUID, self.volUUID).tags
        for t in l:
            if t.startswith(TAG_PREFIX_MD):
                return int(t[3:])
        self.log.error("missing offset tag on volume %s", self.volUUID)
        raise se.VolumeMetadataReadError("missing offset tag on volume %s" % self.volUUID)

    def getMetadata(self, metaid=None, nocache=False):
        """
        Get Meta data array of key,values lines
        """
        if nocache:
            out = self.metaCache()
            if out:
                return out
        if not metaid:
            vgname = self.sdUUID
            offs = self.getMetaOffset()
        else:
            vgname = metaid[0]
            offs = metaid[1]
        try:
            meta = misc.readblockSUDO(lvm.lvPath(vgname, sd.METADATA),
                offs * VOLUME_METASIZE, VOLUME_METASIZE)
            out = {}
            for l in meta:
                if l.startswith("EOF"):
                    return out
                if l.find("=") < 0:
                    continue
                key, value = l.split("=")
                out[key.strip()] = value.strip()
        except Exception, e:
            self.log.error(e, exc_info=True)
            raise se.VolumeMetadataReadError(str(metaid) + ":" + str(e))
        self.putMetaCache(out)
        return out

    def setMetadata(self, metaarr, metaid=None, nocache=False):
        """
        Set the meta data hash as the new meta data of the Volume
        """
        if not metaid:
            metaid = [self.sdUUID, self.getMetaOffset()]
        try:
            self.__putMetadata(metaarr, metaid)
            if not nocache:
                self.putMetaCache(metaarr)
        except Exception, e:
            self.log.error(e, exc_info=True)
            raise se.VolumeMetadataWriteError(str(metaid) + str(e))

    def getVolumeSize(self, bs=512):
        """
        Return the volume size in blocks
        """
        # Just call the class method getVSize() - apparently it does what
        # we need. We consider incurred overhead of producing the SD object
        # to be a small price for code de-duplication.
        sdobj = SDF.produce(sdUUID=self.sdUUID)
        return self.getVSize(sdobj, self.imgUUID, self.volUUID, bs)

    getVolumeTrueSize = getVolumeSize

    def getVolumeMtime(self):
        """
        Return the volume mtime in msec epoch
        """
        try:
            mtime = self.getMetaParam(volume.MTIME)
        except se.MetaDataKeyNotFoundError:
            mtime = 0

        return mtime
