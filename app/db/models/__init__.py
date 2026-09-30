"""
Import every model module here so `Base.metadata` is fully populated before
Alembic's autogenerate (or any create_all in tests) inspects it. A model
file that's never imported is invisible to Alembic and silently produces no
migration -- this file exists specifically to prevent that mistake.
"""
from app.db.models.auth import EmailVerificationToken, RefreshToken  # noqa: F401
from app.db.models.conversation import Conversation, Message  # noqa: F401
from app.db.models.file import UploadedFile  # noqa: F401
from app.db.models.run import AgentRun, RunUsage  # noqa: F401
from app.db.models.user import User  # noqa: F401
