"""Restricted local filesystem storage for immutable experiment artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import ValidationError

from autopilot.domain.artifacts import ArtifactProducer, ArtifactRef
from autopilot.domain.base import utc_now
from autopilot.domain.identifiers import ArtifactId, ExperimentId, Sha256Digest
from autopilot.evidence.models import (
    ARTIFACT_MAX_BYTES,
    ARTIFACT_PAYLOAD_FILENAME,
    ArtifactMetadata,
    artifact_storage_path,
    validate_artifact_category,
)

_ARTIFACT_PAYLOAD_FILENAME = ARTIFACT_PAYLOAD_FILENAME
_ARTIFACT_METADATA_FILENAME = "metadata.json"
_ARTIFACT_METADATA_MAX_BYTES = 1_000_000
_ARTIFACT_MAX_BYTES = ARTIFACT_MAX_BYTES

_INVALID_INPUT = "artifact store input is invalid"
_UNSAFE_PATH = "artifact path is outside the configured root or contains a symbolic link"
_ROOT_UNAVAILABLE = "artifact root cannot be initialized"
_ARTIFACT_EXISTS = "artifact ID already exists and immutable content cannot be overwritten"
_ARTIFACT_NOT_FOUND = "artifact does not exist"
_METADATA_CORRUPT = "artifact metadata is missing, invalid, or inconsistent with its location"
_CONTENT_CORRUPT = "artifact content does not match its recorded size and SHA-256"
_STORAGE_FAILURE = "artifact filesystem operation failed"


class ArtifactStoreErrorCode(StrEnum):
    """Stable error codes exposed by the local Artifact Store boundary."""

    INVALID_INPUT = "ARTIFACT_INVALID_INPUT"
    UNSAFE_PATH = "ARTIFACT_UNSAFE_PATH"
    ALREADY_EXISTS = "ARTIFACT_ALREADY_EXISTS"
    NOT_FOUND = "ARTIFACT_NOT_FOUND"
    INTEGRITY_ERROR = "ARTIFACT_INTEGRITY_ERROR"
    STORAGE_ERROR = "ARTIFACT_STORAGE_ERROR"


class ArtifactStoreError(RuntimeError):
    """Base class for failures that are safe to map at application boundaries."""

    def __init__(self, code: ArtifactStoreErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactInputError(ArtifactStoreError):
    """The logical artifact input is invalid before any filesystem access."""

    def __init__(self, message: str = _INVALID_INPUT) -> None:
        super().__init__(ArtifactStoreErrorCode.INVALID_INPUT, message)


class ArtifactPathError(ArtifactStoreError):
    """A path is unsafe or no longer contained by the configured root."""

    def __init__(self) -> None:
        super().__init__(ArtifactStoreErrorCode.UNSAFE_PATH, _UNSAFE_PATH)


class ArtifactAlreadyExistsError(ArtifactStoreError):
    """An immutable Artifact ID has already been published."""

    def __init__(self) -> None:
        super().__init__(ArtifactStoreErrorCode.ALREADY_EXISTS, _ARTIFACT_EXISTS)


class ArtifactNotFoundError(ArtifactStoreError):
    """The requested artifact cannot be found under the expected logical location."""

    def __init__(self) -> None:
        super().__init__(ArtifactStoreErrorCode.NOT_FOUND, _ARTIFACT_NOT_FOUND)


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored metadata or payload bytes fail deterministic integrity checks."""

    def __init__(self, message: str) -> None:
        super().__init__(ArtifactStoreErrorCode.INTEGRITY_ERROR, message)


class ArtifactStorageError(ArtifactStoreError):
    """The backing filesystem failed without exposing provider details to callers."""

    def __init__(self, message: str = _STORAGE_FAILURE) -> None:
        super().__init__(ArtifactStoreErrorCode.STORAGE_ERROR, message)


