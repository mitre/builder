import docker
import glob
import os
import shutil

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
        for ab in await self.data_svc.locate('abilities'):
            if ab.code:
                ab.HOOKS[ab.language] = self.generate_ability_execution_method

    async def generate_ability_execution_method(self, ability):
        if not ability.test:
            command, payload = await self._dynamically_compile_ability_code(ability=ability)
            ability.test = command
            if payload not in ability.payloads:
                ability.payloads.append(payload)

    async def stage_enabled_dockers(self):
        await self._download_docker_images()

    """ PRIVATE """

    async def _dynamically_compile_ability_code(self, ability):
        await self._stage_code_in_docker_folder(ability)
        self._run_target_docker(ability=ability, args=self._replace_args_props(ability=ability))
        self._stage_payload(language=ability.language, payload=ability.build_target)
        self._purge_build_folder(language=ability.language)
        return self._build_command_block_syntax(payload=ability.build_target), ability.build_target

    @staticmethod
    def _build_command_block_syntax(payload):
        return '.\\%s' % payload

    async def _download_docker_images(self):
        for k, v in self.get_config(prop='enabled', name='build').items():
            await self._stage_build_dir(directory=k)
            data = self.docker_client.images.list(name=v['docker'])
            if not data:
                data = self.docker_client.images.pull(v['docker'])
            self.build_envs[k] = data[0] if isinstance(data, list) else data

    async def _stage_build_dir(self, directory):
        try:
            os.mkdir(os.path.join(self.build_directory, directory))
        except FileExistsError:
            self.log.debug('Build directory for %s already constructed' % directory)

    def _stage_payload(self, language, payload):
        src = os.path.join(self.build_directory, language, payload)
        dst = os.path.join(self.payloads_directory, payload)
        if os.path.exists(src):
            if os.path.isfile(dst):
                os.remove(dst)
            shutil.move(src=src, dst=dst)

    async def _stage_code_in_docker_folder(self, ability):
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

    def _purge_build_folder(self, language):
        for f in glob.iglob(f'{self.build_directory}/{language}/*'):
            os.remove(f)

    def _run_target_docker(self, ability, args):
        env = self.get_config(prop='enabled', name='build').get(ability.language)
        container = self.docker_client.containers.run(image=self.build_envs[ability.language].short_id, remove=True,
                                                      command=('%s %s' % (env['entrypoint'], args)).split(' '),
                                                      working_dir=env['workdir'],
                                                      volumes={os.path.abspath(
                                                               os.path.join(self.build_directory, ability.language)):
                                                               dict(bind=env['workdir'], mode='rw')}, detach=True)
        code = container.wait()
        self.log.debug('Container for %s ran for ability ID %s: %s' % (ability.language, ability.ability_id,
                                                                       code))

    def _replace_args_props(self, ability):
        env = self.get_config(prop='enabled', name='build').get(ability.language)
        cmd = env['entrypoint_args'].replace('#{code}', self.build_file)
        cmd = cmd.replace('#{build_target}', ability.build_target)

        references = [p for p in ability.payloads if p.endswith('.dll')]
        reference_cmd = '/r:{}'.format(','.join(references)) if references else ''
        cmd = cmd.replace('#{references}', reference_cmd)
        return cmd
