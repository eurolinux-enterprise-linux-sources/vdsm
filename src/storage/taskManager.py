#
# Copyright 2009 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import os
import logging

from config import config
import storage_exception as se
from task import Task, Job, TaskCleanType
from threadPool import ThreadPool


class TaskManager:
    log = logging.getLogger('TaskManager')

    def __init__(self, tpSize=config.getfloat('irs', 'thread_pool_size'), waitTimeout=3, maxTasks=config.getfloat('irs', 'max_tasks')):
        self.storage_repository = config.get('irs', 'repository')
        self.tp = ThreadPool(tpSize, waitTimeout, maxTasks)
        self._tasks = {}
        self._unqueuedTasks = []


    def queue(self, task):
        return self._queueTask(task, task.commit)

    def queueRecovery(self, task):
        return self._queueTask(task, task.recover)

    def _queueTask(self, task, method):
        try:
            self.log.debug("queueing task: %s", task.id)
            self._tasks[task.id] = task
            if not self.tp.queueTask(task.id, method):
                self.log.error("unable to queue task: %s", task.dumpTask())
                del self._tasks[task.id]
                raise se.AddTaskError()
            self.log.debug("task queued: %s", task.id)
        except Exception, ex:
            self.log.error("Could not queue task, encountered: %s", str(ex))
            raise
        return task.id

    def scheduleJob(self, type, store, task, jobName, func, *args):
        task.setTag(type)
        task.setPersistence(store, cleanPolicy=TaskCleanType.manual)
        task.setManager(self)
        task.setRecoveryPolicy("auto")
        task.addJob(Job(jobName, func, *args))
        self.log.debug("scheduled job %s for task %s ", jobName, task.id)


    def __getTask(self, taskID):
        Task.validateID(taskID)
        t = self._tasks.get(taskID, None)
        if t is None:
            raise se.UnknownTask(taskID)
        return t


    def prepareForShutdown(self):
        """ Prepare to shutdown. Stop all threads and asyncronous tasks
        """
        self.log.debug("Request to stop all tasks")

        # Clear the task queue and terminate all pooled threads
        for t in self._tasks:
            if hasattr(t, 'stop'): t.stop()
            self.log.info(str(t))

        self.tp.joinAll(waitForTasks=False)


    def getTaskStatus(self, taskID):
        """ Internal return Task status for a given task.
        """
        self.log.debug("Entry. taskID: %s", taskID)
        t = self.__getTask(taskID)
        status = t.deprecated_getStatus()
        self.log.debug("Return. Response: %s", status)
        return status


    def getAllTasksStatuses(self, tag=None):
        """ Return Task status for all tasks by type.
        """
        self.log.debug("Entry.")
        subRes = {}
        for key in self._tasks:
            if not tag or tag in self._tasks[key].getTags():
                subRes[key] = self.getTaskStatus(key)
        self.log.debug("Return: %s", subRes)
        return subRes


    def getAllTasks(self, tag=None):
        """
        Return Tasks for all public tasks.
        """
        self.log.debug("Entry.")
        subRes = {}
        for key in self._tasks:
            if not tag or tag in self._tasks[key].getTags():
                subRes[key] = self._tasks[key]
        self.log.debug("Return: %s", subRes)
        return subRes


    def unloadTasks(self, tag=None):
        """
        Remove Tasks from managed tasks list
        """
        self.log.debug("Entry.")
        for key in self._tasks.keys():
            if not tag or tag in self._tasks[key].getTags():
                del self._tasks[key]
        self.log.debug("Return")


    def stopTask(self, taskID, force=False):
        """ Stop a task according to given uuid.
        """
        self.log.debug("Entry. taskID: %s", taskID)
        t = self.__getTask(taskID)
        t.stop(force=force)
        self.log.debug("Return.")
        return True


    def revertTask(self, taskID):
        self.log.debug("Entry. taskID: %s", taskID)
        #TODO: Should we stop here implicitly ???
        t = self.__getTask(taskID)
        t.rollback()
        self.log.debug("Return.")


    def clearTask(self, taskID):
        """ Clear a task according to given uuid.
        """
        self.log.debug("Entry. taskID: %s", taskID)
        #TODO: Should we stop here implicitly ???
        t = self.__getTask(taskID)
        t.clean()
        del self._tasks[taskID]
        self.log.debug("Return.")


    def getTaskInfo(self, taskID):
        """ Return task's data according to given uuid.
        """
        self.log.debug("Entry. taskID: %s", taskID)
        t = self.__getTask(taskID)
        info = t.getInfo()
        self.log.debug("Return. Response: %s", info)
        return info


    def getAllTasksInfo(self, tag=None):
        """ Return Task info for all public tasks.
            i.e - not internal.
        """
        self.log.debug("Entry.")
        subRes = {}
        for key in self._tasks:
            if not tag or tag in self._tasks[key].getTags():
                subRes[key] = self.getTaskInfo(key)
        self.log.debug("Return. Response: %s", subRes)
        return subRes


    def _addTask(self, taskID, task):
        """
           Add task to the relevant hash.
        """
        self.log.info("Entry: taskID=%s, task=%s", taskID, task.dumpTask(", "))
        Task.validateID(taskID)
        self._tasks[taskID] = task
        self.log.debug("Return.")

    def loadDumpedTasks(self, store):
        if not os.path.exists(store):
            return
        dirList = os.listdir(store)
        dirList = [os.path.basename(os.path.splitext(t)[0]) for t in dirList]
        d = {}
        for x in dirList:
            d[x] = x
        dirList = d.values()
        for taskID in dirList:
            try:
                t = Task.loadTask(store, taskID)
                t.setPersistence(store, str(t.persistPolicy), str(t.cleanPolicy))
                self._tasks[taskID] = t
                self._unqueuedTasks.append(t)
            except Exception, e:
                self.log.error("taskManager: Skipping directory: %s", taskID, exc_info=True)
                continue

    def recoverDumpedTasks(self):
        for task in self._unqueuedTasks[:]:
            self.queueRecovery(task)
            self._unqueuedTasks.remove(task)

