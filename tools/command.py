#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# See LICENSE.txt included in this distribution for the specific
# language governing permissions and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at LICENSE.txt.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

#
# Copyright (c) 2017, 2018, Oracle and/or its affiliates. All rights reserved.
#

import os
import logging
import subprocess
import string
import threading
import time
import resource


class TimeoutException(Exception):
    """
    Exception returned when command exceeded its timeout.
    """
    pass


class Command:
    """
    wrapper for synchronous execution of commands via subprocess.Popen()
    and getting their output (stderr is redirected to stdout) and return value
    """

    # state definitions
    FINISHED = "finished"
    INTERRUPTED = "interrupted"
    ERRORED = "errored"
    TIMEDOUT = "timed out"

    def __init__(self, cmd, args_subst=None, args_append=None, logger=None,
                 excl_subst=False, work_dir=None, env_vars=None, timeout=None,
                 redirect_stderr=True, resource_limits=None):
        self.cmd = cmd
        self.state = "notrun"
        self.excl_subst = excl_subst
        self.work_dir = work_dir
        self.env_vars = env_vars
        self.timeout = timeout
        self.pid = None
        self.redirect_stderr = redirect_stderr
        self.limits = resource_limits

        self.logger = logger or logging.getLogger(__name__)
        logging.basicConfig()

        if args_subst or args_append:
            self.fill_arg(args_append, args_subst)

    def __str__(self):
        return " ".join(self.cmd)

    def execute(self):
        """
        Execute the command and capture its output and return code.
        """

        class TimeoutThread(threading.Thread):
            """
            Wait until the timeout specified in seconds expires and kill
            the process specified by the Popen object after that.
            If timeout expires, TimeoutException is stored in the object
            and can be retrieved by the caller.
            """

            def __init__(self, logger, timeout, condition, p):
                super(TimeoutThread, self).__init__()
                self.timeout = timeout
                self.popen = p
                self.condition = condition
                self.logger = logger
                self.start()
                self.exception = None

            def run(self):
                with self.condition:
                    if not self.condition.wait(self.timeout):
                        p = self.popen
                        self.logger.info("Terminating command {} with PID {} "
                                         "after timeout of {} seconds".
                                         format(p.args, p.pid, self.timeout))
                        p.terminate()
                        self.exception = TimeoutException("Command {} with pid"
                                                          " {} timed out".
                                                          format(p.args,
                                                                 p.pid))
                    else:
                        return None

            def get_exception(self):
                return self.exception

        class OutputThread(threading.Thread):
            """
            Capture data from subprocess.Popen(). This avoids hangs when
            stdout/stderr buffers fill up.
            """

            def __init__(self, event, logger):
                super(OutputThread, self).__init__()
                self.read_fd, self.write_fd = os.pipe()
                self.pipe_fobj = os.fdopen(self.read_fd, encoding='utf8')
                self.out = []
                self.event = event
                self.logger = logger
                self.start()

            def run(self):
                """
                It might happen that after the process is gone, the thread
                still has data to read from the pipe. Hence, event is used
                to synchronize with the caller.
                """
                while True:
                    line = self.pipe_fobj.readline()
                    if not line:
                        self.logger.debug("end of output")
                        self.pipe_fobj.close()
                        self.event.set()
                        return

                    self.out.append(line)

            def getoutput(self):
                return self.out

            def fileno(self):
                return self.write_fd

            def close(self):
                self.logger.debug("closed")
                os.close(self.write_fd)

        orig_work_dir = None
        if self.work_dir:
            try:
                orig_work_dir = os.getcwd()
            except OSError as e:
                self.state = Command.ERRORED
                self.logger.error("Cannot get working directory",
                                  exc_info=True)
                return

            try:
                os.chdir(self.work_dir)
            except OSError as e:
                self.state = Command.ERRORED
                self.logger.error("Cannot change working directory to {}".
                                  format(self.work_dir), exc_info=True)
                return

        timeout_thread = None
        output_event = threading.Event()
        output_thread = OutputThread(output_event, self.logger)

        # If stderr redirection is off, setup a thread that will capture
        # stderr data.
        if self.redirect_stderr:
            stderr_dest = subprocess.STDOUT
        else:
            stderr_event = threading.Event()
            stderr_thread = OutputThread(stderr_event, self.logger)
            stderr_dest = stderr_thread

        try:
            start_time = time.time()
            try:
                self.logger.debug("working directory = {}".format(os.getcwd()))
            except PermissionError:
                pass
            self.logger.debug("command = {}".format(self.cmd))
            my_args = {}
            my_args['stderr'] = stderr_dest
            my_args['stdout'] = output_thread
            if self.env_vars:
                my_env = os.environ.copy()
                my_env.update(self.env_vars)
                my_args['env'] = my_env
            if self.limits:
                my_args['preexec_fn'] = \
                    lambda: self.set_resource_limits(self.limits)

            # Actually run the command.
            p = subprocess.Popen(self.cmd, **my_args)

            self.pid = p.pid

            if self.timeout:
                time_condition = threading.Condition()
                self.logger.debug("Setting timeout to {}".format(self.timeout))
                timeout_thread = TimeoutThread(self.logger, self.timeout,
                                               time_condition, p)

            self.logger.debug("Waiting for process with PID {}".format(p.pid))
            p.wait()
            self.logger.debug("done waiting")

            if self.timeout:
                e = timeout_thread.get_exception()
                if e:
                    raise e

        except KeyboardInterrupt as e:
            self.logger.info("Got KeyboardException while processing ",
                             exc_info=True)
            self.state = Command.INTERRUPTED
        except OSError as e:
            self.logger.error("Got OS error", exc_info=True)
            self.state = Command.ERRORED
        except TimeoutException as e:
            self.logger.error("Timed out")
            self.state = Command.TIMEDOUT
        else:
            self.state = Command.FINISHED
            self.returncode = int(p.returncode)
            self.logger.debug("{} -> {}".format(self.cmd, self.getretcode()))
        finally:
            if self.timeout != 0 and timeout_thread:
                with time_condition:
                    time_condition.notifyAll()

            # The subprocess module does not close the write pipe descriptor
            # it fetched via OutputThread's fileno() so in order to gracefully
            # exit the read loop we have to close it here ourselves.
            output_thread.close()
            self.logger.debug("Waiting on output thread to finish reading")
            output_event.wait()
            self.out = output_thread.getoutput()

            if not self.redirect_stderr:
                stderr_thread.close()
                self.logger.debug("Waiting on stderr thread to finish reading")
                stderr_event.wait()
                self.err = stderr_thread.getoutput()

            elapsed_time = time.time() - start_time
            self.logger.debug("Command {} took {} seconds".
                              format(self.cmd, int(elapsed_time)))

        if orig_work_dir:
            try:
                os.chdir(orig_work_dir)
            except OSError as e:
                self.state = Command.ERRORED
                self.logger.error("Cannot change working directory back to {}".
                                  format(orig_work_dir))
                return

    def fill_arg(self, args_append=None, args_subst=None):
        """
        Replace argument names with actual values or append arguments
        to the command vector.

        The action depends whether exclusive substitution is on.
        If yes, arguments will be appended only if no substitution was
        performed.
        """

        newcmd = []
        subst_done = -1
        for i, cmdarg in enumerate(self.cmd):
            if args_subst:
                for pattern in args_subst.keys():
                    if pattern in cmdarg:
                        newarg = cmdarg.replace(pattern, args_subst[pattern])
                        self.logger.debug("replacing cmdarg with {}".
                                          format(newarg))
                        newcmd.append(newarg)
                        subst_done = i

                if subst_done != i:
                    newcmd.append(self.cmd[i])
            else:
                newcmd.append(self.cmd[i])

        if args_append and (not self.excl_subst or subst_done == -1):
            self.logger.debug("appending {}".format(args_append))
            newcmd.extend(args_append)

        self.cmd = newcmd

    def get_resource(self, name):
        if name == "RLIMIT_NOFILE":
            return resource.RLIMIT_NOFILE

        raise NotImplementedError("unknown resource")

    def set_resource_limit(self, name, value):
        self.logger.debug("Setting resource {} to {}"
                          .format(name, value))
        resource.setrlimit(self.get_resource(name), (value, value))

    def set_resource_limits(self, limits):
        self.logger.debug("Setting resource limits")
        for name in limits.keys():
            self.set_resource_limit(name, limits[name])

    def getretcode(self):
        if self.state is not Command.FINISHED:
            return None
        else:
            return self.returncode

    def getoutputstr(self):
        if self.state is Command.FINISHED:
            return "".join(self.out).strip()
        else:
            return None

    def getoutput(self):
        if self.state is Command.FINISHED:
            return self.out
        else:
            return None

    def geterroutput(self):
        return self.err

    def getstate(self):
        return self.state

    def getpid(self):
        return self.pid

    def log_error(self, msg):
        if self.state is Command.FINISHED:
            self.logger.error("{}: command {} in directory {} exited with {}".
                              format(msg, self.cmd, self.work_dir,
                                     self.getretcode()))
        else:
            self.logger.error("{}: command {} in directory {} ended with "
                              "invalid state".
                              format(msg, self.cmd, self.work_dir, self.state))