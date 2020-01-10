# Copyright 2008-2010 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import os, traceback, signal, errno
import time
import threading, logging, subprocess
import constants
import utils
import netinfo
from define import NORMAL, ERROR, doneCode, errCode
import guestIF
import QemuMonitor
from config import config
import kaxmlrpclib
import pickle
from logUtils import SimpleLogAdapter
from copy import deepcopy
import tempfile

MEGAB = 2 ** 20 # = 1024 ** 2 = 1 MiB

"""
A module containing classes needed for VM communication.
"""

class HdSample:
    """
    Represents a hard disk sample.

    .. attribute:: rdb

        read bytes

    .. attribute:: wrb

        written bytes
    """
    def __init__(self, rdb, wrb):
        self.rdb = rdb
        self.wrb = wrb

class HdsSample:
    """
    Represents a collection of hard disk samples.

    .. attribute:: hds

        A dictionary of hard disk samples
    """
    def __init__(self, vm):
        self.hds = {}
        for line in vm._sendMonitorCommand('info blockstats').splitlines():
            try:
                if not line: continue
                splitted = line.split()
                name = splitted[0][:-1]
                for s in splitted[1:]:
                    if s.startswith('rd_bytes='):
                        rdb = s[9:]
                    elif s.startswith('wr_bytes='):
                        wrb = s[9:]
                self.hds[name] = HdSample(int(rdb), int(wrb))
            except:
                vm.log.warning(line + traceback.format_exc())

class VmSample(utils.BaseSample):
    """
    Represents a sample of a VM's state.
    """
    def __init__(self, pid, ifids, vm):
        # FIXME due to its side-effects of testing whether qemu process is
        # still alive, HdsSample must appear before anything else that might
        # throw an exception. this is ugly.
        self.hdssample = vm.HdsSampleClass(vm)
        utils.BaseSample.__init__(self, pid, ifids)

class VmStatsThread(utils.StatsThread):
    """
    A thread that samples VM statistics periodically.
    """
    VmSampleClass = VmSample
    def __init__(self, log, ifids, ifrates, ifmacs,
                 vm, pid):
        """
        Initialize a new VmStatsThread.

        :param log: a log object to be used for this thread's logging.
        :type log: :class:`logging.Logger`
        :param ifid: Interface IDs of the network interfaces.
        :type ifid: list
        :param ifrates: A list of transefer rates the interfaces work at.
        :type ifrates: list
        :param ifmac: A list of MAC addresses of the interfaces stated.
        :type ifmac: list
        :param vm: The :class:`~vm.VM` object we are going to talk to.
        :type vm: :class:`~vm.VM`
        :param pid: The PID of the Qemu process we are going to monitor.
        :type pid: int
        """
        self.SAMPLE_INTERVAL_SEC = config.getint('vars', 'vm_sample_interval')
        utils.StatsThread.__init__(self, log, ifids, ifrates, ifmacs)
        self.setDaemon(True) # XXX ugly. better use .stop()
        self._vm = vm
        self._pid = pid
        self._sizeMeasureTime = 0
        self.log = log

    def _retryLvExtend(self):
        now = time.time()
        moratorium = config.getint('irs', 'lv_extend_moratorium')
        for d in self._vm._drives:
            if d.needExtend and now - d.lastLvExtend > moratorium:
                self._vm._lvExtend(d.name)
        if now - self._sizeMeasureTime > config.getint('irs',
                                               'vol_size_sample_interval'):
            self._sizeMeasureTime = now
            for d in self._vm._drives:
                res = self._vm.cif.irs.getVolumeSize(d.domainID,
                                               d.poolID, d.imageID,
                                               d.volumeID)
                if res['status']['code'] == 0 and not d.needExtend:
                    d.truesize = int(res['truesize'])
                    d.apparentsize = int(res['apparentsize'])

    def sample(self):
        """
        Create a sample of the current VM state.

        :returns: a :class:`VmSample` representing the current state.
        """
        self._retryLvExtend()
        s = self.VmSampleClass(self._pid, self._ifids, self._vm)
        return s

    def get(self):
        """
        Calculate and obtain averaged statistics of the VM.

        :returns: a dict contating the full stat of the VM.
        """
        stats = utils.StatsThread.get(self)
        if len(self._samples) < 2:
            return stats
        # note that vms0, vms1 may be other than hs0, hs1 used in the
        # superclass.
        vms0, vms1 = self._samples[0], self._samples[-1]
        interval = vms1.timestamp - vms0.timestamp

        for hd, hdsample in vms1.hdssample.hds.items():
            stats[hd] = {}
            stats[hd]['imageID'] = ''
            stats[hd]['truesize'] = ''
            stats[hd]['apparentsize'] = ''
            for d in self._vm._drives:
                if d.name != hd: continue
                stats[hd]['imageID'] = d.imageID
                stats[hd]['truesize'] = str(d.truesize)
                stats[hd]['apparentsize'] = str(d.apparentsize)
            try:
                rdb0 = vms0.hdssample.hds[hd].rdb
                wrb0 = vms0.hdssample.hds[hd].wrb
            except:
                # disks may just appear (such as in mig destination)
                rdb0 = hdsample.rdb
                wrb0 = hdsample.wrb
            stats[hd]['readRate'] = (hdsample.rdb - rdb0)  % 2**64 / interval
            stats[hd]['writeRate'] = (hdsample.wrb - wrb0) % 2**64 / interval

        jiffies = (vms1.pidcpu.user - vms0.pidcpu.user) % 2**32
        stats['cpuUser'] = jiffies / interval
        jiffies = (vms1.pidcpu.sys - vms0.pidcpu.sys) % 2**32
        stats['cpuSys'] = jiffies / interval

        return stats

class Drive:
    def __init__(self, poolID, domainID, imageID, volumeID, path, truesize,
            apparentsize, blockDev, index='', bus='', unit='', serial='',
            format='raw', boot=None, propagateErrors='off', reqsize=0,
            alias='', **kwargs):
        self.poolID = poolID
        self.domainID = domainID
        self.imageID = imageID
        self.volumeID = volumeID
        self.path = path
        self.name = None
        self.truesize = int(truesize)
        self.apparentsize = int(apparentsize)
        self.blockDev = blockDev
        self.lastLvExtend = 0
        self.needExtend = False
        self.reqsize = int(reqsize)
        self.iface = kwargs.get('if')
        self.index = index
        self.bus = bus
        self.unit = unit
        self.serial = serial
        self.format = format
        self.propagateErrors = propagateErrors
        self.boot = boot
        self.alias = alias


class _MigrationError(RuntimeError): pass

