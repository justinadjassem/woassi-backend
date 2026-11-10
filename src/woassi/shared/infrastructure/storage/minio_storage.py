"""MinIO implementation of `StoragePort`."""

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from io import BytesIO

import anyio.to_thread
from minio import Minio
from minio.error import S3Error

from woassi.shared.application.ports import ObjectInfo
from woassi.shared.domain.errors import ObjectNotFound


class BucketNotPublic(RuntimeError):
    """public_url() called on a private bucket: a wiring error, not a business error."""


class MinioStorage:
    """StoragePort backed by MinIO. One instance maps to one bucket.

    The SDK is synchronous: every network call runs in a thread.

    - `client` reaches MinIO from the server (e.g. minio:9000);
    - `signer` holds the public endpoint, the one the browser will see: it is the one that goes
      into the signature. With `region` set, signing makes no network call.
    """

    def __init__(
        self,
        client: Minio,
        signer: Minio,
        bucket: str,
        *,
        public_base_url: str | None = None,
    ) -> None:
        self._client = client
        self._signer = signer
        self._bucket = bucket
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None

    async def presigned_put(self, key: str, content_type: str, expire_s: int = 600) -> str:
        # The SDK does not sign Content-Type, so `content_type` is not enforced on upload.
        # The real format is checked when the image is read.
        return self._signer.presigned_put_object(
            self._bucket, key, expires=timedelta(seconds=expire_s)
        )

    async def presigned_get(self, key: str, expire_s: int = 3600) -> str:
        """Signed download URL, valid for `expire_s` seconds."""
        return self._signer.presigned_get_object(
            self._bucket, key, expires=timedelta(seconds=expire_s)
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Uploads `data` under `key`."""
        await self._in_thread(
            partial(
                self._client.put_object,
                self._bucket,
                key,
                BytesIO(data),
                len(data),
                content_type=content_type,
            )
        )

    async def get(self, key: str) -> bytes:
        """Downloads `key`. Raises ObjectNotFound if it does not exist."""
        return await self._in_thread(partial(self._read, key))

    async def stat(self, key: str) -> ObjectInfo | None:
        """Size and content type of `key`, or None if it does not exist."""
        try:
            obj = await self._in_thread(partial(self._client.stat_object, self._bucket, key))
        except S3Error as error:
            if error.code == "NoSuchKey":
                return None
            raise
        return ObjectInfo(key=key, size=obj.size or 0, content_type=obj.content_type)

    async def delete(self, key: str) -> None:
        """Deletes `key`."""
        await self._in_thread(partial(self._client.remove_object, self._bucket, key))

    def public_url(self, key: str) -> str:
        """Stable, unsigned URL of `key`. Raises BucketNotPublic on a private bucket."""
        if self._public_base_url is None:
            raise BucketNotPublic(f"Bucket {self._bucket} is not public.")
        return f"{self._public_base_url}/{key}"

    def _read(self, key: str) -> bytes:
        """Downloads `key` (blocking), always giving the connection back to the pool."""
        try:
            response = self._client.get_object(self._bucket, key)
        except S3Error as error:
            if error.code == "NoSuchKey":
                raise ObjectNotFound(f"Object not found: {key}") from error
            raise
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    @staticmethod
    async def _in_thread[T](call: Callable[[], T]) -> T:
        """Runs a blocking SDK call in a worker thread."""
        return await anyio.to_thread.run_sync(call)
