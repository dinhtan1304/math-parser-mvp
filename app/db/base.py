# Import the Base class
from app.db.base_class import Base

# Import all models here so Alembic/SQLAlchemy can discover them
from app.db.models.user import User
from app.db.models.exam import Exam
from app.db.models.question import Question
from app.db.models.classroom import (
    Class, ClassMember, Assignment, Submission, AnswerDetail, StudentXP, Badge
)
from app.db.models.subject import Subject
from app.db.models.curriculum import Curriculum
from app.db.models.notification import DeviceToken
from app.db.models.live_session import LiveSession, LiveParticipant, LiveAnswer
from app.db.models.question_report import QuestionReport