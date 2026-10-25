"""DATABASE_URL 환경변수의 DB에 aicc.db 모델 스키마를 적용한다."""
from alembic import context

from aicc.db import Base, engine

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