class MigrationSourceThread(threading.Thread):
    """
    A thread that takes care of migration on the source vdsm.
    """
    _ongoingMigrations = threading.BoundedSemaphore(1)
    @classmethod
    def setMaxOutgoingMigrations(klass, n):
        """Set the initial value of the _ongoingMigrations semaphore.

        must not be called after any vm has been run."""
        klass._ongoingMigrations = threading.BoundedSemaphore(n)

    def __init__ (self, vm, dst='', dstparams='',
                  mode='remote', method='online', **kwargs):
        self.log = vm.log
        self._vm = vm
        self._dst = dst
        self._mode = mode
        self._method = method
        self._dstparams = dstparams
        self._machineParams = {}
        self._downtime = kwargs.get('downtime') or \
                            config.get('vars', 'migration_downtime')
        self.status = {'status': {'code': 0, 'message': 'Migration in process'}, 'progress': 0}
        threading.Thread.__init__(self)

    def getStat (self):
        """
        Get the status of the migration.
        """
        return self.status

    def _setupVdsConnection(self):
        if self._mode == 'file': return
        self.remoteHost = self._dst.split(':')[0]
        self.remotePort = self._vm.cif.serverPort
        try:
            self.remotePort = self._dst.split(':')[1]
        except:
            pass
        if config.getboolean('vars', 'ssl'):
            from M2Crypto import SSL

            KEYFILE, CERTFILE, CACERT = self._vm.cif.getKeyCertFilenames()

            ctx = SSL.Context()
            ctx.set_verify(SSL.verify_peer | SSL.verify_fail_if_no_peer_cert, 16)
            ctx.load_verify_locations(CACERT)
            ctx.load_cert(CERTFILE, KEYFILE)

            serverAddress = 'https://' + self.remoteHost + ':' + self.remotePort
            self.destServer = kaxmlrpclib.SslServer(serverAddress, ctx)
        else:
            serverAddress = 'http://' + self.remoteHost + ':' + self.remotePort
            self.destServer = kaxmlrpclib.Server(serverAddress)
        self.log.debug('Destination server is: ' + serverAddress)
        try:
            self.log.debug('Initiating connection with destination')
            status = self.destServer.getVmStats(self._vm.id)
            if not status['status']['code']:
                self.log.error("Machine already exists on the destination")
                self.status = errCode['exist']
        except:
            self.log.error(traceback.format_exc())
            self.status = errCode['noConPeer']

    def _setupRemoteMachineParams(self):
        self._machineParams.update(self._vm.status())
        if self._vm._guestCpuRunning:
            self._machineParams['afterMigrationStatus'] = 'Up'
        else:
            self._machineParams['afterMigrationStatus'] = 'Pause'
        self._machineParams['elapsedTimeOffset'] = \
                                time.time() - self._vm._startTime
        vmStats = self._vm.getStats()
        if 'username' in vmStats:
            self._machineParams['username'] = vmStats['username']
        if 'guestIPs' in vmStats:
            self._machineParams['guestIPs'] = vmStats['guestIPs']
        for k in ('_migrationParams', 'pid'):
            if k in self._machineParams:
                del self._machineParams[k]

    def _killDestVmIfUnused(self):
        # on recovery, kill remote qemu if it was created and not used
        if self._vm.conf.get('_migrationParams'):
            mstate = self._vm._migrateInfo()
            if not mstate:
                del self._vm.conf['_migrationParams']
                raise _MigrationError('service failed during migration')

    def _prepareGuest(self):
        if self._mode == 'file':
            self.log.debug("Save State begins")
            if self._vm.guestAgent.isResponsive():
                lockTimeout = 30
            else:
                lockTimeout = 0
            self._vm.guestAgent.desktopLock()
            #wait for lock or timeout
            while lockTimeout:
                if self._vm.getStats()['session'] in ["Locked", "LoggedOff"]:
                    break
                time.sleep(1)
                lockTimeout -= 1
                if lockTimeout == 0:
                    self.log.warning('Agent ' + self._vm.id +
                            ' unresponsive. Hiberanting without desktopLock.')
                    break
            self._vm.pause('Saving State')
        else:
            self.log.debug("migration Process begins")
            self._vm.lastStatus = 'Migration Source'

    def _startUnderlyingMigration(self):
        self._vm._mon.postCommand(self._buildMigrateCommand())

    def _recover(self, message):
        self.status = errCode['migrateErr']
        self.log.error(message)
        if self._mode != 'file':
            try:
                self.destServer.destroy(self._vm.id)
            except:
                self.log.error(traceback.format_exc())
        # if the guest was stopped before migration, we need to cont it
        if self._mode == 'file' or self._method != 'online':
            self._vm.cont()
        # either way, migration has finished
        self._vm.lastStatus = 'Up'

    def _waitForOutgoingMigration(self):
        now = time.time()
        timeout = self._vm._migrationTimeout()
        setDowntime = now + timeout / 2
        end = now + 2 * timeout
        currentDowntime = '100ms' # qemu default
        while True:
            if self._vm.lastStatus == 'Down':
                raise _MigrationError('source migration: VM is down')
            mstate = self._vm._migrateInfo()
            if self.status['progress'] <= 90:
                self.status['progress'] += 5
            if mstate == 'failed' or mstate == 'cancelled':
                raise _MigrationError('source migration: %s'% mstate)
            elif mstate == 'completed':
                self.log.debug('Source qemu reports successful migration')
                break
            now = time.time()
            if now > end:
                raise _MigrationError('Migration timeout exceeded at source')
            if self._mode != 'file' and now > setDowntime and \
                                        self._downtime != currentDowntime:
                # FIXME we're handling only the case of _downtime > qemu
                # default. If anyone needs a stronger promise of availability,
                # we should set this value before migration starts.
                self._vm._mon.postCommand('migrate_set_downtime %s' %
                                          self._downtime)
                currentDowntime = self._downtime
                self.log.info('source migration: set max downtime to %s',
                              self._downtime)
            time.sleep(1)

    def _finishSuccessfully(self):
        if self._mode != 'file':
            self._vm.setDownStatus(NORMAL, "Migration succeeded")
            self.status = {'status': {'code': 0, 'message': 'Migration done'}, 'progress': 100}
        else:
            # don't pickle transient params
            for ignoreParam in ('displayIp', 'display', 'pid'):
                if ignoreParam in self._machineParams:
                    del self._machineParams[ignoreParam]

            fname = self._vm.cif._prepareVolumePath(self._dstparams)
            try:
                with file(fname, "w") as f:
                    pickle.dump(self._machineParams, f)
            finally:
                self._vm.cif._teardownVolumePath(self._dstparams)

            self._vm.setDownStatus(NORMAL, "SaveState succeeded")
            self.status = {'status': {'code': 0, 'message': 'SaveState done'}, 'progress': 100}

    def _buildMigrateCommand(self):
        if self._mode != 'file':
            try:
                response = self.destServer.migrationCreate(self._machineParams)
            except:
                self.log.error(traceback.format_exc())
                raise _MigrationError("Destination VDS rejected connection")
            self.status['progress'] = 20
            if response['status']['code']:
                raise _MigrationError(response['status']['message'])
            migrationPort = response['migrationPort']
            destinationParams = response['params'].copy()
            if self._method != 'online':
                response = self._vm.pause('Migration Source')
                if response['status']['code']:
                    raise _MigrationError(response['status']['message'])
            self.status['progress'] = 30
            #setDisplayParameters
            self.log.debug('destinationParams = ' + str(destinationParams))
            if 'qxl' in destinationParams['display']:
                if destinationParams['displayIp'] != '0':
                    displayIp = destinationParams['displayIp']
                else:
                    displayIp = self.remoteHost
                display = ",spicehost=%s,spiceport=%s" % \
                    (displayIp, int(destinationParams['displayPort']))
                if 'displaySecurePort' in destinationParams and \
                       'spiceSslCipherSuite' in self._machineParams:
                    display += ',spicesport=' + \
                               str(destinationParams['displaySecurePort'])
            else:
                display = ""
            startCommand = 'migrate -d tcp:' + self.remoteHost+':'+migrationPort+display
        else:
            startCommand = 'migrate -d exec:' + constants.EXT_CAT + '>' + self._dst
        return startCommand

    def run(self):
        try:
            mstate = ''
            self._setupVdsConnection()
            self._setupRemoteMachineParams()
            self._killDestVmIfUnused()
            self._prepareGuest()
            self.status['progress'] = 10
            MigrationSourceThread._ongoingMigrations.acquire()
            try:
                self.log.debug("migration semaphore acquired")
                if not mstate:
                    self._vm.conf['_migrationParams'] = {'dst': self._dst,
                                'mode': self._mode, 'method': self._method,
                                'dstparams': self._dstparams}
                    self._vm.saveState()
                    self._startUnderlyingMigration()
                self._waitForOutgoingMigration()
                self._finishSuccessfully()
            finally:
                if '_migrationParams' in self._vm.conf:
                    del self._vm.conf['_migrationParams']
                MigrationSourceThread._ongoingMigrations.release()
        except Exception, e:
            self._recover(str(e))
            self.log.error(traceback.format_exc())


class VolumeError(RuntimeError): pass
class DoubleDownError(RuntimeError): pass

VALID_STATES = ('Down', 'Migration Destination', 'Migration Source',
                'Paused', 'Powering down', 'RebootInProgress',
                'Restoring state', 'Saving State',
                'Up', 'WaitForLaunch')

