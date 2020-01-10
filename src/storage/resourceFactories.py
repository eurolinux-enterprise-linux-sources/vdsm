#
# Copyright 2010 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import os
from config import config
import logging
import lvm
import resourceManager as rm
import storage_exception as se
from sdf import StorageDomainFactory as SDF
import sd
import image

LVM_ACTIVATION_NAMESPACE = 'lvmActivationNS'
IMAGE_NAMESPACE = 'imageNS'
VOLUME_NAMESPACE = 'volumeNS'

rmanager = rm.ResourceManager.getInstance()

log = logging.getLogger('Storage.ResourcesFactories')

class LvmActivation(object):
    """
    Represents activation state of the LV.
    When the resource is created (i.e. the LV is being activated)
    it calls lvm.activateLVs(). When the resource is being finally released
    the close() calls lvm.deactivateLVs() to release the DM mappings
    for this volume.
    """
    def __init__(self, vg, lv, lockType):
        self._vg = vg
        self._lv = lv
        lvm.activateLVs(self._vg, self._lv)

    def close(self):
        lvm.deactivateLVs(self._vg, self._lv)

    def switchLockType(self, lockType):
        rw = False if lockType == rm.LockType.shared else True
        lvm.setrwLV(self._vg, self._lv, rw)


class LvmActivationFactory(rm.SimpleResourceFactory):
    def __init__(self, vg):
        rm.SimpleResourceFactory.__init__(self)
        self._vg = vg

    def resourceExists(self, resourceName):
        try:
            lvm.getLV(self._vg, resourceName)
            res = True
        except se.LogicalVolumeDoesNotExistError, e:
            res = False

        return res

    def createResource(self, resourceName, lockType):
        return LvmActivation(self._vg, resourceName, lockType)


class ImageResource(object):
    """
    Represents resource for image's volumes.
    """
    def __init__(self, volResourcesList):
        self.volResourcesList = volResourcesList

    def close(self):
        # Release template/volumes locks
        for volRes in self.volResourcesList:
            volRes.release()

class ImageResourceFactory(rm.SimpleResourceFactory):
    """
    This factory produce resources for images
    """
    storage_repository = config.get('irs', 'repository')
    resource_default_timeout = config.getint('irs', 'task_resource_default_timeout')

    def __init__(self, sdUUID):
        rm.SimpleResourceFactory.__init__(self)
        self.sdUUID = sdUUID
        self.volumeResourcesNamespace = sd.getNamespace(self.sdUUID, VOLUME_NAMESPACE)

    def __getResourceCandidatesList(self, resourceName, lockType):
        """
        Return list of lock candidates (tempalte and volumes)
        """
        volResourcesList = []
        template = None
        dom = SDF.produce(sdUUID=self.sdUUID)
        # Get the list of the volumes
        repoPath = os.path.join(self.storage_repository, dom.getPools()[0])
        try:
            chain = image.Image(repoPath).getChain(sdUUID=self.sdUUID, imgUUID=resourceName)
        except se.ImageDoesNotExistInSD:
            log.debug("Image %s does not exist in domain %s", resourceName, self.sdUUID)
            return []

        # check if the chain is build above a template, or it is a standalone
        pvol = chain[0].getParentVolume()
        if pvol:
            template = pvol.volUUID
        elif chain[0].isShared():
            # Image of template itself,
            # with no other volumes in chain
            template = chain[0].volUUID
            del chain[:]

        volUUIDChain = [vol.volUUID for vol in chain]
        volUUIDChain.sort()

        e = None
        # Acquire template locks:
        # - 'lockType' for template's image itself
        # - Always 'shared' lock for image based on template
        try:
            if template:
                if len(volUUIDChain) > 0:
                    volRes = rmanager.acquireResource(self.volumeResourcesNamespace, template, rm.LockType.shared,
                                                      timeout=self.resource_default_timeout)
                else:
                    volRes = rmanager.acquireResource(self.volumeResourcesNamespace, template, lockType,
                                                      timeout=self.resource_default_timeout)
                volResourcesList.append(volRes)

            # Acquire 'lockType' volume locks
            for volUUID in volUUIDChain:
                volRes = rmanager.acquireResource(self.volumeResourcesNamespace, volUUID, lockType)
                volResourcesList.append(volRes)
        except (se.RequestTimedOutError, se.ResourceAcqusitionFailed), e:
            log.debug("Cannot acquire volume resource (%s)", str(e))
        except Exception, e:
            log.debug("Cannot acquire volume resource", exc_info=True)

        if e:
            # Release already acquired template/volumes locks
            for volRes in self.volResourcesList:
                volRes.release()

        return volResourcesList


    def createResource(self, resourceName, lockType):
        volResourcesList = self.__getResourceCandidatesList(resourceName, lockType)
        return ImageResource(volResourcesList)

