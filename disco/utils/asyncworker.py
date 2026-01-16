from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Awaitable, Callable, Sequence

from croniter import croniter

from disco.models import Deployment, DeploymentEnvironmentVariable
from disco.models.db import AsyncSession
from disco.utils import docker, keyvalues
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.imagecleanup import remove_unused_images

log = logging.getLogger(__name__)


async def no_op() -> None:
    return None


class WorkerTask:
    pass


@dataclass
class Cron(WorkerTask):
    next: datetime


@dataclass
class DiscoCron(Cron):
    name: str
    delta: timedelta
    run: Callable[[], Awaitable[None]] = no_op


async def cron_minute() -> None:
    from disco.utils.tunnels import stop_expired_tunnels

    log.info("Disco minute cron")
    await stop_expired_tunnels()


async def cron_hour() -> None:
    from disco.utils.commandoutputs import clean_up_db_connections
    from disco.utils.commands import clean_up_orphan_commands
    from disco.utils.tunnels import clean_up_rogue_tunnels

    log.info("Disco hour cron")
    await clean_up_db_connections()
    await clean_up_rogue_tunnels()
    await clean_up_orphan_commands()


async def cron_day() -> None:
    from disco.utils.logs import clean_up_rogue_syslogs

    log.info("Disco day cron")
    await clean_up_rogue_syslogs()
    await remove_unused_images()
    await docker.builder_prune()


@dataclass
class ProjectCron(Cron):
    project_name: str
    service_name: str
    registry_host: str | None
    deployment_number: int
    image: str
    volumes: list[tuple[str, str, str]]
    env_variables: list[tuple[str, str]]
    networks: list[str]
    command: str
    schedule: str
    cron: croniter
    paused: bool
    timeout: int

    @staticmethod
    async def from_deployment(
        service_name: str,
        disco_file: DiscoFile,
        deployment: Deployment,
        disco_host: str,
    ) -> ProjectCron:
        from disco.utils.projects import volume_name_for_project

        schedule = disco_file.services[service_name].schedule
        cron = croniter(schedule, datetime.now(timezone.utc))
        deployment_env_variables: Sequence[
            DeploymentEnvironmentVariable
        ] = await deployment.awaitable_attrs.env_variables
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment_env_variables
        ] + [
            ("DISCO_PROJECT_NAME", deployment.project_name),
            ("DISCO_SERVICE_NAME", service_name),
            ("DISCO_HOST", disco_host),
            ("DISCO_DEPLOYMENT_NUMBER", str(deployment.number)),
        ]
        if deployment.commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", deployment.commit_hash),
            ]
        volumes = [
            (
                "volume",
                volume_name_for_project(v.name, deployment.project_id),
                v.destination_path,
            )
            for v in disco_file.services[service_name].volumes
        ]
        image = docker.get_image_name_for_service(
            disco_file=disco_file,
            service_name=service_name,
            registry_host=deployment.registry_host,
            project_name=deployment.project_name,
            deployment_number=deployment.number,
        )
        command = disco_file.services[service_name].command
        assert command is not None
        return ProjectCron(
            project_name=deployment.project_name,
            service_name=service_name,
            image=image,
            volumes=volumes,
            env_variables=env_variables,
            registry_host=deployment.registry_host,
            deployment_number=deployment.number,
            schedule=schedule,
            command=command,
            networks=[
                docker.deployment_network_name(
                    deployment.project_name, deployment.number
                ),
                "disco-main",
            ],
            cron=cron,
            next=cron.get_next(datetime),
            paused=False,
            timeout=disco_file.services[service_name].timeout,
        )

    async def update_for_deployment(
        self,
        disco_file: DiscoFile,
        deployment: Deployment,
        disco_host: str,
    ) -> None:
        from disco.utils.projects import volume_name_for_project

        command = disco_file.services[self.service_name].command
        assert command is not None
        schedule = disco_file.services[self.service_name].schedule
        volumes = [
            (
                "volume",
                volume_name_for_project(v.name, deployment.project_id),
                v.destination_path,
            )
            for v in disco_file.services[self.service_name].volumes
        ]
        deployment_env_variables: Sequence[
            DeploymentEnvironmentVariable
        ] = await deployment.awaitable_attrs.env_variables
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment_env_variables
        ] + [
            ("DISCO_PROJECT_NAME", deployment.project_name),
            ("DISCO_SERVICE_NAME", self.service_name),
            ("DISCO_HOST", disco_host),
            ("DISCO_DEPLOYMENT_NUMBER", str(deployment.number)),
        ]
        if deployment.commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", deployment.commit_hash),
            ]
        self.project_name = deployment.project_name
        self.registry_host = deployment.registry_host
        self.deployment_number = deployment.number
        self.image = docker.get_image_name_for_service(
            disco_file=disco_file,
            service_name=self.service_name,
            registry_host=deployment.registry_host,
            project_name=deployment.project_name,
            deployment_number=deployment.number,
        )
        self.volumes = volumes
        self.env_variables = env_variables
        self.networks = [
            docker.deployment_network_name(deployment.project_name, deployment.number),
            "disco-main",
        ]
        self.command = command
        if self.schedule != schedule:
            self.cron = croniter(schedule, datetime.now(timezone.utc))
            self.next = self.cron.get_next(datetime)
        self.schedule = schedule
        self.timeout = disco_file.services[self.service_name].timeout

    async def run(self) -> None:
        async def log_stdout(stdout: str) -> None:
            pass

        async def log_stderr(stderr: str) -> None:
            pass

        name = f"{self.project_name}-{self.service_name}.{self.deployment_number}"
        if await docker.container_exists(name):
            await docker.remove_container(name)
        await docker.run(
            image=self.image,
            project_name=self.project_name,
            name=name,
            env_variables=self.env_variables,
            volumes=self.volumes,
            networks=self.networks,
            command=self.command,
            timeout=self.timeout,
            stdout=log_stdout,
            stderr=log_stderr,
        )

    def schedule_next(self) -> None:
        self.next = self.cron.get_next(datetime, datetime.now(timezone.utc))


