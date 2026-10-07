"""Public Hermes plugin entrypoint. No core adapter changes or ordinary DM hooks."""
def register(ctx):
    from .config import Config
    from .scope import profile_scope
    from .service import Service
    from .story import register as register_story
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()

    def wire(application, adapter):
        from telegram.ext import BusinessConnectionHandler, MessageHandler, filters, ApplicationHandlerStop
        with profile_scope(home):
            config = Config.load(home)
        service = Service(home, config, application.bot)
        # Namespace by captured home; useful for teardown/diagnostics and real-handler tests.
        application.bot_data.setdefault("business-powerpack", {})[str(home)] = service

        async def connection(update, context):
            try:
                service.connection_update(update.business_connection)
            finally:
                raise ApplicationHandlerStop

        async def message(update, context):
            # Edits are deliberately consumed without execution.
            if update.business_message is not None:
                service.submit(update.business_message, application)
            raise ApplicationHandlerStop

        application.add_handler(BusinessConnectionHandler(connection), group=-100)
        application.add_handler(MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE | filters.UpdateType.EDITED_BUSINESS_MESSAGE,
            message), group=-100)

    ctx.register_telegram_handler(wire)
    register_story(ctx, home)

    def wire_reply_context(application, adapter):
        # Existing handler closures retain this plugin-owned Service instance.
        # Refresh only its handler method; preserve runner, active tasks and state.
        from types import MethodType
        import hashlib, json, os
        service = application.bot_data.get('business-powerpack', {}).get(str(home))
        if service is None:
            raise RuntimeError('Business service is not wired')
        service.handle = MethodType(Service.handle, service)
        application.bot_data.setdefault('business-powerpack-runtime', {})[str(home)] = {
            'pid': os.getpid(), 'reply_context': True,
        }
        # Private runtime receipt verifies the factory actually ran in the live gateway.
        from pathlib import Path
        from utils import atomic_write_text as atomic_write
        marker = home / 'business-powerpack' / 'reply-context-runtime.json'
        data = {'pid': os.getpid(), 'service_sha256': hashlib.sha256(Path(Service.handle.__code__.co_filename).read_bytes()).hexdigest()}
        atomic_write(marker, json.dumps(data), tmp_prefix=marker.name + '.tmp-')
        os.chmod(marker, 0o600)

    ctx.register_telegram_handler(wire_reply_context)