class Vm(object):
    """
    Used for abstracting cummunication between various parts of the
    system and Qemu.

    Runs Qemu in a subprocess and communicates with it, and monitors
    its behaviour.
    """
    log = logging.getLogger("vm.Vm")
    _ongoingCreations = threading.BoundedSemaphore(1)
    HdsSampleClass = HdsSample
    VmStatsThreadClass = VmStatsThread
    MigrationSourceThreadClass = MigrationSourceThread
    def __init__(self, cif, params):
        """
        Initialize a new VM instance.

        :param cif: The client interface that creates this VM.
        :type cif: :class:`clientIF.clientIF`
        :param params: The VM parameters.
        :type params: dict
        """
        self.conf = {'pid': '0'}
        self.conf.update(params)
        self.cif = cif
        self.log = SimpleLogAdapter(self.log, {"vmId" : self.conf['vmId']})
        self.destroyed = False
        self._recoveryFile = constants.P_VDSM_RUN + str(
                                    self.conf['vmId']) + '.recovery'
        self.dumpFile = constants.P_VDSM_RUN + self.conf['vmId'] + ".stdio.dump"
        self.user_destroy = False
        self._migratedAway = False
        self._monitorResponse = 0
        self.conf['clientIp'] = ''
        self.memCommitted = 0
        self._creationThread = threading.Thread(target=self._startUnderlyingVm)
        self.pidfile = constants.P_VDSM_RUN + self.conf['vmId'] + '.pid'
        if 'migrationDest' in self.conf:
            self._lastStatus = 'Migration Destination'
        elif 'restoreState' in self.conf:
            self._lastStatus = 'Restoring state'
        else:
            self._lastStatus = 'WaitForLaunch'
        self._nice = ''
        self._migrationSourceThread = self.MigrationSourceThreadClass(self)
        self._kvmEnable = self.conf.get('kvmEnable', 'true')
        self._guestSocektFile = constants.P_VDSM_RUN + self.conf['vmId'] + \
                                '.guest.socket'
        self._monitorSocketFile = constants.P_VDSM_RUN + self.conf['vmId'] + \
                                        '.monitor.socket'
        self._drives = []
        self._incomingMigrationFinished = threading.Event()
        self.id = self.conf['vmId']
        self._shellPid = 0
        self._pidLock = threading.Lock()
        self._mon = None
        self._monInitLock = threading.Lock()
        self._volPrepareLock = threading.Lock()
        self._preparedDrives = {}
        self._tryMonitorDependentInit = True
        self._initTimePauseCode = None
        self.guestAgent = None
        self._guestEvent = 'Powering up'
        self._guestEventTime = 0
        self._vmStats = None
        self._guestCpuRunning = False
        self._guestCpuLock = threading.Lock()
        self._startTime = time.time() - float(
                                self.conf.pop('elapsedTimeOffset', 0))
        self._cdromPreparedPath = ''
        self._floppyPreparedPath = ''
        self._pathsPreparedEvent = threading.Event()
        self.saveState()

    def _get_lastStatus(self):
        SHOW_PAUSED_STATES = ('Powering down', 'RebootInProgress', 'Up')
        if not self._guestCpuRunning and self._lastStatus in SHOW_PAUSED_STATES:
            return 'Paused'
        return self._lastStatus

    def _set_lastStatus(self, value):
        if self._lastStatus == 'Down':
            self.log.warning('trying to set state to %s when already Down',
                             value)
            if value == 'Down':
                raise DoubleDownError
            else:
                return
        if value not in VALID_STATES:
            self.log.error('setting state to %s', value)
        if self._lastStatus != value:
            self.saveState()
            self._lastStatus = value

    lastStatus = property(_get_lastStatus, _set_lastStatus)

    def run(self):
        self._creationThread.start()

    def memCommit(self):
        """
        Reserve the required memory for this VM.
        """
        self.memCommitted = 2**20 * (int(self.conf['memSize']) +
                                config.getint('vars', 'guest_ram_overhead'))

    def _startUnderlyingVm(self):
        try:
            if 'recover' not in self.conf:
                if not self.cif.memTestAndCommit(self):
                    self.setDownStatus(ERROR,
                                       'Out of memory - machine not created')
                    return
            else:
                self.memCommit()
            rc, err = self._prepostVmScript('pre_vm')
            if rc:
                self.setDownStatus(ERROR, 'pre_vm: ' + err)
                return
            self._ongoingCreations.acquire()
            try:
                self._run()
                if self.lastStatus != 'Down' and 'recover' not in self.conf:
                    self.cif.ksmMonitor.adjust()
            finally:
                self._ongoingCreations.release()
            if ('migrationDest' in self.conf or 'restoreState' in self.conf
                                               ) and self.lastStatus != 'Down':
                self._waitForIncomingMigrationFinish()

            self.lastStatus = 'Up'
            if self._initTimePauseCode:
                self.conf['pauseCode'] = self._initTimePauseCode
                if self._initTimePauseCode == 'ENOSPC':
                    self.cont()
            else:
                try:
                    del self.conf['pauseCode']
                except:
                    pass

            if 'recover' in self.conf:
                del self.conf['recover']
            self.saveState()
        except VolumeError, e:
            self.log.info(traceback.format_exc())
            self.setDownStatus(ERROR, 'Bad volume specification %s' % e)
        except ValueError, e:
            self.log.info(traceback.format_exc())
            self.setDownStatus(ERROR, str(e))
        except Exception, e:
            self.log.error(traceback.format_exc())
            self.setDownStatus(ERROR, self._getQemuError(e))

    def _incomingMigrationPending(self):
        return 'migrationDest' in self.conf or 'restoreState' in self.conf

    def _getQemuError(self, e):
        try:
            for line in file(self.dumpFile).readlines():
                if line.startswith('qemu: could not open disk image '):
                    return line
        except:
            self.log.error(traceback.format_exc())
        return 'Unexpected Create Error'

    def _prepareVolumePath(self, drive):
        volPath = ''
        if not self.destroyed:
            with self._volPrepareLock:
                if not self.destroyed:
                    volPath = self.cif._prepareVolumePath(drive)
                    self._preparedDrives[volPath] = drive

        return volPath

    def _initDriveList(self, drives):
        vindex = 0
        for d in drives:
            if d.get('if') == 'virtio' and not 'index' in d:
                d['index'] = str(vindex)
                vindex += 1

        for index, drive in zip(range(len(drives)), drives):
            drive['path'] = self._prepareVolumePath(drive)
            if not drive.get('if'):
                drive['if'] = 'ide'
                drive['index'] = index
            if not drive.get('serial') and drive.get('imageID'):
                drive['serial'] = drive['imageID'][-20:]

            res = self.cif.irs.getVolumeSize(drive['domainID'],
                                     drive['poolID'], drive['imageID'],
                                     drive['volumeID'])
            drive['truesize'] = res['truesize']
            drive['apparentsize'] = res['apparentsize']
            drive['blockDev'] = self.cif.irs.getStorageDomainInfo(
                                    drive['domainID'])['info']['type'] != 'NFS'
            self._drives.append(Drive(**drive))

    def preparePaths(self):
        self._initDriveList(self.conf.get('drives', []))
        try:
            self._cdromPreparedPath = self._prepareVolumePath(
                                            self.conf.get('cdrom'))
        except VolumeError:
            self.log.warning(traceback.format_exc())
            if self.conf.get('cdrom'):
                del self.conf['cdrom']
        if 'floppy' in self.conf:
            self._floppyPreparedPath = self._prepareVolumePath(
                                            self.conf['floppy'])
        self._pathsPreparedEvent.set()

    def _buildDriveStr(self, drives):
        self._initDriveList(drives)
        s = ''
        for d in self._drives:
            s += ' -drive file=%s,media=disk,if=%s' % (d.path, d.iface)
            s += ',cache=%s' % config.get('vars', 'qemu_drive_cache')
            if d.iface == 'scsi':
                s += ',bus=%s,unit=%s' % (d.bus, d.unit)
            elif d.iface == 'ide':
                s += ',index=%s' % d.index
            if d.serial:
                s += ',serial=%s' % d.serial
            s += ',boot=%s' % ['off','on'][utils.tobool(d.boot)]
            format = 'raw'
            if d.format == 'cow':
                format = 'qcow2'
            s += ',format=' + format
            if d.propagateErrors == 'on':
                s += ',werror=enospc'
            else:
                s += ',werror=stop'
        return s

    def _buildNetStr(self):
        self.interfaces = {}
        tapCreate = tapDelete = networkSetup = ''
        macs = self.conf.get('macAddr', '').split(',')
        models = self.conf.get('nicModel', '').split(',')
        bridges = self.conf.get('bridge', 'rhevm').split(',')
        if macs == ['']: macs = []
        if models == ['']: models = []
        if bridges == ['']: bridges = []
        if len(models) < len(macs) or len(models) < len(bridges):
            raise ValueError('Bad nic specification')
        if models and not (macs or bridges):
            raise ValueError('Bad nic specification')
        if len(models) > 8:
            raise ValueError('Too many nics: %s' % (models,))
        if not macs or not models or not bridges:
            return '', '', ' -net none '
        unknown_bridges = list(set(bridges).difference(set(netinfo.bridges())))
        if unknown_bridges:
            raise ValueError('Unknown bridge: %s' % unknown_bridges)
        macs = macs + [macs[-1]] * (len(models) - len(macs))
        bridges = bridges + [bridges[-1]] * (len(models) - len(bridges))

        for mac, model, bridge, vlan in zip(macs, models, bridges,
                                             range(1, len(macs) + 1)):
            if model == 'pv':
                model = 'virtio'
            interfaceId = model + '_' + self.conf['ifname'] + \
                '_' + str(vlan) + self.cif.multivds_id
            self.interfaces[interfaceId] = (mac, model)
            networkSetup += ' -net nic,vlan=%s,macaddr=%s,model=%s -net tap,vlan=%s,ifname=%s,script=no ' % (vlan, mac, model, vlan, interfaceId)
            tapCreate += constants.EXT_SUDO + "-n " + constants.EXT_TUNCTL + " -b -u vdsm -t %s;" % interfaceId
            tapCreate += constants.EXT_SUDO + "-n " + constants.EXT_IP + " link set dev %s address %s;" % (interfaceId, 'fe' + mac[2:])
            tapCreate += constants.EXT_SUDO + "-n %s link set dev %s up;" % ( constants.EXT_IP, interfaceId )
            tapCreate += constants.EXT_SUDO + "-n %s addif %s %s;" % ( constants.EXT_BRCTL, bridge, interfaceId)
            tapDelete += constants.EXT_SUDO + "-n " + constants.EXT_TUNCTL + " -d %s; " % interfaceId
        return tapDelete, tapCreate, networkSetup

    def _buildSmbiosStr(self):
        osd = self.cif.machineCapabilities.get('operatingSystem', {})
        vr = osd.get('version', '') + '-' + osd.get('release', '')
        s = ' -smbios type=1,manufacturer="Red Hat"'
        s += ',product="%s"' % osd.get('name', '')
        s += ',version=' + vr
        s += ',serial="%s"' % self.cif.machineCapabilities.get('uuid', '')
        s += ',uuid="%s"' % self.conf['vmId']
        return s + ' '

    def _buildCmdLine(self):
        """
        227 lines worth of ugliness that composes the correct command line for running Qemu according to self.conf.

        :returns: the correct command line
        :rtype: str
        """
        soundhw = ''
        ic = {'qxl': 'on', 'qxlnc':'off'}
        if self.conf['display'] == "vnc":
            if 'spiceDisableTicketing' in self.conf:
                vncPassword = ''
            else:
                vncPassword = ',password'
            display = " -vnc %s:%s%s " % (self.conf['displayIp'],
                                            self.conf['displayPort'], vncPassword)
        elif 'qxl' in self.conf['display']:
            spiceParams = {}
            spiceParams['port'] = int(self.conf['displayPort'])
            if self.conf['displayIp'] != '':
                spiceParams['host'] = self.conf['displayIp']
            spiceParams['ic'] = ic[self.conf['display']]
            if 'spiceRenderer' in self.conf:
                spiceParams['renderer'] = self.conf['spiceRenderer']
            if 'spiceDisableTicketing' in self.conf:
                spiceParams['disable-ticketing'] = None
            if 'spiceSecureChannels' in self.conf and \
                    self.conf['spiceSecureChannels'] != '':
                spiceParams['secure-channels'] = '+'.join([c[1:] for c in self.conf['spiceSecureChannels'].split(',')])
                spiceParams['sport'] = int(self.conf['displaySecurePort'])
                if not 'spiceSslCipherSuite' in self.conf:
                    self.conf['spiceSslCipherSuite'] = 'DEFAULT'
            if 'spiceSslCipherSuite' in self.conf:
                spiceParams['sslciphersuite'] = self.conf['spiceSslCipherSuite']
                tsPath = config.get('vars', 'trust_store_path')
                spiceParams['sslpassword'] = ''
                spiceParams['sslkey'] = tsPath + '/keys/vdsmkey.pem'
                spiceParams['sslcert'] = tsPath + '/certs/vdsmcert.pem'
                spiceParams['sslcafile'] = tsPath + '/certs/cacert.pem'
                spiceParams['ssldhfile'] = tsPath + '/keys/dh.pem'
            spiceExtra = self.conf.get('spiceExtra',
                config.get('vars', 'extra_spice_params'))
            if spiceExtra:
                spiceParams[spiceExtra] = None
            display = ' -spice ' + \
                ','.join(map(lambda x: str(x[0]) + ('=' + str(x[1]), '')[x[1]==None],
                            spiceParams.items()))
            display = display + ' -qxl %s ' % (
                                    self.conf.get('spiceMonitors', '1'))
            soundhw = ' -soundhw %s ' % self.conf.get('soundDevice', 'ac97')
        else:
            display = ""

        if 'boot' in self.conf:
            boot = ' -boot ' + self.conf['boot'] + ' '
        else:
            boot = ''

        if 'launchPaused' in self.conf:
            launchPaused = ' -S '
            self.conf['pauseCode'] = 'NOERR'
            del self.conf['launchPaused']
        else:
            launchPaused = ''

        if 'vmName' in self.conf:
            name = ' -name %s ' % self.conf['vmName']
        else:
            name = ''

        acpi = ''
        if 'acpiEnable' in self.conf and \
            self.conf['acpiEnable'].lower() == "false":
                acpi = ' -no-acpi '
                if 'tdf' not in self.conf: self.conf['tdf'] = 'false'
                if 'irqChip' not in self.conf: self.conf['irqChip'] = 'true'
        else:
            self.conf['acpiEnable'] = 'true'
            if 'tdf' not in self.conf: self.conf['tdf'] = 'true'
            if 'irqChip' not in self.conf: self.conf['irqChip'] = 'true'

        win2kHack = ''
        if 'win2kHackEnable' in self.conf:
            if self.conf['win2kHackEnable'].lower() == "true":
                win2kHack = ' -win2k-hack '

        usbdevice = ' -usbdevice tablet '
        if self.conf['display'] == 'qxl' and \
           self.conf.get('spiceMonitors', '1') == '1':
            pass
        elif self.conf['display'] != "vnc":
            usbdevice = ' -usb '
        if 'tabletEnable' in self.conf:
            if utils.tobool(self.conf['tabletEnable']):
                usbdevice = ' -usbdevice tablet '
            else:
                usbdevice = ' -usb '

        tdf = ''
        if self.conf['tdf'].lower() == 'true':
            tdf = ' -rtc-td-hack '

        irqChip = ''
        if self.conf['irqChip'].lower() != 'true':
            irqChip = ' -no-kvm-irqchip '

        pitReinjection = ''
        if not utils.tobool(self.conf.get('pitReinjection', 'true')):
            pitReinjection = ' -no-kvm-pit-reinjection '

        if 'smp' in self.conf:
            smp = ' -smp ' + self.conf['smp']
            if 'smpCoresPerSocket' in self.conf:
                smp += ',cores=%s' % self.conf['smpCoresPerSocket']
            if 'smpThreadsPerCore' in self.conf:
                smp += ',threads=%s' % self.conf['smpThreadsPerCore']
            smp += ' '
        else:
            smp = ''

        if 'keyboardLayout' in self.conf:
            keymap = ' -k %s ' % self.conf['keyboardLayout']
        else:
            keymap = ''

        migrationDest = ''
        if 'migrationDest' in self.conf:
            migrationDest = ' -incoming tcp:' + self.conf['migrationDest']

        if 'restoreState' in self.conf:
            migrationDest = ' -incoming exec:"cat<%s"' \
                                    % self.conf['restoreState']

        fixedIf = config.get('vars', 'use_fixed_tap')
        if fixedIf == "guid":
            self.conf['ifname'] = self.conf['vmId']
        elif fixedIf == "name":
            self.conf['ifname'] = self.conf['vmName']
        else:
            self.conf['ifname'] = self.conf['ifid']

        tapDelete, tapCreate, networkSetup = self._buildNetStr()

        drivesStr = self._buildDriveStr(self.conf.get('drives', []))

        # backward compatibility, in case of no 'drives' param
        if drivesStr == '':
            for drive in ['hda', 'hdb', 'hdc', 'hdd']:
                if self.conf.get(drive):
                    drivesStr += ' -' + drive + ' ' + self.conf[drive]
        try:
            if self.conf.get('cdrom'):
                drivesStr += ' -drive file=%s,media=cdrom,index=2,if=ide ' % \
                                self.cif._prepareVolumePath(self.conf.get('cdrom'))
        except VolumeError:
            self.log.warning(traceback.format_exc())
            if self.conf.get('cdrom'):
                del self.conf['cdrom']

        if 'floppy' in self.conf:
            floppy = ' -fda ' + self.cif._prepareVolumePath(self.conf['floppy'])
        else:
            floppy = ''
        if self.conf['vmType'] == "kvm" and \
                not utils.tobool(self.conf.get('vmchannel', 'false')):
            guestChannelsCmd = ( ' -vmchannel di:0200,unix:' +
                                self._guestSocektFile + ',server')
        else:
            guestChannelsCmd = ''
        if 'cpu' in self.conf:
            taskset = 'taskset -c ' + self.conf['cpu'] + ' '
        else:
            taskset = ''
        if 'cpuType' in self.conf:
            cpuType = ' -cpu %s ' % (self.conf['cpuType'])
        else:
            cpuType = ''
        if 'emulatedMachine' in self.conf:
            emulatedMachine = ' -M %s ' % self.conf['emulatedMachine']
        else:
            emulatedMachine = ''

        nice = int(self.conf.get('nice', '0'))
        vdsm_nice = utils.getPidNiceness('self')
        relnice = max(1, nice - vdsm_nice)
        self._nice = str(vdsm_nice + relnice)

        if 'timeOffset' in self.conf:
            timeOffset = int(self.conf['timeOffset'])
            startdate = time.strftime(' -startdate %Y-%m-%dT%H:%M:%S ',
                                time.gmtime(time.time() + timeOffset))
        else:
            if 'noLocalTime' in self.conf:
                startdate = ''
                self.conf['timeOffset'] = '0'
            else:
                startdate = ' -localtime '
                self.conf['timeOffset'] = str(
                    -[time.timezone, time.altzone][time.daylight])

        cmdline =( constants.EXT_NICE +
                      ' -n %s ' % relnice +
                      self.conf['executable'] +
                      launchPaused +
                      irqChip +
                      pitReinjection +
                      acpi +
                      usbdevice +
                      tdf +
                      startdate +
                      win2kHack +
                      name +
                      smp +
                      keymap +
                      ' -m %s ' % self.conf['memSize'] +
                      boot +
                      networkSetup +
                      drivesStr + floppy +
                      ' -pidfile ' + self.pidfile +
                      soundhw +
                      display +
                      migrationDest +
                      cpuType +
                      emulatedMachine +
                      ' -notify all ' +
                      ' -balloon none ' +
                      self._buildSmbiosStr() +
                      guestChannelsCmd +
                      " -monitor unix:" + self._monitorSocketFile + ",server" +
                      " 1>" + self.dumpFile + " 2>&1; ")
        # Wrap command for "taskset" execution
        cmdline = ("TZ=UTC " + taskset + cmdline)
        # Ano now wrap tap creation deletion around it
        cmdline = (tapDelete + tapCreate + cmdline + tapDelete)
        return cmdline

    def releaseVm(self):
        """
        Stop VM and release all resources (implemented for libvirt VMs)
        """
        pass

    def _onQemuDeath(self):
        self.log.info('underlying process disconnected')
        # Try release VM resources first, if failed stuck in 'Powering Down'
        # state
        response = self.releaseVm()
        if not response['status']['code']:
            if self.user_destroy:
                self.setDownStatus(NORMAL, "User shut down")
            else:
                self.setDownStatus(ERROR, "Lost connection with kvm process")

    def _initQemuMonitor(self, attempts):
        def onMigrationFinish():
            self._incomingMigrationFinished.set()
        def shellDied():
            if self._shellPid:
                try:
                    for line in file('/proc/%s/status' % self._shellPid
                                        ).read().splitlines():
                        if line.startswith('State:') \
                                 and line.split()[1] == 'Z':
                            return True
                except:
                    return True
            return False
        self._monInitLock.acquire()
        try:
            if not self._mon:
                self._mon = QemuMonitor.QemuMonitor(self._monitorSocketFile,
                    attempts, self.log,
                    self._onQemuDeath, self.onReboot, self.onShutdown,
                    self.onConnect, self.onDisconnect, onMigrationFinish,
                    onRtcUpdate=self._rtcUpdate,
                    onVncDisconnect=self.onDisconnect,
                    onAbnormalVmStop=self._onAbnormalStop,
                    onHighWrite=self._onHighWrite,
                    stopConnecting=shellDied)
        finally:
            self._monInitLock.release()

    def _readPauseCode(self, timeout):
        pauseCode = None
        if not self._guestCpuRunning:
            pr = self._sendMonitorCommand('info stop-reason', timeout=timeout)
            WRITE_ERROR_PREF = 'VM is stopped due to disk write error: '
            if pr.startswith(WRITE_ERROR_PREF):
                block_dev, err = pr[len(WRITE_ERROR_PREF)
                                                    :-1].split(': ', 1)
                pauseCode = utils.symbolerror[err]
            else:
                pauseCode = 'NOERR'
        return pauseCode

    def _monitorDependentInit(self, timeout=None):
        if self._tryMonitorDependentInit:
            try:
                self._tryMonitorDependentInit = False
                self._kvmEnable = str(self._isKvmEnabled(timeout))
                self._getQemuDriveInfo(timeout)
                self._setWriteWatermarks()
                self._guestCpuRunning = self._sendMonitorCommand(
                        'info status', timeout=timeout).startswith(
                                                         'VM status: running')
                if self.lastStatus not in ('Migration Destination',
                                           'Restoring state'):
                    self._initTimePauseCode = self._readPauseCode(timeout)
                if 'recover' not in self.conf and self._initTimePauseCode:
                    self.conf['pauseCode'] = self._initTimePauseCode
                    if self._initTimePauseCode == 'ENOSPC':
                        self.cont()
            except:
                self._tryMonitorDependentInit = True
                raise
            self.log.debug('finished _tryMonitorDependentInit')

    def _initVmStats(self):
        ifids = self.interfaces.keys()
        ifrates = [[100, 1000][ model in ('e1000', 'virtio') ]
                        for mac, model in self.interfaces.values()]
        ifmacs = [self.interfaces[ifid][0] for ifid in ifids]
        self._vmStats = self.VmStatsThreadClass(log=self.log, ifids=ifids,
                           ifrates=ifrates, ifmacs=ifmacs,
                           vm=self, pid=int(self.conf['pid']))
        self._vmStats.start()
        self._guestEventTime = self._startTime

    def _loadCorrectedTimeout(self, base, doubler=20, load=None):
        """
        Return load-corrected base timeout

        :param base: base timeout, when system is idle
        :param doubler: when (with how many running VMs) should base timeout be
                        doubled
        :param load: current load, number of VMs by default
        """
        if load is None:
            load = len(self.cif.vmContainer)
        return base * (20 + load) / 20

    def _run(self):
        self.log.info("VM wrapper has started")
        if 'recover' not in self.conf:
            cmdline = self._buildCmdLine()
            self.log.debug(cmdline)
            self.conf['pid'] = '0'
            attempts = self._loadCorrectedTimeout(
                                    config.getint('vars', 'create_timeout'))
            utils.rmFile(self.pidfile)
            utils.rmFile(self._guestSocektFile)
            utils.rmFile(self._monitorSocketFile)
            username = self.conf.get('username', 'Unknown')
            guestIPs = self.conf.get('guestIPs', '')
            self._pidLock.acquire()
            try:
                if self.destroyed:
                    self.log.warning('vm destroyed before qemu started')
                    return
                else:
                    p = subprocess.Popen([constants.EXT_SETSID, constants.EXT_SH, '-c',
                                          cmdline],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        close_fds=True)
                    self._shellPid = p.pid
            finally:
                self._pidLock.release()
        else:
            try:
                self._buildNetStr() # TODO: split .interfaces setup out of here
            except ValueError:
                self.log.debug(traceback.format_exc())
            for drive in self.conf.get('drives', []):
                self._drives.append(Drive(**drive))
                if 'recover' in self.conf and drive.get('blockDev', True):
                    self._refreshLV(drive['domainID'], drive['poolID'],
                                    drive['imageID'], drive['volumeID'])
            self.lastStatus = self.conf['status']
            attempts = 2
            username = self.conf['username']
            guestIPs = self.conf['guestIPs']
            del self.conf['guestIPs']
            del self.conf['username']

        try:
            self._initQemuMonitor(attempts)
        except QemuMonitor.TimeoutError:
            self._monitorResponse = -1
            if 'recover' not in self.conf:
                self.setDownStatus(ERROR, "Desktop launch timeout expired.")
                return

        #search for my kvm pid
        self.conf['pid'] = self._getPid()
        if self.conf['pid'] == '0':
            self.log.error('Could not find PID - Desktop probably crashed')
            reason = file(self.dumpFile).read()
            self.log.error(reason)
            self.setDownStatus(ERROR, "Fatal error during Desktop launch.")
            return

        #vdsAgent interface
        connectToAgent = self.conf['vmType'] == "kvm" and \
                         utils.tobool(self.conf.get('vmchannel', 'true'))
        self.guestAgent = guestIF.GuestAgent(self._guestSocektFile, self.log,
                                             username, guestIPs,
                                             connect=connectToAgent)

        self._initVmStats()

        if 'recover' in self.conf:
            if self.conf.get('_migrationParams'):
                self._migrationSourceThread = \
                                self.MigrationSourceThreadClass(self,
                                    **self.conf['_migrationParams'])
                self._migrationSourceThread.start()
            self.onConnect()
            timeout = 5
        else:
            timeout = None
        try:
            self._monitorDependentInit(timeout=timeout)
        except:
            self.log.warning(traceback.format_exc())

    def _isKvmEnabled(self, timeout=None):
        out = self._sendMonitorCommand('info kvm', timeout=timeout)
        for line in out.splitlines():
            if line.startswith('kvm support: enabled'):
                return True
        return False

    def _getSpiceClientIP(self):
        try:
            out = self._sendMonitorCommand('info spice.state')
            if not out.startswith('spice info: '):
                return ''
            out = out[len('spice info: '):]
            out = out.split()[0]
            if out == 'disconnected':
                return ''
            return out.split('=')[1]
        except:
            self.log.error(traceback.format_exc())
        return ''

    def _getPid(self):
        pid = '0'
        try:
            pid = file(self.pidfile).read().strip()
        except:
            pass
        return pid

    def saveState (self):
        if self.destroyed:
            return
        toSave = deepcopy(self.status())
        toSave['startTime'] = self._startTime
        if self.lastStatus != 'Down' and self._vmStats and self.guestAgent:
            toSave['username'] = self.guestAgent.guestInfo['username']
            toSave['guestIPs'] = self.guestAgent.guestInfo['guestIPs']
        else:
            toSave['username'] = ""
            toSave['guestIPs'] = ""
        if 'sysprepInf' in toSave:
            del toSave['sysprepInf']
            if 'floppy' in toSave: del toSave['floppy']
        for drive in toSave.get('drives', []):
            for d in self._drives:
                if drive.get('volumeID') == d.volumeID:
                    drive['truesize'] = str(d.truesize)
                    drive['apparentsize'] = str(d.apparentsize)

        with tempfile.NamedTemporaryFile(dir=constants.P_VDSM_RUN,
                                         delete=False) as f:
             pickle.dump(toSave, f)

        os.rename(f.name, self._recoveryFile)

    def onReboot (self, withRelaunch):
        try:
            self.log.debug('reboot event')
            self._startTime = time.time()
            self._guestEventTime = self._startTime
            self._guestEvent = 'RebootInProgress'
            self.saveState()
            self.guestAgent.onReboot()
            if self.conf.get('volatileFloppy'):
                self._ejectFloppy()
                self.log.debug('ejected volatileFloppy')
            if withRelaunch:
                self.cif.relaunch(self.status())
        except:
            self.log.error(traceback.format_exc())

    def _ejectFloppy(self):
        self._mon.postCommand("eject -f floppy0")

    def onShutdown (self):
        self.log.debug('onShutdown() event')
        self.user_destroy = True

    def onConnect(self, clientIp=''):
        if clientIp:
            self.conf['clientIp'] = clientIp
        else:
            # FIXME QemuMonitor.sendCommand is not reentrant.
            # calling _getSpiceClientIP (which calls sendCommand) from another
            # thread is an ugly hack to circumvent this problem.
            def updateClientIp():
                self.conf['clientIp'] = self._getSpiceClientIP()
            threading.Thread(target=updateClientIp).start()

    def onDisconnect(self, detail=None):
        self.guestAgent.desktopLock()
        self.conf['clientIp'] = ''

    def _rtcUpdate(self, timeOffset):
        self.log.debug('new rtc offset %s', timeOffset)
        self.conf['timeOffset'] = timeOffset

    def _onAbnormalStop(self, block_dev, err):
        self.log.info('abnormal vm stop device %s error %s', block_dev, err)
        self.conf['pauseCode'] = utils.symbolerror[err]
        self._guestCpuRunning = False
        if err == os.strerror(errno.ENOSPC):
            self._lvExtend(block_dev)

    def _onHighWrite(self, block_dev, offset):
        self.log.info('_onHighWrite: write above watermark on %s offset %s',
                      block_dev, offset)
        self._lvExtend(block_dev)

    def _lvExtend(self, block_dev, newsize=None):
        for d in self._drives:
            if not d.blockDev: continue
            if d.name != block_dev: continue
            if newsize is None:
                newsize = config.getint('irs',
                    'volume_utilization_chunk_mb') + (d.apparentsize + 2**20
                                                     - 1) / 2**20
            # TODO cap newsize by max volume size
            volDict = {'poolID': d.poolID, 'domainID': d.domainID,
                       'imageID': d.imageID, 'volumeID': d.volumeID}
            d.needExtend = True
            d.reqsize = newsize
            # sendExtendMsg expects size in bytes
            self.cif.irs.hsm.sendExtendMsg(d.poolID, volDict, newsize * 2**20,
                                           self._afterLvExtend)
            self.log.debug('_lvExtend: %s: apparentsize %s req %s',
                      d.name, d.apparentsize / MEGAB, newsize) #in MiB
            d.lastLvExtend = time.time()

            # store most recently requested size in conf, to be re-requested on
            # migration destination
            for drive in self.conf.get('drives', []):
                if drive.get('volumeID') == d.volumeID:
                    drive['reqsize'] = str(d.reqsize)

    def _refreshLV(self, domainID, poolID, imageID, volumeID):
        """ Stop vm before refreshing LV. """

        self._guestCpuLock.acquire()
        try:
            wasRunning = self._guestCpuRunning
            if wasRunning:
                self.pause(guestCpuLocked=True)
            self.cif.irs.refreshVolume(domainID, poolID, imageID, volumeID)
            if wasRunning:
                self.cont(guestCpuLocked=True)
        finally:
            self._guestCpuLock.release()

    def _afterLvExtend(self, drive):
        try:
            self.log.debug('_afterLvExtend %s' % drive)
            for d in self._drives:
                if (d.poolID, d.domainID,
                    d.imageID, d.volumeID) != (
                                     drive['poolID'], drive['domainID'],
                                     drive['imageID'], drive['volumeID']):
                    continue
                self._refreshLV(drive['domainID'], drive['poolID'],
                                drive['imageID'], drive['volumeID'])
                res = self.cif.irs.getVolumeSize(d.domainID,
                        d.poolID, d.imageID, d.volumeID)
                apparentsize = int(res['apparentsize'])
                truesize = int(res['truesize'])
                self.log.debug('_afterLvExtend apparentsize %s req size %s' % (apparentsize / MEGAB, d.reqsize)) # in MiB
                if apparentsize >= d.reqsize * MEGAB: #in Bytes
                    self.cont()
                    d.needExtend = False
                # TODO report failure to VDC
                d.truesize = truesize
                d.apparentsize = apparentsize
                self._setWriteWatermarks()
        except:
            self.log.debug(traceback.format_exc())
        return {'status': doneCode}

    def changeCD(self, drivespec):
        return self._changeBlockDev('cdrom', 'ide1-cd0', drivespec)

    def changeFloppy(self, drivespec):
        return self._changeBlockDev('floppy', 'floppy0', drivespec)

    def _changeBlockDev(self, vmDev, blockdev, drivespec):
        try:
            path = self.cif._prepareVolumePath(drivespec)
        except VolumeError, e:
            return {'status': {'code': errCode['imageErr']['status']['code'],
              'message': errCode['imageErr']['status']['message'] % str(e)}}
        self._sendMonitorCommand('change %s %s' % (blockdev, path))
        newDev = ''
        for line in self._sendMonitorCommand('info block').splitlines():
            if line.startswith('%s:' % blockdev):
                for s in line.split():
                    if s.startswith('file='):
                        newDev = drivespec
        if self.conf.get(vmDev):
            self.cif._teardownVolumePath(self.conf.get(vmDev))
        self.conf[vmDev] = newDev
        if newDev == drivespec:
            return {'status': doneCode, 'vmList': self.status()}
        else:
            return {'status': {'code': errCode['imageErr']['status']['code'],
                 'message': errCode['imageErr']['status']['message'] % path }}

    def setTicket(self, otp, seconds, connAct):
        if self.conf.get('display') == 'vnc':
            # connAct is ignored for vnc
            seconds = int(seconds)
            self._changeVncPassword(otp)
            if seconds > 0:
                timer = threading.Timer(seconds, self._expireVncPassword)
                timer.setDaemon(True)
                timer.start()
            return {'status': doneCode}
        else:
            res = self._sendMonitorCommand(
                'spice.set_ticket %s expiration=%s,connected=%s' %
                (otp, seconds, connAct))
            if 'Ticket set successfully' in res:
                return {'status': doneCode}
        ret = errCode['ticketErr'].copy()
        ret['status']['message'] = res
        return ret

    def _expireVncPassword(self):
        self.log.debug('_expireVncPassword called')
        try:
            # RHEVM uses a base64-encoded 9-byte OTP, let us do the same.
            otp = os.urandom(9).encode("base64")[:-1]
            self._changeVncPassword(otp)
        except:
            self.log.error(traceback.format_exc())

    def _changeVncPassword(self, password):
        self._sendMonitorCommand('change vnc password',
            prompt='\nPassword: ', command2=password)

    def _migrationTimeout(self):
        timeout = config.getint('vars', 'migration_timeout')
        mem = int(self.conf['memSize'])
        if mem > 2048:
            timeout = timeout * mem / 2048
        return timeout

    def _waitForIncomingMigrationFinish(self):
        """ wait until migration destination ends (or timeout expires) """
        timeout = self._migrationTimeout()
        if 'restoreState' in self.conf:
            timeout = timeout / 2
        self.log.debug("Waiting %s seconds for end of migration" % timeout)
        self._incomingMigrationFinished.wait(timeout)
        if not self._incomingMigrationFinished.isSet():
            self.setDownStatus(ERROR,  "Migration failed")
            return
        if 'restoreState' in self.conf:
            self.cont()
            del self.conf['restoreState']
            del self.conf['guestIPs']
            del self.conf['username']
        if 'migrationDest' in self.conf:
            # re-request lv extend if we are aware of an unsatisfied former
            # request.
            for d in self._drives:
                if d.reqsize * MEGAB > d.apparentsize: #in MiB
                    self._lvExtend(d.name, d.reqsize)
            #TODO to be removed once the gratitute packat is solved
            #ADDED BY BARAK ... SOLVES THE POST MIGRATION PING ISSUE
            if self.conf['guestIPs'] != '':
              try:
                cmd = constants.EXT_PING + " -c 1 %s"%(self.conf['guestIPs'])
                subprocess.Popen(cmd, shell=True, close_fds=True)
                self.log.debug("vm::migrationValidate::ping cmd = '%s'" % (cmd))
              except:
                self.log.warning("vm::migrationValidate::failed pinging vm ip %s after migration was done"%(self.conf['guestIPs']))
            #ADDED BY BARAK END
            self._guestCpuRunning = self._sendMonitorCommand(
                        'info status', timeout=timeout).startswith(
                                                         'VM status: running')
            del self.conf['migrationDest']
            del self.conf['afterMigrationStatus']
            del self.conf['guestIPs']
            del self.conf['username']
        self.guestAgent.sendHcCmdToDesktop('refresh')
        self.saveState()
        self.log.debug("End of migration")
        return

    def waitForPid(self):
        """Wait until qemu pid is known, or Vm is Down"""
        while True:
            if self._lastStatus == ('Down', 'Powering down'):
                self.log.debug('Destination VM creation failed before acquiring pid.')
                return False
            if self._getPid() != '0':
                return True
            time.sleep(1)

    def _sendMonitorCommand(self, command, prompt=None, command2=None,
                            timeout=None):
        if timeout is None:
            timeout = self._loadCorrectedTimeout(
                                   config.getint('vars', 'vm_command_timeout'))
        try:
            if not self._mon:
                self._initQemuMonitor(timeout)
            self._monitorDependentInit()
            if not command.startswith('info'):
                self.log.debug(command)
            startTime = time.time()
            if prompt is None:
                out = self._mon.sendCommand(command, timeout)
            else:
                out = self._mon.sendCommand2(command, prompt, command2, timeout)
            self._monitorResponse = int(time.time() - startTime)
            return out
        except QemuMonitor.TimeoutError:
            if self.lastStatus != 'Down':
                self.log.warning('command timeout: ' + command)
            self._monitorResponse = -1
            raise

    def doCommand(self, command, newStatus='keep', sendOutput=False):
        """ Obsoleted. Do not use in new code. """
        if self.lastStatus == 'Down':
            return errCode['down']
        try:
            out = self._sendMonitorCommand(command)
            if newStatus != 'keep':
                self.lastStatus = newStatus
                self.saveState()
            if sendOutput:
                return {'status': doneCode, 'output': out.split('\n')}
            else:
                return {'status': doneCode, 'vmList': self.status()}
        except:
            self.log.error(traceback.format_exc())
        return errCode['unexpected']

    def _acquireCpuLockWithTimeout(self):
        timeout = self._loadCorrectedTimeout(
                                config.getint('vars', 'vm_command_timeout'))
        end = time.time() + timeout
        while not self._guestCpuLock.acquire(False):
            time.sleep(0.1)
            if time.time() > end:
                raise RuntimeError('waiting more that %ss for _guestCpuLock' %
                                   timeout)

    def _underlyingCont(self):
        self._sendMonitorCommand('cont')

    def cont(self, afterState='Up', guestCpuLocked=False):
        if not guestCpuLocked:
            self._acquireCpuLockWithTimeout()
        try:
            if self.lastStatus in ('Migration Source', 'Saving State', 'Down'):
                 self.log.error('cannot cont while %s', self.lastStatus)
                 return errCode['unexpected']
            self._underlyingCont()
            self._guestCpuRunning = True
            self._lastStatus = afterState
            try:
                del self.conf['pauseCode']
            except:
                pass
            return {'status': doneCode, 'output': ['']}
        finally:
            if not guestCpuLocked:
                self._guestCpuLock.release()

    def _underlyingPause(self):
        self._sendMonitorCommand('pause')

    def pause(self, afterState='Paused', guestCpuLocked=False):
        if not guestCpuLocked:
            self._acquireCpuLockWithTimeout()
        try:
            self.conf['pauseCode'] = 'NOERR'
            self._underlyingPause()
            self._guestCpuRunning = False
            self._lastStatus = afterState
            return {'status': doneCode, 'output': ['']}
        finally:
            if not guestCpuLocked:
                self._guestCpuLock.release()

    def _migrateInfo(self):
        try:
            out = self._sendMonitorCommand('info migrate')
            for line in out.splitlines():
                if line.startswith('Migration status: '):
                    return out[len('Migration status: '):].strip()
        except:
            self.log.error(traceback.format_exc())
        return ''

    def _killIfMatch(self, pid, sig=signal.SIGTERM):
        try:
            cmd = file('/proc/' + pid + '/cmdline').read()
            if self._monitorSocketFile in cmd:
                self.log.info("Killing non-responsive desktop")
                os.kill(int(pid), sig)
        except:
            pass

    def shutdown(self, timeout, message):
        try:
            now = time.time()
            if self.lastStatus == 'Down':
                return
            if self.guestAgent and self.guestAgent.isResponsive():
                self._guestEventTime = now
                self._guestEvent = 'Powering down'
                self.log.debug('guestAgent shutdown called')
                guest_message = 'shutdown,' + timeout + ',' + message
                self.guestAgent.sendHcCmdToDesktop(guest_message)
                agent_timeout = int(timeout) + config.getint('vars', 'sys_shutdown_timeout')
                timer = threading.Timer(agent_timeout, self._timedShutdown)
                timer.start()
            elif self.conf['acpiEnable'].lower() == "true":
                self._guestEventTime = now
                self._guestEvent = 'Powering down'
                self._acpiShutdown()
            # No tools, no ACPI
            else:
                return {'status': {'code': errCode['exist']['status']['code'],
                        'message': 'VM without ACPI or active SolidICE tools. Try Forced Shutdown.'}}
        except:
            self.log.error(traceback.format_exc())
        return {'status': {'code': doneCode['code'],
                'message': 'Machine shut down'}}

    def _acpiShutdown(self):
        self.log.debug('acpi shutdown called')
        self._mon.postCommand('system_powerdown')

    def _timedShutdown(self):
        self.log.debug('_timedShutdown Called')
        try:
            if self.lastStatus == 'Down':
                return
            if self.conf['acpiEnable'].lower() != "true":
                self.destroy()
            else:
                self._acpiShutdown()
        except:
            self.log.error(traceback.format_exc())

    def destroy(self):
        self.log.debug('destroy Called')
        self.destroyed = True
        try:
            if self._vmStats:
                self._vmStats.stop()
            self._guestEventTime = time.time()
            self._guestEvent = 'Powering down'
            if self._mon:
                try:
                    self._mon.sendCommand('quit', timeout=2)
                except:
                    self._mon.postCommand('quit')
        except:
            self.log.error(traceback.format_exc())
        # make sure qemu process has been destroyed
        self._pidLock.acquire()
        try:
            if self.conf['pid'] != '0':
                self._killIfMatch(self.conf['pid'])
                time.sleep(1)
                self._killIfMatch(self.conf['pid'], signal.SIGKILL)
            elif self._shellPid:
                try:
                    os.killpg(self._shellPid, signal.SIGTERM)
                    time.sleep(1)
                    os.killpg(self._shellPid, signal.SIGKILL)
                except:
                    self.log.debug(traceback.format_exc())
                try:
                    tapDelete, dummy, dummy = self._buildNetStr()
                    utils.execAndGetOutput(tapDelete)
                except:
                    self.log.debug(traceback.format_exc())
            else:
                existingVms = utils.execCmd([constants.EXT_PGREP, 'qemu-kvm'],
                                            raw=False, sudo=False)
                for pid in existingVms:
                    self._killIfMatch(pid.strip())
                time.sleep(1)
                for pid in existingVms:
                    self._killIfMatch(pid.strip(), signal.SIGKILL)
        finally:
            self._pidLock.release()
        if self.user_destroy:
            reason = 'User shut down'
        else:
            reason = 'Admin shut down'
        if self.lastStatus != 'Down':
            self.setDownStatus(NORMAL, reason)
        try:
            del self.cif.vmContainer[self.conf['vmId']]
            self.cif.ksmMonitor.adjust()
            self.log.debug("Total desktops after destroy of %s is %d",
                     self.conf['vmId'], len(self.cif.vmContainer))
        except:
            self.log.error(traceback.format_exc())
        try:
            self.log.debug('qemu stdouterr: ' + file(self.dumpFile).read())
        except:
            pass
        t = threading.Thread(target=self._prepostVmScript, args=['post_vm'])
        t.setDaemon(True)
        t.start()
        self._cleanup()
        return {'status': doneCode}

    def _prepostVmScript(self, script):
    # script is either pre_vm or post_vm
        try:
            cmd = config.get('vars', script)
            if not cmd: raise ValueError
        except:
            return 0, ''
        env = {}
        for k, v in self.conf.iteritems():
            env[k] = str(v)
        p = subprocess.Popen(cmd, shell=True, close_fds=True, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
        out, err = p.communicate()
        self.log.debug('%s rc %s out %s err %s' %
                        (script, p.returncode, out, err))
        return p.returncode, err

    def _teardownVolumePath(self, drive):
        try:
            if self._preparedDrives.has_key(drive):
                resCode = self.cif._teardownVolumePath(self._preparedDrives[drive],
                                                      keepWriteable=self._migratedAway)
                # If teardown failed leave drive in _preparedDrives for next try.
                if not resCode:
                    del self._preparedDrives[drive]
            else:
                self.log.warn("Volume %s missing from preparedDrives", str(drive))
        except:
            self.log.error(traceback.format_exc())

    def _cleanup(self):
        with self._volPrepareLock:
            for drive in self._preparedDrives.keys():
                self.log.debug("Drive %s cleanup" % drive)
                self._teardownVolumePath(drive)

        if self.conf.get('volatileFloppy'):
            try:
                self.log.debug("Floppy %s cleanup" % self.conf['floppy'])
                utils.rmFile(self.conf['floppy'])
            except:
                pass
        try:
            self._mon.stop()
        except:
            pass
        try:
            self.guestAgent.stop()
        except:
            pass
        utils.rmFile(self.pidfile)
        utils.rmFile(self._guestSocektFile)
        utils.rmFile(self._monitorSocketFile)
        utils.rmFile(self._recoveryFile)
        utils.rmFile(self.dumpFile)

    def setDownStatus (self, code, reason):
        if self.lastStatus == 'Migration Source' and code == NORMAL:
            self._migratedAway = True
        try:
            self.lastStatus = 'Down'
            self.conf['exitCode'] = code
            if 'restoreState' in self.conf:
                self.conf['exitMessage'] = "Wake up from hibernation failed"
            else:
                self.conf['exitMessage'] = reason
            self.log.debug("Changed state to Down: " + reason)
        except DoubleDownError:
            pass
        try:
            self.guestAgent.stop()
        except:
            pass
        try:
            self._vmStats.stop()
        except:
            pass
        self.saveState()

    def status(self):
        # used by clientIF.list
        self.conf['status'] = self.lastStatus
        return self.conf

    def getStats(self):
        def _getGuestStatus():
            GUEST_WAIT_TIMEOUT = 60
            now = time.time()
            if now - self._guestEventTime < 5 * GUEST_WAIT_TIMEOUT and \
                    self._guestEvent == 'Powering down':
                return self._guestEvent
            if self.guestAgent and self.guestAgent.isResponsive() and \
                    self.guestAgent.getStatus():
                return self.guestAgent.getStatus()
            if now - self._guestEventTime < GUEST_WAIT_TIMEOUT:
                return self._guestEvent
            return 'Up'

        # used by clientIF.getVmStats
        if self.lastStatus == 'Down':
            stats = {}
            stats['exitCode'] = self.conf['exitCode']
            stats['status'] = self.lastStatus
            stats['exitMessage'] = self.conf['exitMessage']
            if 'timeOffset' in self.conf:
                stats['timeOffset'] = self.conf['timeOffset']
            return stats

        stats = {'displayPort': self.conf['displayPort'],
                 'displaySecurePort': self.conf['displaySecurePort'],
                 'displayType': self.conf['display'],
                 'displayIp': self.conf['displayIp'],
                 'pid': self.conf['pid'],
                 'vmType': self.conf['vmType'],
                 'kvmEnable': self._kvmEnable,
                 'network': {}, 'disks': {},
                 'monitorResponse': str(self._monitorResponse),
                 'nice': self._nice,
                 'elapsedTime' : str(int(time.time() - self._startTime)),
                 }
        if 'cdrom' in self.conf:
            stats['cdrom'] = self.conf['cdrom']
        if 'boot' in self.conf:
            stats['boot'] = self.conf['boot']

        decStats = {}
        try:
            if self._vmStats:
                decStats = self._vmStats.get()
                if (not self._migrationSourceThread.isAlive()
                    and decStats['statsAge'] > config.getint('vars',
                                                       'vm_command_timeout')):
                    stats['monitorResponse'] = '-1'
        except:
            self.log.error("Error fetching vm stats", exc_info=True)
        for var in decStats:
            if type(decStats[var]) is not dict:
                stats[var] = utils.convertToStr(decStats[var])
            elif var == 'network':
                stats['network'] = decStats[var]
            else:
                try:
                    stats['disks'][var] = {}
                    for value in decStats[var]:
                        stats['disks'][var][value] = utils.convertToStr(decStats[var][value])
                except:
                    self.log.error("Error setting vm disk stats", exc_info=True)


        if self.lastStatus in ('Saving State', 'Restoring state', 'Migration Source', 'Migration Destination', 'Paused'):
            stats['status'] = self.lastStatus
        elif self._migrationSourceThread.isAlive():
            if self._migrationSourceThread._mode == 'file':
                stats['status'] = 'Saving State'
            else:
                stats['status'] = 'Migration Source'
        elif self.lastStatus == 'Up':
            stats['status'] = _getGuestStatus()
        else:
            stats['status'] = self.lastStatus
        stats['acpiEnable'] = self.conf.get('acpiEnable', 'true')
        stats['timeOffset'] = self.conf.get('timeOffset', '0')
        stats['clientIp'] = self.conf.get('clientIp', '')
        if 'pauseCode' in self.conf:
            stats['pauseCode'] = self.conf['pauseCode']
        try:
            stats.update(self.guestAgent.getGuestInfo())
        except:
            return stats
        memUsage = 0
        realMemUsage = int(stats['memUsage'])
        if realMemUsage != 0:
            memUsage = 100 - float(realMemUsage) / int(self.conf['memSize']) * 100
        stats['memUsage'] = utils.convertToStr(int(memUsage))
        return stats

    def migrate(self, params):
        self._acquireCpuLockWithTimeout()
        try:
            if self._migrationSourceThread.isAlive():
                self.log.warning('vm already migrating')
                return errCode['exist']
            # while we were blocking, another migrationSourceThread could have
            # taken self Down
            if self._lastStatus == 'Down':
                return errCode['noVM']
            self._migrationSourceThread = self.MigrationSourceThreadClass(self,
                                                                     **params)
            self._migrationSourceThread.start()
            check = self._migrationSourceThread.getStat()
            if check['status']['code']:
                return check
            return {'status': {'code': 0,
                               'message': 'Migration process starting'}}
        finally:
            self._guestCpuLock.release()

    def migrateStatus(self):
        return self._migrationSourceThread.getStat()

    def _getQemuDriveInfo(self, timeout=None):
        """Obtain info block from monitor interface."""
        for line in self._sendMonitorCommand('info block',
                                             timeout=timeout).splitlines():
            name, line = line[0:line.index(': ')], line[line.index(': ') + 2:]
            if line.endswith(' [not inserted]'):
                line = line[:-15]
                path = None
            for s in line.split():
                if s.startswith('file='):
                    path = s[5:]
                elif s.startswith('type='):
                    ty = s[5:]
                elif s.startswith('drv='):
                    drv = s[4:]
            for d in self._drives:
                if d.path == path:
                    d.name = name
                    d.type = ty
                    d.drv = drv

    def _setWriteWatermarks(self):
        min_remain = (100 -
                      config.getint('irs', 'volume_utilization_percent')) \
            * config.getint('irs', 'volume_utilization_chunk_mb') * 2**20 \
            / 100
        for d in self._drives:
            if d.type != 'hd': continue
            if d.drv != 'qcow2': continue # TODO should extend raw, too??
            if not d.blockDev: continue
            watermark = max((d.apparentsize - min_remain) / 2**20, 1)
            self._mon.postCommand('block_set_watermark %s %s' %
                                  (d.name, watermark))

    def waitForMigrationDestinationPrepare(self, port):
        """Wait until port is listened to (hopefully by our qemu)"""
        listenerTimeout = config.getint('vars', 'migration_listener_timeout')
        port = str(port)
        while port not in self.cif._listeningPorts():
            listenerTimeout -= 1
            time.sleep(1)
            if listenerTimeout <= 0:
                self.log.debug('Failed to detect destination listener on port' )
                return False
        return True