@dataclass
class QueueTask(WorkerTask):
    id: str
    run: Callable[[], Awaitable[None]]


class TaskNotFoundError(KeyError):
    pass


class AsyncWorker:
    def __init__(self) -> None:
        self._stopped = False
        self._disco_crons: list[DiscoCron] = []
        self._project_crons: list[ProjectCron] = []
        # not threadsafe, only use in async code
        self.queue: asyncio.Queue[QueueTask] = asyncio.Queue()
        self._queue_tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def enqueue(self, async_callable: Callable[[], Awaitable[None]]) -> str:
        queue_task = QueueTask(id=uuid.uuid4().hex, run=async_callable)
        await async_worker.queue.put(queue_task)
        return queue_task.id

    def cancel_task(self, task_id: str) -> None:
        try:
            task = self._queue_tasks[task_id]
        except KeyError as ex:
            raise TaskNotFoundError() from ex
        task.cancel()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def get_loop(self) -> asyncio.AbstractEventLoop:
        assert self._loop is not None
        return self._loop

    def stop(self) -> None:
        log.info("AsyncWorker received stop command")
        self._stopped = True

    async def work(self) -> None:
        from disco.utils.commands import clean_up_orphan_commands

        log.info("Starting AsyncWorker")
        await clean_up_orphan_commands(remove_all=True)
        self._disco_crons = self._load_disco_crons()
        self._project_crons = await self._load_project_crons()
        tasks: set[asyncio.Task] = set()
        async for task in self._get_tasks():
            tasks.add(task)
            task.add_done_callback(lambda t: tasks.remove(t))
        log.info("Stopping AsyncWorker")
        await asyncio.gather(*tasks)
        log.info("Stopped AsyncWorker")

    def pause_project_crons(self, project_name: str) -> None:
        for cron in self._project_crons:
            if cron.project_name != project_name:
                continue
            cron.paused = True

    def remove_project_crons(self, project_name: str) -> None:
        log.info("Removing project crons for %s", project_name)
        for cron in self._project_crons:
            if cron.project_name == project_name:
                self._project_crons.remove(cron)

    async def reload_and_resume_project_crons(
        self,
        prev_project_name: str | None,
        project_name: str,
        deployment_number: int,
    ) -> None:
        from disco.utils.deployments import get_deployment_by_number
        from disco.utils.projects import get_project_by_name

        log.info("Reloading project crons of %s", project_name)
        async with AsyncSession.begin() as dbsession:
            disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
            project = await get_project_by_name(dbsession, project_name)
            assert project is not None
            deployment = await get_deployment_by_number(
                dbsession, project, deployment_number
            )
            assert deployment is not None
            disco_file = get_disco_file_from_str(deployment.disco_file)
            existing_crons = set()
            for cron in self._project_crons:
                if cron.project_name != prev_project_name:
                    continue
                existing_crons.add(cron.service_name)
                if cron.service_name in disco_file.services:
                    log.info("Updating cron %s %s", project_name, cron.service_name)
                    await cron.update_for_deployment(
                        disco_file=disco_file,
                        deployment=deployment,
                        disco_host=disco_host,
                    )
                else:
                    log.info(
                        "Removing cron %s %s", prev_project_name, cron.service_name
                    )
                    self._project_crons.remove(cron)
            for service_name, service in disco_file.services.items():
                if service.type != ServiceType.cron:
                    continue
                if service_name in existing_crons:
                    continue  # already updated above
                try:
                    log.info("Adding cron %s %s", project_name, service_name)
                    cron = await ProjectCron.from_deployment(
                        service_name=service_name,
                        disco_file=disco_file,
                        deployment=deployment,
                        disco_host=disco_host,
                    )
                    self._project_crons.append(cron)
                except Exception:
                    log.exception(
                        "Failed to add project cron to list %s %s %s",
                        project_name,
                        service_name,
                        deployment.number,
                    )
            for cron in self._project_crons:
                if cron.project_name != project_name:
                    continue
                cron.paused = False
        log.info("Reloaded project crons of %s", project_name)

    async def _get_tasks(self) -> AsyncGenerator[asyncio.Task, None]:
        while not self._stopped:
            worker_tasks = await self._get_worker_tasks()
            for worker_task in worker_tasks:
                yield asyncio.create_task(self._process_worker_task(worker_task))
            next_second_delta = (
                1000000 - datetime.now(timezone.utc).microsecond
            ) / 1000000
            try:
                w_task = await asyncio.wait_for(self.queue.get(), next_second_delta)
                aio_task = asyncio.create_task(self._process_worker_task(w_task))
                self._queue_tasks[w_task.id] = aio_task

                def get_remove_task_func(task_id: str):
                    # defining function that returns function to create closure
                    # otherwise, it would just remove the last task
                    # that was added
                    def remove_task(_):
                        self._queue_tasks.pop(task_id)

                    return remove_task

                aio_task.add_done_callback(get_remove_task_func(w_task.id))
                yield aio_task
            except asyncio.TimeoutError:
                pass

    async def _get_worker_tasks(self) -> list[WorkerTask]:
        worker_tasks: list[WorkerTask] = []
        for disco_cron in self._disco_crons:
            if disco_cron.next <= datetime.now(timezone.utc):
                worker_tasks.append(disco_cron)
        for project_cron in self._project_crons:
            if (
                project_cron.next <= datetime.now(timezone.utc)
                and not project_cron.paused
            ):
                worker_tasks.append(project_cron)
        return worker_tasks

    async def _process_worker_task(self, worker_task: WorkerTask) -> None:
        if isinstance(worker_task, DiscoCron):
            # TODO refac to also use `.schedule_next()`
            worker_task.next += worker_task.delta
            await worker_task.run()
        elif isinstance(worker_task, ProjectCron):
            worker_task.schedule_next()
            log.info(
                "Running cron %s %s", worker_task.project_name, worker_task.service_name
            )
            try:
                await worker_task.run()
            except asyncio.TimeoutError:
                log.info(
                    "Cron timed out %s %s after %d seconds",
                    worker_task.project_name,
                    worker_task.service_name,
                    worker_task.timeout,
                )
            except docker.CommandRunProcessStatusError:
                log.info(
                    "Cron did not complete successfully %s %s",
                    worker_task.project_name,
                    worker_task.service_name,
                )
        elif isinstance(worker_task, QueueTask):
            log.info("Runnning QueueTask")
            await worker_task.run()
            log.info("Done runnning QueueTask")

    def _load_disco_crons(self) -> list[DiscoCron]:
        now = datetime.now(timezone.utc)
        return [
            DiscoCron(
                name="SECOND",
                next=datetime(
                    year=now.year,
                    month=now.month,
                    day=now.day,
                    hour=now.hour,
                    minute=now.minute,
                    second=now.second,
                    microsecond=0,
                    tzinfo=timezone.utc,
                )
                + timedelta(seconds=1),
                delta=timedelta(seconds=1),
            ),
            DiscoCron(
                name="MINUTE",
                next=datetime(
                    year=now.year,
                    month=now.month,
                    day=now.day,
                    hour=now.hour,
                    minute=now.minute,
                    second=0,
                    microsecond=0,
                    tzinfo=timezone.utc,
                )
                + timedelta(minutes=1),
                delta=timedelta(minutes=1),
                run=cron_minute,
            ),
            DiscoCron(
                name="HOUR",
                next=datetime(
                    year=now.year,
                    month=now.month,
                    day=now.day,
                    hour=now.hour,
                    minute=0,
                    second=0,
                    microsecond=0,
                    tzinfo=timezone.utc,
                )
                + timedelta(hours=1),
                delta=timedelta(hours=1),
                run=cron_hour,
            ),
            DiscoCron(
                name="DAY",
                next=datetime(
                    year=now.year,
                    month=now.month,
                    day=now.day,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                    tzinfo=timezone.utc,
                )
                + timedelta(days=1),
                delta=timedelta(days=1),
                run=cron_day,
            ),
        ]

    async def _load_project_crons(self) -> list[ProjectCron]:
        from disco.utils.deployments import get_live_deployment
        from disco.utils.projects import get_all_projects

        crons: list[ProjectCron] = []
        async with AsyncSession.begin() as dbsession:
            disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
            projects = await get_all_projects(dbsession)
            for project in projects:
                deployment = await get_live_deployment(dbsession, project)
                if deployment is None:
                    continue
                disco_file = get_disco_file_from_str(deployment.disco_file)
                for service_name, service in disco_file.services.items():
                    if service.type != ServiceType.cron:
                        continue
                    try:
                        cron = await ProjectCron.from_deployment(
                            service_name=service_name,
                            disco_file=disco_file,
                            deployment=deployment,
                            disco_host=disco_host,
                        )
                        crons.append(cron)
                    except Exception:
                        log.exception(
                            "Failed to add project cron to list %s %s %s",
                            project.name,
                            service_name,
                            deployment.number,
                        )
        return crons


async_worker = AsyncWorker()
