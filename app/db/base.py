# Import the Base class
from app.db.base_class import Base

# Import all models here so Alembic/SQLAlchemy can discover them
from app.db.models.user import User
from app.db.models.exam import Exam
from app.db.models.question import Question
from app.db.models.classroom import (
    Class, ClassMember, Assignment, Submission, AnswerDetail
)
from app.db.models.subject import Subject
from app.db.models.curriculum import Curriculum
from app.db.models.question_report import QuestionReport
from app.db.models.quiz import Quiz, QuizTheory, QuizTheorySection, QuizQuestion
from app.db.models.quiz_attempt import QuizAttempt, QuizAnswer
from app.db.models.teacher_page import TeacherPage
from app.db.models.writing_grade import IeltsWritingGrade
from app.db.models.review import DocumentAsset, QuestionDraft, QuestionDraftAsset, QuestionAsset
from app.db.models.lesson_plan import Yccd, CurriculumYccd, LessonPlan
from app.db.models.consent import ConsentLog, PolicyVersion
