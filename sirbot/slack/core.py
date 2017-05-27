import asyncio
import importlib
import logging
import os
import pluggy
import yaml

from sirbot.utils import merge_dict
from sirbot.core import Plugin

from . import hookspecs, database
from .dispatcher import EventDispatcher, ActionDispatcher, CommandDispatcher
from .__meta__ import DATA as METADATA
from .api import RTMClient, HTTPClient
from .errors import SlackClientError, SlackSetupError
from .facade import SlackFacade
from .store import ChannelStore, UserStore, GroupStore
from .store.user import User

logger = logging.getLogger(__name__)

MANDATORY_PLUGIN = ['sirbot.slack.store.user',
                    'sirbot.slack.store.channel',
                    'sirbot.slack.store.group']

SUPPORTED_DATABASE = ['sqlite']


class SirBotSlack(Plugin):
    __name__ = 'slack'
    __version__ = METADATA['version']
    __facade__ = 'slack'

    def __init__(self, loop):
        super().__init__(loop)
        logger.debug('Initializing slack plugin')
        self._loop = loop
        self._router = None
        self._config = None
        self._facades = None
        self._session = None
        self._bot_token = None
        self._app_token = None
        self._verification_token = None
        self._rtm_client = None
        self._http_client = None
        self._users = None
        self._channels = None
        self._groups = None
        self._started = False
        self._dispatcher = dict()

        self.bot = None

    @property
    def started(self):
        return self._started

    async def configure(self, config, router, session, facades):
        logger.debug('Configuring slack plugin')
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'config.yml'
        )

        with open(path) as file:
            defaultconfig = yaml.load(file)

        self._config = merge_dict(config, defaultconfig[self.__name__])

        self._router = router
        self._session = session
        self._facades = facades

        self._bot_token = os.environ.get('SIRBOT_SLACK_BOT_TOKEN', '')
        self._app_token = os.environ.get('SIRBOT_SLACK_TOKEN', '')
        self._verification_token = os.environ.get(
            'SIRBOT_SLACK_VERIFICATION_TOKEN'
        )

        if 'database' not in self._facades:
            raise SlackSetupError('A database facades is required')

        db = self._facades.get('database')
        if db.type not in SUPPORTED_DATABASE:
            raise SlackSetupError('Database must be one of %s',
                                  ', '.join(SUPPORTED_DATABASE))

        if not self._bot_token and not self._app_token:
            raise SlackSetupError(
                'One of SIRBOT_SLACK_BOT_TOKEN or SIRBOT_SLACK_TOKEN'
                ' must be set'
            )

        if self._app_token and not self._verification_token:
            raise SlackSetupError(
                'SIRBOT_SLACK_VERIFICATION_TOKEN must be set'
            )

        pm = self._initialize_plugins()

        self._http_client = HTTPClient(
            bot_token=self._bot_token,
            app_token=self._app_token,
            loop=self._loop,
            session=self._session
        )

        self._users = UserStore(
            client=self._http_client,
            facades=self._facades,
            refresh=self._config['refresh']['user']
        )

        self._channels = ChannelStore(
            client=self._http_client,
            facades=self._facades,
            refresh=self._config['refresh']['channel']
        )

        self._groups = GroupStore(
            client=self._http_client,
            facades=self._facades,
            refresh=self._config['refresh']['group']
        )

        if self._bot_token:

            data = await self._http_client.info_self()
            data['is_bot'] = True
            self.bot = User(
                data['id'],
                raw=data,
                dm_id=data['id']
            )

            self._rtm_client = RTMClient(
                bot_token=self._bot_token,
                loop=self._loop,
                callback=self._incoming_rtm,
                session=self._session
            )

        if self._bot_token or self._config['endpoints']['events']:
            self._dispatcher['event'] = EventDispatcher(
                http_client=self._http_client,
                users=self._users,
                channels=self._channels,
                groups=self._groups,
                plugins=pm,
                facades=self._facades,
                loop=self._loop,
                save=self._config['save']['events'],
                bot=self.bot
            )

        if self._config['endpoints']['actions']:
            logger.debug('Adding actions endpoint: %s',
                         self._config['endpoints']['actions'])
            self._dispatcher['action'] = ActionDispatcher(
                http_client=self._http_client,
                users=self._users,
                channels=self._channels,
                groups=self._groups,
                plugins=pm,
                facades=self._facades,
                loop=self._loop,
                save=self._config['save']['actions'],
                token=self._verification_token
            )

            self._router.add_route(
                'POST',
                self._config['endpoints']['actions'],
                self._dispatcher['action'].incoming
            )

        if self._config['endpoints']['commands']:
            logger.debug('Adding commands endpoint: %s',
                         self._config['endpoints']['commands'])
            self._dispatcher['command'] = CommandDispatcher(
                http_client=self._http_client,
                users=self._users,
                channels=self._channels,
                groups=self._groups,
                plugins=pm,
                facades=self._facades,
                loop=self._loop,
                save=self._config['save']['commands'],
                token=self._verification_token
            )
            self._router.add_route(
                'POST',
                self._config['endpoints']['commands'],
                self._dispatcher['command'].incoming
            )

        self._dispatcher['action'].started = True
        self._dispatcher['command'].started = True
        self._started = True

    def facade(self):
        """
        Initialize and return a new facade

        This is called by the core when a for each incoming message and when
        another plugin request a slack facade
        """
        return SlackFacade(
            http_client=self._http_client,
            users=self._users,
            channels=self._channels,
            groups=self._groups,
            bot=self.bot,
            facades=self._facades
        )

    async def start(self):
        logger.debug('Starting slack plugin')
        await self._create_db_table()
        if self._rtm_client:
            await self._rtm_client.connect()
        else:
            self._dispatcher.started = True

    async def _rtm_reconnect(self):
        logger.warning('Trying to reconnect to slack')
        try:
            await self._rtm_client.connect()
        except SlackClientError:
            await asyncio.sleep(1, loop=self._loop)
            await self._rtm_reconnect()

    async def _incoming_rtm(self, event):
        msg_type = event.get('type', None)

        if self.started or msg_type == 'connected':
            if msg_type in ('team_migration_started', 'goodbye'):
                logger.debug('Bot needs to reconnect')
                await self._rtm_reconnect()
            else:
                await self._dispatcher['event'].incoming_rtm(event)

    def _initialize_plugins(self):
        """
        Import and register the plugins

        Most likely composed of functions reacting to messages, events, slash
        commands and actions
        """
        logger.debug('Initializing plugins of slack plugin')
        pm = pluggy.PluginManager('sirbot.slack')
        pm.add_hookspecs(hookspecs)

        for plugin in MANDATORY_PLUGIN + self._config.get('plugins', []):
            try:
                p = importlib.import_module(plugin)
                pm.register(p)
            except Exception as e:
                logger.exception(e)

        return pm

    async def database_update(self, metadata, db):

        if metadata['version'] == '0.0.5':
            await database.__dict__[db.type].update.update_005(db)
            metadata['version'] = '0.0.6'

        return self.__version__

    async def _create_db_table(self):
        db = self._facades.get('database')
        await database.__dict__[db.type].create_table(db)
        await db.set_plugin_metadata(self)
        await db.commit()
