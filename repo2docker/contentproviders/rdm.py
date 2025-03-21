import asyncio
import os
import json
from typing import Any, Dict, Optional, Union
import uuid

from urllib.parse import urlparse

from .base import ContentProvider

import aiofiles
from osfclient.api import OSF
from osfclient.models import Folder, Storage
from osfclient.utils import find_by_path


class RDM(ContentProvider):
    """Provide contents of GakuNin RDM."""

    def __init__(self):
        self.hosts = [
            {
                "hostname": [
                    "https://test.some.host.nii.ac.jp/",
                ],
                "api": "https://api.test.some.host.nii.ac.jp/v2/",
            }
        ]
        if "RDM_HOSTS" in os.environ:
            with open(os.path.expanduser(os.environ["RDM_HOSTS"])) as f:
                self.hosts = json.load(f)
        if "RDM_HOSTS_JSON" in os.environ:
            self.hosts = json.loads(os.environ["RDM_HOSTS_JSON"])
        if isinstance(self.hosts, list):
            for host in self.hosts:
                if "hostname" not in host:
                    raise ValueError("No hostname: {}".format(json.dumps(host)))
                if not isinstance(host["hostname"], list):
                    raise ValueError(
                        "hostname should be list of string: {}".format(
                            json.dumps(host["hostname"])
                        )
                    )
                if "api" not in host:
                    raise ValueError("No api: {}".format(json.dumps(host)))

    def detect(self, source, ref=None, extra_args=None):
        """Trigger this provider for directory on RDM"""
        for host in self.hosts:
            if any([source.startswith(s) for s in host["hostname"]]):
                u = urlparse(source)
                path = u.path[1:] if u.path.startswith("/") else u.path
                if "/" in path:
                    self.project_id, self.path = path.split("/", 1)
                    if self.path.startswith("files/"):
                        self.path = self.path[len("files/") :]
                else:
                    self.project_id = path
                    self.path = ""
                self.uuid = ref if self._check_ref_defined(ref) else str(uuid.uuid1())
                return {
                    "project_id": self.project_id,
                    "path": self.path,
                    "host": host,
                    "uuid": self.uuid,
                }
        return None

    def _check_ref_defined(self, ref):
        if ref is None or ref == "HEAD":
            return False
        return True

    def fetch(self, spec, output_dir, yield_output=False):
        """Fetch RDM directory"""
        # Perform the async fetch synchronously
        loop = asyncio.new_event_loop()
        queue = asyncio.Queue()
        loop.create_task(self._fetch_with_error(spec, output_dir, queue))

        try:
            while True:
                result = loop.run_until_complete(queue.get())
                if isinstance(result, BaseException):
                    raise result
                if result is None:
                    break
                yield result
        finally:
            loop.close()

    async def _fetch_with_error(self, spec: Dict[str, Any], output_dir: str, queue: asyncio.Queue):
        try:
            await self._fetch(spec, output_dir, queue)
        except BaseException as e:
            await queue.put(e)
        finally:
            await queue.put(None)

    async def _fetch(self, spec: Dict[str, Any], output_dir: str, queue: asyncio.Queue):
        project_id = spec["project_id"]
        path = spec["path"]
        host = spec["host"]
        api_url = host["api"][:-1] if host["api"].endswith("/") else host["api"]

        await queue.put("Fetching RDM directory {} on {} at {}.\n".format(
            path, project_id, api_url
        ))
        osf = OSF(
            token=host["token"] if "token" in host else os.getenv("OSF_TOKEN"),
            base_url=api_url,
        )
        project = await osf.project(project_id)

        if len(path):
            path = path.rstrip("/")
            storage = await project.storage(path[:path.index("/")] if "/" in path else path)
            if "/" in path:
                storage = await find_by_path(storage, path[path.index("/") + 1:])
                if storage is None:
                    raise RuntimeError(f"Could not find path {path}")
            async for line in self._fetch_storage(storage, output_dir, None):
                await queue.put(line)
        else:
            async for storage in project.storages:
                async for line in self._fetch_storage(storage, output_dir, storage.name):
                    await queue.put(line)

    async def _fetch_storage(
        self,
        storage: Union[Storage, Folder],
        output_dir: str,
        local_dir: Optional[str],
    ):
        async for file_ in storage.files:
            if "/" in file_.name or "\\" in file_.name:
                raise ValueError(f"File.name cannot include path separators: {file_.name}")
            local_path = os.path.join(local_dir, file_.name) if local_dir is not None else file_.name
            local_dir_path = os.path.join(output_dir, local_dir) if local_dir is not None else output_dir
            local_file_path = os.path.join(output_dir, local_path)
            if not os.path.isdir(local_dir_path):
                os.makedirs(local_dir_path)
            async with aiofiles.open(local_file_path, "wb") as f:
                await file_.write_to(f)
            yield "Fetch: {} ({} to {})\n".format(file_.path, local_path, output_dir)
        async for folder in storage.folders:
            if "/" in folder.name or "\\" in folder.name:
                raise ValueError(f"Folder.name cannot include path separators: {folder.name}")
            local_folder_dir = os.path.join(local_dir, folder.name) if local_dir is not None else folder.name
            async for line in self._fetch_storage(folder, output_dir, local_folder_dir):
                yield line

    @property
    def content_id(self):
        """Content ID of the RDM directory - this provider identifies repos by random UUID"""
        return "{}-{}".format(self.project_id, self.uuid)
