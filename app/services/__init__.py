from .file_handler import FileHandler
from .ai_parser import AIQuestionParser

file_handler = FileHandler()
# Force single-pass for K12 exams: the entire markdown (đề bài + Hướng dẫn
# chấm) must go to Gemini in ONE call so it can map answers back to the
# right cau_num. Chunking loses that cross-section context.
#
# Default class constants chunk around 14K chars (4K tokens × 3.5
# chars/token). Bump both the explicit max_chunk_size AND the token-based
# ceiling so even a 50K-char HSG solution dump still parses as a single
# chunk. Gemini 2.5 Flash handles ~1M tokens input → plenty of headroom.
ai_parser_service = AIQuestionParser(max_chunk_size=300_000)
ai_parser_service._TARGET_CHUNK_TOKENS = 90_000  # ~315K chars target
ai_parser_service._MAX_CHUNK_TOKENS = 120_000    # ~420K chars hard ceiling
