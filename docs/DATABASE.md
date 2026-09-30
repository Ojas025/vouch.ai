# Procedure for PostgreSQL migrations

1. Update SQLAlchemy models
2. ```alembic revision --autogenerate -m <commit_message>```
3. Review change
4. ```alembic upgrade head```
