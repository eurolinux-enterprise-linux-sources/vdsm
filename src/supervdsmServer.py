#!/usr/bin/python
import logging
import logging.config
import sys
import os
import errno
import threading
from time import sleep
import signal

from storage.multipath import getScsiSerial as _getScsiSerial
from storage.iscsi import forceIScsiScan as _forceIScsiScan
from supervdsm import _SuperVdsmManager, PIDFILE, ADDRESS
from storage.fileUtils import chown, open_ex
from constants import METADATA_GROUP, METADATA_USER

KB = 2**10
TEST_BUFF_LEN = 4 * KB
LOG_CONF_PATH = "/etc/vdsm/logger.conf"
class _SuperVdsm(object):
    _log = logging.getLogger("SuperVdsm.ServerCallback")
    def getScsiSerial(self, *args, **kwargs):
        return _getScsiSerial(*args, **kwargs)

    def forceIScsiScan(self, *args, **kwargs):
        return _forceIScsiScan(*args, **kwargs)

    def testReadDevices(self, devices):
        for device in devices:
            with open_ex(device, "dr") as f:
                f.seek(TEST_BUFF_LEN)
                if len(f.read(TEST_BUFF_LEN)) < TEST_BUFF_LEN:
                    raise OSError("Could not read from device %s" % device)

def __pokeParent(parentPid):
    try:
        while True:
            os.kill(parentPid, 0)
            sleep(2)
    except Exception:
        os.unlink(ADDRESS)
        os.kill(os.getpid(), signal.SIGTERM)

def main():
    try:
        logging.config.fileConfig(LOG_CONF_PATH)
    except:
        logging.basicConfig(filename='/dev/stdout', filemode='w+', level=logging.DEBUG)
        log = logging.getLogger("SuperVdsm.Server")
        log.warn("Could not init proper logging", exc_info=True)

    log = logging.getLogger("SuperVdsm.Server")
    try:
        log.debug("Making sure I'm root")
        if os.geteuid() != 0:
            sys.exit(errno.EPERM)

        log.debug("Parsing cmd args")
        authkey, parentPid = sys.argv[1:]

        log.debug("Creating PID file")
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()) + "\n")

        log.debug("Cleaning old socket")
        if os.path.exists(ADDRESS):
            os.unlink(ADDRESS)

        log.debug("Setting up keep alive thread")
        monThread = threading.Thread(target=__pokeParent, args=[int(parentPid)])
        monThread.setDaemon(True)
        monThread.start()

        log.debug("Creating remote object manager")
        manager = _SuperVdsmManager(address=ADDRESS, authkey=authkey)
        manager.register('instance', callable=_SuperVdsm)

        server = manager.get_server()
        servThread = threading.Thread(target=server.serve_forever)
        servThread.setDaemon(True)
        servThread.start()

        chown(ADDRESS, METADATA_USER, METADATA_GROUP)

        log.debug("Started serving super vdsm object")
        servThread.join()
    except Exception, ex:
        log.error("Could not start Super Vdsm", exc_info=True)
        sys.exit(1)
    finally:
        try:
            os.unlink(ADDRESS)
        except OSError:
            pass

if __name__ == '__main__':
    main()
