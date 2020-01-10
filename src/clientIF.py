import os
import traceback
import time
import signal
import threading
import logging
import subprocess
import pickle
import SimpleXMLRPCServer

import M2Crypto.threading

from storage.dispatcher import StorageDispatcher
import storage.misc
import storage.hba
from config import config
import ksm
import netinfo
import hooks
import SecureXMLRPCServer
import dsaversion
from define import doneCode, errCode, Kbytes, Mbytes
import vm
import libvirtvm
import libvirtconnection
import constants
import utils

# default message for system shutdown, will be displayed in guest
USER_SHUTDOWN_MESSAGE = 'System going down'

PAGE_SIZE_BYTES = os.sysconf('SC_PAGESIZE')

def wrapApiMethod(f):
    def wrapper(*args, **kwargs):
        try:
            logLevel = logging.DEBUG
            if f.__name__ in ('list', 'getAllVmStats', 'getVdsStats',
                              'fenceNode'):
                logLevel = logging.TRACE
            f.im_self.log.log(logLevel, '[%s]::call %s with %s %s',
                              getattr(f.im_self.threadLocal, 'client', ''),
                              f.__name__, args, kwargs)
            if f.im_self._recovery and f.__name__ != 'create':
                res = errCode['recovery']
            else:
                res = f(*args, **kwargs)
            f.im_self.log.log(logLevel, 'return %s with %s', f.__name__, res)
            return res
        except:
            f.im_self.log.error(traceback.format_exc())
            return errCode['unexpected']
    wrapper.__name__ = f.__name__
    wrapper.__doc__ = f.__doc__
    return wrapper

class OSName:
    unknown = 'unknown'
    ovirt = 'RHEV Hypervisor'
    rhel = 'RHEL'

