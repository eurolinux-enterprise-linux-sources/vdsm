### This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.

## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.

## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

import sos.plugintools
import subprocess
import ConfigParser
import os

class vdsm(sos.plugintools.PluginBase):
    """VDSM server related information
    """
    def setup(self):
        os.environ["LVM_SYSTEM_DIR"] = "@VDSMRUNDIR@/lvm"
        self.collectExtOutput("/etc/init.d/vdsmd status")
        self.addCopySpec("/tmp/vds_installer*")
        self.addCopySpec("/tmp/vds_bootstrap*")
        self.addCopySpec("/etc/vdsm/*")
        self.addCopySpec("/var/log/vdsm/*")
        self.addCopySpec("/var/log/vdsm-reg/")
        self._addVdsmRunDir()
        self.addCopySpec("@TRUSTSTORE@")
        self.addCopySpec("@HOOKSDIR@")
        self.addCopySpec("/var/log/ovirt.log")
        self.addCopySpec("/etc/multipath.conf")
        self.addCopySpec("/etc/lvm/lvm.conf")
        self.collectExtOutput("/bin/netstat")
        p = subprocess.Popen(['/usr/bin/pgrep', 'qemu-kvm'],
                             stdout=subprocess.PIPE)
        out, err = p.communicate()
        for line in out.splitlines():
            pid = line.strip()
            self.addCopySpec("/proc/%s/cmdline" % pid)
            self.addCopySpec("/proc/%s/status" % pid)
            self.addCopySpec("/proc/%s/mountstats" % pid)
        self.collectExtOutput("/bin/ls -l /var/log/core")
        self.collectExtOutput("/bin/ps ax")
        self.collectExtOutput("/sbin/ifconfig -a")
        self.collectExtOutput("/sbin/lsmod")
        self.collectExtOutput("/usr/sbin/dmidecode")
        self.collectExtOutput("/sbin/iptables -L -v")
        self.collectExtOutput("/bin/su vdsm -s /bin/sh -c '/usr/bin/tree /rhev/data-center/'")
        self.collectExtOutput("/bin/su vdsm -s /bin/sh -c '/bin/ls -lR /rhev/'")
        self.collectExtOutput("/sbin/multipath -ll")
        self.collectExtOutput("/sbin/dmsetup info")
        self.collectExtOutput("/sbin/lvm vgs -v -o +tags")
        self.collectExtOutput("/sbin/lvm lvs -v -o +tags")
        self.collectExtOutput("/sbin/lvm pvs -v -o +all")
        self.collectExtOutput("/sbin/fdisk -l")
        self.collectExtOutput("/usr/bin/iostat")
        self.collectExtOutput("/sbin/iscsiadm -m node")
        self.collectExtOutput("/sbin/iscsiadm -m session")
        self.collectExtOutput("/bin/df")
        config = ConfigParser.ConfigParser()
        config.read('/etc/vdsm/vdsm.conf')
        sslopt = ['', '-s '][config.getboolean('vars', 'ssl')]
        vdsclient = "/usr/bin/vdsClient " + sslopt + "0 "
        self.collectExtOutput(vdsclient + "getVdsCapabilities")
        self.collectExtOutput(vdsclient + "getVdsStats")
        self.collectExtOutput(vdsclient + "getAllVmStats")
        self.collectExtOutput(vdsclient + "list")
        self.collectExtOutput(vdsclient + "getVGList")
        self.collectExtOutput(vdsclient + "getDeviceList")
        self.collectExtOutput(vdsclient + "getSessionList")
        self.collectExtOutput(vdsclient + "getAllTasksInfo")
        self.collectExtOutput(vdsclient + "getAllTasksStatuses")
        p = subprocess.Popen(vdsclient + "getConnectedStoragePoolsList",
                             shell=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        out, err = p.communicate()
        for line in out.splitlines()[1:-1]:
            pool = line.strip()
            self.collectExtOutput(vdsclient + "getSpmStatus " + pool)
        self.collectExtOutput('/bin/su vdsm -s /usr/bin/python @VDSMDIR@/dumpStorageTable.pyc')

    def _addVdsmRunDir(self):
        """Add everything under /var/run/vdsm except possibly confidential
        sysprep vfds """

        import glob

        for f in glob.glob("@VDSMRUNDIR@/*"):
            if not f.endswith('.vfd') and not f.endswith('/isoUploader'):
                self.addCopySpec(f)
