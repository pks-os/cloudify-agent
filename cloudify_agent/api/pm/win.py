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
import json

from cloudify.exceptions import CommandExecutionException

from cloudify_agent import VIRTUALENV
from cloudify_agent.api import defaults
from cloudify_agent.api import exceptions
from cloudify_agent.api import utils
from cloudify_agent.api.pm.base import Daemon

from cloudify import constants
from cloudify.utils import (ENV_CFY_EXEC_TEMPDIR,
                            ENV_AGENT_LOG_DIR,
                            ENV_AGENT_LOG_LEVEL,
                            ENV_AGENT_LOG_MAX_BYTES,
                            ENV_AGENT_LOG_MAX_HISTORY)


class WindowsServiceManagerDaemon(Daemon):

    """
    Implementation for the native windows service management.

    Following are all possible custom key-word arguments
    (in addition to the ones available in the base daemon)

    ``startup_policy``

        Specifies the start type for the service.
        possible values are:

            boot - A device driver that is loaded by the boot loader.
            system - A device driver that is started during kernel
                     initialization
            auto - A service that automatically starts each time the
                   computer is restarted and runs even if no one logs on to
                   the computer.
            demand - A service that must be manually started. This is the
                    default value if start= is not specified.
            disabled - A service that cannot be started. To start a disabled
                       service, change the start type to some other value.

    ``failure_reset_timeout``

        Specifies the length of the period (in seconds) with no failures
        after which the failure count should be reset to 0.

    ``failure_restart_delay``

        Specifies delay time (in milliseconds) for the restart action.
    """

    PROCESS_MANAGEMENT = 'win'

    RUNNING_STATES = ['SERVICE_RUNNING', 'SERVICE_STOP_PENDING']

    def __init__(self, logger=None, **params):
        super(WindowsServiceManagerDaemon, self).__init__(
            logger=logger, **params)

        self.config_path = os.path.join(
            self.workdir,
            '{0}.conf.ps1'.format(self.name))
        self.startup_policy = params.get('startup_policy', 'auto')
        self.failure_reset_timeout = params.get('failure_reset_timeout', 60)
        self.failure_restart_delay = params.get('failure_restart_delay', 5000)
        self.service_user = params.get('service_user', '')
        self.service_password = params.get('service_password', '')

    def create_script(self):
        pass

    def create_config(self):
        # Creating the environment variables' file
        envvars_file = {
            constants.REST_HOST_KEY: self.rest_host,
            constants.REST_PORT_KEY: self.rest_port,
            constants.LOCAL_REST_CERT_FILE_KEY: self.local_rest_cert_file,
            constants.MANAGER_FILE_SERVER_URL_KEY:
                "https://{}:{}/resources".format(self.rest_host,
                                                 self.rest_port),
            ENV_AGENT_LOG_DIR: self.log_dir,
            # TODO: This key should be moved elsewhere
            utils._Internal.CLOUDIFY_DAEMON_USER_KEY: self.user,
            ENV_AGENT_LOG_LEVEL: self.log_level.upper(),
            constants.AGENT_WORK_DIR_KEY: self.workdir,
            ENV_AGENT_LOG_MAX_BYTES: self.log_max_bytes,
            ENV_AGENT_LOG_MAX_HISTORY: self.log_max_history,
            utils._Internal.CLOUDIFY_DAEMON_STORAGE_DIRECTORY_KEY:
                utils.internal.get_storage_directory(self.user),
            constants.CLUSTER_SETTINGS_PATH_KEY: self.cluster_settings_path
        }

        if self.executable_temp_path:
            envvars_file[ENV_CFY_EXEC_TEMPDIR] = self.executable_temp_path

        if self.extra_env_path and os.path.exists(self.extra_env_path):
            with open(self.extra_env_path) as f:
                content = f.read()
            for line in content.splitlines():
                if line.startswith('set'):
                    parts = line.split(' ')[1].split('=')
                    key = parts[0]
                    value = parts[1]
                    envvars_file[key] = value

        envvars_file_name = os.path.join(self.workdir, 'environment.json')
        self._logger.info("Rendering environment variables JSON to %s",
                          envvars_file_name,)
        with open(envvars_file_name, 'w') as f:
            json.dump(envvars_file, f, indent=4)

        # creating the installation script
        self._logger.info('Rendering configuration script "{0}" from template'
                          .format(self.config_path))
        utils.render_template_to_file(
            template_path='pm/nssm/nssm.conf.template',
            file_path=self.config_path,
            vars_file=envvars_file_name,
            queue=self.queue,
            service_user=self.service_user,
            service_password=self.service_password,
            max_workers=self.max_workers,
            virtualenv_path=VIRTUALENV,
            name=self.name,
            startup_policy=self.startup_policy,
            failure_reset_timeout=self.failure_reset_timeout,
            failure_restart_delay=self.failure_restart_delay
        )

        self._logger.info('Rendered configuration script: %s',
                          self.config_path)

        # run the configuration script
        self._logger.info('Running configuration script')
        try:
            self._runner.run(self.config_path)
        except Exception:
            # Log the exception here, then re-raise it. This is done in order
            # to ensure that the full exception message is shown.
            self._logger.exception("Failure encountered while running "
                                   "configuration script")
            raise
        self._logger.info('Successfully executed configuration script')

    def before_self_stop(self):
        if self.startup_policy in ['boot', 'system', 'auto']:
            self._logger.debug('Disabling service: %s', self.name)
            self._runner.run('sc config {0} start= disabled'.format(self.name))

    def delete(self, force=defaults.DAEMON_FORCE_DELETE):
        if self._is_daemon_running():
            if not force:
                raise exceptions.DaemonStillRunningException(self.name)
            self.stop()

        self._logger.info('Removing %s service', self.name)
        self._runner.run('sc delete {0}'.format(
            self.name))

        self._logger.debug('Deleting %s', self.config_path)
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    def start_command(self):
        if not os.path.isfile(self.config_path):
            raise exceptions.DaemonNotConfiguredError(self.name)
        return 'sc start {0}'.format(self.name)

    def stop_command(self):
        return 'sc stop {0}'.format(self.name)

    def start(self, *args, **kwargs):
        try:
            super(WindowsServiceManagerDaemon, self).start(*args, **kwargs)
        except CommandExecutionException as e:
            if e.code == 1056:
                self._logger.info('Service already started')
            else:
                raise

    def status(self):
        try:
            command = 'sc status {0}'.format(self.name)
            response = self._runner.run(command)
            # apparently nssm output is encoded in utf16.
            # encode to ascii to be able to parse this
            state = response.std_out.decode('utf16').encode(
                'utf-8').rstrip()
            self._logger.info(state)
            if state in self.RUNNING_STATES:
                return True
            else:
                return False
        except CommandExecutionException as e:
            self._logger.debug(str(e))
            return False