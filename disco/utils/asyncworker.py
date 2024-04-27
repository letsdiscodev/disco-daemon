from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Awaitable, Callable

from croniter import croniter

from disco.models import Deployment
from disco.models.db import Session
from disco.utils import docker, keyvalues
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.encryption import decrypt

log = logging.getLogger(__name__)


class WorkerTask:
    pass


@dataclass
class Cron(WorkerTask):
    next: datetime


@dataclass
class DiscoCron(Cron):
    name: str
    delta: timedelta


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

    @staticmethod
    def from_deployment(
        service_name: str,
        disco_file: DiscoFile,
        deployment: Deployment,
        disco_host: str,
    ) -> ProjectCron:
        schedule = disco_file.services[service_name].schedule
        cron = croniter(schedule, datetime.now(timezone.utc))
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment.env_variables
        ] + [
            ("DISCO_PROJECT_NAME", deployment.project.name),
            ("DISCO_SERVICE_NAME", service_name),
            ("DISCO_HOST", disco_host),
        ]
        if deployment.domain is not None:
            env_variables += [
                ("DISCO_PROJECT_DOMAIN", deployment.domain),
            ]
        if deployment.commit_hash is not None:
            env_variables += [
                ("DISCO_COMMIT", deployment.commit_hash),
            ]
        volumes = [
            ("volume", v.name, v.destination_path)
            for v in disco_file.services[service_name].volumes
        ]
        image = docker.get_image_name_for_service(
            disco_file=disco_file,
            service_name=service_name,
            registry_host=deployment.registry_host,
            project_name=deployment.project.name,
            deployment_number=deployment.number,
        )
        command = disco_file.services[service_name].command
        assert command is not None
        return ProjectCron(
            project_name=deployment.project.name,
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
                    deployment.project.name, deployment.number
                )
            ],
            cron=cron,
            next=cron.get_next(datetime),
            paused=False,
        )

    def update_for_deployment(
        self,
        disco_file: DiscoFile,
        deployment: Deployment,
        disco_host: str,
    ) -> None:
        command = disco_file.services[self.service_name].command
        assert command is not None
        schedule = disco_file.services[self.service_name].schedule
        volumes = [
            ("volume", v.name, v.destination_path)
            for v in disco_file.services[self.service_name].volumes
        ]
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment.env_variables
        ] + [
            ("DISCO_PROJECT_NAME", deployment.project.name),
            ("DISCO_SERVICE_NAME", self.service_name),
            ("DISCO_HOST", disco_host),
        ]
        if deployment.domain is not None:
            env_variables += [
                ("DISCO_PROJECT_DOMAIN", deployment.domain),
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
            project_name=deployment.project.name,
            deployment_number=deployment.number,
        )
        self.volumes = volumes
        self.env_variables = env_variables
        self.networks = [
            docker.deployment_network_name(deployment.project.name, deployment.number)
        ]
        self.command = command
        if self.schedule != schedule:
            self.cron = croniter(schedule, datetime.now(timezone.utc))
            self.next = self.cron.get_next()
        self.schedule = schedule

    async def run(self) -> None:
        await docker.run_async(
            image=self.image,
            project_name=self.project_name,
            name=f"{self.project_name}-{self.service_name}.{self.deployment_number}",
            env_variables=self.env_variables,
            volumes=self.volumes,
            networks=self.networks,
            command=self.command,
            timeout=300,
            log_output=lambda x: log.info("Output: %s", x.replace("\n", "")),
        )

    def schedule_next(self) -> None:
        self.next = self.cron.get_next(datetime, datetime.now(timezone.utc))


@dataclass
class QueueTask(WorkerTask):
    run: Callable[[], Awaitable[None]]


class AsyncWorker:
    def __init__(self) -> None:
        self._stopped = False
        self._disco_crons: list[DiscoCron] = []
        self._project_crons: list[ProjectCron] = []
        # not threadsafe, only use in async code
        self.queue: asyncio.Queue[QueueTask] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def get_loop(self) -> asyncio.AbstractEventLoop:
        assert self._loop is not None
        return self._loop

    def stop(self) -> None:
        log.info("AsyncWorker received stop command")
        self._stopped = True

    async def work(self) -> None:
        log.info("Starting AsyncWorker")
        self._disco_crons = self._load_disco_crons()
        self._project_crons = self._load_project_crons()
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

    def reload_and_resume_project_crons(
        self, prev_project_name: str | None, project_name: str, deployment_number: int
    ) -> None:
        from disco.utils.deployments import get_deployment_by_number
        from disco.utils.projects import get_project_by_name

        with Session.begin() as dbsession:
            disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
            assert disco_host is not None
            project = get_project_by_name(dbsession, project_name)
            assert project is not None
            deployment = get_deployment_by_number(dbsession, project, deployment_number)
            assert deployment is not None
            disco_file = get_disco_file_from_str(deployment.disco_file)
            existing_crons = set()
            crons_to_remove = set()
            for cron in self._project_crons:
                if cron.project_name != prev_project_name:
                    continue
                existing_crons.add(cron.service_name)
                if cron.service_name in disco_file.services:
                    cron.update_for_deployment(
                        disco_file=disco_file,
                        deployment=deployment,
                        disco_host=disco_host,
                    )
                else:
                    crons_to_remove.add(cron)
            for cron_to_remove in crons_to_remove:
                self._project_crons.remove(cron_to_remove)
            for service_name, service in disco_file.services.items():
                if service.type != ServiceType.cron:
                    continue
                if service_name in existing_crons:
                    continue  # already updated above
                try:
                    cron = ProjectCron.from_deployment(
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

    async def _get_tasks(self) -> AsyncGenerator[asyncio.Task, None]:
        while not self._stopped:
            worker_tasks = await self._get_worker_tasks()
            for worker_task in worker_tasks:
                yield asyncio.create_task(self._process_worker_task(worker_task))
            next_second_delta = (
                1000000 - datetime.now(timezone.utc).microsecond
            ) / 1000000
            try:
                worker_task = await asyncio.wait_for(
                    self.queue.get(), next_second_delta
                )
                yield asyncio.create_task(self._process_worker_task(worker_task))
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
            # TODO refac to also use `.run()`
            if worker_task.name == "SECOND":
                pass
            elif worker_task.name == "MINUTE":
                pass
            elif worker_task.name == "HOUR":
                pass
            elif worker_task.name == "DAY":
                pass
        elif isinstance(worker_task, ProjectCron):
            worker_task.schedule_next()
            log.info(
                "Running cron %s %s", worker_task.project_name, worker_task.service_name
            )
            await worker_task.run()
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
            ),
        ]

    def _load_project_crons(self) -> list[ProjectCron]:
        from disco.utils.deployments import get_live_deployment
        from disco.utils.projects import get_all_projects

        crons: list[ProjectCron] = []
        with Session.begin() as dbsession:
            disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
            assert disco_host is not None
            projects = get_all_projects(dbsession)
            for project in projects:
                deployment = get_live_deployment(dbsession, project)
                if deployment is None:
                    continue
                disco_file = get_disco_file_from_str(deployment.disco_file)
                for service_name, service in disco_file.services.items():
                    if service.type != ServiceType.cron:
                        continue
                    try:
                        cron = ProjectCron.from_deployment(
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