class LocalArtifactStore:
    """Immutable, root-confined local Artifact Store with sidecar metadata."""

    def __init__(self, root: Path) -> None:
        try:
            if root.is_symlink():
                raise ArtifactPathError
            root.mkdir(mode=0o750, parents=True, exist_ok=True)
            if root.is_symlink():
                raise ArtifactPathError
            if not root.is_dir():
                raise ArtifactStorageError(_ROOT_UNAVAILABLE)
            self._root = root.resolve(strict=True)
            self._ensure_directory(("experiments",))
        except ArtifactStoreError:
            raise
        except OSError as error:
            raise ArtifactStorageError(_ROOT_UNAVAILABLE) from error

    @property
    def root(self) -> Path:
        """Return the canonical configured storage root for trusted infrastructure code."""
        return self._root

    def write(  # noqa: PLR0913
        self,
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
        data: bytes,
        content_type: str,
        producer: ArtifactProducer,
    ) -> ArtifactMetadata:
        """Atomically publish bytes and metadata without accepting a caller path."""
        safe_category = self._validate_write_input(
            experiment_id=experiment_id,
            category=category,
            artifact_id=artifact_id,
            data=data,
            producer=producer,
        )
        artifact_ref = self._build_ref(
            artifact_id=artifact_id,
            data=data,
            content_type=content_type,
            producer=producer,
        )
        metadata = self._build_metadata(
            experiment_id=experiment_id,
            category=safe_category,
            artifact_ref=artifact_ref,
        )
        metadata_bytes = metadata.model_dump_json().encode("utf-8") + b"\n"
        category_directory = self._ensure_directory(
            ("experiments", str(experiment_id), safe_category)
        )
        artifact_directory = category_directory / str(artifact_id)
        existing = self._existing_identical_artifact(
            artifact_directory=artifact_directory,
            experiment_id=experiment_id,
            category=safe_category,
            artifact_id=artifact_id,
            expected_ref=artifact_ref,
            expected_data=data,
        )
        if existing is not None:
            return existing

        temporary_directory: Path | None = None
        try:
            temporary_directory = Path(
                tempfile.mkdtemp(
                    prefix=f".{artifact_id}.",
                    suffix=".tmp",
                    dir=category_directory,
                )
            )
            self._assert_temporary_directory(temporary_directory, category_directory)
            self._write_new_file(temporary_directory / _ARTIFACT_PAYLOAD_FILENAME, data)
            self._write_new_file(
                temporary_directory / _ARTIFACT_METADATA_FILENAME,
                metadata_bytes,
            )
            self._sync_directory(temporary_directory)
            try:
                temporary_directory.rename(artifact_directory)
            except OSError as error:
                if artifact_directory.is_symlink():
                    raise ArtifactPathError from error
                if artifact_directory.exists():
                    existing = self._existing_identical_artifact(
                        artifact_directory=artifact_directory,
                        experiment_id=experiment_id,
                        category=safe_category,
                        artifact_id=artifact_id,
                        expected_ref=artifact_ref,
                        expected_data=data,
                    )
                    if existing is not None:
                        return existing
                    raise ArtifactAlreadyExistsError from error
                raise
            temporary_directory = None
            self._sync_directory(category_directory)
        except ArtifactStoreError:
            raise
        except OSError as error:
            raise ArtifactStorageError from error
        finally:
            if temporary_directory is not None:
                self._remove_temporary_directory(temporary_directory)

        return metadata

    def get_metadata(
        self,
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
    ) -> ArtifactMetadata:
        """Load metadata and verify its logical location and payload path binding."""
        safe_category = self._validate_location(experiment_id, category, artifact_id)
        artifact_directory = self._artifact_directory(
            experiment_id,
            safe_category,
            artifact_id,
        )
        resolved_directory = self._resolve_secure_existing(artifact_directory)
        if resolved_directory is None:
            raise ArtifactNotFoundError
        if not resolved_directory.is_dir():
            raise ArtifactIntegrityError(_METADATA_CORRUPT)

        metadata_path = artifact_directory / _ARTIFACT_METADATA_FILENAME
        resolved_metadata = self._resolve_secure_existing(metadata_path)
        if resolved_metadata is None or not resolved_metadata.is_file():
            raise ArtifactIntegrityError(_METADATA_CORRUPT)
        try:
            metadata_bytes = self._read_regular_file(
                resolved_metadata,
                max_bytes=_ARTIFACT_METADATA_MAX_BYTES,
                integrity_message=_METADATA_CORRUPT,
            )
            if self._resolve_secure_existing(metadata_path) != resolved_metadata:
                raise ArtifactPathError
            metadata = ArtifactMetadata.model_validate_json(metadata_bytes)
        except (ValidationError, ValueError) as error:
            raise ArtifactIntegrityError(_METADATA_CORRUPT) from error
        except OSError as error:
            raise ArtifactStorageError from error

        if (
            metadata.experiment_id != experiment_id
            or metadata.category != safe_category
            or metadata.artifact_id != artifact_id
        ):
            raise ArtifactIntegrityError(_METADATA_CORRUPT)

        payload_path = self._path_from_relative_storage(metadata.storage_path)
        expected_payload = artifact_directory / _ARTIFACT_PAYLOAD_FILENAME
        if payload_path != expected_payload:
            raise ArtifactIntegrityError(_METADATA_CORRUPT)
        resolved_payload = self._resolve_secure_existing(payload_path)
        if resolved_payload is None or not resolved_payload.is_file():
            raise ArtifactIntegrityError(_CONTENT_CORRUPT)
        return metadata

    def read(
        self,
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
    ) -> bytes:
        """Read bytes only after revalidating containment, size, and SHA-256."""
        metadata = self.get_metadata(
            experiment_id=experiment_id,
            category=category,
            artifact_id=artifact_id,
        )
        payload_path = self._path_from_relative_storage(metadata.storage_path)
        resolved_payload = self._resolve_secure_existing(payload_path)
        if resolved_payload is None:
            raise ArtifactIntegrityError(_CONTENT_CORRUPT)
        try:
            data = self._read_regular_file(
                resolved_payload,
                max_bytes=_ARTIFACT_MAX_BYTES,
                integrity_message=_CONTENT_CORRUPT,
            )
            if self._resolve_secure_existing(payload_path) != resolved_payload:
                raise ArtifactPathError
        except OSError as error:
            raise ArtifactStorageError from error
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if len(data) != metadata.size_bytes or digest != metadata.sha256.root:
            raise ArtifactIntegrityError(_CONTENT_CORRUPT)
        return data

    @staticmethod
    def _validate_write_input(
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
        data: bytes,
        producer: ArtifactProducer,
    ) -> str:
        safe_category = LocalArtifactStore._validate_location(
            experiment_id,
            category,
            artifact_id,
        )
        if type(data) is not bytes or not isinstance(producer, ArtifactProducer):
            raise ArtifactInputError
        return safe_category

    @staticmethod
    def _validate_location(
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
    ) -> str:
        if not isinstance(experiment_id, ExperimentId) or not isinstance(artifact_id, ArtifactId):
            raise ArtifactInputError
        try:
            ExperimentId(root=experiment_id.root)
            ArtifactId(root=artifact_id.root)
            return validate_artifact_category(category)
        except (AttributeError, ValidationError, ValueError) as error:
            raise ArtifactInputError from error

    @staticmethod
    def _build_ref(
        *,
        artifact_id: ArtifactId,
        data: bytes,
        content_type: str,
        producer: ArtifactProducer,
    ) -> ArtifactRef:
        digest = Sha256Digest(root=f"sha256:{hashlib.sha256(data).hexdigest()}")
        try:
            validated_producer = ArtifactProducer.model_validate(producer.model_dump())
            return ArtifactRef(
                artifact_id=artifact_id,
                content_type=content_type,
                size_bytes=len(data),
                sha256=digest,
                producer=validated_producer,
            )
        except ValidationError as error:
            raise ArtifactInputError from error

    @staticmethod
    def _build_metadata(
        *,
        experiment_id: ExperimentId,
        category: str,
        artifact_ref: ArtifactRef,
    ) -> ArtifactMetadata:
        return ArtifactMetadata(
            artifact_id=artifact_ref.artifact_id,
            experiment_id=experiment_id,
            category=category,
            content_type=artifact_ref.content_type,
            size_bytes=artifact_ref.size_bytes,
            sha256=artifact_ref.sha256,
            created_at=utc_now(),
            producer=artifact_ref.producer,
            storage_path=artifact_storage_path(
                experiment_id,
                category,
                artifact_ref.artifact_id,
            ),
        )

    def _artifact_directory(
        self,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
    ) -> Path:
        return self._root / "experiments" / str(experiment_id) / category / str(artifact_id)

    def _ensure_directory(self, parts: tuple[str, ...]) -> Path:
        current = self._root
        self._assert_root_is_unchanged()
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactPathError
            try:
                current.mkdir(mode=0o750, exist_ok=True)
            except FileExistsError as error:
                raise ArtifactPathError from error
            except OSError as error:
                raise ArtifactStorageError from error
            if current.is_symlink():
                raise ArtifactPathError
            resolved = self._resolve_secure_existing(current)
            if resolved is None or not resolved.is_dir():
                raise ArtifactPathError
        return current

    def _assert_root_is_unchanged(self) -> None:
        if self._root.is_symlink():
            raise ArtifactPathError
        try:
            if self._root.resolve(strict=True) != self._root:
                raise ArtifactPathError
        except FileNotFoundError as error:
            raise ArtifactStorageError(_ROOT_UNAVAILABLE) from error
        except OSError as error:
            raise ArtifactStorageError from error

    def _resolve_secure_existing(self, path: Path) -> Path | None:
        self._assert_root_is_unchanged()
        try:
            relative = path.relative_to(self._root)
        except ValueError as error:
            raise ArtifactPathError from error
        current = self._root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactPathError
            try:
                resolved = current.resolve(strict=True)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise ArtifactStorageError from error
            if not resolved.is_relative_to(self._root):
                raise ArtifactPathError
        try:
            return current.resolve(strict=True)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ArtifactStorageError from error

    def _path_from_relative_storage(self, storage_path: str) -> Path:
        posix_path = PurePosixPath(storage_path)
        windows_path = PureWindowsPath(storage_path)
        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in posix_path.parts)
            or "\\" in storage_path
        ):
            raise ArtifactPathError
        path = self._root.joinpath(*posix_path.parts)
        try:
            path.relative_to(self._root)
        except ValueError as error:
            raise ArtifactPathError from error
        return path

    def _existing_identical_artifact(  # noqa: PLR0913
        self,
        *,
        artifact_directory: Path,
        experiment_id: ExperimentId,
        category: str,
        artifact_id: ArtifactId,
        expected_ref: ArtifactRef,
        expected_data: bytes,
    ) -> ArtifactMetadata | None:
        if artifact_directory.is_symlink():
            raise ArtifactPathError
        if not artifact_directory.exists():
            return None
        metadata = self.get_metadata(
            experiment_id=experiment_id,
            category=category,
            artifact_id=artifact_id,
        )
        existing_data = self.read(
            experiment_id=experiment_id,
            category=category,
            artifact_id=artifact_id,
        )
        if metadata.to_ref() == expected_ref and existing_data == expected_data:
            return metadata
        raise ArtifactAlreadyExistsError

    def _assert_temporary_directory(self, temporary: Path, parent: Path) -> None:
        if temporary.is_symlink() or temporary.parent != parent:
            raise ArtifactPathError
        resolved = self._resolve_secure_existing(temporary)
        if resolved is None or not resolved.is_dir():
            raise ArtifactPathError

    @staticmethod
    def _write_new_file(path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _read_regular_file(
        path: Path,
        *,
        max_bytes: int,
        integrity_message: str,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactPathError
            if before.st_size > max_bytes:
                raise ArtifactIntegrityError(integrity_message)
            data = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
        if len(data) > max_bytes or before.st_size != after.st_size or len(data) != after.st_size:
            raise ArtifactIntegrityError(integrity_message)
        return data

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_temporary_directory(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError:
            return
