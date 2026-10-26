import os
import shutil
import json
import logging
import tarfile

from io import BytesIO

import docker, docker.errors

from app.utility.base_service import BaseService


class BuildService(BaseService):

    def __init__(self, services):
        self.log = self.add_service('build_svc', self)
        self.file_svc = services.get('file_svc')
        self.data_svc = services.get('data_svc')
        self.payloads_directory = os.path.join('plugins', 'builder', 'payloads')
        self.build_directory = os.path.join('plugins', 'builder', 'build')
        self.build_envs = dict()
        self.build_file = 'code'
        self.error_file = 'error.log'

        logging.getLogger('docker').setLevel(logging.WARNING)
        self.urllib3_log_level = logging.WARNING
        self.urllib3_log_level_orig = logging.getLogger('urllib3').getEffectiveLevel()
        self.docker_client = self._create_docker_client()

    async def initialize_code_hook_functions(self):
        """Start dynamic code compilation for abilities"""
        for ability in await self.data_svc.locate('abilities'):
            for executor in ability.executors:
                if executor.code and executor.language:
                    executor.HOOKS[executor.language] = self.generate_ability_execution_method

    async def generate_ability_execution_method(self, ability, executor):
        """Set command and payloads for an Ability

        :param ability: Ability to set command and payloads for
        :type ability: Ability
        :param executor: Executor to set command and payloads for
        :type executor: Executor
        """
        if not executor.additional_info.get('built'):
            await self._build_executor(ability, executor)

            for module in self._get_go_modules(executor):
                executor.payloads.remove(module)

            if not executor.command:
                executor.command = self._build_command_block_syntax(
                    payload=executor.build_target, platform=executor.platform
                )
            if executor.build_target not in executor.payloads:
                executor.payloads.append(executor.build_target)

    async def stage_enabled_dockers(self):
        """Start downloading docker images"""
        self._set_urllib3_logging()
        await self._download_docker_images()
        self._unset_urllib3_logging()

    """ PRIVATE """

    def _set_urllib3_logging(self):
        """Silence urllib3 requests"""
        logging.getLogger('urllib3').setLevel(self.urllib3_log_level)

    def _unset_urllib3_logging(self):
        """Unsilence urllib3 requests"""
        logging.getLogger('urllib3').setLevel(self.urllib3_log_level_orig)

    def _create_docker_client(self):
        """Create docker client from environment"""
        self._set_urllib3_logging()
        client = docker.from_env()
        self._unset_urllib3_logging()
        return client

    async def _build_executor(self, ability, executor):
        """Dynamically compile a payload for an ability/executor

        :param ability: Ability to dynamically compile code for
        :type ability: Ability
        :param executor: Executor to dynamically compile code for
        :type executor: Executor
        :return: Command to run, payload name
        :rtype: string, string
        """
        self.log.debug('Building %s/%s (%s) for ability %s', executor.platform, executor.name, executor.language,
                       ability.ability_id)
        env = self.get_config(prop='enabled', name='build').get(executor.language)
        if not env:
            if not executor.additional_info.get('build_error'):
                executor.additional_info['build_error'] = True
                self.log.debug('Error building ability %s: environment "%s" not configured', ability.ability_id,
                               executor.language)
            return

        self._set_urllib3_logging()
        await self._build_executor_with_docker(env, ability, executor)
        self._unset_urllib3_logging()

    async def _build_executor_with_docker(self, env, ability, executor):
        """Dynamically compile executor in Docker

        :param env: Build environment settings
        :type env: dict
        :param ability: Ability to dynamically compile code for
        :type ability: Ability
        :param executor: Executor to dynamically compile code for
        :type executor: Executor
        :return: Command to run, payload name
        :rtype: string, string
        """
        self._purge_build_directory(executor.language)
        await self._stage_docker_directory(env, executor)
        self._run_target_docker(env, ability, executor, self._replace_build_vars(env, executor))
        self._check_errors(executor.language)
        self._stage_payload(executor.language, executor.build_target)
        self._purge_build_directory(executor.language)
        executor.additional_info['built'] = True
        executor.additional_info['build_error'] = False

    @staticmethod
    def _build_command_block_syntax(payload, platform='windows'):
        """Creates the command to run for a given payload

        :param payload: Payload name
        :type payload: string
        :return: Agent command to run
        :rtype: string
        """
        if platform in ["linux", "darwin"]:
            return "./{}".format(payload)

        # Default backslash behavior for Windows environments
        return '.\\{}'.format(payload)

    async def _download_docker_images(self):
        """Download required docker images"""
        for language, language_data in self.get_config(prop='enabled', name='build').items():
            await self._stage_build_dir(language=language)
            data = self.docker_client.images.list(name=language_data['docker'])
            if not data:
                self.log.info('Downloading docker image for builder plugin: %s', language_data['docker'])
                data = self.docker_client.images.pull(language_data['docker'])
            self.build_envs[language] = data[0] if isinstance(data, list) else data

    async def _stage_build_dir(self, language):
        """Create a build directory for a particular language

        :param language: Language to create directory for
        :type language: string
        """
        build_dir = os.path.join(self.build_directory, language)
        if not os.path.exists(build_dir):
            os.mkdir(build_dir)
            self.log.debug('Build directory created for %s', language)

    def _stage_payload(self, language, payload):
        """Move Docker-built payload to Caldera payload directory

        Adds special rules:
        - Appends .exe to .donut files.

        :param language: Language the payload was built for
        :param language: string
        :param payload: Payload name
        :param payload: string
        """
        src = os.path.join(self.build_directory, language, payload)
        dst = os.path.join(self.payloads_directory, payload)
        if dst.endswith('.donut'):
            dst = '{}.exe'.format(dst)
        if os.path.isfile(dst):
            os.remove(dst)
        if os.path.exists(src):
            shutil.move(src=src, dst=dst)

    async def _stage_docker_directory(self, env, executor):
        """Create code file and references in Docker build directory

        :param env: Build environment settings
        :type env: dict
        :param executor: Executor to be built
        :type executor: Executor
        """
        build_file = '{}.{}'.format(self.build_file, env['extension']) if env.get('extension') else self.build_file
        with open(os.path.join(self.build_directory, executor.language, build_file), 'w') as f:
            f.write(executor.code)

        for payload in self._get_build_payloads(executor):
            payload_name = payload
            if self.is_uuid4(payload):
                payload_name, _ = self.file_svc.get_payload_name_from_uuid(payload)
            _, src = await self.file_svc.find_file_path(payload_name)
            dst = os.path.join(self.build_directory, executor.language)
            shutil.copy(src=src, dst=dst)

    def _purge_build_directory(self, language):
        """Remove all files from language build directory

        :param language: Language to clear build directory for
        :type language: string
        """
        for root, dirs, files in os.walk(os.path.join(self.build_directory, language)):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                shutil.rmtree(os.path.join(root, d))

    @staticmethod
    def _is_running_in_container():
        """Detect if Caldera is running in a container"""
        if os.path.exists("/.dockerenv"):
            return True
        return False

    def _run_target_docker(self, env, ability, executor, build_command):
        """Run docker container to build ability files

        :param env: Build environment settings
        :type env: dict
        :param ability: Ability to build code for
        :type ability: Ability
        :param executor: Executor to build code for
        :type executor: Executor
        :param build_command: Command to run on docker container
        :type build_command: string
        """

        in_container = self._is_running_in_container()
        local_build_dir = os.path.abspath(
            os.path.join(self.build_directory, executor.language)
        )

        # Create named volume for builder
        volume_name = f"builder_vol_{executor.language}_{ability.ability_id}"
        volume = None

        if in_container:
            try:
                volume = self.docker_client.volumes.get(volume_name)
            except docker.errors.NotFound:
                volume = self.docker_client.volumes.create(name=volume_name)
            volume_mount = volume_name
            self.log.debug("Initialized Docker volume: %s", volume_name)
        else:
            volume_mount = local_build_dir

        container = None
        try:
            container = self.docker_client.containers.create(
                image=self.build_envs[executor.language].short_id,
                command=build_command,
                working_dir=env["workdir"],
                user=os.getuid() if hasattr(os, "getuid") else None,
                volumes={volume_mount: {"bind": env["workdir"], "mode": "rw"}},
            )

            # If in a container, sync source data into the volume
            if in_container and os.path.exists(local_build_dir):
                tar_stream = BytesIO()
                with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                    tar.add(local_build_dir, arcname=".")
                tar_stream.seek(0)
                container.put_archive(path=env["workdir"], data=tar_stream)

            # Start the container to build the payload
            container.start()
            exit_information = container.wait()
            exit_code = exit_information.get("StatusCode", 0)

            if in_container:
                try:
                    bits, stat = container.get_archive(env["workdir"])
                    tar_stream = BytesIO(b"".join(bits))
                    tar_stream.seek(0)

                    with tarfile.open(fileobj=tar_stream, mode="r") as tar:
                        root_dir = stat.get("name", "src")
                        for member in tar.getmembers():
                            clean_name = member.name
                            if member.name.startswith(root_dir + "/"):
                                clean_name = member.name[len(root_dir) + 1 :]
                            elif member.name == root_dir:
                                continue

                            if clean_name == "error.log":
                                error_file = tar.extractfile(member)
                                if error_file:
                                    errors = error_file.read().decode("utf-8").strip()
                                    if errors:
                                        self.log.info(
                                            "--- [BUILD LOGS (error.log)] ---\n%s", errors
                                        )

                            member.name = clean_name
                            tar.extract(member, path=local_build_dir)
                except Exception as e:
                    self.log.error("Failed to unpack containerized assets: %s", e)

            else:
                error_log_host_path = os.path.join(local_build_dir, "error.log")
                if os.path.exists(error_log_host_path):
                    try:
                        with open(error_log_host_path, "r") as ef:
                            errors = ef.read().strip()
                            if errors:
                                self.log.info(
                                    "--- [BUILD LOGS (error.log)] ---\n%s", errors
                                )
                    except Exception as e:
                        self.log.info("Failed to read error.log: %s", e)

            expected_payload = os.path.join(local_build_dir, executor.build_target)
            if not os.path.exists(expected_payload):
                self.log.error(
                    "FAILURE: Compilation completed but binary missing from host path: %s",
                    expected_payload,
                )

            self.log.debug(
                "Container for %s (%s/%s) ran for ability ID %s with exit code: %s",
                executor.language,
                executor.platform,
                executor.name,
                ability.ability_id,
                exit_code,
            )

        finally:
            if container:
                try:
                    container.remove(force=True)
                except docker.errors.APIError:
                    pass
            if volume:
                try:
                    volume.remove(force=True)
                except docker.errors.APIError:
                    pass


    def _check_errors(self, language):
        """Check for errors which occurred during the build

        :param language: Language to check errors for
        :type language: string
        """
        error_log = os.path.join(self.build_directory, language, self.error_file)
        if os.path.isfile(error_log):
            if language == 'csharp':
                with open(error_log) as f:
                    log_data = json.load(f)

                errors = log_data['runs'][0]['results']
                for error in errors:
                    location_data = ''
                    locations = error.get('locations', [])
                    if locations:
                        region = locations[0]['resultFile']['region']
                        location_data = '{}({},{},{},{}): '.format(locations[0]['resultFile']['uri'],
                                                                   region.get('startLine'), region.get('startColumn'),
                                                                   region.get('endLine'), region.get('endColumn'))
                    self.log.debug('%s%s %s: %s', location_data, error.get('level').capitalize(), error.get('ruleId'),
                                   error.get('message'))
            elif language.startswith('c_'):
                with open(error_log) as f:
                    for line in f:
                        self.log.debug(line.rstrip())
            elif language.startswith('cpp_'):
                with open(error_log) as f:
                    for line in f:
                        self.log.debug(line.rstrip())
            elif language.startswith('go_'):
                with open(error_log) as f:
                    for line in f:
                        self.log.debug(line.rstrip())

    def _replace_build_vars(self, env, executor):
        """Replace template arguments in build command

        :param env: Build environment settings
        :type env: dict
        :param executor: Executor to create command for
        :type executor: Executor
        :return: Created command string
        :rtype: string
        """
        build_file = '{}.{}'.format(self.build_file, env['extension']) if env.get('extension') else self.build_file
        build_command = env['build_command'].replace('#{code}', build_file)
        build_command = build_command.replace('#{build_target}', executor.build_target)
        build_command = self._replace_build_payload_vars(executor, build_command)
        return build_command

    def _replace_build_payload_vars(self, executor, build_command):
        """Replace reference arguments in build command

        :param executor: Executor to replace references for
        :type executor: Executor
        :param build_command: Build command
        :type build_command: str
        :return: Command string with replaced references
        :rtype: string
        """
        if executor.language == 'csharp':
            references = self._get_csharp_references(executor)
            reference_cmd = ' -r:{}'.format(','.join(references)) if references else ''
            build_command = build_command.replace(' #{references}', reference_cmd)
        elif executor.language.startswith('go_'):
            modules = self._get_go_modules(executor)
            module_cmds = ['tar -xf {} && go mod init */*/*'.format(m) for m in modules]
            module_cmd = '{}; '.format('; '.join(module_cmds)) if module_cmds else ''
            build_command = build_command.replace('#{modules} ', module_cmd)

        return build_command

    def _get_build_payloads(self, executor):
        """Get additional payloads required for building

        :param executor: Executor to get build payloads for
        :type executor: Executor
        :return: Required references and modules
        :rtype: List[str]
        """
        return self._get_csharp_references(executor) + self._get_go_modules(executor)

    @staticmethod
    def _get_csharp_references(executor):
        """Get C# DLL references

        :param executor: Executor to get references for
        :type executor: Executor
        :return: C# DLLs
        :rtype: List[str]
        """
        return [p for p in executor.payloads if p.endswith('.dll')] if executor.language == 'csharp' else []

    @staticmethod
    def _get_go_modules(executor):
        """Get Golang modules

        :param executor: Executor to get modules for
        :type executor: Executor
        :return: Golang modules
        :rtype: List[str]
        """
        return [p for p in executor.payloads if '.tar' in p] if executor.language.startswith('go_') else []
