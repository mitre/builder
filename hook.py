from plugins.builder.app.build_svc import BuildService
from app.utility.base_world import BaseWorld

name = 'builder'
description = 'Dynamically compile ability code via docker containers'
address = None
access = BaseWorld.Access.RED


async def enable(services):
    BaseWorld.apply_config('build', BaseWorld.strip_yml('plugins/builder/conf/environments.yml')[0])
    build_svc = BuildService(services)
    await build_svc.stage_enabled_dockers()


async def expansion(services):
    await services.get('build_svc').initialize_code_hook_functions()
