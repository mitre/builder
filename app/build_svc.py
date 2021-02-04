import glob
import os
import shutil
import json
import logging

import docker

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
        for ab in await self.data_svc.locate('abilities'):
            if ab.code and ab.language:
                ab.HOOKS[ab.language] = self.generate_ability_execution_method

    async def generate_ability_execution_method(self, ability):
        """Set command and payloads for an Ability

        :param ability: Ability to set command and payloads for
        :type ability: Ability
        """
        if not ability.additional_info.get('built'):
            await self._build_ability(ability=ability)

            for module in self._get_go_modules(ability):
                ability.payloads.remove(module)

            if not ability.test:
                ability.test = self._build_command_block_syntax(payload=ability.build_target)
            if ability.build_target not in ability.payloads:
                ability.payloads.append(ability.build_target)

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

    async def _build_ability(self, ability):
        """Dynamically compile an Ability

        :param ability: Ability to dynamically compile code for
        :type ability: Ability
        :return: Command to run, payload name
        :rtype: string, string
        """
        self.log.debug('Building ability {}'.format(ability.ability_id))
        env = self.get_config(prop='enabled', name='build').get(ability.language)
        if not env:
            if not ability.additional_info.get('build_error'):
                ability.additional_info['build_error'] = True
                self.log.debug('Error building ability {}: environment "{}" not configured'.format(ability.ability_id,
                                                                                                   ability.language))
            return

        self._set_urllib3_logging()
        await self._build_ability_with_docker(env, ability)
        self._unset_urllib3_logging()

    async def _build_ability_with_docker(self, env, ability):
        """Dynamically compile Ability in Docker

        :param env: Build environment settings
        :type env: dict
        :param ability: Ability to dynamically compile code for
        :type ability: Ability
        :return: Command to run, payload name
        :rtype: string, string
        """
        self._purge_build_directory(ability.language)
        await self._stage_docker_directory(env, ability)
        self._run_target_docker(env, ability, self._replace_build_vars(env, ability))
        self._check_errors(ability.language)
        self._stage_payload(ability.language, ability.build_target)
        self._purge_build_directory(ability.language)
        ability.additional_info['built'] = True
        ability.additional_info['build_error'] = False

    @staticmethod
    def _build_command_block_syntax(payload):
        """Creates the command to run for a given payload

        :param payload: Payload name
        :type payload: string
        :return: Agent command to run
        :rtype: string
        """
        return '.\\{}'.format(payload)

    async def _download_docker_images(self):
        """Download required docker images"""
        for language, language_data in self.get_config(prop='enabled', name='build').items():
            await self._stage_build_dir(language=language)
            data = self.docker_client.images.list(name=language_data['docker'])
            if not data:
                self.log.info('Downloading docker image for builder plugin: {}'.format(language_data['docker']))
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
            self.log.debug('Build directory created for {}'.format(language))

    def _stage_payload(self, language, payload):
        """Move Docker-built payload to CALDERA payload directory

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

    async def _stage_docker_directory(self, env, ability):
        """Create code file and references in Docker build directory

        :param env: Build environment settings
        :type env: dict
        :param ability: Ability to be built
        :type ability: Ability
        """
        build_file = '{}.{}'.format(self.build_file, env['extension']) if env.get('extension') else self.build_file
        with open(os.path.join(self.build_directory, ability.language, build_file), 'w') as f:
            f.write(self.decode_bytes(ability.code, strip_newlines=False))

        for payload in self._get_build_payloads(ability):
            payload_name = payload
            if self.is_uuid4(payload):
                payload_name, _ = self.file_svc.get_payload_name_from_uuid(payload)
            _, src = await self.file_svc.find_file_path(payload_name)
            dst = os.path.join(self.build_directory, ability.language)
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

    def _run_target_docker(self, env, ability, build_command):
        """Run docker container to build ability files

        :param env: Build environment settings
        :type env: dict
        :param ability: Ability to build code for
        :type ability: Ability
        :param build_command: Command to run on docker container
        :type build_command: string
        """
        container = self.docker_client.containers.run(image=self.build_envs[ability.language].short_id, remove=True,
                                                      command=build_command,
                                                      working_dir=env['workdir'],
                                                      user=os.getuid(),
                                                      volumes={os.path.abspath(
                                                               os.path.join(self.build_directory, ability.language)):
                                                               dict(bind=env['workdir'], mode='rw')}, detach=True)
        code = container.wait()
        self.log.debug('Container for {} ran for ability ID {}: {}'.format(ability.language, ability.ability_id, code))

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
                    self.log.debug('{}{} {}: {}'.format(location_data, error.get('level').capitalize(),
                                                        error.get('ruleId'), error.get('message')))
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

    def _replace_build_vars(self, env, ability):
        """Replace template arguments in build command

        :param env: Build environment settings
        :type env: dict
        :param ability: Ability to create command for
        :type ability: Ability
        :return: Created command string
        :rtype: string
        """
        build_file = '{}.{}'.format(self.build_file, env['extension']) if env.get('extension') else self.build_file
        build_command = env['build_command'].replace('#{code}', build_file)
        build_command = build_command.replace('#{build_target}', ability.build_target)
        build_command = self._replace_build_payload_vars(ability, build_command)
        return build_command

    def _replace_build_payload_vars(self, ability, build_command):
        """Replace reference arguments in build command

        :param ability: Ability to replace references for
        :type ability: Ability
        :param build_command: Build command
        :type build_command: str
        :return: Command string with replaced references
        :rtype: string
        """
        if ability.language == 'csharp':
            references = self._get_csharp_references(ability)
            reference_cmd = ' -r:{}'.format(','.join(references)) if references else ''
            build_command = build_command.replace(' #{references}', reference_cmd)
        elif ability.language.startswith('go_'):
            modules = self._get_go_modules(ability)
            module_cmds = ['tar -xf {} && go mod init */*/*'.format(m) for m in modules]
            module_cmd = '{}; '.format('; '.join(module_cmds)) if module_cmds else ''
            build_command = build_command.replace('#{modules} ', module_cmd)

        return build_command

    def _get_build_payloads(self, ability):
        """Get additional payloads required for building

        :param ability: Ability to get build payloads for
        :type ability: Ability
        :return: Required references and modules
        :rtype: List[str]
        """
        return self._get_csharp_references(ability) + self._get_go_modules(ability)

    @staticmethod
    def _get_csharp_references(ability):
        """Get C# DLL references

        :param ability: Ability to get references for
        :type ability: Ability
        :return: C# DLLs
        :rtype: List[str]
        """
        return [p for p in ability.payloads if p.endswith('.dll')] if ability.language == 'csharp' else []

    @staticmethod
    def _get_go_modules(ability):
        """Get Golang modules

        :param ability: Ability to get modules for
        :type ability: Ability
        :return: Golang modules
        :rtype: List[str]
        """
        return [p for p in ability.payloads if '.tar' in p] if ability.language.startswith('go_') else []
