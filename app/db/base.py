# Import the Base class
from app.db.base_class import Base

# Import all models here so Alembic/SQLAlchemy can discover them
from app.db.models.user import User
from app.db.models.exam import Exam
from app.db.models.question import Question