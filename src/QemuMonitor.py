"""
Contains the Qemu Monitor and auxilliary classes.
Used to communicate with the Qemu process.
"""
import threading
import Queue
import time
import socket
import logging


class TimeoutError(RuntimeError):
    pass

class StoppedError(RuntimeError):
    pass

class MonitorQuery:
    """
    A means for sending a message to qemu monitor and waiting for reply
    """
    def __init__(self, input, notify=False, prompt=None):
        """
        Initialize a MonitorQuery instance.

        :param input: The string that would be sent to Qemu.
        :param notify: Should the query wait for monitor response.
        :param prompt: Qemu prompt that is expected after *input* is processed.
                       If :keyword:`None`, '(qemu) ' is expected
        """
        if '\n' in input or '\r' in input:
            raise ValueError, 'newline not allowed in query input'
        self._input = input
        self._output = None
        self._exception = None
        if notify:
            self._event = threading.Event()
        else:
            self._event = None
        self._prompt = prompt
        self.cancelled = False

    def wait(self, timeout):
        self._event.wait(timeout)
        if not self._event.isSet():
            # NOTE not-so-dangerous race: monq may have already been sent to
            # the monitor.
            self.cancelled = True
            raise TimeoutError
        if self._exception:
            raise self._exception
        return self._output


class BasicQemuMonitor:
    """
    Used to monitor and communicate with a running VM.

    Uses the Unix socket opened by Qemu to communicate with the VM.
    """
    def __init__(self, socketName, timeout=10, stopConnecting=None):
        """
        Initialize the instance.

        :param socketName: path to the Unix socket that was opened by Qemu.
        :type socketName: str
        :param timeout: Number of tries to connect to the socket before failing.
        :param stopConnecting: A function to test whether connection attempt
                               should continue. The function should take no
                               parameters and return True or equivilant if the
                               operation should stop.
        :type stopConnection: callable
        """
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._queue = Queue.Queue(-1)
        attempts = int(timeout)
        while attempts:
            if stopConnecting and stopConnecting():
                raise StoppedError
            try:
                self._sock.connect(socketName)
                break
            except:
                time.sleep(1)
            attempts -= 1
        if attempts == 0:
            raise TimeoutError 
        self._stopped = False
        self._enqueueLock = threading.Lock()
        self._worker = threading.Thread(name=threading._newname('QMon-%d'), 
                            target=self._work) # not nice, using private _newname
        self._worker.start()

    READSIZE = 2**16
    def _readTillNextPrompt(self, accum='', prompt=None):
        e = None
        done = False
        prompt_index = -1
        if prompt is None:
            prompt = '\n(qemu) '
        while not done:
            try:
                s = self._sock.recv(self.READSIZE)
            except Exception, e:
                break
            accum += s
            prompt_index = accum.find(prompt)
            if s == '' or prompt_index != -1:
                done = True
        #logging.debug('_readTillNextPrompt\n' + accum + 'STOP')
        if prompt_index != -1:
            return accum[:prompt_index], accum[prompt_index + len(prompt):], e
        return accum, '', e

    def sendCommand(self, s, timeout=None, prompt=None):
        """
        Send a command Qemu monitor and wait for a reply.

        :param s: The string to send to Qemu.
        :param timeout: The maximum time to wait for the command to complete.
        :param prompt: Qemu prompt that is expected after *input* is processed.
                       If :keyword:`None`, '(qemu) ' is expected
        """
        monq = MonitorQuery(s, notify=True, prompt=prompt)
        self._enqueueLock.acquire()
        try:
            self._queue.put(monq)
        finally:
            self._enqueueLock.release()
        return monq.wait(timeout)

    def sendCommand2(self, s, prompt, s2, timeout=None):
        """
        Send two command and contcatenate their outputs.

        Currently used only when changing vnc password.
        
        :param s: The first monitor command.
        :param prompt: The prompt expected after the first command.
        :param s2: The second monitor command.
        :param timeout: The timeout for the operation.
                        If :keyword:`None` the operation will never timeout.
        """
        monq = MonitorQuery(s, notify=True, prompt=prompt)
        monq2 = MonitorQuery(s2, notify=True)
        self._enqueueLock.acquire()
        try:
            self._queue.put(monq)
            self._queue.put(monq2)
        finally:
            self._enqueueLock.release()
        return monq.wait(timeout) + monq2.wait(timeout)

    def postCommand(self, s):
        """
        Send a command and return immidiatly, without waiting for a response.

        :param s: The monitor command.
        """
        monq = MonitorQuery(s)
        self._enqueueLock.acquire()
        try:
            self._queue.put(monq)
        finally:
            self._enqueueLock.release()

    def _work(self):
        # main loop of worker thread
        try:
            e = None
            # read welcome message
            dummy, leftover, errin = self._readTillNextPrompt('')
            self._filterOutput(dummy)
            if errin:
                self._filterOutput(leftover)
                raise errin
            while not self._stopped:
                # poll for work. if stopped, raise StoppedError
                monq = None
                while not monq and not self._stopped:
                    try:
                        monq = self._queue.get(timeout=2)
                    except Queue.Empty:
                        pass
                if self._stopped:
                    e = StoppedError()
                    break
                if monq.cancelled: continue
                s = None
                try:
                    try:
                        self._sock.sendall(monq._input + '\n')
                        #logging.debug('### ' + monq._input + '\n')
                        s, leftover, errin = self._readTillNextPrompt(leftover,
                                                        prompt=monq._prompt)
                        s = self._filterOutput(s)
                        if errin:
                            self._filterOutput(leftover)
                            raise errin
                    except Exception, e:
                        monq._exception = e
                        raise
                finally:
                    if monq._event:
                        monq._output = s
                        monq._event.set()
                    if hasattr(self._queue, 'task_done'):
                        # older Queue doesn't keep track of unfinished_tasks
                        self._queue.task_done()
        finally:
            self._stopped = True
            # notify all waiting producers about the error
            while not self._queue.empty():
                monq = self._queue.get()
                monq._exception = e
                if hasattr(self._queue, 'task_done'):
                    self._queue.task_done()
                if monq._event:
                    monq._event.set()

    def stop(self):
        self._stopped = True

    def _filterOutput(self, s):
        # filter-out nonsense and run callbacks
        ret = ''
        for line in s.split('\n'):
            if line[-4:] == '\x1b[K\r':
                pass
            elif line == '(qemu) ':
                pass
            elif self._runCallbacks(line):
                pass
            else:
                ret += line[0:-1] + '\n'
        return ret

    def _runCallbacks(self, line):
        return False

