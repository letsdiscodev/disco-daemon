"""Tests of the cleanup of old static deployment directories.

They use the database of the dev container (created with alembic, see
BUILD.md) and the real /disco/srv of the container:

    bin/test

"""

import asyncio
import os
import shutil
import unittest
import uuid
from typing import Any, Coroutine
from unittest import mock

from sqlalchemy import delete

from disco.models import Deployment, Project
from disco.models.db import Session, build_engines
from disco.utils import caddy, staticcleanup
from disco.utils.filesystem import (
    static_site_deployment_path,
    static_site_deployments_path,
    static_sites_root,
)
from disco.utils.staticcleanup import removable_deployment_numbers
from disco.utils.worker import worker

# one loop for the whole module: the database engines are module globals
# bound to the loop that created them
LOOP = asyncio.new_event_loop()


def run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return LOOP.run_until_complete(coroutine)


class RemovableDeploymentNumbersTests(unittest.TestCase):
    def test_below_live_except_kept(self) -> None:
        numbers = removable_deployment_numbers(
            entries=["1", "2", "3", "4", "5", "6"],
            live=5,
            keep={5, 4},
            served_root=None,
            project_name="p",
        )
        self.assertEqual(numbers, [1, 2, 3])

    def test_live_previous_served_and_above_live_are_never_returned(self) -> None:
        numbers = removable_deployment_numbers(
            entries=[str(n) for n in range(1, 9)],
            live=5,
            keep={5, 4},
            served_root=static_site_deployment_path("p", 2),
            project_name="p",
        )
        self.assertEqual(numbers, [1, 3])
        for kept in (2, 4, 5, 6, 7, 8):
            self.assertNotIn(kept, numbers)

    def test_served_root_of_another_project_protects_nothing(self) -> None:
        numbers = removable_deployment_numbers(
            entries=["1", "2", "3", "4", "5"],
            live=5,
            keep={5, 4},
            served_root=static_site_deployment_path("other", 1),
            project_name="p",
        )
        self.assertEqual(numbers, [1, 2, 3])

    def test_entries_that_are_not_deployment_numbers(self) -> None:
        numbers = removable_deployment_numbers(
            entries=["0", "01", "-1", "", "notes", "1a", ".trash", "3"],
            live=5,
            keep={5},
            served_root=None,
            project_name="p",
        )
        self.assertEqual(numbers, [3])

    def test_nothing_below_live(self) -> None:
        for entries, live in (([], 5), (["1"], 1), (["5", "6"], 5)):
            numbers = removable_deployment_numbers(
                entries=entries,
                live=live,
                keep={live},
                served_root=None,
                project_name="p",
            )
            self.assertEqual(numbers, [], entries)

    def test_oldest_first(self) -> None:
        numbers = removable_deployment_numbers(
            entries=["3", "10", "1", "2"],
            live=11,
            keep={11},
            served_root=None,
            project_name="p",
        )
        self.assertEqual(numbers, [1, 2, 3, 10])


class CleanUpProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        run(build_engines())
        self.project_name = f"test-{uuid.uuid4().hex[:12]}"
        self.project_id = uuid.uuid4().hex
        self.served_root: str | None = None
        self.caddy_error = False

        async def add_project() -> None:
            async with Session.begin() as dbsession:
                dbsession.add(Project(id=self.project_id, name=self.project_name))

        run(add_project())
        os.makedirs(self.project_dir())

        async def get_static_site_root(project_name: str) -> str | None:
            if self.caddy_error:
                raise caddy.CaddyError("Caddy is down")
            return self.served_root

        patcher = mock.patch.object(
            caddy, "get_static_site_root", side_effect=get_static_site_root
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        async def remove_project() -> None:
            async with Session.begin() as dbsession:
                await dbsession.execute(
                    delete(Deployment).where(Deployment.project_id == self.project_id)
                )
                await dbsession.execute(
                    delete(Project).where(Project.id == self.project_id)
                )

        run(remove_project())
        shutil.rmtree(self.project_dir(), ignore_errors=True)

    def project_dir(self) -> str:
        return static_site_deployments_path(self.project_name)

    def dirs(self) -> list[str]:
        return sorted(os.listdir(self.project_dir()))

    def add_deployments(
        self, statuses: dict[int, str], task_ids: dict[int, str] | None = None
    ) -> None:
        """Deployment rows, each with a directory holding one file."""

        async def add() -> None:
            async with Session.begin() as dbsession:
                for number, status in statuses.items():
                    dbsession.add(
                        Deployment(
                            number=number,
                            status=status,
                            deployment_type="GITHUB",
                            project_name=self.project_name,
                            project_id=self.project_id,
                            task_id=(task_ids or {}).get(number),
                        )
                    )

        run(add())
        for number in statuses:
            path = static_site_deployment_path(self.project_name, number)
            os.makedirs(path)
            with open(f"{path}/index.html", "w") as f:
                f.write(f"deployment {number}")

    def assert_intact(self, number: int) -> None:
        with open(
            f"{static_site_deployment_path(self.project_name, number)}/index.html"
        ) as f:
            self.assertEqual(f.read(), f"deployment {number}")

    def clean_up(self, limit: int = 100) -> int:
        return run(staticcleanup._clean_up_project(self.project_name, limit))

    def test_removes_all_but_live_and_previous(self) -> None:
        self.add_deployments({n: "COMPLETE" for n in range(1, 6)})
        self.assertEqual(self.clean_up(), 3)
        self.assertEqual(self.dirs(), ["4", "5"])
        self.assert_intact(4)
        self.assert_intact(5)
        self.assertEqual(os.listdir(staticcleanup.trash_path()), [])

    def test_keeps_what_caddy_serves(self) -> None:
        self.add_deployments({n: "COMPLETE" for n in range(1, 6)})
        self.served_root = static_site_deployment_path(self.project_name, 2)
        self.assertEqual(self.clean_up(), 2)
        self.assertEqual(self.dirs(), ["2", "4", "5"])
        self.assert_intact(2)

    def test_keeps_everything_above_live(self) -> None:
        self.add_deployments(
            {1: "COMPLETE", 2: "COMPLETE", 3: "COMPLETE", 4: "FAILED", 5: "CANCELLED"}
        )
        self.assertEqual(self.clean_up(), 1)
        self.assertEqual(self.dirs(), ["2", "3", "4", "5"])

    def test_removes_nothing_while_deploying(self) -> None:
        for status in staticcleanup.DEPLOYING_STATUSES:
            with self.subTest(status=status):
                self.add_deployments({1: "COMPLETE", 2: "COMPLETE", 3: "COMPLETE"})
                self.add_deployments({4: status})
                self.assertEqual(self.clean_up(), 0)
                self.assertEqual(self.dirs(), ["1", "2", "3", "4"])
                self.tearDown()
                self.setUp()

    def test_removes_nothing_while_a_recovery_runs(self) -> None:
        # a deployment that failed, whose task is still running: it's
        # putting the previous deployment back
        self.add_deployments(
            {1: "COMPLETE", 2: "COMPLETE", 3: "COMPLETE", 4: "FAILED"},
            task_ids={4: "task4"},
        )
        with mock.patch.object(worker, "running_task_ids", return_value=["task4"]):
            self.assertEqual(self.clean_up(), 0)
        self.assertEqual(self.dirs(), ["1", "2", "3", "4"])
        # the task of another deployment, long done, doesn't count
        with mock.patch.object(worker, "running_task_ids", return_value=["other"]):
            self.assertEqual(self.clean_up(), 1)
        self.assertEqual(self.dirs(), ["2", "3", "4"])

    def test_removes_nothing_without_a_complete_deployment(self) -> None:
        self.add_deployments({1: "FAILED", 2: "FAILED"})
        self.assertEqual(self.clean_up(), 0)
        self.assertEqual(self.dirs(), ["1", "2"])

    def test_removes_nothing_without_deployments(self) -> None:
        os.makedirs(f"{self.project_dir()}/1")
        self.assertEqual(self.clean_up(), 0)
        self.assertEqual(self.dirs(), ["1"])

    def test_leaves_other_entries_and_symlinks_alone(self) -> None:
        self.add_deployments(
            {1: "COMPLETE", 3: "COMPLETE", 4: "COMPLETE", 5: "COMPLETE"}
        )
        os.makedirs(f"{self.project_dir()}/notes")
        os.symlink(
            static_site_deployment_path(self.project_name, 5),
            static_site_deployment_path(self.project_name, 2),
        )
        self.assertEqual(self.clean_up(), 2)
        self.assertEqual(self.dirs(), ["2", "4", "5", "notes"])
        self.assertTrue(
            os.path.islink(static_site_deployment_path(self.project_name, 2))
        )
        self.assert_intact(5)

    def test_stops_when_the_project_was_recreated(self) -> None:
        # same name, another id: its deployment numbers start at 1 again
        self.add_deployments({n: "COMPLETE" for n in range(1, 6)})
        get_project_by_name = staticcleanup.get_project_by_name
        calls = 0

        async def recreated(dbsession: Any, name: str) -> Project | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return await get_project_by_name(dbsession, name)
            return Project(id=uuid.uuid4().hex, name=name)

        with mock.patch.object(staticcleanup, "get_project_by_name", recreated):
            self.assertEqual(self.clean_up(), 0)
        self.assertEqual(self.dirs(), ["1", "2", "3", "4", "5"])

    def test_stops_when_the_project_was_removed(self) -> None:
        self.add_deployments({n: "COMPLETE" for n in range(1, 6)})
        get_project_by_name = staticcleanup.get_project_by_name
        calls = 0

        async def removed(dbsession: Any, name: str) -> Project | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return await get_project_by_name(dbsession, name)
            return None

        with mock.patch.object(staticcleanup, "get_project_by_name", removed):
            self.assertEqual(self.clean_up(), 0)
        self.assertEqual(self.dirs(), ["1", "2", "3", "4", "5"])

    def test_caddy_error_removes_nothing(self) -> None:
        self.add_deployments({n: "COMPLETE" for n in range(1, 6)})
        self.caddy_error = True
        with self.assertRaises(caddy.CaddyError):
            self.clean_up()
        self.assertEqual(self.dirs(), ["1", "2", "3", "4", "5"])

    def test_limit(self) -> None:
        self.add_deployments({n: "COMPLETE" for n in range(1, 6)})
        self.assertEqual(self.clean_up(limit=2), 2)
        self.assertEqual(self.dirs(), ["3", "4", "5"])
        self.assertEqual(self.clean_up(limit=2), 1)
        self.assertEqual(self.dirs(), ["4", "5"])

    def test_whole_run(self) -> None:
        self.add_deployments({n: "COMPLETE" for n in range(1, 4)})
        ghost = f"{static_sites_root()}/ghost-{uuid.uuid4().hex[:12]}"
        os.makedirs(f"{ghost}/1")  # a directory with no project
        self.addCleanup(shutil.rmtree, ghost)
        os.makedirs(f"{staticcleanup.trash_path()}/leftover", exist_ok=True)
        run(staticcleanup.remove_old_static_deployments())
        self.assertEqual(self.dirs(), ["2", "3"])
        self.assertEqual(os.listdir(ghost), ["1"])
        self.assertEqual(os.listdir(staticcleanup.trash_path()), [])


if __name__ == "__main__":
    unittest.main()
