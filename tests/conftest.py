from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from netmover.crypto import SecretBox
from netmover.db import make_engine, make_session_factory, session_scope, upgrade
from netmover.models import Credential, CredentialKind, Mount, MountType
from netmover.mounting.fake import FakeMountBackend
from netmover.mounting.manager import MountManager


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    upgrade(url)
    return url


@pytest.fixture
def session_factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(db_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def secretbox() -> SecretBox:
    return SecretBox(Fernet.generate_key())


@pytest.fixture
def fake_backend(tmp_path: Path) -> Iterator[FakeMountBackend]:
    backend = FakeMountBackend(tmp_path / "fake")
    yield backend
    backend.release_hangs()


@pytest.fixture
def mount_base(tmp_path: Path) -> Path:
    return tmp_path / "mnt"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def notifications() -> list[tuple[str, str, str]]:
    return []


@pytest.fixture
def manager(
    session_factory: sessionmaker[Session],
    fake_backend: FakeMountBackend,
    secretbox: SecretBox,
    mount_base: Path,
    sleeps: list[float],
    notifications: list[tuple[str, str, str]],
) -> MountManager:
    return MountManager(
        session_factory,
        fake_backend,
        secretbox,
        mount_base,
        probe_timeout=0.5,
        recover_backoff=(0, 1, 2),
        sleep=sleeps.append,
        notifier=lambda *a: notifications.append(a),
    )


MountFactory = Callable[..., int]


@pytest.fixture
def make_mount(
    session_factory: sessionmaker[Session], secretbox: SecretBox, mount_base: Path
) -> MountFactory:
    def factory(
        name: str = "scans",
        type: MountType = MountType.CIFS,
        server: str = "nas",
        share: str = "scans",
        password: str | None = "geheim123",
        **kw: object,
    ) -> int:
        with session_scope(session_factory) as s:
            cred_id = None
            if password is not None:
                c = Credential(
                    name=f"cred-{name}",
                    kind=CredentialKind.PASSWORD,
                    username="scanner",
                    secret_enc=secretbox.encrypt(password),
                )
                s.add(c)
                s.flush()
                cred_id = c.id
            m = Mount(
                name=name,
                slug=name,
                type=type,
                server=server,
                share=share,
                mountpoint=None if type is MountType.SFTP else str(mount_base.resolve() / name),
                cred_id=cred_id,
                **kw,
            )
            s.add(m)
            s.flush()
            return m.id

    return factory