class QemuMonitor(BasicQemuMonitor):
    """Adds callback support to BasicQemuMonitor"""
    noneFunc = lambda *x: None
    def __init__(self, socketName, timeout=10, log=logging.root, 
            onStopMonitor=noneFunc,
            onReboot=noneFunc, onShutdown=noneFunc, onConnect=noneFunc,
            onDisconnect=noneFunc, onMigrationFinish=noneFunc,
            onRtcUpdate=noneFunc,
            onVncDisconnect=noneFunc, onAbnormalVmStop=noneFunc,
            onHighWrite=noneFunc, stopConnecting=None):
        BasicQemuMonitor.__init__(self, socketName, timeout, stopConnecting)
        self._log = log
        self._onStopMonitorCallback = onStopMonitor
        self._onRebootCallback = onReboot
        self._onShutdownCallback = onShutdown
        self._onConnectedCallback = onConnect
        self._onDisconnectCallback = onDisconnect
        self._onMigrationFinishCallback = onMigrationFinish
        self._onRtcUpdateCallback = onRtcUpdate
        self._onVncDisconnectCallback = onVncDisconnect
        self._onAbnormalVmStop = onAbnormalVmStop
        self._onHighWrite = onHighWrite

    def _work(self):
        try:
            BasicQemuMonitor._work(self)
        except:
            self._log.exception('QemuMonitor')
        try:
            self._filterOutput(self._sock.recv(self.READSIZE,
                                               socket.MSG_DONTWAIT))
        except:
            self._log.exception('QemuMonitor')
        self._onStopMonitorCallback()

    def _runCallbacks(self, line):
        """
        Call a callback if the line requires so.

        :Returns: :keyowrd:`True` if a callback was called.
        """
        VNC_DISCONNECT = '# VNC: Closing down connection '
        RTC_UPDATE = '# RTC: new time is UTC'
        SHUTDOWN_REQ = '# GUEST: Got shutdown request'
        POWERDOWN_REQ = '# GUEST: Got powerdown request'
        REBOOT_REQ = '# GUEST: Got reboot request'
        ABN_VM_STOP = '# VM is stopped due to disk write error: '
        WATERMARK_REACHED = '# high watermark reached for '
        MIGRATION_FINISH = '# migration: migration process finished'
        calledback = True
        if line.startswith(REBOOT_REQ):
            self._onRebootCallback(False)
        elif line.startswith(SHUTDOWN_REQ) or line.startswith(POWERDOWN_REQ):
            self._onShutdownCallback()
        elif line.startswith('# spice: new user connection'):
            self._onConnectedCallback()
        elif line.startswith('# spice: user disconnected'):
            self._onDisconnectCallback()
        elif line.startswith(RTC_UPDATE):
            self._onRtcUpdateCallback(line[len(RTC_UPDATE):-1])
        elif line.startswith(VNC_DISCONNECT):
            self._onVncDisconnectCallback(line[len(VNC_DISCONNECT)+1:-1])
        elif line.startswith(ABN_VM_STOP):
            block_dev, err = line[len(ABN_VM_STOP):-1].split(': ', 1)
            self._onAbnormalVmStop(block_dev, err)
        elif line.startswith(WATERMARK_REACHED):
            block_dev, alloc, mark = line[len(WATERMARK_REACHED):-1].split()
            offset = int(alloc[len('alloc='):])
            self._onHighWrite(block_dev, offset)
        elif line.startswith(MIGRATION_FINISH):
            self._onMigrationFinishCallback()
        elif line.startswith('# '):
            pass
        else:
            calledback = False
        if calledback:
            self._log.debug(line)
        return calledback

if __name__ == '__main__':
    m = BasicQemuMonitor('/tmp/hello')
    s = m.sendCommand('info blockstats')
    print 'output was\n', s
    m.stop()
