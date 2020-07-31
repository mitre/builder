import docker
import glob
import os
import shutil
import json

from app.utility.base_service import BaseService


class BuildService(BaseService):

    def __init__(self, services):
        self.log = self.add_service('build_svc', self)
        self.file_svc = services.get('file_svc')
        self.data_svc = services.get('data_svc')
        self.docker_client = docker.from_env()
        self.payloads_directory = os.path.join('plugins', 'builder', 'payloads')
        self.build_directory = os.path.join('plugins', 'builder', 'build')
        self.build_envs = dict()
        self.build_file = 'code'

    async def initialize_code_hook_functions(self):
        """Start dynamic code compilation for abilities"""
        for ab in await self.data_svc.locate('abilities'):
            if ab.code:
                ab.HOOKS[ab.language] = self.generate_ability_execution_method

    async def generate_ability_execution_method(self, ability):
        """Set command and payloads for an Ability

        :param ability: Ability to set command and payloads for
        :type ability: Ability
        """
        if not ability.additional_info.get('built'):
            await self._dynamically_compile_ability_code(ability=ability)

            if not ability.test:
                ability.test = self._build_command_block_syntax(payload=ability.build_target)
            if ability.build_target not in ability.payloads:
                ability.payloads.append(ability.build_target)

    async def stage_enabled_dockers(self):
        """Start downloading docker images"""
        await self._download_docker_images()

    """ PRIVATE """

    async def _dynamically_compile_ability_code(self, ability):
        """Dynamically compile code for an Ability

        :param ability: Ability to dynamically compile code for
        :type ability: Ability
        :return: Command to run, payload name
        :rtype: string, string
        """
        await self._stage_docker_directory(ability)
        self._run_target_docker(ability=ability, args=self._replace_args_props(ability=ability))
        self._check_errors(language=ability.language)
        self._stage_payload(language=ability.language, payload=ability.build_target)
        self._purge_build_directory(language=ability.language)
        ability.additional_info['built'] = True

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
                data = self.docker_client.images.pull(language_data['docker'])
            self.build_envs[language] = data[0] if isinstance(data, list) else data

    async def _stage_build_dir(self, language):
        """Create a build directory for a particular language

        :param language: Language to create directory for
        :type language: string
        """
        try:
            os.mkdir(os.path.join(self.build_directory, language))
        except FileExistsError:
            self.log.debug('Build directory for {} already constructed'.format(language))

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

    async def _stage_docker_directory(self, ability):
        """Create code file and DLL references in Docker build directory

        :param ability: Ability to be built
        :type ability: Ability
        """
        if ability.code:
            with open(os.path.join(self.build_directory, ability.language, self.build_file), 'w') as f:
                f.write(self.decode_bytes(ability.code))

        for payload in [p for p in ability.payloads if p.endswith('.dll')]:
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
        for f in glob.iglob(f'{self.build_directory}/{language}/*'):
            os.remove(f)

    def _run_target_docker(self, ability, args):
        """Run docker container to build ability files

        :param ability: Ability to build code for
        :type ability: Ability
        :param args: Arguments to pass to the entrypoint command
        :type args: string
        """
        env = self.get_config(prop='enabled', name='build').get(ability.language)
        container = self.docker_client.containers.run(image=self.build_envs[ability.language].short_id, remove=True,
                                                      command='{} {}'.format(env['entrypoint'], args).split(' '),
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
        if language == 'csharp':
            error_log = os.path.join(self.build_directory, language, 'error.log')
            if os.path.isfile(error_log):
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

    def _replace_args_props(self, ability):
        """Replace template arguments in build command

        :param ability: Ability to create command for
        :type ability: Ability
        :return: Created command string
        :rtype: string
        """
        env = self.get_config(prop='enabled', name='build').get(ability.language)
        cmd = env['entrypoint_args'].replace('#{code}', self.build_file)
        cmd = cmd.replace('#{build_target}', ability.build_target)

        references = [p for p in ability.payloads if p.endswith('.dll')]
        reference_cmd = ' -r:{}'.format(','.join(references)) if references else ''
        cmd = cmd.replace(' #{references}', reference_cmd)
        return cmd