class clientIF:
    """
    The client interface of vdsm.

    Exposes vdsm verbs as xml-rpc functions.
    """
    def __init__ (self, log):
        """
        Initialize the (single) clientIF instance

        :param log: a log object to be used for this object's logging.
        :type log: :class:`logging.Logger`
        """
        self.vmContainerLock = threading.Lock()
        self._networkSemaphore = threading.Semaphore()
        self._shutdownSemaphore = threading.Semaphore()
        self.log = log
        self._recovery = True
        self.serverPort = config.get('addresses', 'management_port')
        self.serverIP = self._getServerIP()
        self.server = self._createXMLRPCServer()
        self._initIRS()
        try:
            self.vmContainer = {}
            ifids = netinfo.nics() + netinfo.bondings()
            ifrates = map(netinfo.speed, ifids)
            self._hostStats = utils.HostStatsThread(cif=self, log=log, ifids=ifids,
                                                ifrates=ifrates)
            self._hostStats.start()
            self.machineCapabilities = self._getCapabilities()
            cpuCores = int(self.machineCapabilities['cpuCores'])
            self.coresUsage = [0] * cpuCores
            mog = min(config.getint('vars', 'max_outgoing_migrations'), cpuCores)
            vm.MigrationSourceThread.setMaxOutgoingMigrations(mog)

            self.lastRemoteAccess = 0
            self.migrationLowPort = config.getint('vars', 'migrationLowPort')
            self.migrationPort = self.migrationLowPort
            self.migrationHighPort = config.getint('vars', 'migrationHighPort')
            self.migrationPortLock = threading.Lock()
            self._memLock = threading.Lock()
            self._enabled = True
            self.ksmMonitor = ksm.KsmMonitorThread(self)
            self.multivds_id = ''
            self._netConfigDirty = False
            threading.Thread(target=self._recoverExistingVms,
                             name='clientIFinit').start()
            self.threadLocal = threading.local()
            self.threadLocal.client = ''
        except:
            self.log.error('failed to init clientIF, shutting down storage dispatcher')
            if self.irs:
                self.irs.prepareForShutdown()
            raise

    def _getServerIP(self):
        """Return the IP address we should listen on"""

        addr = config.get('addresses', 'management_ip')
        if addr:
            return addr
        try:
            addr = netinfo.ifconfig()['rhevm']['addr']
        except:
            pass
        return addr

    def prepareForShutdown(self):
        """
        Prepare server for shutdown.

        Should be called before taking server down.
        """
        if not self._shutdownSemaphore.acquire(blocking=False):
            self.log.debug('cannot run prepareForShutdown concurrently')
            return errCode['unavail']
        try:
            if not self._enabled:
                self.log.debug('cannot run prepareForShutdown twice')
                return errCode['unavail']
            # stop listening ASAP
            self.server.server_close()
            self._enabled = False
            if self.irs:
                return self.irs.prepareForShutdown()
            else:
                return {'status': doneCode}
        finally:
            self._shutdownSemaphore.release()


    def setLogLevel(self, level):
        """
        Set verbosity level of vdsm's log.

        params
            level: requested logging level. `logging.DEBUG` `logging.ERROR`

        Doesn't survive a restart
        """
        logging.getLogger('clientIF.setLogLevel').info('Setting loglevel to %s' % level)
        handlers = logging.getLogger().handlers
        [fileHandler] = [h for h in handlers if isinstance(h, logging.FileHandler)]
        fileHandler.setLevel(int(level))

        return dict(status=doneCode)

    def getKeyCertFilenames(self):
        """
        Get the locations of key and certificate files.
        """
        tsPath = config.get('vars', 'trust_store_path')
        KEYFILE = tsPath + '/keys/vdsmkey.pem'
        CERTFILE = tsPath + '/certs/vdsmcert.pem'
        CACERT = tsPath + '/certs/cacert.pem'
        return KEYFILE, CERTFILE, CACERT

    def _createXMLRPCServer(self):
        """
        Create xml-rpc server over http or https.
        """
        cif = self
        class LoggingMixIn:
            def log_request(self, code='-', size='-'):
                """Track from where client connections are coming."""
                self.server.lastClient = self.client_address[0]
                self.server.lastClientTime = time.time()
                file(constants.P_VDSM_CLIENT_LOG, 'w')

        server_address = (self.serverIP, int(self.serverPort))
        if config.getboolean('vars', 'ssl'):
            class LoggingHandler(LoggingMixIn, SecureXMLRPCServer.SecureXMLRpcRequestHandler):
                def setup(self):
                    cif.threadLocal.client = self.client_address[0]
                    return SecureXMLRPCServer.SecureXMLRpcRequestHandler.setup(self)
            M2Crypto.threading.init()
            KEYFILE, CERTFILE, CACERT = self.getKeyCertFilenames()
            return SecureXMLRPCServer.SecureThreadedXMLRPCServer(server_address,
                        KEYFILE, CERTFILE, CACERT,
                        timeout=config.getint('vars', 'vds_responsiveness_timeout'),
                        requestHandler=LoggingHandler)
        else:
            class LoggingHandler(LoggingMixIn, SimpleXMLRPCServer.SimpleXMLRPCRequestHandler):
                def setup(self):
                    cif.threadLocal.client = self.client_address[0]
                    return SimpleXMLRPCServer.SimpleXMLRPCRequestHandler.setup(self)
            return utils.SimpleThreadedXMLRPCServer(server_address,
                        requestHandler=LoggingHandler, logRequests=True)

    def _initIRS(self):
        def wrapIrsMethod(f):
            def wrapper(*args, **kwargs):
                if self.threadLocal.client:
                    f.im_self.log.debug('[%s]', self.threadLocal.client)
                return f(*args, **kwargs)
            wrapper.__name__ = f.__name__
            wrapper.__doc__ = f.__doc__
            return wrapper
        self.irs = None
        if config.getboolean('irs', 'irs_enable'):
            try:
                self.irs = StorageDispatcher()
                for name in dir(self.irs):
                    method = getattr(self.irs, name)
                    if callable(method) and name[0] != '_':
                        self.server.register_function(wrapIrsMethod(method), name)
            except:
                self.log.error(traceback.format_exc())
        if not self.irs:
            err = errCode['recovery'].copy()
            err['status'] = err['status'].copy()
            err['status']['message'] = 'Failed to initialize storage'
            self.server._dispatch = lambda method, params: err


    def _registerFunctions(self):
        self.server.register_introspection_functions()
        for method, name in (
                (self.destroy, 'destroy'),
                (self.create, 'create'),
                (self.list, 'list'),
                (self.pause, 'pause'),
                (self.cont, 'cont'),
                (self.sysReset, 'reset'),
                (self.shutdown, 'shutdown'),
                (self.setVmTicket, 'setVmTicket'),
                (self.changeCD, 'changeCD'),
                (self.changeFloppy, 'changeFloppy'),
                (self.sendkeys, 'sendkeys')    ,
                (self.migrate, 'migrate'),
                (self.migrateStatus, 'migrateStatus'),
                (self.migrateCancel, 'migrateCancel'),
                (self.getVdsCapabilities, 'getVdsCapabilities'),
                (self.getVdsStats, 'getVdsStats'),
                (self.getVmStats, 'getVmStats'),
                (self.getAllVmStats, 'getAllVmStats'),
                (self.migrationCreate, 'migrationCreate'),
                (self.desktopLogin, 'desktopLogin'),
                (self.desktopLogoff, 'desktopLogoff'),
                (self.desktopLock, 'desktopLock'),
                (self.sendHcCmdToDesktop, 'sendHcCmdToDesktop'),
                (self.hibernate, 'hibernate'),
                (self.monitorCommand, 'monitorCommand'),
                (self.addNetwork, 'addNetwork'),
                (self.delNetwork, 'delNetwork'),
                (self.editNetwork, 'editNetwork'),
                (self.setSafeNetworkConfig, 'setSafeNetworkConfig'),
                (self.fenceNode, 'fenceNode'),
                (self.prepareForShutdown, 'prepareForShutdown'),
                (self.setLogLevel, 'setLogLevel'),
                        ):
           self.server.register_function(wrapApiMethod(method), name)


    def serve(self):
        """
        Register xml-rpc functions and serve clients until stopped
        """
        try:
            try:
                self._registerFunctions()
                while self._enabled:
                    try:
                        self.server.handle_request()
                    except Exception, e:
                        logmsg = traceback.format_exc()
                        if 'addr' in dir(e):
                            logmsg += 'remote address: ' + str(e.addr)
                        if self._enabled:
                            self.log.error(logmsg)

            except:
                self.log.error(traceback.format_exc())
        finally:
            self._hostStats.stop()

    #Global services

    def sendkeys(self, vmId, keySeq):
        """
        Send a string of keys to a guest's keyboard (OBSOLETE)

        Used only by QA and might be discontinued in next version.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        for key in keySeq:
            response = vm.doCommand(vmId, 'sendkey ' + key, sendOutput=True)
            if response['status']['code'] > 0:
                return response
            time.sleep(1)
        return response

    def hibernate(self, vmId, hiberVolHandle=None):
        """
        Hibernate a VM.

        :param hiberVolHandle: opaque string, indicating the location of
                               hibernation images.
        """
        params = {'vmId': vmId, 'mode': 'file',
                  'hiberVolHandle': hiberVolHandle}
        response = self.migrate(params)
        if not response['status']['code']:
            response['status']['message'] = 'Hibernation process starting'
        return response

    def migrate(self, params):
        """
        Migrate a VM to a remote host.

        :param params: a dictionary containing:
            *dst* - remote host or hibernation image filname
            *dstparams* - hibernation image filname for vdsm parameters
            *mode* - ``remote``/``file``
            *method* - ``online``
            *downtime* - allowed down time during online migration
        """
        self.log.debug(params)
        try:
            vmId = params['vmId']
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']

        vmParams = vm.status()
        if vmParams['status'] in ('WaitForLaunch', 'Down'):
            return errCode['noVM']
        if params.get('mode') == 'file':
            if 'dst' not in params:
                params['dst'], params['dstparams'] = \
                    self._getHibernationPaths(params['hiberVolHandle'])
        else:
            params['mode'] = 'remote'
        return vm.migrate(params)

    def migrateStatus(self, vmId):
        """
        Report status of a currently outgoing migration.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        return vm.migrateStatus()

    def migrateCancel(self, vmId):
        """
        Cancel a currently outgoing migration process. **Not implemented**
        """
        return {'status': {'code': 0, 'message': 'Unsupported yet'}}

    def monitorCommand(self, vmId, cmd):
        """
        Send a monitor command to the specified VM and wait for the answer.

        :param vmId: uuid of the specified VM
        :type vmId: UUID
        :param command: a single monitor command (without terminating newline)
        :type command: string
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        return vm.doCommand(cmd, sendOutput=True)

    def shutdown(self, vmId, timeout=None, message=None):
        """
        Shut a VM down politely.

        :param message: message to be shown to guest user before shutting down
                        his machine.
        :param timeout: grace period (seconds) to let guest user close his
                        applications.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        if not timeout:
            timeout = config.get('vars', 'user_shutdown_timeout')
        if not message:
            message = USER_SHUTDOWN_MESSAGE
        return vm.shutdown(timeout, message)

    def setVmTicket(self, vmId, otp, seconds, connAct='disconnect'):
        """
        Set the ticket (password) to be used to connect to a VM display

        :param vmId: specify the VM whos ticket is to be changed.
        :param otp: new password
        :type otp: string
        :param seconds: ticket lifetime (seconds)
        :param connAct: what to do with a currently-connected client (SPICE only):
                ``disconnect`` - disconnect old client when a new client
                                 connects.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        return vm.setTicket(otp, seconds, connAct)

    def sysReset(self, vmId):
        """
        Press the virtual reset button for the specified VM.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        return vm.doCommand(vmId, 'system_reset', sendOutput=True)

    def destroy(self, vmId):
        """
        Destroy the specified VM.
        """
        self.vmContainerLock.acquire()
        self.log.info("vmContainerLock aquired by vm %s", vmId)
        try:
            v = self.vmContainer.get(vmId)
            if not v:
                return errCode['noVM']
            status = v.destroy()
            if status['status']['code'] == 0:
                status['status']['message'] = "Machine destroyed"
            return status
        finally:
            self.vmContainerLock.release()

    def pause(self, vmId):
        v = self.vmContainer.get(vmId)
        if not v:
            return errCode['noVM']
        return v.pause()

    def cont(self, vmId):
        v = self.vmContainer.get(vmId)
        if not v:
            return errCode['noVM']
        return v.cont()

    def changeCD(self, vmId, path):
        """
        Change the CD in the specified VM.

        :param vmId: uuid of specific VM.
        :type vmId: UUID
        :param path: specfication of the new CD image. Either an image
                path or a `storage`-centric quartet.
        """
        vm = self.vmContainer.get(vmId)
        if not vm:
            return errCode['noVM']
        return vm.changeCD(path)

    def changeFloppy(self, vmId, path):
        """
        Change the floppy disk in the specified VM.

        :param vmId: uuid of specific VM.
        :type vmId: UUID
        :param path: specfication of the new floppy image. Either an image
                path or a `storage`-centric quartet.
        """
        vm = self.vmContainer.get(vmId)
        if not vm:
            return errCode['noVM']
        return vm.changeFloppy(path)

    def _getFreeIfDisplay(self, vmId, remote):
        registeredIfids = set([ int(vm.conf['ifid'])
                            for vm in self.vmContainer.values() ])
        ID = config.getint('vars', 'if_and_display_base')
        while ID in registeredIfids:
            ID += 1
        if remote:
            displayID = ID
        else:
            displayID = 0
        return (displayID, ID)

    def _createSysprepFloppyFromInf(self, infFileBinary, floppyImage):
        try:
            rc, out, err = utils.execCmd([constants.EXT_MK_SYSPREP_FLOPPY,
                                         floppyImage],
                                        sudo=True, data=infFileBinary.data)
            if rc:
                return False
            else:
                return True
        except:
            self.log.error(traceback.format_exc())
            return False

    def _getNetworkIp(self, bridge):
        try:
            ip = netinfo.ifconfig()[bridge]['addr']
        except:
            ip = config.get('addresses', 'guests_gateway_ip')
            if ip == '':
                ip = '0'
            self.log.info('network %s: using %s', bridge, ip)
        return ip

    def _getHibernationPaths(self, hiberVolHandle):
        """
        Break *hiberVolHandle* into the "quartets" of hibernation images.
        """
        domainID, poolID, stateImageID, stateVolumeID, \
            paramImageID, paramVolumeID = hiberVolHandle.split(',')

        return dict(domainID=domainID, poolID=poolID,
                    imageID=stateImageID, volumeID=stateVolumeID), \
               dict(domainID=domainID, poolID=poolID,
                    imageID=paramImageID, volumeID=paramVolumeID)


    def _prepareVolumePath(self, drive):
        if type(drive) == dict:
            res = self.irs.prepareVolume(drive['domainID'], drive['poolID'],
                            drive['imageID'], drive['volumeID'])
            if res['status']['code']:
                raise vm.VolumeError(drive)
            res = self.irs.getVolumePath(drive['domainID'],
                            drive['poolID'],
                            drive['imageID'], drive['volumeID'])
            if res['status']['code']:
                raise vm.VolumeError(drive)
            path = res['path']
        else:
            if drive and not os.path.exists(drive):
                raise vm.VolumeError(drive)
            path = drive
        return path

    def _teardownVolumePath(self, drive, keepWriteable=False):
        result = {'status':doneCode}
        if type(drive) == dict:
            result = self.irs.teardownVolume(drive['domainID'],
                        drive['poolID'], drive['imageID'], drive['volumeID'],
                        keepWriteable)

        return result['status']['code']

    def create(self, vmParams, recovery=False):
        """
        Start up a virtual machine.

        :param vmParams: required and optional VM parameters.
        :type vmParams: dict
        :param recovery: whether to connect to a currently-running VM.
        :type recovery: bool
        """
        if self._recovery and not recovery:
            return errCode['recovery']
        try:
            if vmParams.get('vmId') in self.vmContainer:
                self.log.warning('vm %s already exists' % vmParams['vmId'])
                return errCode['exist']

            if 'hiberVolHandle' in vmParams:
                vmParams['restoreState'], paramFilespec = \
                         self._getHibernationPaths(vmParams.pop('hiberVolHandle'))
                try: # restore saved vm parameters
                # NOTE: pickled params override command-line params. this
                # might cause problems if an upgrade took place since the
                # parmas were stored.
                    fname = self._prepareVolumePath(paramFilespec)
                    try:
                        with file(fname) as f:
                            pickledMachineParams = pickle.load(f)

                        if type(pickledMachineParams) == dict:
                            self.log.debug('loaded pickledMachineParams '
                                                   + str(pickledMachineParams))
                            self.log.debug('former conf ' + str(vmParams))
                            vmParams.update(pickledMachineParams)
                    finally:
                        self._teardownVolumePath(paramFilespec)
                except:
                    self.log.error(traceback.format_exc())

            requiredParams = ['vmId', 'memSize', 'display']
            for param in requiredParams:
                if param not in vmParams:
                    self.log.error('Missing required parameter %s' % (param))
                    return {'status': {'code': errCode['MissParam']['status']['code'],
                                       'message': 'Missing required parameter %s' % (param)}}
            try:
                storage.misc.validateUUID(vmParams['vmId'])
            except:
                return {'status': {'code': errCode['MissParam']['status']['code'],
                                   'message': 'vmId must be a valid UUID'}}
            if vmParams['memSize'] == 0:
                return {'status': {'code': errCode['MissParam']['status']['code'],
                                   'message': 'Must specify nonzero memSize'}}

            if vmParams.get('boot') == 'c' and not 'hda' in vmParams \
                                           and not vmParams.get('drives'):
                return {'status': {'code': errCode['MissParam']['status']['code'],
                                   'message': 'missing boot disk'}}

            #From this point update Launch counter
            if config.getboolean('vars', 'lock_cpu'):
                cpu = self.coresUsage.index(min(self.coresUsage))
                self.coresUsage[cpu] += 1
                vmParams['cpu'] = str(cpu)

            if 'vmType' not in vmParams:
                vmParams['vmType'] = 'kvm'
            if vmParams['vmType'] == 'qemu':
                vmParams['executable'] = config.get('vars', 'qemuexec')
            elif vmParams['vmType'] == 'kvm':
                vmParams['executable'] = config.get('vars', 'vtexec')
                if 'kvmEnable' not in vmParams:
                    vmParams['kvmEnable'] = 'true'
                if not utils.tobool(vmParams['kvmEnable']):
                    vmParams['executable'] += ' -no-kvm'

            if 'sysprepInf' in vmParams:
                if not vmParams.get('floppy'):
                    vmParams['floppy'] = '%s%s.vfd' % (constants.P_VDSM_RUN,
                                                vmParams['vmId'])
                vmParams['volatileFloppy'] = True

            if 'sysprepInf' in vmParams:
                if not self._createSysprepFloppyFromInf(vmParams['sysprepInf'],
                                 vmParams['floppy']):
                    return {'status': {'code': errCode['createErr']
                                                      ['status']['code'],
                                       'message': 'Failed to create '
                                                  'sysprep floppy image. '
                                                  'No space on /tmp?'}}
                    return errCode['createErr']

            if vmParams.get('display') not in ('vnc', 'qxl', 'qxlnc', 'local'):
                return {'status': {'code': errCode['createErr']
                                                  ['status']['code'],
                                   'message': 'Unknown display type %s'
                                                % vmParams.get('display') }}
            if 'nicModel' not in vmParams:
                vmParams['nicModel'] = config.get('vars', 'nic_model')
            vmParams['displayIp'] = self._getNetworkIp(vmParams.get(
                                                        'displayNetwork'))
            self.vmContainerLock.acquire()
            self.log.info("vmContainerLock aquired by vm %s", vmParams['vmId'])
            try:
                if 'recover' not in vmParams:
                    if vmParams['vmId'] in self.vmContainer:
                        self.log.warning('vm %s already exists' % vmParams['vmId'])
                        return errCode['exist']
                    displayid, ifid = self._getFreeIfDisplay(vmParams['vmId'], vmParams['display'] != 'local')
                    vmParams['ifid'] = str(ifid)
                if config.getboolean('vars', 'use_libvirt'):
                    vmParams['displayPort'] = '-1' # selected by libvirt
                    vmParams['displaySecurePort'] = '-1'
                    VmClass = libvirtvm.LibvirtVm
                else:
                    vmParams['displayPort'] = str(5900 + displayid)
                    vmParams['displaySecurePort'] = str(5900 - displayid)
                    VmClass = vm.Vm
                self.vmContainer[vmParams['vmId']] = VmClass(self, vmParams)
            finally:
                self.vmContainerLock.release()
            self.vmContainer[vmParams['vmId']].run()
            self.log.debug("Total desktops after creation of %s is %d" % (vmParams['vmId'], len(self.vmContainer)))
            return {'status': doneCode, 'vmList': self.vmContainer[vmParams['vmId']].status()}
        except OSError, e:
            self.log.debug(traceback.format_exc())
            return {'status': {'code': errCode['createErr']['status']['code'],
                               'message': 'Failed to create VM. '
                                          'No space on /tmp? ' + e.message}}
        except:
            self.log.debug(traceback.format_exc())
            return errCode['unexpected']

    def list(self, full=False):
        """ return a list of known VMs with full (or partial) config each """
        def reportedStatus(vm, full):
            d = vm.status()
            if full:
                return d
            else:
                return {'vmId': d['vmId'], 'status': d['status']}
        return {'status': doneCode,
                'vmList': [reportedStatus(vm, full) for vm
                            in self.vmContainer.values()]}

    def _getSingleVmStats (self, vmId):
        v = self.vmContainer.get(vmId)
        if not v:
            return None
        stats = v.getStats().copy()
        stats['vmId'] = vmId
        return stats

    def getVmStats(self, vmId):
        """
        Obtain statistics of the specified VM
        """
        response = self._getSingleVmStats(vmId)
        if response:
            return {'status': doneCode, 'statsList': [response]}
        else:
            return errCode['noVM']

    def getAllVmStats(self):
        """
        Get statistics of all running VMs.
        """
        statsList = []
        for vmId in self.vmContainer.keys():
            response = self._getSingleVmStats(vmId)
            if response:
                statsList.append(response)
        return {'status': doneCode, 'statsList': statsList}

    def _getCapabilities(self):
        """
        Collect host capabilities.
        """
        def _getIfaceByIP(addr):
            import struct, socket
            remote = struct.unpack('I', socket.inet_aton(addr))[0]
            for line in file('/proc/net/route').readlines()[1:]:
                iface, dest, gateway, flags, refcnt, use, metric, \
                        mask, mtu, window, irtt = line.split()
                dest = int(dest, 16)
                mask = int(mask, 16)
                if remote & mask == dest & mask:
                    return iface
            return '' # should never get here w/ default gw
        def osversion(osname):
            version = release = None
            try:
                if osname == OSName.ovirt:
                    if os.path.exists('/etc/rhev-hypervisor-release'):
                        for line in file('/etc/default/version').readlines():
                            if line.startswith('VERSION='):
                                version = line[len('VERSION='):].strip()
                            elif line.startswith('RELEASE='):
                                release = line[len('RELEASE='):].strip()
                else:
                    p = subprocess.Popen([constants.EXT_RPM, '-q', '--qf',
                        '%{VERSION} %{RELEASE}\n', 'redhat-release'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, close_fds=True)
                    out, err = p.communicate()
                    if p.returncode == 0:
                        version, release = out.split()
                    else:
                        p = subprocess.Popen([constants.EXT_RPM, '-q', '--qf',
                            '%{VERSION} %{RELEASE}\n', 'redhat-release-server'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, close_fds=True)
                        out, err = p.communicate()
                        if p.returncode == 0:
                            version, release = out.split()
            except:
                self.log.debug(traceback.format_exc())
            return version, release
        def getos():
            try:
                for osname in (OSName.ovirt, OSName.rhel):
                    v, r = osversion(osname)
                    if v is not None and r is not None:
                        return {'name': osname, 'version': v, 'release': r}
            except:
                self.log.error(traceback.format_exc())
            return {'name': OSName.unknown, 'version': '', 'release': ''}
        def getKeyPackages():
            def kernelDict():
                try:
                    ver, rel = file('/proc/sys/kernel/osrelease').read(). \
                                        strip().split('-', 1)
                except:
                    ver, rel = '0', '0'
                try:
                    t = file('/proc/sys/kernel/version').read().strip().split(None, 2)[2]
                    t = time.mktime(time.strptime(t, '%a %b %d %H:%M:%S %Z %Y'))
                except:
                    t = '0'
                return dict(version=ver, release=rel, buildtime=t)

            KEY_PACKAGES = ['qemu-kvm', 'qemu-img',
                            'vdsm', 'spice-server', 'libvirt']
            pkgs = {'kernel': kernelDict()}
            try:
                for pkg in KEY_PACKAGES:
                    rc, out, err = utils.execCmd([constants.EXT_RPM, '-q', '--qf',
                          '%{NAME}\t%{VERSION}\t%{RELEASE}\t%{BUILDTIME}\n', pkg], sudo=False)
                    if rc: continue
                    line = out[-1]
                    n, v, r, t = line.split()
                    pkgs[pkg] = dict(version=v, release=r, buildtime=t)
            except:
                self.log.error(traceback.format_exc())
            return pkgs
        def getEmulatedMachines():
            return [ m.split()[0] for m in
                     utils.execAndGetOutput(config.get('vars', 'vtexec') +
                                            ' -M ?') ][1:]
        def getIscsiIniName():
            try:
                for line in file('/etc/iscsi/initiatorname.iscsi').readlines():
                    k, v = line.split('=', 1)
                    if k.strip() == 'InitiatorName':
                        return v.strip()
            except:
                pass
            return ''
        def getCompatibleCpuModels():
            import libvirt
            from xml.dom import minidom

            c = libvirtconnection.get(self)
            cpu_map = minidom.parseString(
                            file('/usr/share/libvirt/cpu_map.xml').read())
            allModels = [ m.getAttribute('name') for m
                  in cpu_map.getElementsByTagName('arch')[0].childNodes
                  if m.nodeName == 'model' ]
            def compatible(model):
                xml = '<cpu match="minimum"><model>%s</model></cpu>' % model
                return c.compareCPU(xml, 0) in (
                                        libvirt.VIR_CPU_COMPARE_SUPERSET,
                                        libvirt.VIR_CPU_COMPARE_IDENTICAL)
            return [ 'model_' + model for model
                     in allModels if compatible(model) ]

        caps = {'kvmEnabled': 'false',
                'management_ip': self.serverIP}
        caps.update(dsaversion.version_info)
        caps['kvmEnabled'] = \
                str(config.getboolean('vars', 'fake_kvm_support') or
                    os.path.exists('/dev/kvm')).lower()

        infoDict = {}
        caps['cpuCores'] = 0
        cpuSockets = set()

        try:
            cpuInfo = file('/proc/cpuinfo').readlines()
            memInfo = file('/proc/meminfo').readlines()
        except:
            self.log.error('Error retrieving machine info')
        for entry in cpuInfo + memInfo:
            if ':' in entry:
                param, val = entry.split(':')
                param = param.strip()
                val = val.strip()
                infoDict[param] = val
                if param == 'processor':
                    caps['cpuCores'] += 1
                if param == 'physical id':
                    cpuSockets.add(val)
        caps['cpuSpeed'] = infoDict['cpu MHz']
        if config.getboolean('vars', 'fake_kvm_support'):
            caps['cpuModel'] = 'Intel(Fake) CPU'
            flags = set(infoDict['flags'].split()).union(['vmx', 'sse2', 'nx'])
            caps['cpuFlags'] = ','.join(flags) + 'model_486,model_pentium,' + \
                'model_pentium2,model_pentium3,model_pentiumpro,model_qemu32,' \
                'model_coreduo,model_core2duo,model_n270,model_Conroe,' \
                'model_Penryn,model_Nehalem,model_Opteron_G1'
        else:
            caps['cpuModel'] = infoDict['model name']
            caps['cpuFlags'] = infoDict['flags'].strip().replace(' ',',') + \
                ',' + ','.join(getCompatibleCpuModels())
        caps['vmTypes'] = ['kvm', 'qemu']
        caps['cpuSockets'] = str(len(cpuSockets))
        if 'MemTotal' in infoDict:
            mem = str(int(infoDict['MemTotal'].replace('kB','')) / 1024)
            caps['memSize'] = mem
        else:
            self.log.error('Could not retrieve memory information')
        caps['reservedMem'] = str(
            config.getint('vars', 'host_mem_reserve') +
            config.getint('vars', 'extra_mem_reserve') )
        caps['guestOverhead'] = config.get('vars', 'guest_ram_overhead')
        if 'server' in dir(self) and 'lastClient' in dir(self.server):
            caps['lastClient'] = self.server.lastClient
            caps['lastClientIface'] = _getIfaceByIP(self.server.lastClient)
        caps['operatingSystem'] = getos()
        caps['uuid'] = utils.getHostUUID()
        caps['packages2'] = getKeyPackages()
        caps['emulatedMachines'] = getEmulatedMachines()
        caps['ISCSIInitiatorName'] = getIscsiIniName()
        caps['HBAInventory'] = storage.hba.HBAInventory()
        caps.update(netinfo.get())

        caps['hooks'] = hooks.installed()
        return caps

    def getVdsCapabilities(self):
        """
        Report host capabilities.
        """
        self.machineCapabilities = self._getCapabilities()
        return {'status': doneCode, 'info': self.machineCapabilities}

    def getVdsStats(self):
        """
        Report host statistics.
        """
        def _readSwapTotalFree():
            meminfo = utils.readMemInfo()
            return meminfo['SwapTotal'] / 1024, meminfo['SwapFree'] / 1024

        stats = {}
        decStats = self._hostStats.get()
        for var in decStats:
            stats[var] = utils.convertToStr(decStats[var])
        stats['memAvailable'] = self._memAvailable() / Mbytes
        stats['memShared'] = self._memShared() / Mbytes
        stats['memCommitted'] = self._memCommitted() / Mbytes
        stats['swapTotal'], stats['swapFree'] = _readSwapTotalFree()
        stats['vmCount'], stats['vmActive'], stats['vmMigrating'] = self._countVms()
        (tm_year, tm_mon, tm_day, tm_hour, tm_min, tm_sec,
             dummy, dummy, dummy) = time.gmtime(time.time())
        stats['dateTime'] = '%02d-%02d-%02dT%02d:%02d:%02d GMT' % (
                tm_year, tm_mon, tm_day, tm_hour, tm_min, tm_sec)
        stats['ksmState'] = self.ksmMonitor.state
        stats['ksmPages'] = self.ksmMonitor.pages
        stats['ksmCpu'] = self.ksmMonitor.cpuUsage
        stats['netConfigDirty'] = str(self._netConfigDirty)
        return {'status': doneCode, 'info': stats}

    def _listeningPorts(self):
        return map(lambda l: l.split()[3].split(':')[-1],
                utils.execCmd([constants.EXT_NETSTAT, '-lnt'],
                              raw=False, sudo=False)[1][2:])

    def _findFreeMigrationPort(self):
        self.migrationPortLock.acquire()
        try:
            listeningPorts = self._listeningPorts()
            startpoint = self.migrationPort
            while True:
                if self.migrationPort < self.migrationHighPort:
                    self.migrationPort += 1
                else:
                    self.migrationPort = self.migrationLowPort
                if not str(self.migrationPort) in listeningPorts:
                    break
                if startpoint == self.migrationPort:
                    self.log.error('no free listening port in range %s,%s',
                            self.migrationLowPort, self.migrationHighPort)
                    raise RuntimeError
            port = str(self.migrationPort)
        finally:
            self.migrationPortLock.release()
        return port

    #Migration only methods
    def migrationCreate (self, params):
        """
        Start a migration-destination VM.

        :param params: parameters of new VM, to be passed to :meth:`~clientIF.create`.
        :type params: dict
        """
        self.log.debug('Migration create')

        if not utils.validLocalHostname():
            self.log.error('Migration failed: local hostname is not correct')
            return errCode['createErr']

        port = 0
        if params.get('migrationDest') != 'libvirt':
            port = self._findFreeMigrationPort()
            params['migrationDest'] = self.serverIP + ":" + port
        # FIXME there's a race here: after the port is found free, someone
        # (outside vdsm) may bind to it before the 'create' manages to do it.
        response = self.create(params)
        if response['status']['code']:
            self.log.debug('Migration create - Failed')
            return response

        v = self.vmContainer.get(params['vmId'])
        if port:
            # wait until destination qemu is created
            if not v.waitForPid():
                return errCode['createErr']

        if not v.waitForMigrationDestinationPrepare(port):
            return errCode['createErr']

        self.log.debug('Destination VM creation succeeded')
        return {'status': doneCode, 'migrationPort': port, 'params': response['vmList']}

    #SSO
    def desktopLogin (self, vmId, domain, user, password):
        """
        Log into guest operating system using guest agent.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        vm.guestAgent.desktopLogin(domain, user, password)
        if vm.guestAgent.isResponsive():
            return {'status': doneCode}
        else:
            return errCode['nonresp']

    def desktopLogoff (self, vmId, force):
        """
        Log out of guest operating system using guest agent.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        vm.guestAgent.desktopLogoff(force)
        if vm.guestAgent.isResponsive():
            return {'status': doneCode}
        else:
            return errCode['nonresp']

    def desktopLock (self, vmId):
        """
        Lock user session in guest operating system using guest agent.
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        vm.guestAgent.desktopLock()
        if vm.guestAgent.isResponsive():
            return {'status': doneCode}
        else:
            return errCode['nonresp']

    def sendHcCmdToDesktop (self, vmId, message):
        """
        Send a command to the guest agent (depricated).
        """
        try:
            vm = self.vmContainer[vmId]
        except KeyError:
            return errCode['noVM']
        vm.guestAgent.sendHcCmdToDesktop(message)
        if vm.guestAgent.isResponsive():
            return {'status': doneCode}
        else:
            return errCode['nonresp']

    def memTestAndCommit(self, newVm):
        """
        Test if enough memory is available for new VM (currently unused)
        """
        self._memLock.acquire()
        try:
            memAvailable = self._memAvailable()
            newVm.memCommit()
            memRequired = newVm.memCommitted
            self.log.debug('%s: memAvailable %d memRequired %d Mb' %
                    (newVm.conf['vmId'], memAvailable / Mbytes, memRequired / Mbytes))
            if newVm.destroyed:
                newVm.memCommitted = 0
            return newVm.memCommitted
        finally:
            self._memLock.release()

    # take a rough estimate on how much free mem is available for new vm
    # memTotal = memFree + memCached + mem_used_by_non_qemu + resident  .
    # simply returning (memFree + memCached) is not good enough, as the
    # resident set size of qemu processes may grow - up to  memCommitted.
    # Thus, we deduct the growth potential of qemu processes, which is
    # (memCommitted - resident)
    def _memAvailable(self):
        """
        Return an approximation of available memory for new VMs.
        """
        memCommitted = self._memCommitted()
        resident = 0
        for vm in self.vmContainer.values():
            if vm.conf['pid'] == '0': continue
            try:
                statmfile = file('/proc/' + vm.conf['pid'] + '/statm')
                resident += int(statmfile.read().split()[1])
            except:
                pass
        resident *= PAGE_SIZE_BYTES
        meminfo = utils.readMemInfo()
        freeOrCached = (meminfo['MemFree'] +
                        meminfo['Cached'] + meminfo['Buffers']) * Kbytes
        return freeOrCached + resident - memCommitted - \
                config.getint('vars', 'host_mem_reserve') * Mbytes

    # take a rough estimate on how much memory is shared between VMs
    def _memShared(self):
        """
        Return an approximation of memory shared by VMs thanks to KSM.
        """
        shared = 0
        for vm in self.vmContainer.values():
            if vm.conf['pid'] == '0': continue
            try:
                statmfile = file('/proc/' + vm.conf['pid'] + '/statm')
                shared += int(statmfile.read().split()[2]) * PAGE_SIZE_BYTES
            except:
                pass
        return shared

    def _memCommitted(self):
        """
        Return the amount of memory (Mb) committed for VMs
        """
        committed = 0
        for vm in self.vmContainer.values():
            committed += vm.memCommitted
        return committed

    def _countVms(self):
        count = active = migrating = 0
        for vmId, vm in self.vmContainer.items():
            try:
                count += 1
                status = vm.lastStatus
                if status == 'Up':
                    active += 1
                elif 'Migration' in status:
                    migrating += 1
            except:
                self.log.error(vmId + ': Lost connection to VM')
        return count, active, migrating

    def _recoverExistingVms(self):
        try:
            pids = [pid.strip() for pid in
                    utils.execCmd([constants.EXT_PGREP, 'qemu-kvm'],
                                  raw=False, sudo=False)[1]]
            for pid in pids:
                if not self._recoverVm(pid):
                    try:
                        self.log.info('loose kvm process %s found, killing it.' % pid)
                        os.kill(int(pid), signal.SIGKILL)
                    except:
                        self.log.error('failed to kill loose kvm process %s' % pid)
            while self._enabled and \
                  'WaitForLaunch' in [v.lastStatus for v in self.vmContainer.values()]:
                time.sleep(1)
            self._cleanOldFiles()
            self._recovery = False

            # Now if we have VMs to restore we should wait pool connection
            # and then prepare all volumes.
            # Actually, we need it just to get the resources for future
            # volumes manipulations
            while self._enabled and self.vmContainer and \
                  not self.irs.getConnectedStoragePoolsList()['poollist']:
                time.sleep(5)

            for vmId in self.vmContainer.keys():
                # Do not prepare volumes when system goes down
                if self._enabled:
                    self.vmContainer[vmId].preparePaths()
        except:
            self.log.error(traceback.format_exc())

    def _recoverVm(self, pid):
        def pidToVmId(pid):
            """ find vmId from vdsm-initiated command line """
            arg1 = ''
            for arg2 in file('/proc/%s/cmdline' % pid).read().split('\x00'):
                if arg1 == '-uuid':
                    return arg2
                if arg1 == '-smbios':
                    for elem in arg2.split(','):
                        kv = elem.split('=')
                        if len(kv) == 2 and kv[0] == 'uuid':
                            return kv[1]
                arg1 = arg2
        try:
            vmid = pidToVmId(pid)
            if vmid is None:
                return None
            recoveryFile = constants.P_VDSM_RUN + vmid + ".recovery"
            params = pickle.load(file(recoveryFile))
            params['recover'] = True
            now = time.time()
            pt = float(params.pop('startTime', now))
            params['elapsedTimeOffset'] = now - pt
            self.log.debug("Trying to recover " + params['vmId'])
            if not self.create(params, recovery=True)['status']['code']:
                return recoveryFile
        except:
            self.log.debug(traceback.format_exc())
        return None

    def _cleanOldFiles(self):
        for f in os.listdir(constants.P_VDSM_RUN):
            try:
                vmId, fileType = f.split(".", 1)
                if fileType in ["guest.socket", "monitor.socket", "pid",
                                    "stdio.dump", "recovery"]:
                    if vmId in self.vmContainer: continue
                    if f == 'vdsmd.pid': continue
                    if f == 'respawn.pid': continue
                    if f == 'svdsm.pid': continue
                    if f == 'svdsm.sock': continue
                else:
                    continue
                self.log.debug("removing old file " + f)
                utils.rmFile(constants.P_VDSM_RUN + f)
            except:
                pass


    def addNetwork(self, bridge, vlan, bond, nics, options):
        """Add a new network to this vds.

        Network topology is bridge--[vlan--][bond--]nics.
        vlan(number) and bond are optional - pass the empty string to discard
        them.  """

        self.log.debug('addNetwork(%s,%s,%s,%s,%s)' % (bridge, vlan, bond,
                    nics, options))
        if not self._networkSemaphore.acquire(blocking=False):
            self.log.warn('concurrent network verb already executing')
            return errCode['unavail']
        try:
            self._netConfigDirty = True
            rc, out, err = utils.execCmd([constants.EXT_ADDNETWORK,
                    bridge,
                    vlan, bond, ','.join(nics)] +
                    [ '%s=%s' % (k, v) for k, v in options.iteritems()],
                    sudo=True, raw=True)
            return {'status': {'code': rc, 'message': out + err}}
        finally:
            self._networkSemaphore.release()

    def delNetwork(self, bridge, vlan, bond, nics, options={}):
        """Delete a network from this vds."""

        try:
            self.log.debug('delNetwork(%s,%s,%s,%s,%s)' % (bridge, vlan, bond,
                            nics, options))
            if not self._networkSemaphore.acquire(blocking=False):
                self.log.warn('concurrent network verb already executing')
                return errCode['unavail']
            self._netConfigDirty = True
            rc, out, err = utils.execCmd([constants.EXT_DELNETWORK,
                    bridge,
                    vlan, bond, ','.join(nics)] +
                    [ '%s=%s' % (k, v) for k, v in options.iteritems()],
                    sudo=True, raw=True)
            return {'status': {'code': rc, 'message': out + err}}
        finally:
            self._networkSemaphore.release()

    def editNetwork(self, oldBridge, newBridge, vlan, bond, nics, options):
        """Add a new network to this vds, replacing an old one."""

        self.log.debug('editNetwork(%s,%s,%s,%s,%s,%s)', oldBridge,
                        newBridge, vlan, bond, nics, options)
        if not self._networkSemaphore.acquire(blocking=False):
            self.log.warn('concurrent network verb already executing')
            return errCode['unavail']
        try:
            self._netConfigDirty = True
            rc, out, err = utils.execCmd([constants.EXT_EDITNETWORK,
                    oldBridge, newBridge,
                    vlan, bond, ','.join(nics)] +
                    [ '%s=%s' % (k, v) for k, v in options.iteritems()],
                    sudo=True, raw=True)
            return {'status': {'code': rc, 'message': out + err}}
        finally:
            self._networkSemaphore.release()

    def setSafeNetworkConfig(self):
        """Declare current network configuration as 'safe'"""
        self.log.debug('setSafeNetworkConfig')
        self._netConfigDirty = False
        rc, out, err = utils.execCmd([constants.EXT_VDSM_STORE_NET_CONFIG],
                                     sudo=True)
        return {'status': doneCode}

    def fenceNode(self, addr, port, agent, user, passwd, action,
                  secure=False, options=''):
        """Send a fencing command to a remote node.

           agent is one of (rsa, ilo, drac5, ipmilan, etc)
           action can be one of (status, on, off, reboot)."""

        def waitForPid(p, inp):
            """ Wait until p.pid exits. Kill it if vdsm exists before. """
            try:
                p.stdin.write(inp)
                p.stdin.close()
                while p.poll() is None:
                    if not self._enabled:
                        self.log.debug('killing fence script pid %s', p.pid)
                        os.kill(p.pid, signal.SIGTERM)
                        time.sleep(1)
                        try:
                            # improbable race: p.pid may now belong to another
                            # process
                            os.kill(p.pid, signal.SIGKILL)
                        except:
                            pass
                        return
                    time.sleep(1)
                self.log.debug('rc %s inp %s out %s err %s', p.returncode,
                               hidePasswd(inp),
                               p.stdout.read(), p.stderr.read())
            except:
                self.log.error(traceback.format_exc())

        def hidePasswd(text):
            cleantext = ''
            for line in text.splitlines(True):
                if line.startswith('passwd='):
                    line = 'passwd=XXXX\n'
                cleantext += line
            return cleantext

        self.log.debug('fenceNode(addr=%s,port=%s,agent=%s,user=%s,' +
               'passwd=%s,action=%s,secure=%s,options=%s)', addr, port, agent,
               user, 'XXXX', action, secure, options)

        if action not in ('status', 'on', 'off', 'reboot'):
            raise ValueError('illegal action ' + action)

        script = constants.EXT_FENCE_PREFIX + agent

        try:
            p = subprocess.Popen([script], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True)
        except OSError, e:
            if e.errno == os.errno.ENOENT:
                return errCode['fenceAgent']
            raise

        inp = ('agent=fence_%s\nipaddr=%s\nlogin=%s\noption=%s\n' +
                      'passwd=%s\n') % (agent, addr, user, action, passwd)
        if port != '':
            inp += 'port=%s\n' % (port,)
        if utils.tobool(secure):
            inp += 'secure=yes\n'
        inp += options
        if action == 'status':
            out, err = p.communicate(inp)
            self.log.debug('rc %s in %s out %s err %s', p.returncode,
                           hidePasswd(inp), out, err)
            if not 0 <= p.returncode <= 2:
                return {'status': {'code': 1,
                                   'message': out + err}}
            message = doneCode['message']
            if p.returncode == 0:
                power = 'on'
            elif p.returncode == 2:
                power = 'off'
            else:
                power = 'unknown'
                message = out + err
            return {'status': {'code': 0, 'message': message},
                    'power': power}
        threading.Thread(target=waitForPid, args=(p, inp)).start()
        return {'status': doneCode}
