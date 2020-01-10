#!/usr/bin/python
#
# Copyright 2008 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import subprocess
import errno
import sys
import os
import logging
import logging.config
import traceback
from time import strftime
import ConfigParser
import deployUtil

VDSM_CONF_FILE = '/etc/vdsm/vdsm.conf'
log_filename = '/var/log/vdsm-reg/vds_bootstrap_upgrade.'+strftime("%Y%m%d_%H%M%S")+'.log'

try:
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%a, %d %b %Y %H:%M:%S',
                        filename=log_filename,
                        filemode='w')
except:
    log_filename = '/var/log/vds_bootstrap_upgrade.'+strftime("%Y%m%d_%H%M%S")+'.log'
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%a, %d %b %Y %H:%M:%S',
                        filename=log_filename,
                        filemode='w')

def setMountPoint(config):
    strFile = ""
    try:
        strFile = config.get('vars', 'upgrade_iso_file')
    except:
        strFile = '/data/updates/ovirt-node-image.iso'

    try:
        strMountPoint = config.get('vars', 'upgrade_mount_point')
    except:
        strMountPoint = '/var/vdsm/image-update'

    try:
        fOK = True
        ret = None
        err = ""
        out = None

        #First look for the upgrade file
        if not os.path.exists(strFile):
           fOK = False
           msg = "<BSTRAP component='setMountPoint' status='FAIL' message='Upgrade file not found'/>"
           logging.error(msg)
           print (msg)

        #Now, check if we need to create a mount-point dir.
        if fOK and not os.path.exists(strMountPoint):
           try: os.mkdir(strMountPoint)
           except OSError, err:
               if err.errno != errno.EEXIST:
                   fOK = False

        #Now, loop-mount the upgrade iso file.
        if not fOK:
           msg = "<BSTRAP component='setMountPoint' status='FAIL' message='Failed to create mount point: " + deployUtil.escapeXML(str(err)) + "'/>"
           print (msg)
           logging.error(msg)
        else:
           out, err, ret = deployUtil._logExec(["/bin/mount", "-o", "loop", strFile, strMountPoint])
           fOK = (ret != None and ret == 0)

        msg = ""
        if fOK:
           msg = "<BSTRAP component='setMountPoint' status='OK' message='Mount succeeded.'/>"
           logging.debug(msg)
        else:
           msg = "<BSTRAP component='setMountPoint' status='FAIL' message='Failed to mount ISO file: " + deployUtil.escapeXML(str(err)) + "'/>"
           logging.error(msg)
        print msg
    except Exception, e:
        fOK = False
        msg = "<BSTRAP component='setMountPoint' status='FAIL' message='setMountPoint exception: " + deployUtil.escapeXML(str(e)) + "'/>"
        logging.error(msg)
        print (msg)

    return fOK

def doUpgrade(config):
    out = None
    err = None
    ret = None
    fReturn = True

    try:
        strMountPoint = config.get('vars', 'upgrade_mount_point')
    except:
        strMountPoint = '/var/vdsm/image-update'

    if os.path.exists("/usr/libexec/ovirt-config-boot"):
        out, err, ret = deployUtil._logExec(["/usr/libexec/ovirt-config-boot", strMountPoint])
    else:
        err = "Can not find /usr/libexec/ovirt-config-boot !"

    if ret == None or ret != 0:
        msg = "<BSTRAP component='doUpgrade' status='FAIL' message='" + deployUtil.escapeXML(str(err)) + "'/>"
        print (msg)
        logging.error(msg)
        fReturn = False
    else:
        msg = "<BSTRAP component='doUpgrade' status='OK' message='Upgrade Succeeded. Rebooting'/>"
        print (msg)
        logging.debug(msg)

    return fReturn

def umount(config, shouldReport=True):
    out = None
    err = None
    ret = None
    fReturn = True

    try:
        strMountPoint = config.get('vars', 'upgrade_mount_point')
    except:
        strMountPoint = '/var/vdsm/image-update'

    if os.path.exists(strMountPoint):
        out, err, ret = deployUtil._logExec(["/bin/umount", strMountPoint])
        fReturn = (ret != None and ret == 0)

    if fReturn:
        msg = "<BSTRAP component='umount' status='OK' message='umount Succeeded'/>"
    else:
        msg = "<BSTRAP component='umount' status='FAIL' message=' " + deployUtil.escapeXML(str(err)) + "'/>"

    if shouldReport:
        print (msg)

    logging.debug(msg)
    return fReturn

def main():
    """Usage: vdsm-upgrade """
    fOK = True
    fMounted = False

    try:
        config = ConfigParser.ConfigParser()
        config.read('/etc/vdsm-reg/vdsm-reg.conf')

        #First, quietly try to clean any previous problems.
        umount(config, False)

        #Now: Try mounting
        fOK = setMountPoint(config)
        if fOK:
            fMounted = True
            fOK = doUpgrade(config)

        #Finally, is possible umount current upgrade file.
        if fMounted:
            umount(config) #cleanup, may fail in some cases- device busy.
    except:
        fOK = False

    if not fOK:
        msg = "<BSTRAP component='RHEV_INSTALL' status='FAIL'/>"
        logging.error("<BSTRAP component='RHEV_INSTALL' status='FAIL'/>")
    else:
        msg = "<BSTRAP component='RHEV_INSTALL' status='OK'/>"
        logging.debug("<BSTRAP component='RHEV_INSTALL' status='OK'/>")
    print (msg)

    sys.stdout.flush()
    return fOK

if __name__ == "__main__":
    sys.exit(main())

