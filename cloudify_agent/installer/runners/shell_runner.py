#########
# Copyright (c) 2015 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import os
import shlex
import logging
import subprocess

from cloudify.utils import CommandExecutionResponse
from cloudify.utils import setup_logger
from cloudify.exceptions import CommandExecutionException
from cloudify.exceptions import CommandExecutionError

from cloudify_agent.installer import exceptions
from cloudify_agent.api import utils as api_utils


class ShellRunner(object):

    def __init__(self,
                 is_shell,
                 logger=None,
                 conn_cmd=None,
                 sh_cmd="/bin/sh",
                 validate_connection=True):

        assert is_shell is True

        # logger
        self.logger = logger or setup_logger('shell_runner')
        self._conn_cmd = conn_cmd
        self._sh_cmd = sh_cmd

        if validate_connection:
            self.validate_connection()

        self.check_and_install_program('sudo')
        self.check_and_install_program('wget')
        self.check_and_install_program('rsync')
        self.check_and_install_program('python')

    def validate_connection(self):
        self.logger.debug('Validating SSH connection')
        self.ping()
        self.logger.debug('SSH connection is ready')

    def run(self, command, check_return_code=True, **attributes):
        try:
            child = subprocess.Popen(shlex.split(self._conn_cmd + ' ' + self._sh_cmd),
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     close_fds=True)

            # TODO: Should control env
            stdout, stderr = child.communicate(input=command)

            if check_return_code:
                return_code = child.returncode
            else:
                return_code = 0

            if return_code != 0:
                raise CommandExecutionException(
                    command=command,
                    error=stderr,
                    output=None,
                    code=return_code
                )

            return CommandExecutionResponse(
                command=command,
                std_out=stdout,
                std_err=None,
                return_code=return_code
            )
        except CommandExecutionException:
            raise
        except BaseException as e:
            raise CommandExecutionError(
                command=command,
                error=str(e)
            )

    def sudo(self, command, **attributes):

        """
        Execute a command under sudo.

        :param command: The command to execute.
        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: a response object containing information
                 about the execution
        :rtype: FabricCommandExecutionResponse
        """
        return self.run('sudo {0}'.format(command), **attributes)

    def run_script(self, script):
        """
        Execute a script.

        :param script: The path to the script to execute.
        :return: a response object containing information
                 about the execution
        :rtype: FabricCommandExecutionResponse
        :raise: FabricCommandExecutionException
        """

        remote_path = self.put_file(script)
        # try:
        self.sudo('chmod +x {0}'.format(remote_path))
        result = self.sudo(remote_path)
        # finally:
            # The script is pushed to a remote directory created with mkdtemp.
            # Hence, to cleanup the whole directory has to be removed.
            # self.delete(os.path.dirname(remote_path))
        return result

    def put_file(self, src, dst=None, sudo=False, **attributes):

        """
        Copies a file from the src path to the dst path.

        :param src: Path to a local file.
        :param dst: The remote path the file will copied to.
        :param sudo: indicates that this operation
                     will require sudo permissions
        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: the destination path
        """

        if dst:
            self.verify_dir_exists(os.path.dirname(dst))
        else:
            basename = os.path.basename(src)
            tempdir = self.mkdtemp()
            dst = os.path.join(tempdir, basename)

        command = "rsync -av --blocking-io --rsync-path= --rsh='{0}' " \
                  "{1} rsync:{2}".format(self._conn_cmd, src, dst)
        output = subprocess.check_output(shlex.split(command))
        # TODO: Is this return code valid?
        if not output:
            raise CommandExecutionException(
                command=command,
                error='Failed uploading {0} to {1}'
                .format(src, dst),
                code=-1,
                output=None
            )

        dir_name, file_name = os.path.split(os.path.abspath(dst))
        self.run('mv {0} {1}/{2}'.format(file_name, dir_name, file_name))

        return dst

    def ping(self, **attributes):

        """
        Tests that the connection is working.

        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: a response object containing information
                 about the execution
        :rtype: FabricCommandExecutionResponse
        """
        return self.run('echo', **attributes)

    def mktemp(self, create=True, directory=False, **attributes):

        """
        Creates a temporary path.

        :param create: actually create the file or just construct the path
        :param directory: path should be a directory or not.
        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: the temporary path
        """

        flags = []
        if not create:
            flags.append('-u')
        if directory:
            flags.append('-d')
        return self.run('mktemp {0}'
                        .format(' '.join(flags)),
                        **attributes).std_out.rstrip()

    def mkdtemp(self, create=True, **attributes):

        """
        Creates a temporary directory path.

        :param create: actually create the file or just construct the path
        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: the temporary path
        """

        return self.mktemp(create=create, directory=True, **attributes)

    def home_dir(self, username):

        """
        Retrieve the path of the user's home directory.

        :param username: the username

        :return: path to the home directory
        """
        return self.python(
            imports_line='import pwd',
            command='pwd.getpwnam(\'{0}\').pw_dir'
            .format(username))

    def verify_dir_exists(self, dirname):
        self.run('mkdir -p {0}'.format(dirname))

    def python(self, imports_line, command, **attributes):

        """
        Run a python command and return the output.

        To overcome the situation where additional info is printed
        to stdout when a command execution occurs, a string is
        appended to the output. This will then search for the string
        and the following closing brackets to retrieve the original output.

        :param imports_line: The imports needed for the command.
        :param command: The python command to run.
        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: the string representation of the return value of
                 the python command
        """

        start = '###CLOUDIFYCOMMANDOPEN'
        end = 'CLOUDIFYCOMMANDCLOSE###'

        stdout = self.run('python -c "import sys; {0}; '
                          'sys.stdout.write(\'{1}{2}{3}\\n\''
                          '.format({4}))"'
                          .format(imports_line,
                                  start,
                                  '{0}',
                                  end,
                                  command), **attributes).std_out
        result = stdout[stdout.find(start) - 1 + len(end):
                        stdout.find(end)]
        return result

    def machine_distribution(self, **attributes):

        """
        Retrieves the distribution information of the host.

        :param attributes: custom attributes passed directly to
                           fabric's run command

        :return: dictionary of the platform distribution as returned from
                 'platform.dist()'

        """

        response = self.python(
            imports_line='import platform, json',
            command='json.dumps(platform.dist())', **attributes
        )
        return api_utils.json_loads(response)

    def check_and_install_program(self, program):
        if program not in str(self.run('which ' + program, check_return_code=False).std_out):
            if 'apt-get' in str(self.run('which apt-get', check_return_code=False).std_out):
                stdout = self.run('apt-get update')
                stdout = self.run('apt-get install -y ' + program)
            elif 'yum' in str(self.run('which yum', check_return_code=False).std_out):
                stdout = self.run('yum update')
                stdout = self.run('yum install -y ' + program)


    def delete(self, path):
        self.run('rm -rf {0}'.format(path))

    @staticmethod
    def close():
        pass