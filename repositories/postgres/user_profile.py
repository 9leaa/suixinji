"""User-chosen aliases, scoped to the console's authenticated tenant."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import Tenant, User


def save_alias(user_id: str, alias: str, *, tenant_id: str) -> None:
    with session_scope() as session:
        session.execute(insert(Tenant).values(id=tenant_id, name=tenant_id).on_conflict_do_nothing())
        statement = insert(User).values(id=user_id, tenant_id=tenant_id, source='console',
                                        source_user_id=user_id, profile_json={'alias': alias})
        saved = session.execute(statement.on_conflict_do_update(
            index_elements=[User.id],
            set_={'profile_json': User.profile_json.op('||')(statement.excluded.profile_json)},
            where=User.tenant_id == tenant_id,
        ).returning(User.id)).scalar_one_or_none()
        if saved is None:
            raise ValueError('该用户 ID 已属于其他租户，请更换用户 ID')


def get_alias(user_id: str, *, tenant_id: str) -> str | None:
    with session_scope() as session:
        profile = session.execute(select(User.profile_json).where(
            User.id == user_id, User.tenant_id == tenant_id)).scalar_one_or_none() or {}
        return profile.get('alias')
