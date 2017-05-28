import json
import logging
import time

from .. import database
from ..hookimpl import hookimpl
from .store import SlackStore, SlackItem

logger = logging.getLogger(__name__)


class User(SlackItem):
    def __init__(self, id_, raw=None, dm_id=None, last_update=None):
        """
        Class representing a slack user.

        :param id_: id of the user
        """

        super().__init__(id_, raw, last_update)
        self.dm_id = dm_id

    @property
    def admin(self):
        return self._raw.get('is_admin', False)

    @admin.setter
    def admin(self, _):
        raise NotImplementedError

    @property
    def send_id(self):
        return self.dm_id


class UserStore(SlackStore):
    """
    Manager for the user object
    """

    def __init__(self, client, facades, refresh=3600):
        super().__init__(client, facades, refresh)

    async def all(self):
        db = self._facades.get('database')
        data = await database.__dict__[db.type].user.get_all(db)

        return [
            User(
                id_=raw_data['id'],
                raw=raw_data['raw'],
                last_update=raw_data['last_update'],
                dm_id=raw_data['dm_id']
            ) for raw_data in data
        ]

    async def get(self, id_, update=False, dm=False):
        """
        Return an User from the User Manager

        If the user doesn't exist query the slack API for it

        :param id_: id of the user
        :param dm: Query the direct message channel id
        :param update: query the slack api for updated user info
        :return: User
        """
        db = self._facades.get('database')
        data = await database.__dict__[db.type].user.find(db, id_)

        if data and (
                update or data['last_update'] < time.time() - self._refresh
        ):
            user = await self._query(id_, data['dm_id'])

            if user:
                await self._add(user)
            else:
                await self._delete(id_, db)
        elif data:
            user = User(
                id_=id_,
                raw=json.loads(data['raw']),
                dm_id=data['dm_id'],
                last_update=data['last_update']
            )
        else:
            user = await self._query(id_)
            if user:
                await self._add(user)
        if dm:
            self.ensure_dm(user, db)

        return user

    async def _add(self, user, db=None):
        """
        Add an user to the UserManager

        :param user: users to add
        """

        if not db:
            db = self._facades.get('database')

        await database.__dict__[db.type].user.add(db, user)
        await db.commit()

    async def _delete(self, id_, db=None):
        """
        Delete an user from the UserManager

        :param id_: id of the user
        :return: None
        """

        if not db:
            db = self._facades.get('database')

        await database.__dict__[db.type].user.delete(db, id_)
        await db.commit()

    async def _query(self, id_, dm_id=None):

        if id_.startswith('B'):
            raw = await self._client.get_bot_info(id_)
        else:
            raw = await self._client.get_user_info(id_)
        user = User(
            id_=id_,
            raw=raw,
            last_update=time.time(),
            dm_id=dm_id
        )

        return user

    async def ensure_dm(self, user, db=None):

        if not user.send_id:
            if not db:
                db = self._facades.get('database')

            user.dm_id = await self._client.get_user_dm_channel(user.id)
            await database.__dict__[db.type].user.update_dm_id(
                db, user.id, user.dm_id)
            await db.commit()


async def user_typing(event, slack, _):
    """
    Use the user typing event to make sure the user is in cache
    """
    await slack.users.get(event['user'], update=False)


async def team_join(event, slack, _):
    """
    Use the team join event to add an user to the user store
    """
    await slack.users.get(event['user']['id'])


@hookimpl
def register_slack_events():
    events = [
        {
            'event': 'user_typing',
            'func': user_typing
        },
        {
            'event': 'team_join',
            'func': team_join
        }
    ]

    return events